"""Read controller batteries and official status icons from the running SteamVR.

Uses the public OpenVR IVRSystem_026 function table (Valve BSD-3-Clause; see
licenses/Valve-OpenVR.txt). Background mode does not start or keep SteamVR alive.
Nothing is installed on the headset; icon files are loaded from its runtime,
not bundled as application assets.
"""

import base64
import ctypes as C
import math
from pathlib import Path
import platform


def controller_icon(runtime: Path, hand: str, connected: bool) -> str | None:
    state = "ready" if connected else "off"
    path = runtime / "drivers/frame_controller/resources/icons" / f"{hand}_frame_controller_{state}_2x.png"
    try:
        with path.open("rb") as source:
            data = source.read(65537)
        if len(data) <= 65536 and data.startswith(b"\x89PNG\r\n\x1a\n"):
            return base64.b64encode(data).decode("ascii")
    except OSError:
        pass
    return None


def read_controllers(runtime: Path = Path("/opt/steamvr")) -> list[dict] | None:
    architecture = "linuxarm64" if platform.machine().lower() in ("aarch64", "arm64") else "linux64"
    library = runtime / "bin" / architecture / "libopenvr_api.so"
    if not library.is_file():
        return None
    try:
        api = C.CDLL(str(library))
        api.VR_InitInternal2.argtypes = [C.POINTER(C.c_int), C.c_int, C.c_char_p]
        api.VR_InitInternal2.restype = C.c_uint32
        api.VR_GetGenericInterface.argtypes = [C.c_char_p, C.POINTER(C.c_int)]
        api.VR_GetGenericInterface.restype = C.c_void_p
        api.VR_ShutdownInternal.argtypes = []
        api.VR_ShutdownInternal.restype = None
        error = C.c_int()
        api.VR_InitInternal2(C.byref(error), 3, None)  # VRApplication_Background
        if error.value:
            return None
        try:
            pointer = api.VR_GetGenericInterface(b"FnTable:IVRSystem_026", C.byref(error))
            if error.value or not pointer:
                return None
            table = C.cast(pointer, C.POINTER(C.c_void_p))
            # Slot positions belong specifically to IVRSystem_026, not older ABIs.
            role = C.CFUNCTYPE(C.c_uint32, C.c_int)(table[18])
            device_class = C.CFUNCTYPE(C.c_int, C.c_uint32)(table[20])
            is_connected = C.CFUNCTYPE(C.c_bool, C.c_uint32)(table[21])
            battery = C.CFUNCTYPE(C.c_float, C.c_uint32, C.c_int, C.POINTER(C.c_int))(table[23])
            readings = []
            for hand, controller_role in (("left", 1), ("right", 2)):
                index = role(controller_role)
                connected = index < 64 and device_class(index) == 2 and is_connected(index)
                percent = None
                if connected:
                    property_error = C.c_int()
                    level = battery(index, 1012, C.byref(property_error))
                    if not property_error.value and math.isfinite(level) and 0 <= level <= 1:
                        percent = int(level * 100 + .5)
                readings.append({"hand": hand, "connected": bool(connected), "percent": percent,
                                 "icon": controller_icon(runtime, hand, bool(connected))})
            return readings
        finally:
            api.VR_ShutdownInternal()
    except (OSError, AttributeError, ValueError):
        return None
