"""Read-only status collector, sent to the headset over SSH stdin.

Nothing is installed on the headset. Keep this file standard-library only.
"""

import json
import math
from pathlib import Path
import platform
import shutil
import socket
import subprocess

from steam_api import query_steam
from vr_status import read_controllers


# Steam scales the physical battery gauge. Ask its battery API for the same
# estimate used by the Steam client, without installing a service or keeping a
# listener alive. The timeout also disposes the listener if no value arrives.
STEAM_BATTERY_QUERY = """new Promise((resolve) => {
    let subscription;
    let complete = false;
    const dispose = () => { try { subscription?.unregister?.(); } catch (_) {} };
    const finish = (value) => {
        if (complete) return;
        complete = true;
        clearTimeout(timer);
        dispose();
        resolve(value);
    };
    const timer = setTimeout(() => finish(null), 750);
    try {
        subscription = SteamClient.System.RegisterForBatteryStateChanges((state) => {
            finish(state.bHasBattery ? {level: state.flLevel, seconds: state.nSecondsRemaining} : null);
        });
        if (complete) dispose();
    } catch (_) { finish(null); }
})"""


def read_steam_battery() -> dict | None:
    """Steam's displayed percentage and its estimate of remaining use."""
    value = query_steam(STEAM_BATTERY_QUERY)
    if not isinstance(value, dict):
        return None
    level = value.get("level")
    if type(level) not in (int, float) or not math.isfinite(level) or not 0 <= level <= 1:
        return None
    result = {"percent": int(level * 100 + .5), "source": "steam"}
    seconds = value.get("seconds")
    # The Frame's gauge exposes a saturated 0xffff time register as 368634 s.
    if type(seconds) is int and 0 < seconds <= 604800 and seconds != 368634:
        result["time_to_empty"] = seconds
    return result


def downloads_directory() -> str:
    try:
        result = subprocess.run(
            ["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True,
            check=True, timeout=3,
        )
        path = result.stdout.strip()
        if path and Path(path).is_absolute():
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    return str(Path.home() / "Downloads")


def read_attribute(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return None


def read_batteries(root: Path = Path("/sys/class/power_supply")) -> list[dict]:
    batteries = []
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return batteries

    for supply in supplies:
        if read_attribute(supply / "type") != "Battery":
            continue
        # Controllers can also appear here. Only report system batteries.
        if read_attribute(supply / "scope") == "Device":
            continue
        if read_attribute(supply / "present") == "0":
            continue
        try:
            percent = int(read_attribute(supply / "capacity") or "")
        except ValueError:
            continue
        if 0 <= percent <= 100:
            reading = {
                "name": supply.name,
                "percent": percent,
                "state": read_attribute(supply / "status") or "Unknown",
            }
            for attribute, key in (("time_to_full_now", "time_to_full"), ("time_to_empty_now", "time_to_empty")):
                try:
                    seconds = int(read_attribute(supply / attribute) or "")
                    if 0 < seconds <= 604800 and seconds != 368634:
                        reading[key] = seconds
                except ValueError:
                    pass
            try:
                current = int(read_attribute(supply / "current_now") or "")
                voltage = int(read_attribute(supply / "voltage_now") or "")
                watts = current * voltage / 1e12
                if voltage > 0 and math.isfinite(watts) and abs(watts) <= 200:
                    reading["power_watts"] = round(watts, 3)
            except ValueError:
                pass
            batteries.append(reading)
    return batteries


def read_external_power(root: Path = Path("/sys/class/power_supply")) -> bool | None:
    """Read charger presence separately from the battery's instantaneous status."""
    try:
        supplies = sorted(root.iterdir())
    except OSError:
        return None
    online = []
    for supply in supplies:
        kind = read_attribute(supply / "type") or ""
        if kind not in ("Mains", "Wireless") and not kind.startswith("USB"):
            continue
        if read_attribute(supply / "scope") == "Device":
            continue
        value = read_attribute(supply / "online")
        # USB-PD supplies can report 2 for programmable power, as well as 1.
        if value in ("0", "1", "2"):
            online.append(value != "0")
    return any(online) if online else None


def read_temperature(root: Path = Path("/sys/class/thermal")) -> dict | None:
    """Highest readable thermal-zone temperature, preserving its sensor name."""
    try:
        zones = sorted(root.glob("thermal_zone[0-9]*"))
    except OSError:
        return None
    readings = []
    for zone in zones:
        try:
            # Linux thermal-zone temperatures are in millidegrees Celsius.
            celsius = int(read_attribute(zone / "temp") or "") / 1000
        except ValueError:
            continue
        # Reject implausible headset readings and driver error sentinels.
        if -100 <= celsius <= 200:
            readings.append({
                "celsius": celsius,
                "sensor": read_attribute(zone / "type") or zone.name,
            })
    return max(readings, key=lambda reading: reading["celsius"], default=None)


def read_memory(path: Path = Path("/proc/meminfo")) -> dict | None:
    """Usable RAM and the kernel's available-memory estimate, in bytes."""
    source = read_attribute(path)
    if source is None:
        return None
    values = {}
    for line in source.splitlines():
        key, separator, value = line.partition(":")
        if key not in ("MemTotal", "MemAvailable"):
            continue
        parts = value.split()
        if not separator or key in values or len(parts) != 2 or parts[1] != "kB":
            return None
        try:
            values[key] = int(parts[0]) * 1024
        except ValueError:
            return None
    total, available = values.get("MemTotal", 0), values.get("MemAvailable", -1)
    if total <= 0 or not 0 <= available <= total:
        return None
    return {"total": total, "available": available}


def read_os_version() -> str | None:
    """Read the distribution version, rather than the Linux kernel version."""
    try:
        release = platform.freedesktop_os_release()
    except (OSError, UnicodeError):
        return None
    version = release.get("VERSION_ID", "").strip() or release.get("VERSION", "").strip()
    if not version:
        return None
    name = release.get("NAME", "OS").strip() or "OS"
    return f"{name} {version}"


def collect_status() -> dict:
    # SteamOS keeps user content under /home, separate from the OS partition.
    usage = shutil.disk_usage(Path.home())
    batteries = read_batteries()
    if len(batteries) == 1:
        steam = read_steam_battery()
        if steam is not None:
            # Steam's timer describes remaining use, never time to full.
            if batteries[0]["state"] != "Discharging":
                steam.pop("time_to_empty", None)
            batteries[0].update(steam)
    external_power = read_external_power()
    if external_power is not None:
        for battery in batteries:
            battery["external_power"] = external_power
    return {
        "hostname": socket.gethostname(),
        "downloads": downloads_directory(),
        "batteries": batteries,
        "storage": {"total": usage.total, "used": usage.used, "free": usage.free},
        "temperature": read_temperature(),
        "memory": read_memory(),
        "os_version": read_os_version(),
        "controllers": read_controllers(),
    }


if __name__ == "__main__":
    print(json.dumps(collect_status()))
