import base64
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import socket
import struct
from threading import Thread
import unittest
from unittest.mock import Mock, patch

from headset import HeadsetError, parse_status
from probe import collect_status, read_batteries, read_external_power, read_steam_battery
from test_headset import SAMPLE, fixture_directory


TARGETS = [{"title": "SharedJSContext", "webSocketDebuggerUrl":
            "ws://127.0.0.1:8080/devtools/page/battery-test"}]


def receive(connection, size):
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise OSError("Test peer closed")
        data.extend(chunk)
    return bytes(data)


def frame(opcode, payload, final=True):
    first = opcode | (128 if final else 0)
    header = bytes((first, len(payload))) if len(payload) < 126 else bytes((first, 126)) + struct.pack("!H", len(payload))
    return header + payload


@contextmanager
def steam_server(reply, *, fragmented=False, bad_handshake=False, oversized=False):
    client, server = socket.socketpair()
    errors, requests = [], []
    server.settimeout(2)

    def serve():
        try:
            with server:
                header = bytearray()
                while not header.endswith(b"\r\n\r\n"):
                    header.extend(receive(server, 1))
                fields = dict(line.split(": ", 1) for line in header.decode().split("\r\n")[1:] if line)
                accept = base64.b64encode(hashlib.sha1(
                    (fields["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
                ).digest()).decode()
                if bad_handshake:
                    accept = "invalid"
                server.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                                f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
                if bad_handshake:
                    return
                first, second = receive(server, 2)
                assert first == 0x81 and second & 128, "Client must send masked text"
                size = second & 127
                if size == 126:
                    size = struct.unpack("!H", receive(server, 2))[0]
                mask = receive(server, 4)
                payload = receive(server, size)
                requests.append(json.loads(bytes(value ^ mask[index % 4] for index, value in enumerate(payload))))
                if oversized:
                    server.sendall(bytes((0x81, 127)) + struct.pack("!Q", 256 * 1024 + 1))
                    return
                payload = json.dumps(reply).encode()
                if fragmented:
                    middle = len(payload) // 2
                    server.sendall(frame(1, payload[:middle], False) + frame(9, b"ping"))
                    first, second = receive(server, 2)
                    assert first == 0x8a and second == 0x84, "Client must answer ping"
                    mask, pong = receive(server, 4), receive(server, 4)
                    assert bytes(value ^ mask[index % 4] for index, value in enumerate(pong)) == b"ping"
                    server.sendall(frame(0, payload[middle:]))
                else:
                    server.sendall(frame(1, payload))
        except Exception as error:
            errors.append(error)

    thread = Thread(target=serve, daemon=True)
    thread.start()
    opener = Mock()
    opener.open.return_value = BytesIO(json.dumps(TARGETS).encode())
    try:
        with patch("steam_api.build_opener", return_value=opener), patch("steam_api.socket.create_connection", return_value=client):
            yield requests
    finally:
        client.close()
        thread.join(3)
        if thread.is_alive():
            server.close()
            raise AssertionError("Battery test server did not finish")
        if errors:
            raise errors[0]


class SteamBatteryTests(unittest.TestCase):
    def test_timing_and_measured_power_use_driver_units_and_preserve_real_zero(self):
        with fixture_directory() as root:
            supply = root / "battery"
            supply.mkdir()
            attributes = {"type": "Battery", "capacity": "80", "status": "Charging",
                          "time_to_full_now": "2182", "time_to_empty_now": "6721",
                          "current_now": "277500", "voltage_now": "8560780"}
            for name, value in attributes.items():
                (supply / name).write_text(value, encoding="utf-8")
            battery = read_batteries(root)[0]
            self.assertEqual(battery["time_to_full"], 2182)
            self.assertEqual(battery["time_to_empty"], 6721)
            self.assertAlmostEqual(battery["power_watts"], 2.376)
            for current, expected in (("-1032500", -8.839), ("0", 0), ("unreadable", None)):
                (supply / "current_now").write_text(current, encoding="utf-8")
                self.assertEqual(read_batteries(root)[0].get("power_watts"), expected)
            for value in ("0", "-1", "368634", "4294967295", "unknown"):
                (supply / "time_to_full_now").write_text(value, encoding="utf-8")
                self.assertNotIn("time_to_full", read_batteries(root)[0])

    def test_battery_details_show_the_applicable_estimate_and_power_direction(self):
        for state, percent, full, empty, power, expected, excluded in (
            ("Charging", 80, 2182, 6721, 2.376, ("37 min until full", "2.4 W in"), "remaining"),
            ("Discharging", 80, 2182, 6598, -8.839, ("1 h 50 min remaining", "8.8 W out"), "until full"),
            ("Charging", 100, 2182, 6598, 1.3, ("Fully charged", "1.3 W in"), "until full"),
            ("Discharging", 80, None, None, None, ("Time estimate unavailable",), "0.0 W"),
        ):
            data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0], state=state, percent=percent,
                external_power=state == "Charging", time_to_full=full, time_to_empty=empty, power_watts=power)])
            detail = parse_status(json.dumps(data)).batteries[0].detail
            for text in expected:
                self.assertIn(text, detail)
            self.assertNotIn(excluded, detail)
        for key, value in (("time_to_full", -1), ("time_to_empty", 0), ("time_to_full", True),
                           ("power_watts", float("nan")), ("power_watts", True), ("power_watts", 201)):
            data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0], **{key: value})])
            with self.subTest(key=key, value=value), self.assertRaises(HeadsetError):
                parse_status(json.dumps(data))

    def test_steam_timer_is_used_only_for_remaining_use(self):
        for state in ("Charging", "Discharging"):
            with patch("probe.read_batteries", return_value=[dict(SAMPLE["batteries"][0], state=state,
                time_to_full=2182, time_to_empty=6721)]), patch("probe.read_steam_battery", return_value={
                    "percent": 80, "source": "steam", "time_to_empty": 6598,
                }):
                battery = parse_status(json.dumps(collect_status())).batteries[0]
            self.assertEqual(battery.time_to_empty, 6598 if state == "Discharging" else 6721)
            self.assertEqual(battery.time_to_full, 2182)

    def test_unknown_steam_timer_does_not_discard_the_percentage(self):
        for seconds in (None, 0, -1, True, "3600", 368634, 4294967295):
            with patch("probe.query_steam", return_value={"level": .8, "seconds": seconds}):
                self.assertEqual(read_steam_battery(), {"percent": 80, "source": "steam"})

    def test_charger_presence_uses_system_inputs_including_programmable_usb_power(self):
        with fixture_directory() as root:
            for name, attributes in {
                "charger": {"type": "USB_PD", "scope": "System", "online": "0"},
                "controller": {"type": "USB", "scope": "Device", "online": "1"},
                "battery": {"type": "Battery", "online": "1"},
                "mains": {"type": "Mains", "online": "0"},
            }.items():
                supply = root / name
                supply.mkdir()
                for attribute, value in attributes.items():
                    (supply / attribute).write_text(value, encoding="utf-8")
            for value, expected in (("0", False), ("1", True), ("2", True), ("unknown", False)):
                (root / "charger/online").write_text(value, encoding="utf-8")
                with self.subTest(online=value):
                    self.assertIs(read_external_power(root), expected)
            (root / "mains/online").unlink()
            self.assertIsNone(read_external_power(root))
            self.assertIsNone(read_external_power(root / "absent"))

    def test_full_charge_label_requires_full_status_or_confirmed_power(self):
        for percent, state, power, expected in (
            (100, "Discharging", True, "Fully charged"),
            (100, "Not charging", True, "Fully charged"),
            (100, "Charging", True, "Fully charged"),
            (100, "Charging", None, "Fully charged"),
            (99, "Full", None, "Fully charged"),
            (100, "Discharging", False, "Discharging"),
            (100, "Discharging", None, "Discharging"),
            (75, "Charging", True, "Charging"),
            (75, "Discharging", True, "Plugged in · Not charging"),
            (75, "Unknown", None, "Unknown"),
        ):
            with self.subTest(percent=percent, state=state, power=power):
                data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0],
                    percent=percent, state=state, external_power=power)])
                self.assertEqual(parse_status(json.dumps(data)).batteries[0].display_state, expected)
        for value in (1, 0, "true", [], {}):
            data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0], external_power=value)])
            with self.subTest(invalid=value), self.assertRaises(HeadsetError):
                parse_status(json.dumps(data))

    def test_collector_combines_steam_percentage_with_independent_charger_presence(self):
        with patch("probe.read_batteries", return_value=[dict(SAMPLE["batteries"][0])]), patch(
            "probe.read_steam_battery", return_value={"percent": 100, "source": "steam"}
        ), patch("probe.read_external_power", return_value=True):
            battery = parse_status(json.dumps(collect_status())).batteries[0]
        self.assertEqual(battery.percent, 100)
        self.assertEqual(battery.source, "steam")
        self.assertEqual(battery.state, "Discharging")
        self.assertIs(battery.external_power, True)
        self.assertEqual(battery.display_state, "Fully charged")

    def test_reports_steam_percentage_including_zero_and_rounds_without_offset(self):
        for level, percent in ((0, 0), (.934, 93), (.995, 100), (1, 100)):
            with self.subTest(level=level), steam_server({"id": 1, "result": {"result": {"value": {"level": level, "seconds": 6598}}}}) as requests:
                self.assertEqual(read_steam_battery(), {"percent": percent, "source": "steam", "time_to_empty": 6598})
                self.assertEqual(requests[0]["method"], "Runtime.evaluate")
                self.assertTrue(requests[0]["params"]["awaitPromise"])

    def test_fragmented_response_and_ping_are_supported(self):
        with steam_server({"id": 1, "result": {"result": {"value": {"level": .99}}}}, fragmented=True):
            self.assertEqual(read_steam_battery(), {"percent": 99, "source": "steam"})

    def test_invalid_or_unavailable_steam_values_fall_back(self):
        for value in (None, True, "99", -1, 1.01, float("nan"), float("inf"), {}):
            with self.subTest(value=value), steam_server({"id": 1, "result": {"result": {"value": {"level": value}}}}):
                self.assertIsNone(read_steam_battery())
        with steam_server({"id": 1, "error": {"message": "Steam is restarting"}}):
            self.assertIsNone(read_steam_battery())

    def test_bad_handshake_and_oversized_message_fall_back(self):
        for options in ({"bad_handshake": True}, {"oversized": True}):
            with self.subTest(options=options), steam_server({}, **options):
                self.assertIsNone(read_steam_battery())

    def test_missing_interface_and_non_loopback_targets_are_not_used(self):
        with patch("steam_api.build_opener", side_effect=OSError("Interface unavailable")):
            self.assertIsNone(read_steam_battery())
        for targets in ([], [{"title": "SharedJSContext", "webSocketDebuggerUrl":
                             "ws://192.0.2.1:8080/devtools/page/test"}]):
            opener = Mock()
            opener.open.return_value = BytesIO(json.dumps(targets).encode())
            with patch("steam_api.build_opener", return_value=opener), patch("steam_api.socket.create_connection") as connect:
                self.assertIsNone(read_steam_battery())
                connect.assert_not_called()

    def test_collector_prefers_steam_and_preserves_system_fallback_and_state(self):
        for percent in (99, 0, None):
            with self.subTest(percent=percent), patch("probe.read_batteries", return_value=[
                {"name": "max1720x", "percent": 93, "state": "Discharging"}
            ]), patch("probe.read_steam_battery", return_value=None if percent is None else {"percent": percent, "source": "steam"}):
                status = parse_status(json.dumps(collect_status()))
                self.assertEqual(status.batteries[0].percent, 93 if percent is None else percent)
                self.assertEqual(status.batteries[0].source, "system" if percent is None else "steam")
                self.assertEqual(status.batteries[0].state, "Discharging")

    def test_multiple_system_batteries_are_not_replaced_with_one_steam_value(self):
        with patch("probe.read_batteries", return_value=[SAMPLE["batteries"][0].copy()] * 2), patch("probe.read_steam_battery") as query:
            self.assertEqual(len(collect_status()["batteries"]), 2)
            query.assert_not_called()

    def test_battery_source_defaults_to_system_and_rejects_invalid_sources(self):
        self.assertEqual(parse_status(json.dumps(SAMPLE)).batteries[0].source, "system")
        for value in (None, "invented", True, []):
            data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0], source=value)])
            with self.subTest(value=value), self.assertRaises(HeadsetError):
                parse_status(json.dumps(data))
