import json
from pathlib import Path
from threading import Event
import tomllib
import unittest
from unittest.mock import patch

from qt_helpers import APPLICATION, dispose, render, wait_until
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragLeaveEvent, QDropEvent, QPalette
from PySide6.QtWidgets import QLabel, QPushButton

from app import FrameConnectApp
from headset import HeadsetError, parse_status
from test_headset import READINGS, SAMPLE
from transfers import TransferCancelled


class AppTests(unittest.TestCase):
    def setUp(self):
        self.app = FrameConnectApp()
        self.app.show()
        APPLICATION.processEvents()

    def tearDown(self):
        dispose(self.app)

    def finish_refresh(self):
        self.assertTrue(wait_until(lambda: not self.app.busy), "Operation did not complete")

    def assert_footer(self, text, color):
        self.assertEqual(self.app.footer_status.text(), text)
        self.assertEqual(self.app.footer_status.palette().color(QPalette.ColorRole.WindowText).name(), color)

    def test_footer_version_matches_project_release(self):
        project = tomllib.loads(Path(__file__).resolve().parents[1].joinpath("pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(self.app.footer_version.text(), f"v{project['project']['version']}")

    @patch("app.fetch_status")
    def test_startup_has_no_network_request_or_invented_readings(self, fetch):
        fetch.assert_not_called()
        self.assertEqual(self.app.connection.text(), "Not connected")
        self.assertEqual(self.app.battery.text(), "—")
        self.assertEqual(self.app.storage.text(), "—")
        self.assertEqual(self.app.temperature.text(), "— °C")
        self.assertEqual(self.app.ram.text(), "— / — GB")
        self.assertIsNone(self.app.battery_icon.percent)
        self.assertIsNone(self.app.storage_usage.fraction)
        self.assertEqual(self.app.os_version.text(), "—")
        self.assertFalse(self.app.progress.fraction)
        self.assertEqual(self.app._auto_timer.interval(), 5000)
        self.assertFalse(self.app._auto_timer.isActive())
        self.assert_footer("Waiting for headset…", "#a7b6c8")

    def test_automatic_timer_updates_all_readings_without_clearing_or_overlapping(self):
        started, release = Event(), Event()
        updated = dict(SAMPLE, **READINGS, os_version="SteamOS 3.7-test")
        updated["batteries"] = [{"name": "battery", "percent": 90, "state": "Charging",
                                 "time_to_full": 2182, "power_watts": 2.376}]
        updated["controllers"] = [dict(hand="left", connected=True, percent=57),
                                  dict(hand="right", connected=True, percent=50)]
        updated["storage"] = {"total": 1000 * 1024**3, "used": 410 * 1024**3, "free": 590 * 1024**3}
        updated["temperature"] = {"celsius": 60.0, "sensor": "soc-thermal"}
        updated["memory"] = {"total": 16 * 1024**3, "available": 8 * 1024**3}
        def delayed_status(*_args):
            started.set()
            release.wait(3)
            return parse_status(json.dumps(updated))
        self.app._auto_timer.setInterval(20)
        with patch("app.fetch_status", side_effect=delayed_status) as fetch:
            try:
                self.app.show_status(parse_status(json.dumps(dict(SAMPLE, **READINGS))))
                self.app.destination.setText("/home/steamos/Documents")
                self.app.transfer_detail.setText("Sent 1 file(s) to /home/steamos/Documents.")
                self.assertTrue(wait_until(started.is_set), "Automatic timer did not refresh")
                self.app._auto_timer.setInterval(5000)
                self.assertEqual(self.app.battery.text(), "84%")
                self.assert_footer("Headset connected", "#78c78e")
                self.assertFalse(self.app.busy)
                self.assertTrue(self.app._can_drop())
                self.assertTrue(self.app.settings_button.isEnabled())
                self.assertTrue(self.app.connect.isEnabled())
                self.assertEqual(self.app.connect.text(), "Refresh")
                self.app._set_busy(False)
                self.assertTrue(self.app.connect.isEnabled())
                self.app._auto_refresh()
                self.app.refresh()
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: self.app._status_request is None))
        self.assertEqual(self.app.battery.text(), "90%")
        self.assertTrue(self.app.connect.isEnabled())
        self.assertEqual(self.app.connect.text(), "Refresh")
        self.assertEqual(self.app.battery_icon.percent, 90)
        self.assertIn("37 min until full", self.app.battery_detail.text())
        self.assertIn("2.4 W in", self.app.battery_detail.text())
        self.assertEqual(self.app.controllers["left"].value.text(), "57%")
        self.assertEqual(self.app.controllers["right"].value.text(), "50%")
        self.assertEqual(self.app.storage.text(), "590.0 GiB free")
        self.assertEqual(self.app.storage_usage.fraction, .41)
        self.assertEqual(self.app.temperature.text(), "60.0 °C")
        self.assertEqual(self.app.ram.text(), "8.6 / 17.2 GB")
        self.assertEqual(self.app.os_version.text(), "SteamOS 3.7-test")
        self.assertEqual(self.app.destination.text(), "/home/steamos/Documents")
        self.assertIn("Sent 1 file(s)", self.app.transfer_detail.text())
        self.assertEqual(self.app.refresh_interval_button.text(), "Auto-refresh: 5 seconds")
        self.assertNotIn("Auto-refresh", self.app.detail.text())
        self.assertTrue(self.app._auto_timer.isActive())

    def test_refresh_menu_options_select_and_schedule_each_interval(self):
        options = (("Never", 0), ("5 seconds", 5000), ("10 seconds", 10000),
                   ("30 seconds", 30000), ("1 minute", 60000), ("5 minutes", 300000))
        actions = self.app.refresh_interval_button.menu().actions()
        self.assertEqual([(action.text(), action.data()) for action in actions], list(options))
        self.assertTrue(actions[1].isChecked())
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        for action, (text, interval) in zip(actions, options):
            with self.subTest(interval=interval):
                action.trigger()
                self.assertEqual(self.app.refresh_interval_button.text(), f"Auto-refresh: {text}")
                self.assertEqual(sum(item.isChecked() for item in actions), 1)
                self.assertTrue(action.isChecked())
                self.assertEqual(self.app._auto_timer.isActive(), interval > 0)
                if interval:
                    self.assertEqual(self.app._auto_timer.interval(), interval)

    def test_never_stops_automatic_retries_but_manual_connect_still_works(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        with patch("app.fetch_status", side_effect=HeadsetError("Headset is asleep")):
            self.app._auto_timer.timeout.emit()
            self.assertTrue(wait_until(lambda: self.app._status_request is None))
        self.app.refresh_actions[0].trigger()
        self.assertFalse(self.app._auto_timer.isActive())
        self.assertNotIn("Retrying", self.app.footer_status.text())
        self.assertNotIn("automatically", self.app.detail.text())
        self.assertIn("Click Connect", self.app.transfer_detail.text())
        with patch("app.fetch_status", return_value=parse_status(json.dumps(SAMPLE))) as fetch:
            self.app._auto_timer.timeout.emit()
            fetch.assert_not_called()
            self.app.connect.click()
            self.finish_refresh()
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.app.connect.text(), "Refresh")
        self.assertFalse(self.app._auto_timer.isActive())
        self.app.refresh_actions[10000].trigger()
        self.assertTrue(self.app._auto_timer.isActive())
        self.assertEqual(self.app._auto_timer.interval(), 10000)

    def test_disabling_auto_refresh_during_a_check_does_not_schedule_another(self):
        started, release = Event(), Event()
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        def status(*_args):
            started.set()
            release.wait(3)
            return parse_status(json.dumps(SAMPLE))
        with patch("app.fetch_status", side_effect=status) as fetch:
            try:
                self.app._auto_timer.timeout.emit()
                self.assertTrue(started.wait(1))
                self.app.refresh_actions[0].trigger()
                self.app._auto_timer.timeout.emit()
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: self.app._status_request is None))
        self.assertFalse(self.app._auto_timer.isActive())
        self.assertEqual(self.app.connect.text(), "Refresh")

    def test_refresh_interval_changes_apply_during_upload(self):
        for interval in (0, 60000):
            with self.subTest(interval=interval), patch("app.Thread"), patch("app.fetch_status") as fetch:
                self.app.show_status(parse_status(json.dumps(SAMPLE)))
                self.app.start_upload(("C:/clip.mp4",))
                self.assertTrue(self.app.refresh_interval_button.isEnabled())
                self.app.refresh_actions[interval].trigger()
                self.assertEqual(self.app._auto_timer.isActive(), interval > 0)
                self.app._auto_timer.timeout.emit()
                self.assertEqual(self.app._status_request is not None, interval > 0)
                if interval:
                    self.app._handle_status_result(self.app._status_request, parse_status(json.dumps(SAMPLE)))
                fetch.assert_not_called()  # Worker threads are mocked in this interval test.
                self.app._handle_transfer_result("uploaded", ("/home/steamos/Downloads/clip.mp4",))
                self.assertEqual(self.app._auto_timer.isActive(), interval > 0)
                if interval:
                    self.assertEqual(self.app._auto_timer.interval(), interval)

    def test_monitoring_updates_during_upload_and_recovers_without_touching_transfer(self):
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, **READINGS))))
        self.app.destination.setText("/home/steamos/Videos")
        started, release = Event(), Event()
        updated = dict(SAMPLE, **READINGS)
        updated["batteries"] = [dict(SAMPLE["batteries"][0], time_to_empty=6598, power_watts=-8.839)]
        updated["controllers"] = [dict(hand="left", connected=True, percent=57),
                                  dict(hand="right", connected=True, percent=50)]
        updated["temperature"] = {"celsius": 60.0, "sensor": "soc-thermal"}
        updated["memory"] = {"total": 16 * 1024**3, "available": 8 * 1024**3}
        updated["storage"] = {"total": 1000 * 1024**3, "used": 410 * 1024**3, "free": 590 * 1024**3}
        def upload(_host, _user, _key, _paths, destination, _cancel, progress, *, on_verifying):
            progress("clip.mp4", 0, 1, 25, 100)
            started.set()
            release.wait(3)
            return (destination + "/clip.mp4",)
        with patch("app.upload_files", side_effect=upload), patch("app.fetch_status", side_effect=[
            HeadsetError("Status check timed out"), parse_status(json.dumps(updated)),
        ]) as fetch:
            try:
                self.assertTrue(self.app.start_upload(("C:/clip.mp4",)))
                self.assertTrue(started.wait(1))
                self.assertTrue(wait_until(lambda: self.app.progress.fraction == .25))
                transfer_text = self.app.transfer_detail.text()
                for count in (1, 2):
                    self.assertTrue(self.app._auto_timer.isActive())
                    self.app._auto_timer.timeout.emit()
                    self.assertTrue(wait_until(lambda: fetch.call_count == count and self.app._status_request is None))
                    self.assertTrue(self.app.uploading)
                    self.assertTrue(self.app.busy)
                    self.assertTrue(self.app.cancel_button.isEnabled())
                    self.assertFalse(self.app.power_button.isEnabled())
                    self.assertFalse(self.app.destination.isEnabled())
                    self.assertEqual(self.app.destination.text(), "/home/steamos/Videos")
                    self.assertEqual(self.app.transfer_filename.text(), "clip.mp4")
                    self.assertEqual(self.app.transfer_percent.text(), "25%")
                    self.assertEqual(self.app.progress.fraction, .25)
                    self.assertEqual(self.app.transfer_detail.text(), transfer_text)
                    self.assertFalse(self.app.cancel_upload.is_set())
                self.assertEqual(self.app.temperature.text(), "60.0 °C")
                self.assertEqual(self.app.ram.text(), "8.6 / 17.2 GB")
                self.assertEqual(self.app.storage.text(), "590.0 GiB free")
                self.assertIn("1 h 50 min remaining", self.app.battery_detail.text())
                self.assertIn("8.8 W out", self.app.battery_detail.text())
                self.assertEqual(self.app.controllers["left"].value.text(), "57%")
                self.assertEqual(self.app.connection.text(), "Connected to frame")
            finally:
                release.set()
            self.finish_refresh()
        self.assertIn("Sent 1 file(s) to /home/steamos/Videos", self.app.transfer_detail.text())

    def test_connect_button_text_tracks_connection_and_stays_refresh_during_manual_check(self):
        status = parse_status(json.dumps(SAMPLE))
        self.assertEqual(self.app.connect.text(), "Connect")
        with patch("app.Thread"):
            self.app.connect.click()
            self.assertEqual(self.app.connect.text(), "Connect")
            self.app._handle_status_result(self.app._status_request, status)
            self.assertEqual(self.app.connect.text(), "Refresh")
            self.app.connect.click()
            self.assertEqual(self.app.connect.text(), "Refresh")
            self.assertFalse(self.app.connect.isEnabled())
            self.app._handle_status_result(self.app._status_request, HeadsetError("Headset is asleep"))
            self.assertEqual(self.app.connect.text(), "Connect")
            self.app._auto_timer.timeout.emit()
            self.app._handle_status_result(self.app._status_request, status)
            self.assertEqual(self.app.connect.text(), "Refresh")
        self.app.host.setText("different-headset")
        self.assertEqual(self.app.connect.text(), "Connect")
        self.assertFalse(self.app._poll_timer.isActive())

    def test_automatic_failure_marks_stale_readings_and_recovers_on_retry(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        recovered = dict(SAMPLE, batteries=[{"name": "battery", "percent": 89, "state": "Discharging"}])
        with patch("app.fetch_status", side_effect=[HeadsetError("Headset is asleep"), parse_status(json.dumps(recovered))]) as fetch:
            self.app._auto_timer.timeout.emit()
            self.assertTrue(wait_until(lambda: self.app._status_request is None))
            self.assertEqual(self.app.connection.text(), "Headset unavailable")
            self.assert_footer("Headset unavailable · Retrying…", "#ef7777")
            self.assertEqual(self.app.footer_status.toolTip(), "Headset is asleep")
            self.assertIn("Last update at", self.app.detail.text())
            self.assertIn("Headset is asleep", self.app.detail.text())
            self.assertEqual(self.app.battery.text(), "84%")
            self.assertFalse(self.app._can_drop())
            self.assertTrue(self.app._auto_timer.isActive())
            self.app._auto_timer.timeout.emit()
            self.assertTrue(wait_until(lambda: self.app._status_request is None))
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.app.battery.text(), "89%")
        self.assertEqual(self.app.connection.text(), "Connected to frame")
        self.assert_footer("Headset connected", "#78c78e")
        self.assertTrue(self.app._can_drop())
        self.assertTrue(self.app._auto_timer.isActive())

    def test_old_automatic_response_cannot_replace_a_new_connection(self):
        started, release = Event(), Event()
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        def status_for_host(host, *_args):
            if host == "frame":
                started.set()
                release.wait(3)
                return parse_status(json.dumps(SAMPLE))
            return parse_status(json.dumps(dict(SAMPLE, hostname="new-frame")))
        with patch("app.fetch_status", side_effect=status_for_host) as fetch:
            try:
                self.app._auto_timer.timeout.emit()
                self.assertTrue(started.wait(1))
                self.app.host.setText("new-frame")
                self.assertFalse(self.app._auto_timer.isActive())
                self.assertEqual(self.app.battery.text(), "—")
                self.app.refresh()
                self.finish_refresh()
                self.assertEqual(self.app.connection.text(), "Connected to new-frame")
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: not self.app.results.empty()))
            self.app._poll()
            self.assertEqual(fetch.call_count, 2)
        self.assertEqual(self.app.connection.text(), "Connected to new-frame")
        self.assertEqual(self.app.connected_to[0], "new-frame")
        self.assertEqual(self.app.footer_status.toolTip(), "Connected to new-frame")
        self.assertTrue(self.app._auto_timer.isActive())

    def test_status_and_upload_completions_preserve_controls_and_transfer_messages(self):
        for status_first in (True, False):
            with self.subTest(status_first=status_first):
                self.app.show_status(parse_status(json.dumps(SAMPLE)))
                status_started, release_status, upload_started, release_upload = Event(), Event(), Event(), Event()
                def status(*_args):
                    status_started.set()
                    release_status.wait(3)
                    return parse_status(json.dumps(SAMPLE))
                def upload(_host, _user, _key, _paths, destination, _cancel, progress, *, on_verifying):
                    progress("clip.mp4", 0, 1, 25, 100)
                    upload_started.set()
                    release_upload.wait(3)
                    return (destination + "/clip.mp4",)
                with patch("app.fetch_status", side_effect=status) as fetch, patch("app.upload_files", side_effect=upload):
                    try:
                        self.app._auto_timer.timeout.emit()
                        self.assertTrue(status_started.wait(1))
                        self.assertTrue(self.app.start_upload(("C:/clip.mp4",)))
                        self.assertTrue(upload_started.wait(1))
                        self.assertTrue(wait_until(lambda: self.app.progress.fraction))
                        self.assertFalse(self.app._auto_timer.isActive())
                        self.app._auto_timer.timeout.emit()
                        self.assertEqual(fetch.call_count, 1)
                        if status_first:
                            release_status.set()
                            self.assertTrue(wait_until(lambda: self.app._status_request is None))
                            self.assertTrue(self.app.busy)
                            self.assertFalse(self.app.destination.isEnabled())
                            self.assertFalse(self.app.connect.isEnabled())
                            self.assertIn("0/1", self.app.transfer_detail.text())
                        else:
                            release_upload.set()
                            self.finish_refresh()
                            self.assertTrue(self.app._poll_timer.isActive())
                            self.assertFalse(self.app._auto_timer.isActive())
                    finally:
                        release_status.set()
                        release_upload.set()
                    self.assertTrue(wait_until(lambda: not self.app.busy and self.app._status_request is None))
                self.assertIn("Sent 1 file(s)", self.app.transfer_detail.text())
                self.assertTrue(self.app._can_drop())
                self.assertTrue(self.app._auto_timer.isActive())
                self.assertFalse(self.app._poll_timer.isActive())

    def test_folder_browser_pauses_monitoring_and_resumes_when_closed(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        with patch("app.FolderPicker") as picker, patch("app.fetch_status") as fetch:
            self.app.choose_folder()
            self.assertFalse(self.app._auto_timer.isActive())
            self.app._auto_timer.timeout.emit()
            fetch.assert_not_called()
            picker.call_args.args[-1]()
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app._auto_timer.isActive())

    def test_close_during_automatic_refresh_stops_monitoring_and_ignores_result(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        started, release = Event(), Event()
        def delayed_status(*_args):
            started.set()
            release.wait(3)
            return parse_status(json.dumps(dict(SAMPLE, hostname="late-result")))
        with patch("app.fetch_status", side_effect=delayed_status) as fetch:
            try:
                self.app._auto_timer.timeout.emit()
                self.assertTrue(started.wait(1))
                self.app.close()
                self.assertFalse(self.app.isVisible())
                self.assertFalse(self.app._auto_timer.isActive())
                self.assertFalse(self.app._poll_timer.isActive())
                self.app._auto_timer.timeout.emit()
                self.assertEqual(fetch.call_count, 1)
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: not self.app.results.empty()))
            self.app._poll()
        self.assertNotIn("late-result", self.app.connection.text())
        self.assertFalse(self.app._auto_timer.isActive())

    def test_os_version_shows_reported_release_missing_fallback_and_connection_reset(self):
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, os_version="SteamOS 3.7-test"))))
        self.assertEqual(self.app.os_version.text(), "SteamOS 3.7-test")
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        self.assertEqual(self.app.os_version.text(), "Unavailable")
        self.assertTrue(self.app._can_drop())
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, os_version="SteamOS 3.7-test"))))
        self.app.host.setText("another-frame")
        self.assertEqual(self.app.os_version.text(), "—")

    def test_os_version_card_is_thin_full_width_and_below_measurements(self):
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, **READINGS, os_version="SteamOS 3.7-test"))))
        for size in ((680, 700), (800, 950), (1100, 900)):
            with self.subTest(size=size):
                self.app.resize(*size)
                APPLICATION.processEvents()
                card = self.app.os_card
                left_card, right_card = self.app.temperature.parentWidget(), self.app.ram.parentWidget()
                self.assertEqual(card.x(), left_card.x())
                self.assertEqual(card.x() + card.width(), right_card.x() + right_card.width())
                self.assertGreater(card.y(), left_card.y() + left_card.height())
                self.assertLess(card.height(), left_card.height() / 2)
                self.assertGreaterEqual(self.app.os_version.height(), self.app.os_version.heightForWidth(self.app.os_version.width()))
                scroll = self.app.status_scroll.verticalScrollBar()
                scroll.setValue(scroll.maximum())
                APPLICATION.processEvents()
                self.assertLess(card.mapTo(self.app, QPoint()).y() + card.height(), self.app.drop_area.mapTo(self.app, QPoint()).y())

    def test_storage_bar_shows_empty_half_and_full_usage_and_clears_on_disconnect(self):
        bar = self.app.storage_usage
        def filled_pixels():
            image = render(bar)
            return sum(image.pixelColor(x, image.height() // 2).name() == "#66c0f4" for x in range(image.width()))
        self.assertEqual(filled_pixels(), 0)
        # Byte counts exceed Qt's integer range; the visual fraction must still be correct.
        total = 100 * 1024**3
        for percent in (0, 50, 100):
            used = total * percent // 100
            self.app.show_status(parse_status(json.dumps(dict(
                SAMPLE, storage={"total": total, "used": used, "free": total - used},
            ))))
            APPLICATION.processEvents()
            with self.subTest(percent=percent):
                self.assertAlmostEqual(filled_pixels(), bar.width() * percent / 100, delta=4)
                self.assertEqual(self.app.storage.text(), f"{100 - percent:.1f} GiB free")
                self.assertIn(f"{percent:.1f} GiB used / 100.0 GiB total", self.app.storage_detail.text())
                self.assertIn(f"{percent:.1f}%", bar.accessibleDescription())
        self.app.host.setText("another-frame")
        self.assertIsNone(bar.fraction)
        self.assertEqual(filled_pixels(), 0)
        self.assertIn("unavailable", bar.accessibleDescription())

    def test_battery_icon_fill_matches_reported_charge_including_zero(self):
        fill_counts = []
        for percent in (0, 25, 84, 100):
            data = dict(SAMPLE, batteries=[{"name": "battery", "percent": percent, "state": "Discharging"}])
            self.app.show_status(parse_status(json.dumps(data)))
            APPLICATION.processEvents()
            self.assertEqual(self.app.battery.text(), f"{percent}%")
            self.assertEqual(self.app.battery_icon.percent, percent)
            self.assertIn(f"{percent}%", self.app.battery_icon.accessibleDescription())
            icon = self.app.battery_icon
            self.assertLess(icon.x() + icon.width(), self.app.battery.x())
            image = render(icon)
            fill_counts.append(sum(
                image.pixelColor(x, y).name() == "#66c0f4"
                for x in range(image.width()) for y in range(image.height())
            ))
        self.assertEqual(fill_counts[0], 0)
        self.assertTrue(all(left < right for left, right in zip(fill_counts, fill_counts[1:])))

    def test_battery_icon_clears_for_missing_multiple_and_changed_connections(self):
        for batteries in ([], [*SAMPLE["batteries"], {"name": "second", "percent": 20, "state": "Charging"}]):
            self.app.show_status(parse_status(json.dumps(SAMPLE)))
            self.app.show_status(parse_status(json.dumps(dict(SAMPLE, batteries=batteries))))
            self.assertIsNone(self.app.battery_icon.percent)
            image = render(self.app.battery_icon)
            self.assertFalse(any(
                image.pixelColor(x, y).name() == "#66c0f4"
                for x in range(image.width()) for y in range(image.height())
            ))
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        self.app.host.setText("another-frame")
        self.assertIsNone(self.app.battery_icon.percent)
        self.assertEqual(self.app.battery.text(), "—")

    def test_charging_bolt_and_full_label_follow_state_and_clear_on_disconnect(self):
        images = {}
        for percent, state, power, text, charging in (
            (75, "Discharging", False, "Discharging", False),
            (75, "Charging", True, "Charging", True),
            (100, "Discharging", True, "Fully charged", False),
            (100, "Discharging", False, "Discharging", False),
        ):
            data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0],
                percent=percent, state=state, external_power=power)])
            self.app.show_status(parse_status(json.dumps(data)))
            self.assertEqual(self.app.battery_detail.text().splitlines()[0], text)
            self.assertEqual(self.app.battery_icon.charging, charging)
            self.assertIn(text, self.app.battery_icon.toolTip())
            if percent == 75:
                images[charging] = render(self.app.battery_icon)
        # The bolt changes the center of the existing battery, without moving its outline.
        self.assertNotEqual(images[False].pixelColor(21, 16), images[True].pixelColor(21, 16))
        self.assertEqual(images[False].pixelColor(2, 16), images[True].pixelColor(2, 16))
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, batteries=[
            dict(SAMPLE["batteries"][0], state="Charging"),
        ]))))
        self.assertTrue(self.app.battery_icon.charging)
        self.app.host.setText("another-frame")
        self.assertFalse(self.app.battery_icon.charging)
        self.assertIsNone(self.app.battery_icon.percent)

    def test_battery_tooltip_identifies_steam_estimate_and_system_fallback(self):
        for source, tooltip in (("steam", "reported by Steam"), ("system", "Raw system")):
            with self.subTest(source=source):
                data = dict(SAMPLE, batteries=[dict(SAMPLE["batteries"][0], source=source)])
                self.app.show_status(parse_status(json.dumps(data)))
                self.assertIn(tooltip, self.app.battery.toolTip())
                self.assertIn(tooltip, self.app.battery_icon.toolTip())
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, batteries=[]))))
        self.assertEqual(self.app.battery.toolTip(), "")

    def test_main_screen_keeps_drop_panel_visible_at_small_and_large_sizes(self):
        for width, height in ((680, 680), (800, 950), (800, 1040), (1100, 850)):
            with self.subTest(size=(width, height)):
                self.app.resize(width, height)
                APPLICATION.processEvents()
                dock = self.app.drop_area
                position = dock.mapTo(self.app, QPoint(0, 0))
                self.assertEqual(position.x(), 28)
                self.assertEqual(dock.width(), self.app.width() - 56)
                footer = self.app.status_footer
                self.assertEqual(self.app.transfer_panel.mapTo(self.app, QPoint()).y() + self.app.transfer_panel.height(), footer.y())
                self.assertTrue(footer.isVisible())
                self.assertEqual(footer.x(), 0)
                self.assertEqual(footer.width(), self.app.width())
                self.assertEqual(footer.y() + footer.height(), self.app.height())
                self.assertEqual(self.app.footer_status.mapTo(self.app, QPoint()).x(), 18)
                self.assertEqual(self.app.footer_version.mapTo(self.app, QPoint()).x() + self.app.footer_version.width(), self.app.width() - 18)
                before_scroll = footer.geometry()
                scroll = self.app.status_scroll.verticalScrollBar()
                scroll.setValue(scroll.maximum())
                APPLICATION.processEvents()
                self.assertEqual(footer.geometry(), before_scroll)
                for widget in (self.app.destination, self.app.drop_title, self.app.file_button, self.app.transfer_filename, self.app.transfer_percent, self.app.transfer_detail):
                    self.assertTrue(widget.isVisible())
                    self.assertLess(widget.mapTo(self.app, QPoint()).y() + widget.height(), footer.y())
                self.assertLess(self.app.connection_panel.mapTo(self.app, QPoint()).y(), position.y())
                destination_y = self.app.destination.mapTo(self.app, QPoint()).y()
                self.assertGreaterEqual(destination_y - (self.app.os_card.mapTo(self.app, QPoint()).y() + self.app.os_card.height()), 18)
                self.assertLess(destination_y + self.app.destination.height(), position.y())
                progress_y = self.app.progress.mapTo(self.app, QPoint()).y()
                self.assertLess(position.y() + dock.height(), progress_y)
                self.assertGreater(self.app.transfer_filename.mapTo(self.app, QPoint()).y(), progress_y)
                container = self.app.transfer_metadata
                container_position = container.mapTo(self.app, QPoint())
                self.assertEqual(container_position.x(), position.x())
                self.assertEqual(container.width(), dock.width())
                self.assertGreater(container_position.y(), progress_y + self.app.progress.height())
                self.assertGreater(self.app.transfer_filename.mapTo(self.app, QPoint()).x(), container_position.x())
                self.assertLess(self.app.transfer_percent.mapTo(self.app, QPoint()).x() + self.app.transfer_percent.width(), container_position.x() + container.width())
                for label in (self.app.transfer_filename, self.app.transfer_percent):
                    self.assertGreaterEqual(label.y(), 0)
                    self.assertLessEqual(label.y() + label.height(), container.height())

    def test_four_status_cards_form_two_rows_without_squashing_text(self):
        for data in (SAMPLE, dict(SAMPLE, **READINGS)):
            self.app.show_status(parse_status(json.dumps(data)))
            for size in ((680, 700), (800, 850), (1100, 900)):
                with self.subTest(size=size, telemetry=bool(data.get("memory"))):
                    self.app.resize(*size)
                    self.app.status_scroll.verticalScrollBar().setValue(0)
                    APPLICATION.processEvents()
                    cards = [value.parentWidget() for value in (
                        self.app.battery, self.app.storage, self.app.temperature, self.app.ram,
                    )]
                    positions = [card.mapTo(self.app, QPoint()) for card in cards]
                    self.assertEqual(positions[0].x(), positions[2].x())
                    self.assertEqual(positions[1].x(), positions[3].x())
                    self.assertEqual(positions[0].y(), positions[1].y())
                    self.assertEqual(positions[2].y(), positions[3].y())
                    self.assertLess(positions[0].y() + cards[0].height(), positions[2].y())
                    self.assertLess(positions[0].x() + cards[0].width(), positions[1].x())
                    for card in cards:
                        self.assertTrue(card.isVisible())
                        self.assertGreaterEqual(card.height(), card.minimumSizeHint().height())
                        for text in card.findChildren(QLabel):
                            self.assertGreaterEqual(text.height(), text.heightForWidth(text.width()))
                            self.assertLess(text.y() + text.height(), card.height())
                    # Short windows scroll the status area; the transfer panel stays fixed.
                    scroll = self.app.status_scroll.verticalScrollBar()
                    scroll.setValue(scroll.maximum())
                    APPLICATION.processEvents()
                    dock_top = self.app.drop_area.mapTo(self.app, QPoint()).y()
                    for card in cards[2:]:
                        self.assertLess(card.mapTo(self.app, QPoint()).y() + card.height(), dock_top)

    def test_temperature_and_ram_readings_and_missing_sensor_fallback(self):
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, **READINGS))))
        self.assertEqual(self.app.temperature.text(), "52.5 °C")
        self.assertIn("soc-thermal", self.app.temperature_detail.text())
        self.assertEqual(self.app.ram.text(), "6.4 / 17.2 GB")
        self.assertIn("38% used", self.app.ram_detail.text())
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        self.assertEqual(self.app.temperature.text(), "Unavailable")
        self.assertEqual(self.app.ram.text(), "Unavailable")
        self.assertTrue(self.app._can_drop())

    def test_changing_connection_clears_temperature_and_ram(self):
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, **READINGS))))
        self.app.host.setText("another-frame")
        self.assertEqual(self.app.temperature.text(), "— °C")
        self.assertEqual(self.app.ram.text(), "— / — GB")

    def test_repeated_settings_dialog_changes_do_not_resize_the_main_window(self):
        before = self.app.size()
        for _ in range(5):
            self.app.settings_button.click()
            APPLICATION.processEvents()
            self.assertTrue(self.app.settings_dialog.isVisible())
            self.assertEqual(self.app.size(), before)
            self.app.settings_dialog.reject()
            APPLICATION.processEvents()
            self.assertEqual(self.app.size(), before)
        self.assertTrue(self.app.drop_title.isVisible())

    def test_button_corners_remain_dark_in_disabled_and_pressed_states(self):
        for pressed in (False, True):
            self.app.connect.setDown(pressed)
            image = render(self.app)
            for widget in self.app.findChildren(QPushButton):
                if widget.isVisible():
                    corner = widget.mapTo(self.app, QPoint(0, 0))
                    for x, y in ((0, 0), (widget.width() - 1, 0), (0, widget.height() - 1)):
                        color = image.pixelColor(corner.x() + x, corner.y() + y)
                        self.assertEqual(color.alpha(), 255)
                        self.assertLess(color.lightness(), 150, widget.text())

    def test_progress_bar_is_empty_partial_and_fully_filled_at_reported_values(self):
        def colors():
            image = render(self.app.progress)
            return {image.pixelColor(x, 2).name() for x in range(image.width())}
        self.assertNotIn("#66c0f4", colors())
        self.app.progress.set_progress(0.25)
        partial = render(self.app.progress)
        self.assertEqual(partial.pixelColor(partial.width() // 8, 2).name(), "#66c0f4")
        self.assertEqual(partial.pixelColor(partial.width() // 2, 2).name(), "#121b27")
        self.assertEqual(render(self.app.progress), partial)
        self.app.progress.set_progress(1)
        full = render(self.app.progress)
        for x in range(2, full.width() - 2):
            self.assertEqual(full.pixelColor(x, 2).name(), "#66c0f4")
        self.assertIn("#66c0f4", colors())
        self.app.progress.set_progress(0)
        self.assertNotIn("#66c0f4", colors())

    def test_refresh_keeps_ui_responsive_and_shows_readings(self):
        started, release = Event(), Event()
        def delayed_status(*_args):
            started.set()
            release.wait(2)
            return parse_status(json.dumps(dict(SAMPLE, **READINGS, os_version="SteamOS 3.7-test")))
        with patch("app.fetch_status", side_effect=delayed_status):
            try:
                self.app.refresh()
                self.assertTrue(started.wait(1))
                APPLICATION.processEvents()
                self.assertEqual(self.app.connection.text(), "Connecting…")
                self.assert_footer("Connecting to headset…", "#a7b6c8")
                self.assertFalse(self.app.host.isEnabled())
            finally:
                release.set()
            self.finish_refresh()
        self.assertEqual(self.app.connection.text(), "Connected to frame")
        self.assert_footer("Headset connected", "#78c78e")
        self.assertEqual(self.app.battery.text(), "84%")
        self.assertEqual(self.app.storage.text(), "600.0 GiB free")
        self.assertEqual(self.app.storage_usage.fraction, .4)
        self.assertEqual(self.app.temperature.text(), "52.5 °C")
        self.assertEqual(self.app.ram.text(), "6.4 / 17.2 GB")
        self.assertEqual(self.app.os_version.text(), "SteamOS 3.7-test")
        self.assertTrue(self.app._auto_timer.isActive())

    def test_failed_refresh_clears_readings_and_allows_retry(self):
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, **READINGS, os_version="SteamOS 3.7-test"))))
        with patch("app.fetch_status", side_effect=HeadsetError("Headset is offline")):
            self.app.refresh()
            self.finish_refresh()
        self.assertEqual(self.app.connection.text(), "Could not connect")
        self.assert_footer("Connection failed", "#ef7777")
        self.assertEqual(self.app.footer_status.toolTip(), "Headset is offline")
        self.assertIn("Headset is offline", self.app.detail.text())
        self.assertIn("Retrying automatically", self.app.detail.text())
        self.assertTrue(self.app._auto_timer.isActive())
        self.assertEqual(self.app.battery.text(), "—")
        self.assertEqual(self.app.temperature.text(), "— °C")
        self.assertEqual(self.app.ram.text(), "— / — GB")
        self.assertIsNone(self.app.battery_icon.percent)
        self.assertIsNone(self.app.storage_usage.fraction)
        self.assertEqual(self.app.os_version.text(), "—")
        self.assertFalse(self.app._can_drop())
        self.assertTrue(self.app.host.isEnabled())

    def test_missing_battery_is_unavailable_instead_of_zero(self):
        self.app.show_status(parse_status(json.dumps(dict(SAMPLE, batteries=[]))))
        self.assertEqual(self.app.battery.text(), "Unavailable")
        self.assertEqual(self.app.storage.text(), "600.0 GiB free")

    def test_drop_disabled_until_connected_and_after_connection_changes(self):
        self.assertEqual(self.app.destination.text(), "~/Downloads")
        self.assertFalse(self.app._can_drop())
        self.assertFalse(self.app.file_button.isEnabled())
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        self.assertTrue(self.app._can_drop())
        self.app.destination.setText("/home/steamos/Documents")
        self.app.host.setText("different-frame")
        self.assert_footer("Waiting for headset…", "#a7b6c8")
        self.assertFalse(self.app._can_drop())
        self.assertEqual(self.app.destination.text(), "~/Downloads")

    def test_qt_drag_and_drop_preserves_multiple_paths_and_forces_copy(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        paths = ("C:/My Files/clip [1].mp4", "C:/Other/{Ää}.txt")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        enter = QDragEnterEvent(QPointF(20, 20), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        drop = QDropEvent(QPointF(20, 20), actions, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        drop.setDropAction(Qt.DropAction.MoveAction)
        with patch.object(self.app.drop_area, "send", return_value=True) as send:
            APPLICATION.sendEvent(self.app.drop_area, enter)
            self.assertTrue(enter.isAccepted())
            self.assertTrue(self.app.drop_area.property("dragActive"))
            APPLICATION.sendEvent(self.app.drop_area, drop)
            self.assertFalse(self.app.drop_area.property("dragActive"))
        self.assertTrue(drop.isAccepted())
        self.assertEqual(drop.dropAction(), Qt.DropAction.CopyAction)
        send.assert_called_once_with(paths)

    def test_drag_highlight_clears_on_leave_and_when_headset_disconnects(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("C:/clip.mp4")])
        def enter():
            event = QDragEnterEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            APPLICATION.sendEvent(self.app.drop_area, event)
            self.assertTrue(event.isAccepted())
            self.assertTrue(self.app.drop_area.property("dragActive"))
        enter()
        APPLICATION.sendEvent(self.app.drop_area, QDragLeaveEvent())
        self.assertFalse(self.app.drop_area.property("dragActive"))
        enter()
        self.app.host.setText("another-frame")
        self.assertFalse(self.app.drop_area.property("dragActive"))

    def test_select_files_link_uses_the_native_picker_and_starts_the_upload(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        paths = ["C:/My Files/clip.mp4", "C:/notes.txt"]
        with patch("app.QFileDialog.getOpenFileNames", return_value=(paths, "")), patch.object(self.app, "start_upload") as send:
            self.app.file_button.click()
        send.assert_called_once_with(paths)

    def test_transfer_layout_stays_fixed_for_long_names_and_cancel_visibility(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        self.app.resize(680, 680)
        APPLICATION.processEvents()
        def positions():
            return tuple((widget.mapTo(self.app, QPoint()), widget.size()) for widget in (
                self.app.drop_area, self.app.file_button, self.app.progress, self.app.transfer_metadata, self.app.transfer_percent,
            ))
        before = positions()
        original_size = self.app.size()
        name = "Recording from Steam Frame " * 7 + "[1].mp4"
        with patch("app.Thread"):
            self.app.start_upload(("C:/" + name,))
        self.app._handle_transfer_result("progress", (name, 0, 1, 50, 100))
        APPLICATION.processEvents()
        self.assertEqual(positions(), before)
        self.assertTrue(self.app.cancel_button.isVisible())
        self.assertEqual(self.app.transfer_filename.text(), name)
        self.assertEqual(self.app.transfer_filename.toolTip(), name)
        self.assertEqual(self.app.transfer_percent.text(), "50%")
        self.app._handle_transfer_result("upload_error", HeadsetError("Connection lost: " + "details " * 100))
        APPLICATION.processEvents()
        self.assertEqual(self.app.size(), original_size)
        self.assertEqual(positions(), before)
        self.assertFalse(self.app.cancel_button.isVisible())
        self.assertIn("Transfer failed", self.app.transfer_detail.toolTip())

    def test_drop_rejects_remote_urls_and_disconnected_headset(self):
        for connected, url in ((False, QUrl.fromLocalFile("C:/file.txt")), (True, QUrl("https://example.com/file.txt"))):
            if connected:
                self.app.show_status(parse_status(json.dumps(SAMPLE)))
            mime = QMimeData()
            mime.setUrls([url])
            event = QDragEnterEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
            APPLICATION.sendEvent(self.app.drop_area, event)
            self.assertFalse(event.isAccepted())

    def test_move_only_drag_is_refused_to_keep_the_original_file(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile("C:/file.txt")])
        event = QDragEnterEvent(QPointF(20, 20), Qt.DropAction.MoveAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        APPLICATION.sendEvent(self.app.drop_area, event)
        self.assertFalse(event.isAccepted())

    def test_upload_uses_selected_folder_and_locks_controls_until_finished(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        self.app.destination.setText("/home/steamos/VR videos")
        started, release = Event(), Event()
        def upload(host, user, key, paths, destination, cancel, progress, *, on_verifying):
            self.assertEqual((host, user, key), ("frame", "steamos", ""))
            self.assertEqual(destination, "/home/steamos/VR videos")
            progress("clip.mp4", 0, 1, 25, 100)
            started.set()
            release.wait(2)
            return (destination + "/clip.mp4",)
        with patch("app.upload_files", side_effect=upload):
            try:
                self.assertTrue(self.app.start_upload(("C:/clip.mp4",)))
                self.assertTrue(started.wait(1))
                self.assertTrue(wait_until(lambda: "0/1" in self.app.transfer_detail.text()))
                self.assertFalse(self.app._can_drop())
                self.assertFalse(self.app.destination.isEnabled())
                self.assertFalse(self.app.connect.isEnabled())
            finally:
                release.set()
            self.finish_refresh()
        self.assertIn("Sent 1 file(s)", self.app.transfer_detail.text())
        self.assertTrue(self.app._can_drop())

    def test_progress_runs_preparation_bytes_verification_and_returns_empty(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        preparing, begin_upload, begin_verify, finish_verify = Event(), Event(), Event(), Event()
        def upload(_host, _user, _key, _paths, destination, _cancel, progress, *, on_verifying):
            preparing.set()
            begin_upload.wait(3)
            progress("clip.mp4", 0, 1, 25, 100)
            begin_verify.wait(3)
            progress("clip.mp4", 0, 1, 100, 100)
            on_verifying("clip.mp4", 0, 1)
            finish_verify.wait(3)
            return (destination + "/clip.mp4",)
        with patch("app.upload_files", side_effect=upload):
            try:
                self.app.progress.set_progress(1)
                self.app.start_upload(("C:/clip.mp4",))
                self.assertTrue(preparing.wait(1))
                APPLICATION.processEvents()
                self.assertFalse(self.app.progress.fraction)
                self.assertFalse(self.app.progress._animation_timer.isActive())
                begin_upload.set()
                self.assertTrue(wait_until(lambda: self.app.progress.fraction == .25))
                self.assertFalse(self.app.progress.verifying)
                begin_verify.set()
                self.assertTrue(wait_until(lambda: self.app.progress.verifying))
                self.assertEqual(self.app.progress.fraction, 1)
                self.assertEqual(self.app.transfer_percent.text(), "100%")
                self.assertEqual(self.app.transfer_filename.text(), "clip.mp4")
                self.assertIn("Verifying file", self.app.transfer_detail.text())
                self.assertTrue(self.app.busy)
                self.assertFalse(self.app.file_button.isEnabled())
                full = render(self.app.progress)
                self.assertTrue(all(full.pixelColor(x, 2).name() == "#66c0f4" for x in range(2, full.width() - 2)))
                self.assertTrue(wait_until(lambda: self.app.progress._verification_elapsed.elapsed() >= 450))
                moving = render(self.app.progress)
                blue = sum(moving.pixelColor(x, 2).name() == "#66c0f4" for x in range(moving.width()))
                self.assertGreater(blue, 0)
                self.assertLess(blue, moving.width() / 3)
                self.assertTrue(self.app.progress._animation_timer.isActive())
                self.assertTrue(wait_until(lambda: render(self.app.progress) != moving))
            finally:
                begin_upload.set()
                begin_verify.set()
                finish_verify.set()
            self.finish_refresh()
        self.assertEqual(self.app.progress.fraction, 0)
        self.assertFalse(self.app.progress.verifying)
        self.assertFalse(self.app.progress._animation_timer.isActive())
        self.assertTrue(self.app._can_drop())
        self.assertNotIn("#66c0f4", {render(self.app.progress).pixelColor(x, 2).name() for x in range(self.app.progress.width())})
        self.assertEqual(self.app.transfer_percent.text(), "0%")
        self.assertEqual(self.app.transfer_filename.text(), "")
        self.assertEqual(self.app.transfer_filename.toolTip(), "")
        self.assertIn("Sent 1 file(s)", self.app.transfer_detail.text())

    def test_folder_selection_is_used_for_upload_and_filename_clears_after_success(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        folder = "/home/steamos/Videos"
        response = {"path": "/home/steamos", "parent": "/home",
                    "folders": [{"name": "Videos", "path": folder}]}
        with patch("folder_picker.list_folders", return_value=response):
            self.app.choose_folder()
            picker = self.app.folder_picker
            self.assertTrue(wait_until(lambda: picker.ready))
            picker.listbox.setCurrentRow(0)
            picker.choose.click()
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.destination.text(), folder)
        with patch("app.upload_files", return_value=(folder + "/clip.mp4",)) as upload:
            self.app.start_upload(("C:/clip.mp4",))
            self.finish_refresh()
        self.assertEqual(upload.call_args.args[4], folder)
        self.assertEqual(self.app.destination.text(), folder)
        self.assertEqual(self.app.transfer_filename.text(), "")
        self.assertEqual(self.app.transfer_filename.toolTip(), "")
        self.assertEqual(self.app.transfer_percent.text(), "0%")
        self.assertIn("Sent 1 file(s)", self.app.transfer_detail.text())

    def test_progress_uses_batch_bytes_and_cancel_stops_verification(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        with patch("app.Thread"):
            self.app.start_upload(("C:/small.txt", "C:/large.mp4"))
        for reading, expected in (
            (("small.txt", 0, 2, 5, 100), 0.05),
            (("small.txt", 1, 2, 10, 100), 0.10),
            (("large.mp4", 1, 2, 55, 100), 0.55),
            (("large.mp4", 1, 2, 100, 100), 1),
        ):
            self.app._handle_transfer_result("progress", reading)
            self.assertAlmostEqual(self.app.progress.fraction, expected)
        self.assertEqual(self.app.transfer_percent.text(), "100%")
        self.app._handle_transfer_result("verifying", ("large.mp4", 1, 2))
        self.assertTrue(self.app.progress.verifying)
        self.app.cancel_transfer()
        self.assertFalse(self.app.progress.verifying)
        self.assertFalse(self.app.progress._animation_timer.isActive())
        self.app._handle_transfer_result("progress", ("large.mp4", 2, 2, 100, 100))
        self.app._handle_transfer_result("verifying", ("large.mp4", 1, 2))
        self.assertFalse(self.app.progress.verifying)
        self.app._handle_transfer_result("upload_error", TransferCancelled("Cleaned up", 1))
        self.assertIn("Cancelled", self.app.transfer_detail.text())
        self.app._handle_transfer_result("progress", ("large.mp4", 2, 2, 100, 100))
        self.app._handle_transfer_result("verifying", ("large.mp4", 1, 2))
        self.assertFalse(self.app.progress.verifying)

    def test_batch_verification_returns_to_progress_for_the_next_file(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        with patch("app.Thread"):
            self.app.start_upload(("C:/small.txt", "C:/large.mp4"))
        self.app._handle_transfer_result("progress", ("small.txt", 0, 2, 10, 100))
        self.app._handle_transfer_result("verifying", ("small.txt", 0, 2))
        self.assertEqual(self.app.progress.fraction, .1)
        self.assertTrue(self.app.progress.verifying)
        self.app._handle_transfer_result("progress", ("large.mp4", 1, 2, 55, 100))
        self.assertEqual(self.app.progress.fraction, .55)
        self.assertFalse(self.app.progress.verifying)
        self.assertFalse(self.app.progress._animation_timer.isActive())
        self.app._handle_transfer_result("uploaded", ("/home/steamos/Downloads/small.txt", "/home/steamos/Downloads/large.mp4"))

    def test_verification_failure_stops_animation_and_keeps_the_error_visible(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        with patch("app.Thread"):
            self.app.start_upload(("C:/clip.mp4",))
        self.app._handle_transfer_result("verifying", ("clip.mp4", 0, 1))
        self.app._handle_transfer_result("upload_error", HeadsetError("Uploaded size did not match"))
        self.assertFalse(self.app.progress.verifying)
        self.assertFalse(self.app.progress._animation_timer.isActive())
        self.assertIn("Transfer failed", self.app.transfer_detail.text())
        self.assertIn("Uploaded size did not match", self.app.transfer_detail.text())
        self.assertTrue(self.app._can_drop())

    def test_zero_byte_upload_verifies_then_returns_empty(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        with patch("app.Thread"):
            self.app.start_upload(("C:/empty.txt",))
        self.app._handle_transfer_result("progress", ("empty.txt", 0, 1, 0, 0))
        self.assertEqual(self.app.progress.fraction, 0)
        self.app._handle_transfer_result("verifying", ("empty.txt", 0, 1))
        self.assertTrue(self.app.progress.verifying)
        self.assertEqual(self.app.progress.fraction, 1)
        self.app._handle_transfer_result("uploaded", ("/home/steamos/Downloads/empty.txt",))
        self.assertEqual(self.app.progress.fraction, 0)
        self.assertFalse(self.app.progress.verifying)

    def test_cancel_reports_completed_files_and_restores_controls(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        with patch("app.upload_files", side_effect=TransferCancelled("Transfer cancelled", 1)):
            self.app.start_upload(("C:/one.txt", "C:/two.txt"))
            self.finish_refresh()
        self.assertIn("Cancelled · 1 file(s) uploaded", self.app.transfer_detail.text())
        self.assertFalse(self.app.cancel_button.isEnabled())
        self.assertFalse(self.app.progress.fraction)
        self.assertTrue(self.app._can_drop())

    def test_close_during_verification_waits_for_cleanup_and_stops_animation(self):
        self.app.show_status(parse_status(json.dumps(SAMPLE)))
        started, release = Event(), Event()
        def upload(_host, _user, _key, _paths, _destination, cancel, progress, *, on_verifying):
            progress("clip.mp4", 0, 1, 100, 100)
            on_verifying("clip.mp4", 0, 1)
            started.set()
            release.wait(2)
            self.assertTrue(cancel.is_set())
            raise TransferCancelled("Cleaned up")
        with patch("app.upload_files", side_effect=upload):
            try:
                self.app.start_upload(("C:/clip.mp4",))
                self.assertTrue(started.wait(1))
                self.assertTrue(wait_until(lambda: self.app.progress.verifying))
                self.app.close()
                self.assertTrue(self.app.isVisible())
                self.assertTrue(self.app.cancel_upload.is_set())
                self.assertFalse(self.app.progress.verifying)
                self.assertFalse(self.app.progress._animation_timer.isActive())
            finally:
                release.set()
            self.assertTrue(wait_until(lambda: not self.app.isVisible()))

    def test_actual_downloads_default_preserves_custom_folder(self):
        status = parse_status(json.dumps(dict(SAMPLE, downloads="/home/steamos/Lataukset")))
        self.app.show_status(status)
        self.assertEqual(self.app.destination.text(), "/home/steamos/Lataukset")
        self.app.destination.setText("/home/steamos/Documents")
        self.app.show_status(status)
        self.assertEqual(self.app.destination.text(), "/home/steamos/Documents")


if __name__ == "__main__":
    unittest.main()
