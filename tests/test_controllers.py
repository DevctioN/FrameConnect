import base64
import ctypes as C
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from headset import HeadsetError, parse_status
from qt_helpers import APPLICATION, dispose, render
from app import FrameConnectApp
from test_headset import SAMPLE, fixture_directory
from vr_status import controller_icon, read_controllers


class ControllerTests(TestCase):
    def test_background_api_reads_roles_and_batteries_and_always_shuts_down(self):
        with fixture_directory() as root, patch("vr_status.platform.machine", return_value="aarch64"):
            library = root / "bin/linuxarm64/libopenvr_api.so"
            library.parent.mkdir(parents=True)
            library.touch()
            role = C.CFUNCTYPE(C.c_uint32, C.c_int)(lambda hand: hand)
            device_class = C.CFUNCTYPE(C.c_int, C.c_uint32)(lambda index: 2)
            connected = C.CFUNCTYPE(C.c_bool, C.c_uint32)(lambda index: index == 1)
            get_battery = C.CFUNCTYPE(C.c_float, C.c_uint32, C.c_int, C.POINTER(C.c_int))(
                lambda index, prop, error: .57)
            table = (C.c_void_p * 24)()
            for slot, callback in ((18, role), (20, device_class), (21, connected), (23, get_battery)):
                table[slot] = C.cast(callback, C.c_void_p).value
            api = Mock()
            api.VR_GetGenericInterface.return_value = C.addressof(table)
            with patch("vr_status.C.CDLL", return_value=api):
                readings = read_controllers(root)
                self.assertEqual([(r["hand"], r["connected"], r["percent"]) for r in readings],
                                 [("left", True, 57), ("right", False, None)])
                self.assertEqual(api.VR_InitInternal2.call_args.args[1:], (3, None))
                self.assertEqual(api.VR_GetGenericInterface.call_args.args[0], b"FnTable:IVRSystem_026")
                api.VR_ShutdownInternal.assert_called_once()
                api.reset_mock()
                api.VR_GetGenericInterface.return_value = None
                self.assertIsNone(read_controllers(root))
                api.VR_ShutdownInternal.assert_called_once()
        self.assertIsNone(read_controllers(root))

    def test_icon_reads_are_bounded_and_only_use_the_runtime_controller_paths(self):
        with fixture_directory() as root:
            path = root / "drivers/frame_controller/resources/icons/left_frame_controller_ready_2x.png"
            path.parent.mkdir(parents=True)
            self.assertIsNone(controller_icon(root, "left", True))
            image = b"\x89PNG\r\n\x1a\nfixture"
            path.write_bytes(image)
            self.assertEqual(base64.b64decode(controller_icon(root, "left", True)), image)
            path.write_bytes(b"not an image")
            self.assertIsNone(controller_icon(root, "left", True))
            path.write_bytes(image + b"x" * 65536)
            self.assertIsNone(controller_icon(root, "left", True))

    def test_controller_data_is_optional_and_rejects_invalid_readings(self):
        self.assertIsNone(parse_status(json.dumps(SAMPLE)).controllers)
        for readings in (
            {}, [dict(hand="left", connected=True, percent=101)],
            [dict(hand="left", connected=False, percent=50)],
            [dict(hand="left", connected=1, percent=50)],
            [dict(hand="other", connected=True, percent=50)],
            [dict(hand="left", connected=True, percent=50)] * 2,
            [dict(hand="left", connected=True, percent=50, icon="not base64")],
        ):
            with self.subTest(readings=readings), self.assertRaises(HeadsetError):
                parse_status(json.dumps(dict(SAMPLE, controllers=readings)))

    def test_card_shows_quarter_dots_and_clears_disconnected_and_failed_readings(self):
        window = FrameConnectApp()
        window.show()
        try:
            for percent, dots in ((0, 0), (1, 1), (25, 1), (26, 2), (50, 2), (57, 3), (100, 4)):
                readings = [dict(hand="left", connected=True, percent=percent),
                            dict(hand="right", connected=False, percent=None)]
                window.show_status(parse_status(json.dumps(dict(SAMPLE, controllers=readings))))
                self.assertEqual(window.controllers["left"].value.text(), f"{percent}%")
                self.assertEqual(window.controllers["left"].dots.filled, dots)
                self.assertEqual(window.controllers["right"].detail.text(), "Not connected")
                self.assertEqual(window.controllers["right"].dots.filled, 0)
            for size in ((680, 680), (800, 1100)):
                window.resize(*size)
                APPLICATION.processEvents()
                self.assertLess(window.controllers_card.height(), 150)
                for reading in window.controllers.values():
                    self.assertLess(reading.icon.y() + reading.icon.height(), reading.dots.y())
                    self.assertLessEqual(reading.value.x() + reading.value.width(), reading.width())
            window._show_status_error(HeadsetError("Headset asleep"), automatic=True)
            for reading in window.controllers.values():
                self.assertEqual(reading.value.text(), "—")
                self.assertEqual(reading.dots.filled, 0)
                self.assertEqual(reading.detail.text(), "Status unavailable")
        finally:
            dispose(window)
