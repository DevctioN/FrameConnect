"""SSH transport and validation, separate from the desktop interface."""

from dataclasses import dataclass
import base64
import binascii
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess


class HeadsetError(Exception):
    """An actionable connection or status error for the interface."""


@dataclass(frozen=True)
class Battery:
    name: str
    percent: int
    state: str
    source: str = "system"
    external_power: bool | None = None
    time_to_full: int | None = None
    time_to_empty: int | None = None
    power_watts: float | None = None

    @property
    def display_state(self) -> str:
        if self.state == "Full" or (self.percent == 100 and (
            self.external_power is True or self.state == "Charging"
        )):
            return "Fully charged"
        if self.external_power is True and self.state in ("Discharging", "Not charging", "Unknown"):
            return "Plugged in · Not charging"
        return self.state

    @property
    def detail(self) -> str:
        state = self.display_state
        seconds = self.time_to_full if state == "Charging" else self.time_to_empty if state == "Discharging" else None
        lines = [state]
        if seconds is not None:
            minutes = math.ceil(seconds / 60)
            hours, minutes = divmod(minutes, 60)
            duration = f"{hours} h" + (f" {minutes} min" if minutes else "") if hours else f"{minutes} min"
            lines.append(f"About {duration} " + ("until full" if state == "Charging" else "remaining"))
        elif state in ("Charging", "Discharging"):
            lines.append("Time estimate unavailable")
        if self.power_watts is not None:
            direction = "in" if self.power_watts > 0 else "out" if self.power_watts < 0 else ""
            lines.append(f"Battery power: {abs(self.power_watts):.1f} W {direction}".rstrip())
        return "\n".join(lines)


@dataclass(frozen=True)
class Controller:
    hand: str
    connected: bool
    percent: int | None
    icon: bytes | None = None


@dataclass(frozen=True)
class Temperature:
    celsius: float
    sensor: str


@dataclass(frozen=True)
class Memory:
    total: int
    available: int

    @property
    def used(self) -> int:
        return self.total - self.available


@dataclass(frozen=True)
class HeadsetStatus:
    hostname: str
    batteries: tuple[Battery, ...]
    total: int
    used: int
    free: int
    downloads: str = "~/Downloads"
    temperature: Temperature | None = None
    memory: Memory | None = None
    os_version: str | None = None
    controllers: tuple[Controller, ...] | None = None


def parse_status(output: str) -> HeadsetStatus:
    try:
        data = json.loads(output)
        hostname = data["hostname"]
        if not isinstance(hostname, str) or not hostname.strip():
            raise ValueError("Missing hostname")
        storage = data["storage"]
        total, used, free = (storage[key] for key in ("total", "used", "free"))
        if any(type(value) is not int for value in (total, used, free)):
            raise ValueError("Invalid storage values")
        if total <= 0 or not (0 <= used <= total and 0 <= free <= total):
            raise ValueError("Storage values out of range")
        if used + free > total:
            raise ValueError("Inconsistent storage values")
        if not isinstance(data["batteries"], list):
            raise ValueError("Invalid batteries")
        batteries = []
        for item in data["batteries"]:
            name, percent, state = item["name"], item["percent"], item["state"]
            if not isinstance(name, str) or not isinstance(state, str):
                raise ValueError("Invalid battery description")
            if type(percent) is not int or not 0 <= percent <= 100:
                raise ValueError("Invalid battery percentage")
            source = item.get("source", "system")
            if source not in ("system", "steam"):
                raise ValueError("Invalid battery source")
            external_power = item.get("external_power")
            if external_power is not None and type(external_power) is not bool:
                raise ValueError("Invalid external power state")
            times = [item.get(key) for key in ("time_to_full", "time_to_empty")]
            if any(value is not None and (type(value) is not int or not 0 < value <= 604800) for value in times):
                raise ValueError("Invalid battery time estimate")
            watts = item.get("power_watts")
            if watts is not None and (type(watts) not in (int, float) or not math.isfinite(watts) or abs(watts) > 200):
                raise ValueError("Invalid battery power")
            batteries.append(Battery(name, percent, state, source, external_power, *times, watts))
        downloads = data.get("downloads", "~/Downloads")
        if not isinstance(downloads, str) or not downloads.strip():
            raise ValueError("Invalid Downloads folder")
        temperature = None
        if data.get("temperature") is not None:
            reading = data["temperature"]
            celsius, sensor = reading["celsius"], reading["sensor"]
            if type(celsius) not in (int, float) or not -100 <= celsius <= 200:
                raise ValueError("Invalid temperature")
            if not isinstance(sensor, str) or not sensor.strip():
                raise ValueError("Invalid temperature sensor")
            temperature = Temperature(celsius, sensor)
        memory = None
        if data.get("memory") is not None:
            reading = data["memory"]
            ram_total, available = reading["total"], reading["available"]
            if type(ram_total) is not int or type(available) is not int:
                raise ValueError("Invalid memory values")
            if ram_total <= 0 or not 0 <= available <= ram_total:
                raise ValueError("Memory values out of range")
            memory = Memory(ram_total, available)
        os_version = data.get("os_version")
        if os_version is not None and (not isinstance(os_version, str) or not os_version.strip()):
            raise ValueError("Invalid OS version")
        controllers = None
        if data.get("controllers") is not None:
            if not isinstance(data["controllers"], list) or len(data["controllers"]) > 2:
                raise ValueError("Invalid controllers")
            controllers = []
            for item in data["controllers"]:
                hand, connected, percent = item["hand"], item["connected"], item.get("percent")
                if hand not in ("left", "right") or any(reading.hand == hand for reading in controllers):
                    raise ValueError("Invalid controller hand")
                if type(connected) is not bool or (percent is not None and (
                    type(percent) is not int or not 0 <= percent <= 100 or not connected
                )):
                    raise ValueError("Invalid controller battery")
                icon = item.get("icon")
                if icon is not None:
                    if not isinstance(icon, str) or len(icon) > 90000:
                        raise ValueError("Invalid controller icon")
                    icon = base64.b64decode(icon, validate=True)
                    if len(icon) > 65536 or not icon.startswith(b"\x89PNG\r\n\x1a\n"):
                        raise ValueError("Invalid controller icon")
                controllers.append(Controller(hand, connected, percent, icon))
            controllers = tuple(controllers)
        return HeadsetStatus(hostname, tuple(batteries), total, used, free, downloads, temperature, memory, os_version, controllers)
    except (ValueError, TypeError, KeyError, binascii.Error) as error:
        raise HeadsetError("The headset returned an unreadable status response.") from error


def validate_address(host: str, user: str) -> None:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,252}", host):
            raise HeadsetError("Enter a hostname or IP address, without a port or username.")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", user):
        raise HeadsetError("Enter a valid SSH username (normally steamos).")


def ssh_options(key_file: str = "") -> list[str]:
    options = [
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectTimeout=8",
        "-o", "ConnectionAttempts=1",
        "-o", "ClearAllForwardings=yes",
        "-o", "ForwardAgent=no",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=3",
    ]
    if key_file.strip():
        key = Path(key_file.strip()).expanduser()
        if not key.is_file():
            raise HeadsetError("The selected private key file does not exist.")
        options.extend(["-i", str(key), "-o", "IdentitiesOnly=yes"])
    return options


def ssh_command(host: str, user: str, key_file: str = "") -> list[str]:
    host, user = host.strip(), user.strip()
    validate_address(host, user)
    ssh = shutil.which("ssh")
    if not ssh:
        raise HeadsetError("SSH is missing. Enable OpenSSH Client in Windows Optional Features.")
    return [ssh, "-T", *ssh_options(key_file), "-l", user, "--", host]


def ssh_error(detail: str) -> HeadsetError:
    if "REMOTE HOST IDENTIFICATION HAS CHANGED" in detail:
        message = "The headset's SSH identity changed. Verify its identity in a terminal before reconnecting."
    elif "Host key verification failed" in detail:
        message = "First connect with SSH in a terminal to verify the headset's identity. See README.md."
    elif "Permission denied (" in detail:
        message = "SSH key login failed. Select your private key or load it into ssh-agent. See README.md."
    elif "Permission denied" in detail:
        message = "Permission denied. Check your SSH key and the destination folder's permissions. See README.md."
    elif "python3" in detail and ("not found" in detail or "No such file" in detail):
        message = "SSH connected, but Python 3 is unavailable on the headset."
    else:
        message = detail[-600:] or "SSH failed. Check Developer Mode, the address, and your network."
    return HeadsetError(message)


def remote_script_source(filename: str) -> str:
    """Load read-only helpers in memory on the headset, then run a script."""
    root = Path(__file__).resolve().parent
    source = (root / filename).read_text(encoding="utf-8")
    bootstrap = "import sys, types\n"
    for name in (("steam_api", "vr_status") if filename == "probe.py" else ("steam_api",)):
        helper = (root / (name + ".py")).read_text(encoding="utf-8")
        bootstrap += (f"_frame_helper = types.ModuleType({name!r})\n"
                      f"exec({helper!r}, _frame_helper.__dict__)\n"
                      f"sys.modules[{name!r}] = _frame_helper\n")
    return bootstrap + f"exec({source!r}, {{'__name__': '__main__'}})\n"


def fetch_status(host: str, user: str = "steamos", key_file: str = "") -> HeadsetStatus:
    command = [*ssh_command(host, user, key_file), "python3", "-"]
    try:
        source = remote_script_source("probe.py")
        result = subprocess.run(
            command,
            input=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as error:
        raise HeadsetError("Connection timed out. Wake the headset and check its address and network.") from error
    except OSError as error:
        raise HeadsetError(f"Could not read headset status: {error}") from error

    if result.returncode:
        raise ssh_error(result.stderr.strip())
    return parse_status(result.stdout)


def power_off_headset(host: str, user: str = "steamos", key_file: str = "") -> None:
    """Request shutdown once; never retry an action with an uncertain outcome."""
    command = [*ssh_command(host, user, key_file), "python3", "-"]
    try:
        result = subprocess.run(
            command, input=remote_script_source("remote_power.py"), capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as error:
        raise HeadsetError("Power-off request timed out. Check the headset before trying again.") from error
    except OSError as error:
        raise HeadsetError(f"Could not send the power-off request: {error}") from error
    if result.returncode:
        detail = ssh_error(result.stderr.strip())
        raise HeadsetError(f"Could not confirm power off. Check the headset before trying again.\n{detail}")
    try:
        response = json.loads(result.stdout)
        if not isinstance(response, dict) or response.get("requested") is not True:
            message = response.get("error") if isinstance(response, dict) else None
            raise HeadsetError(message if isinstance(message, str) and message else "Steam did not confirm the power-off request.")
    except (ValueError, TypeError) as error:
        raise HeadsetError("The power-off response was unreadable. Check the headset before trying again.") from error
