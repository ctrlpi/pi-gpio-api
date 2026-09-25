#!/usr/bin/env python3
"""
A Python server that exposes Raspberry Pi GPIO control through an OpenAPI REST API,
allowing HTTP clients to trigger outputs and read inputs remotely.
"""

import os, sys, json, time, threading, socket, logging, re, secrets, shutil, subprocess, shlex, signal
import platform as _platform
from collections import deque
from datetime import datetime, timedelta

# Redact api_key/session_id values from URLs before they reach the access log
def _strip_qs_params(s):
    s = re.sub(r'\?(api_key|session_id)=[^&\s"]*&', '?', s, flags=re.IGNORECASE)
    s = re.sub(r'\?(api_key|session_id)=[^&\s"]*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'&(api_key|session_id)=[^&\s"]*', '', s, flags=re.IGNORECASE)
    return s

class _StripQsFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.args, tuple):
            record.args = tuple(_strip_qs_params(a) if isinstance(a, str) else a for a in record.args)
        return True

logging.getLogger("uvicorn.access").addFilter(_StripQsFilter())
logging.getLogger("watchfiles").setLevel(logging.WARNING)  # quiet --reload's file-watcher chatter

try:
    import requests
    from pydantic import BaseModel, Field, field_validator, ConfigDict
    from fastapi import FastAPI, Request, HTTPException, Depends
    from fastapi.concurrency import run_in_threadpool
    from fastapi.responses import JSONResponse, Response
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException
except ModuleNotFoundError as e:
    print(f"Error: missing package '{e.name}'.")
    print("  Run: pip install -r requirements.txt")
    print("  or use: run.sh to create/enable a virtual environment (venv) and start")
    raise SystemExit(1)

from typing import Optional, Union, Literal, Dict, Any
from contextlib import asynccontextmanager
VERSION = "0.9.23"  # agent software version (kept in lockstep with pico-gpio-api); surfaced in /config/read
IS_PI = os.path.exists("/proc/device-tree/model")  # only real Pi hardware exposes this
_PI_GPIO_PINS = [str(p) for p in range(2, 28)]  # usable BCM range (2-27)
# GPIO 2 and 3 have fixed hardware pull-ups on all Pi boards.
_PI_FIXED_PULLUP_PINS = ("2", "3")
# Maximum accepted request body size in bytes (protects against 413s).
MAX_REQUEST_BYTES = 16384

# Words dropped from board strings to clean up device names.
_MODEL_NOISE = {"raspberry", "model"}

def _strip_model_words(text: str) -> str:
    parts = [w for w in str(text).split() if w.lower() not in _MODEL_NOISE]
    if len(parts) > 1 and parts[0].lower() == "pi" and parts[1].lower() == "pico":
        parts = parts[1:]
    return " ".join(parts)

def _get_platform() -> str:
    if IS_PI:
        try:
            with open("/proc/device-tree/model", "r") as f:
                return _strip_model_words(f.read().strip().rstrip("\x00"))
        except Exception:
            pass
    try:
        if _platform.system() == "Darwin":
            return subprocess.check_output(["sysctl", "-n", "hw.model"], text=True).strip()
    except Exception:
        pass
    return _platform.machine() or _platform.system()

def _get_os() -> str:
    """Returns a short, human-readable OS label (e.g., 'Bookworm 64-bit')."""
    # Determine word size based on the userland architecture.
    machine = (_platform.machine() or "").lower()
    if machine.startswith(("armv6", "armv7")) or machine in ("i386", "i686"):
        bits = "32-bit"
    elif machine in ("aarch64", "arm64", "x86_64", "amd64"):
        bits = "64-bit"
    else:
        bits = "64-bit" if sys.maxsize > 2**32 else "32-bit"
    try:
        with open("/etc/os-release", "r") as f:
            fields = {}
            for line in f:
                if "=" in line:
                    k, _, v = line.partition("=")
                    fields[k.strip()] = v.strip().strip('"')
        # VERSION_CODENAME is "bookworm"/"trixie"; PRETTY_NAME carries it in brackets on
        # some images ("Debian GNU/Linux 12 (bookworm)"), so fall back to parsing that.
        codename = fields.get("VERSION_CODENAME", "")
        if not codename:
            m = re.search(r"\(([^)]+)\)", fields.get("PRETTY_NAME", ""))
            codename = m.group(1) if m else ""
        if codename:
            return f"{codename.capitalize()} {bits}"
        pretty = fields.get("PRETTY_NAME") or fields.get("NAME")
        if pretty:
            return f"{pretty} {bits}"
    except Exception:
        pass
    return f"{_platform.system()} {_platform.release()}".strip()

def _get_cpu_temperature():
    """Pi CPU temperature in °C, or None when unavailable (non-Pi)."""
    if not IS_PI:
        return None
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read().strip()) / 1000, 1)
    except Exception:
        return None

def _get_signal():
    """Returns WiFi signal strength in dBm, or None if not on wireless."""
    try:
        with open("/proc/net/wireless", "r") as f:
            for line in f.readlines()[2:]:        # two header rows
                parts = line.split()
                if len(parts) >= 4 and parts[0].endswith(":"):
                    return int(float(parts[3].rstrip(".")))
    except Exception:
        pass
    return None

def _get_wifi() -> str:
    """Returns the SSID of the active WiFi network, or empty string if not connected."""
    try:
        ssid = subprocess.check_output(["iwgetid", "-r"], text=True,
                                       timeout=2, stderr=subprocess.DEVNULL).strip()
        if ssid:
            return ssid
    except Exception:
        pass
    return ""

def _get_serial() -> str:
    """Returns the board's serial number (lowercase), or empty string if unavailable."""
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.lower().startswith("serial"):
                    serial = line.split(":", 1)[1].strip().lower()
                    if serial:
                        return serial
    except Exception:
        pass
    try:
        with open("/proc/device-tree/serial-number", "r") as f:
            return f.read().strip("\0 \n").lower()
    except Exception:
        pass
    return ""

def _get_mac() -> str:
    """Returns the MAC address of the default network interface, or empty string if unavailable."""
    try:
        iface, best_metric = None, None
        with open("/proc/net/route", "r") as f:
            for line in f.readlines()[1:]:          # one header row
                parts = line.split()
                # Destination 00000000 is the default route; lowest metric wins when a
                # box is on both eth and wifi, matching what the kernel would choose.
                if len(parts) >= 7 and parts[1] == "00000000":
                    metric = int(parts[6])
                    if best_metric is None or metric < best_metric:
                        iface, best_metric = parts[0], metric
        if iface:
            with open(f"/sys/class/net/{iface}/address", "r") as f:
                return f.read().strip().lower()
    except Exception:
        pass
    return ""

def _format_uptime(seconds: float) -> str:
    """Seconds -> the short human string the lab and the dashboards display as-is.
    At most two units, largest first, so precision drops as the number grows:
    "3d 4h", "4h 12m", "12m", "38s". Same format on pico-gpio-api."""
    s = int(seconds)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m"
    return f"{sec}s"

def _get_uptime() -> str:
    """How long the machine has been up, or "" if it can't be determined. Linux
    (every Pi) has /proc/uptime; the sysctl fallback keeps this working on a Mac
    dev box, where the rest of the agent already runs against mock GPIO."""
    try:
        with open("/proc/uptime", "r") as f:
            return _format_uptime(float(f.read().split()[0]))
    except Exception:
        pass
    try:
        out = subprocess.check_output(["sysctl", "-n", "kern.boottime"], text=True,
                                      timeout=2, stderr=subprocess.DEVNULL)
        m = re.search(r"sec\s*=\s*(\d+)", out)
        if m:
            return _format_uptime(time.time() - int(m.group(1)))
    except Exception:
        pass
    return ""

def _serial4() -> str:
    """Last 4 chars of the board serial (from /proc/cpuinfo), or '0000'."""
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("Serial"):
                    return line.split(":")[-1].strip()[-4:]
    except Exception:
        pass
    return "0000"

def _short_model() -> str:
    """Condense the model to a short tag without the revision, for the fallback agent
    name: 'Pi 4 B Rev 1.1' -> '4B', 'Pi Zero 2 W Rev 1.0' -> 'Zero2W'. _get_platform()
    has already dropped Raspberry/Model, so this removes the leading 'Pi', the word 'Rev'
    and the trailing revision number."""
    parts = [w for w in _get_platform().split() if w.lower() != "rev"]
    if parts and parts[0].lower() == "pi":
        parts = parts[1:]
    if parts and all(c.isdigit() or c == "." for c in parts[-1]):
        parts = parts[:-1]
    return "".join(parts) or "pi"

def _default_name() -> str:
    """Generates a default agent name based on hostname or device model/serial."""
    host = socket.gethostname()
    if host.lower().startswith("raspberry"):
        return f"pi-{_short_model()}-{_serial4()}"
    return host

# On non-Pi systems (Mac/dev), swap gpiozero for minimal mocks so the app runs without hardware.
# On a real Pi, a missing gpiozero is a hard error, not something to mock around.
if IS_PI:
    try:
        from gpiozero import DigitalInputDevice, DigitalOutputDevice
    except ModuleNotFoundError as e:
        print(f"Error: missing package '{e.name}'.")
        print("  Run: pip install -r requirements.txt")
        print("  or use: run.sh to create/enable a virtual environment (venv) and start")
        raise SystemExit(1)
else:
    class DigitalInputDevice:
        def __init__(self, pin, **kw): self.pin, self.value, self.when_activated, self.when_deactivated = pin, 0, None, None
        def close(self): pass

    class DigitalOutputDevice:
        def __init__(self, pin, initial_value=False, **kw): self.pin, self.value = pin, int(initial_value or 0)
        def on(self): self.value = 1
        def off(self): self.value = 0
        def toggle(self): self.value = 1 - self.value
        def close(self): pass


# --- Models ---

# Lowercase incoming strings so enum-like fields (type, pullup, value, ...) are case-insensitive
def _norm_str(v):
    if not isinstance(v, str): return v
    return v.lower()

# Body for POST /gpio/write/{name_or_gpio}
class WriteRequest(BaseModel):
    value: Union[int, Literal["toggle", "on", "off"]]
    duration: Optional[float] = None

    @field_validator("value", mode="before")
    @classmethod
    def _norm(cls, v): return _norm_str(v)

# Persisted per-pin settings; also the body for POST /gpio/config/{name_or_gpio}
class PinConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    name: Optional[str] = None
    type: Optional[Literal["input", "output", "vcc", "gnd", "remove"]] = None
    init: Optional[Union[int, Literal["last"]]] = None
    pullup: Optional[Literal["up", "down", "none"]] = "up"
    max: Optional[float] = Field(None, description="Max duration in seconds")
    reversed: Optional[bool] = False
    watched: Optional[bool] = None

    @field_validator("type", "pullup", mode="before")
    @classmethod
    def _norm(cls, v): return _norm_str(v)

    @field_validator("init", mode="before")
    @classmethod
    def _norm_init(cls, v):
        # Word spellings are folded to the canonical form here, before the type is
        # checked, so config.json only ever holds 0/1/"last" and /config/read only ever
        # answers those - "on"/"off"/"restore" are input conveniences, not new states.
        if isinstance(v, str):
            low = v.lower()
            if low == "none":    return None
            if low == "on":      return 1
            if low == "off":     return 0
            if low == "restore": return "last"
            return low
        return v

    @field_validator("init")
    @classmethod
    def _collapse_init(cls, v):
        # Same rule as /gpio/write: any nonzero integer counts as 1 (some systems
        # use -1 for true), 0 only when exactly 0. A fractional float (e.g. 1.5)
        # never reaches here - the Union[int, ...] type above already rejects it.
        if v is None or v == "last": return v
        return 1 if v != 0 else 0

# Response shape for pin reads (/gpio/read, /gpio/scan, webhook payloads, ...)
class PinInfo(BaseModel):
    gpio: int
    value: int
    level: Optional[int] = None
    type: Literal["input", "output", "vcc", "gnd"]
    name: Optional[str] = None
    init: Optional[Literal[0, 1, "last"]] = None
    pullup: Optional[Literal["up", "down", "none"]] = None
    max: Optional[float] = None
    reversed: Optional[bool] = None
    duration: Optional[float] = None
    watched: Optional[bool] = None

# A named sensor: runs a script from the agent's scripts/ folder and returns its
# stdout. `script` is "<script-file> [args...]" (see _validate_sensor_script).
class SensorConfig(BaseModel):
    script: Optional[str] = None

# The general-purpose webhook target, grouped under config.notifications. `webhook_key`
# is a key for the *target*, never the agent's own api_key, and is sent as the Api-Key
# header only when set (see _notify_targets).
class NotificationsConfig(BaseModel):
    webhook: Optional[str] = None
    webhook_key: Optional[str] = None

# Callback URLs owned by the smart-home bridges, grouped under config.bridges. Each
# bridge sets its own field rather than competing for the single notifications.webhook, and
# every configured target gets the same notification (see _notify_targets). Each URL has
# its own key, which the bridge generates fresh on every start and pushes here; the agent
# sends it back as the Api-Key header so the bridge can tell a real event from a forged
# one. Its own api_key is never sent to a bridge, same as notifications.webhook.
class BridgesConfig(BaseModel):
    matter: Optional[str] = None
    matter_key: Optional[str] = None
    homekit: Optional[str] = None
    homekit_key: Optional[str] = None
    # homebridge-ctrlpi has its own field rather than sharing homekit's. Both put pins in
    # the Home app, so a household may well run the standalone homekit-bridge and the
    # Homebridge plugin at once; sharing one slot meant whichever configured itself last
    # silently stopped the other receiving events.
    homebridge: Optional[str] = None
    homebridge_key: Optional[str] = None

# Operational toggles, grouped under config.settings. `docs_enabled` is Pi-only
# (it gates FastAPI's /docs); pico-gpio-api has no such field.
class SettingsConfig(BaseModel):
    docs_enabled: bool = False
    logs_enabled: bool = True
    log_days: int = 7

# The same three, all-optional, for a partial update - so posting one of them never
# resets the other two to their defaults.
class SettingsUpdate(BaseModel):
    docs_enabled: Optional[bool] = None
    logs_enabled: Optional[bool] = None
    log_days: Optional[int] = None

# Full persisted application config (config.json)
class AppConfig(BaseModel):
    api_key: str
    name: Optional[str] = None
    notifications: NotificationsConfig = NotificationsConfig()
    bridges: BridgesConfig = BridgesConfig()
    settings: SettingsConfig = SettingsConfig()
    sensors: Dict[str, SensorConfig] = {}
    gpios: Dict[str, PinConfig] = {}

# Partial-update body for POST /config/update - every field is optional, only the ones
# present get applied (see GPIOManager.set_app_config). Each group merges key by key, so
# a bridge pushing its own URL never clears the other bridge's. Every grouped field may
# also be sent at the **root** under its alias (see _ROOT_ALIASES) instead of inside its
# group; both spellings are accepted on the same endpoint.
class ConfigUpdate(BaseModel):
    name: Optional[str] = None
    api_key: Optional[str] = None
    notifications: Optional[NotificationsConfig] = None
    bridges: Optional[BridgesConfig] = None
    settings: Optional[SettingsUpdate] = None
    # Root-level aliases - the ungrouped spelling of the fields above
    webhook_url: Optional[str] = None
    webhook_key: Optional[str] = None
    docs_enabled: Optional[bool] = None
    logs_enabled: Optional[bool] = None
    log_days: Optional[int] = None
    matter: Optional[str] = None
    matter_key: Optional[str] = None
    homekit: Optional[str] = None
    homekit_key: Optional[str] = None
    homebridge: Optional[str] = None
    homebridge_key: Optional[str] = None

# Body for POST /sensor/config/{name}: a script to configure, or remove=true to
# delete the sensor.
class SensorConfigUpdate(BaseModel):
    script: Optional[str] = None
    remove: Optional[bool] = None

# Keys a client may see but never set. `agent` is computed live on every read
# (platform/os/ip/cpu_temperature/version), so it is dropped from whatever comes in
# on /config/update and /config/load rather than being written to config.json - a
# read-modify-load round trip would otherwise persist a stale snapshot of it.
_READONLY_KEYS = ("agent",)

# Root-level (ungrouped) spelling -> (group, field). A client may send either form on
# /config/update and /config/load; _normalize_config() routes the root form into its
# group so config.json is always written in the canonical grouped shape. The webhook
# pair keeps its familiar `webhook_*` names rather than a bare `url`/`key`, which would
# be ambiguous next to the agent's own api_key.
_ROOT_ALIASES = {
    "webhook_url":  ("notifications", "webhook"),
    "webhook_key":  ("notifications", "webhook_key"),
    "docs_enabled": ("settings", "docs_enabled"),
    "logs_enabled": ("settings", "logs_enabled"),
    "log_days":     ("settings", "log_days"),
    "matter":       ("bridges", "matter"),
    "matter_key":   ("bridges", "matter_key"),
    "homekit":      ("bridges", "homekit"),
    "homekit_key":  ("bridges", "homekit_key"),
    "homebridge":     ("bridges", "homebridge"),
    "homebridge_key": ("bridges", "homebridge_key"),
}

def _normalize_config(data: dict) -> dict:
    """Drops read-only keys and groups root-level aliases."""
    out = {k: v for k, v in data.items()
           if k not in _READONLY_KEYS and k not in _ROOT_ALIASES}
    for alias, (group, field) in _ROOT_ALIASES.items():
        if data.get(alias) is None:
            continue
        target = out.setdefault(group, {})
        if isinstance(target, dict):
            target.setdefault(field, data[alias])
    return out


# --- Sensor script validation ---
# A sensor may only run a script out of one hardcoded folder: `scripts/`, next to
# main.py. The folder is fixed on purpose - there is no config field or API knob
# to widen it - and the script is named by a BARE filename, so a remote API-key
# holder can't reach anything outside that folder or turn sensors into an
# arbitrary file-read or command-execution primitive. Validation runs both when a
# sensor is configured (400 with the reason) and again on every read
# ("blocked: <reason>"), because /config/load and a hand-edited config.json
# bypass /sensor/config.
# The folder isn't created here - staging scripts is the operator's job (install.sh
# seeds it), and a missing folder simply means every sensor reads "script not found".

SENSOR_SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")

# A bare filename: letters/digits/._- only and never starting with a dot, so ".",
# "..", hidden files, and anything containing a path separator are all rejected -
# the name can only ever resolve to a file directly inside SENSOR_SCRIPTS_DIR.
_SCRIPT_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")

def _validate_sensor_script(script: Optional[str]) -> Optional[str]:
    """Returns an error string if `script` isn't a valid sensor script, else None."""
    try:
        tokens = shlex.split(script or "")
    except ValueError:
        return "script is not parseable"
    if not tokens:
        return "empty script"
    if not _SCRIPT_NAME_RE.fullmatch(tokens[0]):
        return "script must be a bare filename in the scripts folder"
    if any(not re.fullmatch(r"[A-Za-z0-9]+", a) for a in tokens[1:]):
        return "script args must be letters and digits only"
    path = os.path.join(SENSOR_SCRIPTS_DIR, tokens[0])
    if os.path.exists(path) and not os.path.isfile(path):
        return "not a regular file"
    return None


# --- Logging ---

CONFIG_FILE = "config.json"
BACKUP_FILE = "config-backup.json"
STATUS_FILE = "status.json"
LOG_FILE = "agent.log"
_CONFIG_EXISTED_AT_STARTUP = os.path.exists(CONFIG_FILE)  # captured before GPIOManager() can create it

def _get_config_updated() -> str:
    """Returns the last modified time of config.json in UTC ('YYYY-MM-DD HH:MM:SS')."""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(os.path.getmtime(CONFIG_FILE)))
    except OSError:
        return ""

log_lock = threading.Lock()

def _atomic_write_json(path: str, data) -> None:
    """Atomically writes JSON data to a file."""
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, path)

def _line_date(line: str) -> datetime:
    try:
        return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return datetime.max

def _rewrite_log(cutoff: Optional[datetime]):
    """Filters log entries to keep only lines at or after `cutoff`."""
    tmp_path = LOG_FILE + ".tmp"
    with log_lock:
        try:
            with open(LOG_FILE, "r") as f:
                lines = f.readlines()
        except FileNotFoundError:
            lines = []
        kept = [] if cutoff is None else [l for l in lines if _line_date(l) >= cutoff]
        with open(tmp_path, "w") as f:
            f.writelines(kept)
        os.replace(tmp_path, LOG_FILE)

def clear_log_file():
    """Erase the log file immediately (used when log_days is set to 0)."""
    _rewrite_log(cutoff=None)

def _log_cleanup_loop():
    """Periodically trims agent.log based on configured retention."""
    while True:
        time.sleep(3600)
        try:
            days = manager.config.settings.log_days
            cutoff = datetime.now() - (timedelta(hours=1) if days == 0 else timedelta(days=days))
            _rewrite_log(cutoff)
        except Exception as e:
            log_to_file(f"[Log] Hourly cleanup failed: {e}")

def log_to_file(message: str):
    try:
        if manager and not manager.config.settings.logs_enabled:
            return
    except Exception:
        pass
    with log_lock:
        with open(LOG_FILE, "a") as f:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"{ts} {message}\n")

def _restart_process():
    """Re-exec the current process in place (same PID, same argv/venv/cwd), which
    re-reads config.json from scratch. Python 3.4+ opens sockets as
    non-inheritable (PEP 446), so uvicorn's listening socket is closed by the
    kernel during execv and the new process can rebind the port immediately -
    no 'address already in use' race, no external supervisor needed."""
    time.sleep(1.0)
    log_to_file("[CONFIG] Restarting process now")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def _reboot_device():
    """Reboots the physical device. Requires passwordless sudo for `reboot`
    (e.g. a sudoers entry: `<user> ALL=(ALL) NOPASSWD: /sbin/reboot`)."""
    time.sleep(1.0)
    log_to_file("[CONFIG] Rebooting device now")
    try:
        subprocess.run(["sudo", "reboot"], check=True)
    except Exception as e:
        log_to_file(f"[CONFIG] Reboot failed: {e}")

def _upgrade_and_restart():
    """Run install.sh --upgrade (re-downloads the release files and reinstalls
    dependencies into the venv), then re-exec the process so the freshly-downloaded
    code takes effect. Runs in a daemon thread so the /upgrade response can flush
    first. If install.sh fails, the process is left running the current code."""
    time.sleep(1.0)
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "install.sh")
    log_to_file("[CONFIG] Upgrade: running install.sh --upgrade")
    try:
        result = subprocess.run(["bash", script, "--upgrade", "-y"],
                                cwd=os.path.dirname(script),
                                capture_output=True, text=True, timeout=300)
    except Exception as e:
        log_to_file(f"[CONFIG] Upgrade aborted: install.sh error: {e}")
        return
    if result.returncode != 0:
        log_to_file(f"[CONFIG] Upgrade aborted: install.sh exited {result.returncode}; "
                    "keeping current code")
        return
    log_to_file("[CONFIG] Upgrade complete; restarting process now")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# --- GPIO Manager ---

# Serializes access to GPIOManager's shared state (devices/config/status). Route handlers
# run in FastAPI's threadpool, and gpiozero edge callbacks and duration-revert timers
# arrive on their own threads - without this, concurrent mutation of the dicts is a data
# race. RLock because locked methods call each other (e.g. write_pin → _trigger_webhook).
def _locked(fn):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper

class GPIOManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._revert_token: Dict[str, int] = {}  # gpio -> int; bumped on write/config to cancel a pending revert
        self._last_edge: Dict[str, float] = {}   # gpio -> monotonic time of last edge webhook (debounce)
        self.config: AppConfig = self._load_config()
        self.status: Dict[str, int] = self._load_status()
        self.devices: Dict[str, Any] = {}
        self.watches: Dict[str, bool] = {}
        self._initialize_pins()

    @property
    def api_key(self): return self.config.api_key

    def _build_config(self, data: dict) -> AppConfig:
        if "api_key" not in data:
            data["api_key"] = "your-secret-key"
        # Migrate legacy pin configs that stored input/output as `init` values
        # (e.g. init="input"/"clear"/"output") into the current type+init schema
        for gpio, pin_data in data.get("gpios", {}).items():
            old_init = pin_data.get("init")
            if "type" not in pin_data or pin_data["type"] is None:
                if old_init in ("input", "clear"):
                    pin_data["type"] = "input"
                    pin_data["init"] = None
                elif old_init == "output":
                    pin_data["type"] = "output"
                    pin_data["init"] = None
            # Drop what doesn't apply to the pin's type. config_pin() already does this,
            # but /config/load and a hand-edited config.json write pins verbatim - and a
            # pullup on a non-input, or watched:false, would then sit in /config/read
            # while /gpio/read (which never shows either) disagreed.
            # None, not a pop: PinConfig.pullup defaults to "up", so a removed key would
            # just come back as the default and still show up in /config/read.
            if _norm_str(pin_data.get("type")) in ("output", "vcc", "gnd"):
                pin_data["pullup"] = None
            # Same reasoning for a pull the board will not honour: _setup_input forces the
            # hardware pull-up on GPIO 2/3 either way, so leaving the stored "down"/"none"
            # in place would make /config/read and /gpio/read describe a pin that reads the
            # opposite of what they claim.
            elif str(gpio) in _PI_FIXED_PULLUP_PINS and _norm_str(pin_data.get("pullup")) not in (None, "up"):
                pin_data["pullup"] = "up"
            if pin_data.get("watched") is not True:
                pin_data.pop("watched", None)
        # Same idea for log_days: set_app_config() clamps it to 0-30 on /config/update,
        # but /config/load and a hand-edited config.json write settings verbatim. Out of
        # range it isn't just undocumented - a negative value puts the hourly cleanup's
        # cutoff in the FUTURE, so every pass erases the entire log.
        settings = data.get("settings")
        if isinstance(settings, dict):
            try:
                settings["log_days"] = max(0, min(int(settings["log_days"]), 30))
            except (KeyError, TypeError, ValueError):
                pass  # absent or not a number - the model's default/validation handles it
        return AppConfig(**data)

    def _load_config(self) -> AppConfig:
        data = {}
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r") as f: data = json.load(f)
                conf = self._build_config(data)
            except Exception as e:
                # Primary config missing/corrupt - fall back to the last known-good backup;
                # if that's missing/corrupt too, boot with defaults rather than crashing.
                log_to_file(f"[Config] {CONFIG_FILE} is invalid ({e}); loading backup {BACKUP_FILE}")
                try:
                    with open(BACKUP_FILE, "r") as f: data = json.load(f)
                    conf = self._build_config(data)
                except Exception as e2:
                    log_to_file(f"[Config] Backup unusable too ({e2}); starting with defaults")
                    conf = self._build_config({})
        else:
            conf = self._build_config(data)
        self._save_config(conf)
        return conf

    def _backup_config(self):
        if os.path.exists(CONFIG_FILE):
            shutil.copy2(CONFIG_FILE, BACKUP_FILE)

    def _save_config(self, conf: Optional[AppConfig] = None, path: str = None):
        _atomic_write_json(path or CONFIG_FILE, (conf or self.config).model_dump())

    def _load_status(self) -> dict:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r") as f: return json.load(f)
        return {}

    def _save_status(self):
        # Store the LOGICAL value (what /gpio/read reports), not the raw gpiozero one:
        # for a pulled-up input those differ, so saving the raw value meant `init: "last"`
        # compared a reference in one space against a reading in another. get_pin_info is
        # the single definition of "the value", so this can't drift away from it.
        self.status = {g: self.get_pin_info(g)["value"] for g in list(self.devices)}
        _atomic_write_json(STATUS_FILE, self.status)

    # Brings up the gpiozero device for a configured pin per its type/init/watch settings.
    # Retries because claiming a GPIO can transiently fail (e.g. right after a previous close)
    # `default_new_output_to_zero`: when type=output and no init (0/1/"last") is given, a pin
    # that's newly becoming an output (wasn't one already) starts at LOW rather than undriven -
    # set by config_pin() based on the pin's type just before this update was applied. A pin
    # that was already an output keeps the old "leave it alone" behavior (initial_value=None).
    def _init_pin(self, gpio: str, pin_conf, default_new_output_to_zero: bool = False):
        for attempt in range(5):
            try:
                pin_type = pin_conf.type
                init = pin_conf.init

                if pin_type == "vcc":
                    self._setup_output(gpio, False, True)
                elif pin_type == "gnd":
                    self._setup_output(gpio, False, False)
                elif pin_type == "output":
                    if init == 1:
                        self._setup_output(gpio, pin_conf.reversed, True)
                    elif init == 0:
                        self._setup_output(gpio, pin_conf.reversed, False)
                    elif init == "last":
                        last_val = int(self.devices[gpio].value) if gpio in self.devices else self.status.get(gpio)
                        self._setup_output(gpio, pin_conf.reversed, bool(last_val) if last_val is not None else False)
                    elif default_new_output_to_zero:
                        self._setup_output(gpio, pin_conf.reversed, False)
                    else:
                        self._setup_output(gpio, pin_conf.reversed, None)
                elif pin_type == "input" or (pin_type is None and init is not None):
                    self._setup_input(gpio, pin_conf.pullup)
                    if init is not None and pin_conf.watched:
                        # Compare in the same space the API reports: get_pin_info applies
                        # the pull-up normalisation and `reversed`, whereas the raw
                        # gpiozero value used to do neither - so a reversed or pulled-up
                        # input was tested against a number it never shows anywhere.
                        ref = init if init != "last" else self.status.get(gpio)
                        if ref is not None and self.get_pin_info(gpio)["value"] != ref:
                            self._trigger_webhook(gpio)

                if pin_conf.watched and pin_type not in ("vcc", "gnd"):
                    self.watches[gpio] = True
                    # Wire the input's edge callbacks now - _setup_input ran above, before
                    # this flag was set, so it couldn't have applied the watch itself.
                    self._apply_watch(gpio)
                elif self.watches.pop(gpio, None):
                    # Config says unwatched but a watch was live, so this call is turning it
                    # off. (Reading the live watch rather than a stored watched:false is what
                    # lets that flag never be persisted - see config_pin.) _setup_input above
                    # re-attached the edge callbacks (self.watches was still set when it
                    # ran), so detach them now or webhooks keep firing despite the unwatch.
                    dev = self.devices.get(gpio)
                    if isinstance(dev, DigitalInputDevice):
                        dev.when_activated = dev.when_deactivated = None
                break
            except Exception as e:
                if attempt == 4:
                    log_to_file(f"[GPIO] Failed to initialize GPIO {gpio} after 5 attempts: {e}")
                else:
                    time.sleep(0.5)

    def _initialize_pins(self):
        for gpio, pin_conf in self.config.gpios.items():
            self._init_pin(gpio, pin_conf)

    def _is_static(self, gpio: str) -> bool:
        conf = self.config.gpios.get(gpio)
        return conf is not None and conf.type in ("vcc", "gnd")

    # Fail fast on out-of-range pins - otherwise _init_pin burns its full 5×0.5 s retry
    # loop on a GPIO that can never be claimed, and the bogus pin still lands in config.
    @staticmethod
    def _require_valid_gpio(gpio: str):
        if gpio not in _PI_GPIO_PINS:
            raise ValueError(f"GPIO {gpio} is out of range (usable BCM range is 2-27)")

    # Like the sensor script rules, this is checked twice: here for a 400 with the reason,
    # and again in _setup_input, because /config/load and a hand-edited config.json never
    # come through /gpio/config.
    @staticmethod
    def _require_valid_pullup(gpio: str, pullup: Optional[str]):
        if gpio in _PI_FIXED_PULLUP_PINS and pullup not in (None, "up"):
            raise ValueError(f"GPIO {gpio} has a fixed hardware pull-up (I2C), so pullup must be \"up\"")

    def _setup_input(self, gpio: str, pullup: Optional[str] = None):
        gpio = str(gpio)
        if gpio in self.devices: self.devices[gpio].close()
        pull = False if pullup == "down" else (None if pullup == "none" else True)
        # The hardware wins over the stored config: a "down"/"none" that predates the
        # check in _require_valid_pullup would otherwise raise here on every read.
        if gpio in _PI_FIXED_PULLUP_PINS: pull = True
        kwargs = {"active_state": True} if pull is None else {}
        self.devices[gpio] = DigitalInputDevice(int(gpio), pull_up=pull, bounce_time=0.05, **kwargs)
        if self.watches.get(gpio): self._apply_watch(gpio)

    # gpiozero's lgpio backend keeps ONE edge registration for the whole chip, and closing
    # an input device tears it down: every other watched input then stops firing while
    # still reading correctly, so nothing looks wrong until an event never arrives.
    # Recreating a device is the only thing that restores it - reassigning when_activated
    # does not, because gpiozero only re-registers on a None -> handler transition.
    # _setup_input recreates as it goes, so only the paths that close an input WITHOUT
    # putting another in its place need this.
    def _rearm_watches(self, exclude: str):
        for gpio in list(self.watches):
            if gpio == exclude or not isinstance(self.devices.get(gpio), DigitalInputDevice):
                continue
            conf = self.config.gpios.get(gpio)
            self._setup_input(gpio, conf.pullup if conf else None)

    def _setup_output(self, gpio: str, reversed_logic: bool = False, initial_value: Optional[bool] = False):
        gpio = str(gpio)
        replaced_input = isinstance(self.devices.get(gpio), DigitalInputDevice)
        if gpio in self.devices: self.devices[gpio].close()
        self.devices[gpio] = DigitalOutputDevice(int(gpio), active_high=not reversed_logic, initial_value=initial_value)
        if replaced_input:
            self._rearm_watches(exclude=gpio)

    # Accepts either a numeric GPIO ("17") or a configured pin's friendly name ("relay")
    def resolve_gpio(self, name_or_gpio: str) -> Optional[str]:
        if name_or_gpio.isdigit(): return name_or_gpio
        return next((g for g, c in self.config.gpios.items() if c.name and c.name.lower() == name_or_gpio.lower()), None)

    # Builds the response for a pin read. `value` is the logical state (what the caller
    # configured/expects); `level` is the raw physical pin state - they differ only when
    # `reversed` (active-low) is set, and `level` is included only when it's informative
    def get_pin_info(self, gpio: str, show_level: bool = False) -> dict:
        gpio = str(gpio)
        conf = self.config.gpios.get(gpio, PinConfig())
        if gpio not in self.devices:
            if conf.type == "vcc": self._setup_output(gpio, False, True)
            elif conf.type == "gnd": self._setup_output(gpio, False, False)
            elif conf.type == "output": self._setup_output(gpio, conf.reversed or False, False)
            else: self._setup_input(gpio, conf.pullup)
        dev = self.devices[gpio]
        pin_type = conf.type if conf.type in ("vcc", "gnd") else ("output" if isinstance(dev, DigitalOutputDevice) else "input")
        reversed_flag = bool(conf.reversed) if pin_type not in ("vcc", "gnd") else False
        dev_val = int(dev.value)
        if pin_type == "output":
            # dev.value is logical (gpiozero inverts physical via active_high); physical pin is the opposite when reversed
            value = dev_val
            level = (1 - dev_val) if reversed_flag else dev_val
        elif pin_type == "input":
            # gpiozero makes pull_up=True devices active-low, so dev.value is the INVERSE of
            # the raw pin level for a pulled-up input (an idle pulled-HIGH pin reports 0).
            # Normalize back to the true physical level so e.g. pullup=up reads 1 when idle.
            pulled_up = conf.pullup not in ("down", "none")
            level = (1 - dev_val) if pulled_up else dev_val
            value = (1 - level) if reversed_flag else level
        else:  # vcc / gnd - no reversal
            value = dev_val
            level = dev_val
        info = PinInfo(
            gpio=int(gpio), value=value, type=pin_type,
            level=level if (show_level or reversed_flag) else None,
            name=conf.name, init=conf.init,
            pullup=conf.pullup if pin_type == "input" else None,
            max=conf.max, reversed=False if pin_type in ("vcc", "gnd") else conf.reversed,
            watched=True if self.watches.get(gpio) else None
        )
        return info.model_dump(exclude_none=True)

    @_locked
    def read_pin(self, name_or_gpio: str) -> Any:
        if name_or_gpio.lower() == "all": return {g: self.get_pin_info(g) for g in self.config.gpios}
        gpio = self.resolve_gpio(name_or_gpio)
        if not gpio: raise ValueError(f"Pin {name_or_gpio} not found")
        if name_or_gpio.isdigit(): self._require_valid_gpio(gpio)
        return self.get_pin_info(gpio)

    @_locked
    def write_pin(self, name_or_gpio: str, value: Any, duration: Optional[float] = None) -> dict:
        gpio = self.resolve_gpio(name_or_gpio) or (name_or_gpio if name_or_gpio.isdigit() else None)
        if not gpio: raise ValueError(f"Pin {name_or_gpio} not found")
        self._require_valid_gpio(gpio)
        if self._is_static(gpio): raise ValueError(f"Pin {name_or_gpio} is vcc/gnd and cannot be written")

        dev = self.devices.get(gpio)
        conf = self.config.gpios.get(gpio, PinConfig())
        # Writing to an UNCONFIGURED pin auto-configures it as an output. A pin already
        # configured as anything else (e.g. an input sensor) is never silently flipped -
        # that's a 400, matching the vcc/gnd guard above.
        if not isinstance(dev, DigitalOutputDevice):
            if conf.type not in (None, "output"):
                raise ValueError(f"Pin {name_or_gpio} is configured as {conf.type} and cannot be written")
            self._setup_output(gpio, conf.reversed)
            dev = self.devices[gpio]
            # pullup is input-only; clear the model's default "up" so an auto-created output
            # (or one promoted from a prior input) doesn't carry a meaningless pullup.
            if gpio not in self.config.gpios:
                self.config.gpios[gpio] = PinConfig(type="output", pullup=None)
            else:
                self.config.gpios[gpio].type = "output"
                self.config.gpios[gpio].pullup = None
            self._save_config()
            log_to_file(f"[GPIO] GPIO {gpio} configured as Output")

        # Capture the pre-write level to revert to, and bump this pin's token so any
        # pending duration-revert is cancelled - a new write supersedes it (otherwise a
        # stale revert fires later and clobbers the new value).
        prev = int(dev.value)
        token = self._revert_token.get(gpio, 0) + 1
        self._revert_token[gpio] = token

        # "toggle" flips the current state; "on"/"off" are word spellings of 1/0. Any
        # other string is rejected by WriteRequest before it ever reaches here (the old
        # "flip" spelling included - it was renamed to "toggle", with no alias kept).
        sval = str(value).lower()
        if sval == "toggle":
            dev.toggle()
            log_to_file(f"[GPIO] GPIO {gpio} toggled to {int(dev.value)}")
        else:
            if sval in ("on", "off"):
                val_int = 1 if sval == "on" else 0
            else:
                val_int = 1 if int(value) != 0 else 0  # any nonzero value counts as "1" (some systems use -1 for true)
            if val_int == 1: dev.on()
            else: dev.off()
            log_to_file(f"[GPIO] GPIO {gpio} set to {int(dev.value)}")

        self._save_status()
        if self.watches.get(gpio):
            self._trigger_webhook(gpio)

        # The pin's configured `max` caps (and, if no duration was given, supplies) the duration
        effective_duration = duration
        if conf.max:
            if effective_duration is None: effective_duration = conf.max
            elif effective_duration > conf.max: effective_duration = conf.max

        info = self.get_pin_info(gpio)
        if effective_duration:
            info["duration"] = effective_duration
            def revert(g=gpio, d=dev, dur=effective_duration, restore=prev, tok=token):
                time.sleep(dur)
                with self._lock:
                    # Skip if the device was replaced (reconfig) or this revert was
                    # superseded by a later write/config (token bump).
                    if self.devices.get(g) is not d or self._revert_token.get(g) != tok:
                        return
                    try:
                        d.value = restore   # restore the pre-write level, not a blind toggle
                        log_to_file(f"[GPIO] GPIO {g} reverted to {int(d.value)} (duration expired)")
                        self._save_status()
                        if self.watches.get(g):
                            self._trigger_webhook(g)
                    except Exception:
                        pass
            threading.Thread(target=revert, daemon=True).start()
        return info

    @_locked
    def config_pin(self, name_or_gpio: str, update: dict) -> dict:
        if not name_or_gpio.isdigit():
            raise ValueError(f"GPIO number required, got: {name_or_gpio}")
        gpio = name_or_gpio
        self._require_valid_gpio(gpio)
        # Reconfiguring a pin supersedes any pending duration-revert for it.
        self._revert_token[gpio] = self._revert_token.get(gpio, 0) + 1

        # type="remove" deletes the pin's config/device/watch entirely
        if update.get("type") == "remove":
            self.watches.pop(gpio, None)
            # No need to switch a removed output to an undriven input first: close()
            # already releases the pin, so it stops driving either way. Creating an input
            # here and closing it again is what used to tear down gpiozero's shared lgpio
            # edge registration, silently stopping every OTHER watched input from firing.
            self.config.gpios.pop(gpio, None)
            if gpio in self.devices:
                self.devices[gpio].close()
                del self.devices[gpio]
            self._save_config()
            log_to_file(f"[GPIO] GPIO {gpio} configuration removed")
            return {"status": "removed"}

        current = self.config.gpios.get(gpio, PinConfig())
        was_output_before = current.type == "output"
        original_type = current.type
        effective_type = update.get("type") or current.type
        # Only an input carries a pullup at all - every other type has it cleared below.
        if "pullup" in update and effective_type in (None, "input"):
            self._require_valid_pullup(gpio, update["pullup"])
        for k, v in update.items():
            # Ignore fields that don't apply to the (new) effective type
            if k == "max" and effective_type == "input":
                continue
            if k == "reversed" and effective_type in ("vcc", "gnd"):
                continue
            if k == "max": v = float(v) if v is not None else None
            setattr(current, k, v)
        # Clear out any leftover fields that don't make sense for the new type
        if effective_type in ("vcc", "gnd"):
            current.pullup = None
            current.reversed = False
            current.watched = None
            self.watches.pop(gpio, None)
        elif effective_type == "output":
            current.pullup = None
            if not current.max:
                current.max = None
        elif effective_type == "input":
            current.max = None
        # `watched: false` is the absence of a watch, not a setting: it is never stored,
        # so config.json and /config/read can't carry a flag /gpio/read never shows. The
        # unwatch itself still happens - _init_pin below acts on the live watch.
        if current.watched is not True:
            current.watched = None
        # A stale `init` from a previous type means something different for the new
        # type (e.g. an output's startup value misread as an input's expected sensor
        # reading) - drop it on an actual type change unless this same call supplies
        # a fresh one.
        if effective_type != original_type and "init" not in update:
            current.init = None
        self.config.gpios[gpio] = current
        self._save_config()
        self._init_pin(gpio, current, default_new_output_to_zero=(effective_type == "output" and not was_output_before))
        log_to_file(f"[GPIO] GPIO {gpio} configured: {update}")
        return self.get_pin_info(gpio)

    @_locked
    def scan(self) -> dict: return {g: self.get_pin_info(g, show_level=True) for g in self.config.gpios}

    # Full BCM 2-27 sweep for /gpio/scan - Pi-only, includes pins with no saved config
    @_locked
    def scan_all(self) -> list:
        result = []
        for gpio in _PI_GPIO_PINS:
            try:
                info = self.get_pin_info(gpio, show_level=True)
                entry = {
                    "gpio": info["gpio"],
                    "level": info["level"],
                    "value": info["value"],
                    "type": info["type"],
                }
                if info.get("name"):
                    entry["name"] = info["name"]
                if info.get("pullup"):
                    entry["pullup"] = info["pullup"]
                if info.get("reversed") is True:
                    entry["reversed"] = True
                result.append(entry)
            except Exception:
                pass
        return result

    @_locked
    def get_watched_pins(self) -> dict:
        return {
            "watched": {g: self.get_pin_info(g) for g, watched in self.watches.items() if watched},
            # URLs only - the target keys stay exclusive to /config/read
            "notifications": {"webhook": self.config.notifications.webhook or ""},
            "bridges": {
                "matter": self.config.bridges.matter or "",
                "homekit": self.config.bridges.homekit or "",
                "homebridge": self.config.bridges.homebridge or "",
            },
        }

    # Live, non-persisted facts about the machine this agent runs on. Returned under
    # `agent` by /config/read and /gpio/scan, and never stored in config.json - which
    # is why _strip_readonly() drops it from anything posted back to /config/*.
    def _agent_info(self) -> dict:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
        except Exception:
            local_ip = "127.0.0.1"
        info = {
            "platform": _get_platform(),
            "os": _get_os(),
            "host": socket.gethostname(),
            "serial": _get_serial(),
            "ip": local_ip,
            "mac": _get_mac(),
            "wifi": _get_wifi(),
            "signal": _get_signal(),
        }
        if IS_PI:
            info["cpu_temperature"] = _get_cpu_temperature()
        info["uptime"] = _get_uptime()
        info["version"] = VERSION
        info["config_updated"] = _get_config_updated()
        return info

    # NOTE: deliberately never includes api_key - this is the response for GET /config/read
    @_locked
    def get_app_config(self) -> dict:
        return {
            "name": self.config.name or _default_name(),
            "agent": self._agent_info(),
            "notifications": {
                "webhook": self.config.notifications.webhook or "",
                "webhook_key": self.config.notifications.webhook_key or "",
            },
            "bridges": {
                "matter": self.config.bridges.matter or "",
                "matter_key": self.config.bridges.matter_key or "",
                "homekit": self.config.bridges.homekit or "",
                "homekit_key": self.config.bridges.homekit_key or "",
                "homebridge": self.config.bridges.homebridge or "",
                "homebridge_key": self.config.bridges.homebridge_key or "",
            },
            "settings": {
                "docs_enabled": self.config.settings.docs_enabled,
                "logs_enabled": self.config.settings.logs_enabled,
                "log_days": self.config.settings.log_days,
            },
            "sensors": {name: conf.model_dump(exclude_none=True) for name, conf in self.config.sensors.items()},
            "gpios": {gpio: conf.model_dump(exclude_none=True) for gpio, conf in self.config.gpios.items()},
        }

    # --- Sensors ---
    # A sensor names a script in the agent's scripts/ folder to run; /sensor/read
    # turns its output into JSON. Sensors are pure config + on-demand reads - no
    # GPIO, no background threads, no persisted state beyond config.json.
    SENSOR_TIMEOUT = 15.0  # seconds a sensor script may run before it's killed

    @_locked
    def config_sensor(self, name: str, update: dict) -> dict:
        if update.get("remove"):
            existed = self.config.sensors.pop(name, None) is not None
            self._save_config()
            log_to_file(f"[Sensor] {name} removed")
            return {"status": "removed" if existed else "not found"}
        script = update.get("script")
        if not script:
            raise ValueError("Configure 'script', or 'remove': true to delete the sensor")
        err = _validate_sensor_script(script)
        if err:
            raise ValueError(err)
        self.config.sensors[name] = SensorConfig(script=script)
        self._save_config()
        log_to_file(f"[Sensor] {name} configured: {script}")
        return {"name": name, **self.config.sensors[name].model_dump(exclude_none=True)}

    # Runs one sensor's script and shapes its stdout into JSON. The result is always
    # nested under the sensor's name, so two sensors can never overwrite each other in
    # /sensor/read: a JSON object with several fields nests whole ({name: {...}}), a
    # JSON object with a single field contributes just that field's value
    # ({name: value}, the field's own key is dropped), and anything else (a bare
    # number, string, list, or non-JSON text) is wrapped the same way. Missing scripts and
    # failed/empty scripts return a clear {name: "<reason>"} message. The script
    # name always resolves inside scripts/ next to main.py. Scripts are re-validated
    # here on every read (not just at config time) because /config/load and a
    # hand-edited config.json can install sensors without passing through
    # config_sensor; one that fails validation reads as {name: "blocked: <reason>"}.
    SENSOR_OUTPUT_CAP = 2048   # max chars read from a script's stdout

    def _read_sensor_value(self, name: str, conf: SensorConfig) -> dict:
        err = _validate_sensor_script(conf.script)
        if err:
            return {name: f"blocked: {err}"}
        # '<script-file> [args...]' - always run as `sh scripts/<file> <args>`, so it
        # works without a ./ prefix, a +x bit, or the directory being on PATH, and no
        # shell ever parses the configured string. start_new_session puts the script in
        # its own process group so a timeout can kill it together with any children.
        script, *args = shlex.split(conf.script)
        path = os.path.join(SENSOR_SCRIPTS_DIR, script)
        if not os.path.isfile(path):
            return {name: "script not found"}
        proc = subprocess.Popen(["sh", path, *args], stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, start_new_session=True)
        try:
            stdout, _ = proc.communicate(timeout=self.SENSOR_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            return {name: "script timed out"}
        output = stdout[:self.SENSOR_OUTPUT_CAP].strip()
        if proc.returncode != 0 or not output:
            return {name: "script failed or did not produce output"}
        try:
            parsed = json.loads(output)
        except Exception:
            return {name: output}
        if isinstance(parsed, dict) and len(parsed) == 1:
            parsed = next(iter(parsed.values()))
        return {name: parsed}

    @_locked
    def read_sensor(self, name: str) -> dict:
        conf = self.config.sensors.get(name)
        if conf is None:
            raise ValueError(f"Sensor not found: {name}")
        return self._read_sensor_value(name, conf)

    @_locked
    def read_sensors(self) -> dict:
        result = {}
        for name, conf in self.config.sensors.items():
            try:
                result.update(self._read_sensor_value(name, conf))
            except Exception as e:
                result[name] = f"error: {e}"
        return result

    # Applies a partial config update (POST /config/update). The body may spell each
    # grouped field either inside its group or at the root (_normalize_config folds the
    # root form in). Every group merges key by key rather than replacing, so a bridge
    # pushing its own URL never clears the other bridge's, and setting log_days never
    # resets logs_enabled. api_key only changes on a real value; log_days is clamped to
    # 0-30 and 0 wipes the log immediately; an empty string clears a URL/key field.
    @_locked
    def set_app_config(self, update: dict) -> dict:
        self._backup_config()
        for k, v in _normalize_config(update).items():
            if k == "api_key":
                if v:
                    self.config.api_key = v
            elif k in ("notifications", "bridges"):
                group = getattr(self.config, k)
                for gk, gv in (v or {}).items():
                    if gv is not None:
                        setattr(group, gk, None if gv == "" else gv)
            elif k == "settings":
                for sk, sv in (v or {}).items():
                    if sv is None:
                        continue
                    if sk == "log_days":
                        days = max(0, min(int(sv), 30))
                        self.config.settings.log_days = days
                        if days == 0:
                            clear_log_file()
                    else:
                        setattr(self.config.settings, sk, sv)
            elif v is not None:
                setattr(self.config, k, None if v == "" else v)
        self._save_config()
        return self.get_app_config()

    # Replaces the whole config file (POST /config/load); an empty dict resets
    # everything to defaults while preserving the current API key
    @_locked
    def load_config(self, data: dict) -> None:
        self._backup_config()
        # Preserve the current API key when the loaded config doesn't supply one
        # (matching pico-gpio-api) - /config/read never exposes api_key, so a
        # read-modify-load round trip would otherwise silently reset the key to
        # the well-known default.
        data = _normalize_config(data)
        data.setdefault("api_key", self.config.api_key)
        _atomic_write_json(CONFIG_FILE, data)
        self._reload()

    # Tears down all GPIO devices/watches and re-initializes everything from the config on disk
    def _reload(self):
        for dev in list(self.devices.values()):
            try: dev.close()
            except: pass
        self.devices.clear()
        self.watches.clear()
        self.config = self._load_config()
        self._initialize_pins()

    def get_webhook_url(self): return self.config.notifications.webhook or ""

    @_locked
    def set_webhook_url(self, url: str):
        self.config.notifications.webhook = url
        self._save_config()
        return {"webhook_url": url or ""}

    # Every place a watched event should be delivered, as (url, headers) pairs:
    # the general-purpose webhook_url plus each smart-home bridge's own callback
    # field. A bridge sets only its own field, so no bridge can overwrite another's
    # the way sharing webhook_url forced them to.
    def _notify_targets(self) -> list:
        targets = []
        if self.config.notifications.webhook:
            # Only authenticate with the dedicated notifications.webhook_key when one is
            # configured - the agent's own api_key must never leak to the target.
            key = self.config.notifications.webhook_key
            targets.append((self.config.notifications.webhook, {"Api-Key": key} if key else {}))
        # Each bridge callback carries the key that bridge generated and pushed into
        # its own bridges.<name>_key field - never the agent's own api_key, same rule
        # as webhook_url. A bridge with no key set yet gets the POST unauthenticated
        # (and will reject it, which is the point).
        for url, key in ((self.config.bridges.matter, self.config.bridges.matter_key),
                         (self.config.bridges.homekit, self.config.bridges.homekit_key),
                         (self.config.bridges.homebridge, self.config.bridges.homebridge_key)):
            if url:
                targets.append((url, {"Api-Key": key} if key else {}))
        return targets

    # POSTs the pin's current info to every configured target on background threads
    # (fire-and-forget - failures are logged but not retried or surfaced to the caller)
    @_locked
    def _trigger_webhook(self, gpio: str):
        targets = self._notify_targets()
        if not targets:
            # Watched event with nowhere to deliver it - record it instead of sending.
            # Keep "GPIO <n>" out of the fire-log format ("[Webhook] GPIO <n> → <url>")
            # so log-based fire counts don't pick these up.
            log_to_file(f"[Webhook] Not sent (no notifications.webhook configured), GPIO: {gpio}")
            return
        payload = {"agent": self.config.name or _default_name(), **self.get_pin_info(gpio)}
        for url, headers in targets:
            log_to_file(f"[Webhook] GPIO {gpio} → {url}")
            threading.Thread(target=self._post_webhook,
                             args=(url, payload, headers), daemon=True).start()

    WEBHOOK_RETRIES = 2         # extra delivery attempts after the first
    WEBHOOK_RETRY_DELAY = 30.0  # seconds to wait before each of them

    @staticmethod
    def _post_webhook(url: str, payload: dict, headers: dict, timeout: float = 8.0):
        """One delivery attempt, then up to WEBHOOK_RETRIES more WEBHOOK_RETRY_DELAY apart.
        A non-2xx answer counts as a failure too, not just a connection error: a bridge
        that restarted answers 401 until it has re-pushed its key onto this agent, and the
        retry is what gets that event through once it has. Runs on the caller's daemon
        thread, so a failing target keeps one sleeping thread alive per event for about a
        minute - bounded by the per-pin WEBHOOK_MIN_INTERVAL debounce."""
        for attempt in range(GPIOManager.WEBHOOK_RETRIES + 1):
            if attempt:
                time.sleep(GPIOManager.WEBHOOK_RETRY_DELAY)
            try:
                status = requests.post(url, json=payload, headers=headers, timeout=timeout).status_code
                if status < 300:
                    return
                reason = f"HTTP {status}"
            except Exception as e:
                reason = str(e)
            retrying = attempt < GPIOManager.WEBHOOK_RETRIES
            log_to_file(f"[WEBHOOK] POST to {url} failed ({reason})" +
                        (f", retrying in {int(GPIOManager.WEBHOOK_RETRY_DELAY)}s" if retrying else ", giving up"))

    WEBHOOK_MIN_INTERVAL = 0.2   # per-pin edge-webhook debounce, matching the Pico ports

    # Edge-callback entry point (runs on gpiozero's callback thread). gpiozero's own
    # bounce_time (50 ms) still lets a chattering input fire ~20 webhooks/s, each
    # spawning a thread - cap it at one per WEBHOOK_MIN_INTERVAL per pin.
    @_locked
    def _on_edge(self, gpio: str):
        now = time.monotonic()
        last = self._last_edge.get(gpio)
        if last is not None and now - last < self.WEBHOOK_MIN_INTERVAL:
            return
        self._last_edge[gpio] = now
        self._trigger_webhook(gpio)

    # Wires an input pin's edge callbacks to fire the webhook on every state change
    def _apply_watch(self, gpio: str):
        dev = self.devices.get(gpio)
        if isinstance(dev, DigitalInputDevice):
            dev.when_activated = dev.when_deactivated = lambda: self._on_edge(gpio)


# Direct JSON patch to config.json - used by CLI flags (--api-key with --save)
# to persist settings before `manager` exists yet
def update_config_file(updates: dict):
    """Direct JSON patch to config.json (the CLI --save path). Accepts root aliases like
    the REST endpoints do, and merges a group one level deep so patching one field of
    `settings` doesn't drop the other two."""
    data = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f: data = json.load(f)
        except Exception:
            pass
    for k, v in _normalize_config(updates).items():
        if isinstance(v, dict) and isinstance(data.get(k), dict):
            data[k].update(v)
        else:
            data[k] = v
    _atomic_write_json(CONFIG_FILE, data)


# Runs once when config.json doesn't exist yet. GPIOManager() has already saved
# a config.json with the default API key ("your-secret-key") by the time this
# runs - this just fills in the hostname as the agent name. No prompt: main.py
# starts unattended either way. Interactive/-y setup happens in install.sh,
# before main.py is ever invoked.
def _first_time_setup():
    manager.config.name = _default_name()
    manager._save_config()
    print(f"Created {CONFIG_FILE} - API key '{manager.config.api_key}', name '{manager.config.name}'.")
    print("")


manager = GPIOManager()

threading.Thread(target=_log_cleanup_loop, daemon=True).start()


# --- FastAPI ---

# Resolves the LAN-facing private IP (via a UDP "connect" - no packets actually sent)
# and the internet-facing public IP, for the startup banner
def _get_ips():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        private = s.getsockname()[0]
        s.close()
    except Exception:
        private = "unavailable"
    try:
        public = requests.get("https://api.ipify.org", timeout=5).text.strip()
    except Exception:
        public = "unavailable"
    return private, public

@asynccontextmanager
async def lifespan(app: FastAPI):
    def _banner():
        # The public-IP lookup can take up to 5 s - don't hold up startup for a banner.
        private, public = _get_ips()
        log_to_file(f"[SERVER] Private: http://{private}:8314 | Public: http://{public}:8314")
        print(f"INFO:     Private: http://{private}:8314")
        print(f"INFO:     Public:  http://{public}:8314")
    threading.Thread(target=_banner, daemon=True).start()
    # Let every configured target (webhook + bridge callbacks) know the server just (re)started
    boot_targets = manager._notify_targets()
    if boot_targets:
        payload = {"agent": manager.config.name or _default_name(), "note": "Up and running"}
        for url, headers in boot_targets:
            log_to_file(f"[Webhook] Boot → {url}")
            threading.Thread(target=GPIOManager._post_webhook,
                             args=(url, payload, headers), daemon=True).start()
    yield

app = FastAPI(title="Raspberry Pi GPIO API", lifespan=lifespan)

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code == 404:
        return Response(status_code=404, content="")
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"error": exc.errors()})


async def _buffer_body(receive, limit: int):
    """Drain a request body that carries no Content-Length (a chunked upload) up to `limit`,
    so the header check isn't the only thing standing between us and an unbounded read.

    Returns `(messages, oversized)`: the ASGI messages consumed, to be replayed to the app,
    and whether the cap was passed. Deliberately buffer-and-replay rather than truncating
    the stream: a truncated body reaches the app as *no* body, and `/config/load` treats an
    unparseable body as `{}`, which resets the whole config. Answering 413 and never calling
    the app is the only safe way to refuse it."""
    messages = []
    total = 0
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request":
            break                       # http.disconnect: stop, let the app see it
        total += len(message.get("body", b""))
        if total > limit:
            return messages, True
        if not message.get("more_body", False):
            break
    return messages, False


def _replay_receive(messages, receive):
    """ASGI `receive` wrapper that serves buffered messages first."""
    pending = list(messages)

    async def wrapped():
        if pending:
            return pending.pop(0)
        return await receive()

    return wrapped


class CheckApiMiddleware:
    """Pure ASGI middleware for REST API gating and request logging."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = request.url.path
        query = f"?{request.url.query}" if request.url.query else ""

        if not (request.method == "GET" and path == "/hello"):
            # Redact api_key/session_id (as the uvicorn access log does) and keep the
            # file append off the event loop - log_to_file can wait on log_lock while
            # the hourly cleanup rewrites the whole file.
            await run_in_threadpool(log_to_file, f"[API] Received {request.method} {path}{_strip_qs_params(query)}")

        async def send_json_response(status_code: int, content: dict):
            body = json.dumps(content).encode()
            await send({
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})

        # Reject an oversized body here, ahead of routing and of verify_api_key, so nothing
        # is parsed or authenticated for a request that can't be legitimate. Content-Length
        # covers every real client (they all send it for a JSON body); a chunked request
        # carries none, so its body is bounded by _capped_receive instead.
        content_length = request.headers.get("content-length")
        oversized = False
        if content_length is not None:
            try:
                oversized = int(content_length) > MAX_REQUEST_BYTES
            except ValueError:
                oversized = False       # unparseable: let the server reject it as it would anyway
        elif request.method in ("POST", "PUT", "PATCH"):
            # No Content-Length on a method that carries a body means a chunked upload, whose
            # size is only knowable by reading it. Buffer up to the cap here and replay it to
            # the app, so the limit holds without the app ever seeing a partial body.
            buffered, oversized = await _buffer_body(receive, MAX_REQUEST_BYTES)
            receive = _replay_receive(buffered, receive)
        if oversized:
            await run_in_threadpool(
                log_to_file, f"[API] Rejected {request.method} {path} - body over {MAX_REQUEST_BYTES} bytes")
            await send_json_response(413, {"error": "Request body too large"})
            return

        # /hello bypasses Api-Key auth entirely (it's the unauthenticated discovery/health probe)
        if path == "/hello":
            await self.app(scope, receive, send)
            return

        if path.startswith(("/docs", "/redoc", "/openapi.json")):
            if not manager.config.settings.docs_enabled:
                await send_json_response(404, {"error": "Not Found"})
                return

        await self.app(scope, receive, send)


app.add_middleware(CheckApiMiddleware)


async def verify_api_key(request: Request):
    api_key = request.headers.get("Api-Key")
    # compare_digest on bytes: the str form raises TypeError on non-ASCII input,
    # which would turn a bad key into a 500 instead of a 403.
    if not api_key or not secrets.compare_digest(api_key.encode(), manager.api_key.encode()):
        raise HTTPException(status_code=403, detail="Invalid or missing Api-Key header")
    return api_key

# --- REST Routes ---
# Handlers are plain `def` on purpose: FastAPI runs sync handlers in its threadpool, so
# blocking work (gpiozero claims with retry sleeps, SD-card file writes) can't stall the
# event loop - /hello and other requests keep responding while one request is slow.
# GPIOManager's RLock serializes the actual state mutation across those threads.

@app.get("/gpio/read", dependencies=[Depends(verify_api_key)])
def read_gpio_all():
    return manager.read_pin("all")

@app.get("/gpio/read/{name_or_gpio}", dependencies=[Depends(verify_api_key)])
def read_gpio(name_or_gpio: str):
    try:
        return manager.read_pin(name_or_gpio)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.post("/gpio/write/{name_or_gpio}", dependencies=[Depends(verify_api_key)])
def write_gpio(name_or_gpio: str, req: WriteRequest):
    try:
        return manager.write_pin(name_or_gpio, req.value, req.duration)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/gpio/config/{name_or_gpio}", dependencies=[Depends(verify_api_key)])
def config_gpio(name_or_gpio: str, config: PinConfig):
    try:
        return manager.config_pin(name_or_gpio, config.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/gpio/watched", dependencies=[Depends(verify_api_key)])
def get_watched_gpio():
    return manager.get_watched_pins()

@app.post("/sensor/config/{name}", dependencies=[Depends(verify_api_key)])
def config_sensor(name: str, config: SensorConfigUpdate):
    try:
        return manager.config_sensor(name, config.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/sensor/read", dependencies=[Depends(verify_api_key)])
def read_sensors_all():
    return manager.read_sensors()

@app.get("/sensor/read/{name}", dependencies=[Depends(verify_api_key)])
def read_sensor(name: str):
    try:
        return manager.read_sensor(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

@app.get("/gpio/scan", dependencies=[Depends(verify_api_key)])
def scan_gpio():
    if not IS_PI:
        return JSONResponse(status_code=422, content={"agent": manager._agent_info(), "error": "Platform does not support GPIOs"})
    return {"agent": manager._agent_info(), "gpios": manager.scan_all()}


# No verify_api_key dependency - auth bypass and logging are handled directly
# in CheckApiMiddleware to keep this a lightweight, always-cheap polling/discovery target
@app.get("/hello")
async def hello():
    return {"name": manager.config.name or _default_name()}

@app.get("/logs", dependencies=[Depends(verify_api_key)])
def get_logs():
    try:
        with open(LOG_FILE, "r") as f:
            lines = deque(f, maxlen=50)   # tail without loading the whole file
        return {"logs": [l.rstrip("\n") for l in lines]}
    except FileNotFoundError:
        return {"logs": []}

@app.get("/config/read", dependencies=[Depends(verify_api_key)])
def get_app_config():
    return manager.get_app_config()

@app.post("/config/update", dependencies=[Depends(verify_api_key)])
def set_app_config(config: ConfigUpdate):
    return manager.set_app_config(config.model_dump(exclude_unset=True))

@app.post("/config/load", dependencies=[Depends(verify_api_key)])
async def config_load(request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        # Threadpool: load_config rebuilds every pin (_init_pin retry sleeps) - too
        # blocking for the event loop. Handler stays async for the tolerant body parse.
        await run_in_threadpool(manager.load_config, body)
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/restart", dependencies=[Depends(verify_api_key)])
async def restart_server(request: Request):
    """Restarts the server process. Pass {"reboot": true} to reboot the physical
    device instead - only honored on a real Raspberry Pi (IS_PI); on Mac/dev
    machines reboot is ignored and the process is restarted as usual. The actual
    action runs on a short delay in a daemon thread so this response can flush
    to the client first."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    reboot = bool(body.get("reboot", False)) if isinstance(body, dict) else False

    if reboot and IS_PI:
        log_to_file("[CONFIG] Reboot requested via /restart")
        threading.Thread(target=_reboot_device, daemon=True).start()
        return {"status": "rebooting"}
    else:
        if os.environ.get("CTRLPI_RELOAD") == "1":
            raise HTTPException(
                status_code=409,
                detail="Restart is unavailable while running in --reload (dev) mode: "
                       "the in-place re-exec collides with uvicorn's reload supervisor, "
                       "which still owns the listening socket, and wedges the server. "
                       "Restart the dev server manually instead.",
            )
        log_to_file(f"[CONFIG] Restart requested via /restart (reboot={reboot})")
        threading.Thread(target=_restart_process, daemon=True).start()
        return {"status": "restarting"}


@app.post("/upgrade", dependencies=[Depends(verify_api_key)])
async def upgrade_server():
    """Upgrade the agent in place: runs `install.sh --upgrade` (re-downloads the
    latest release files and reinstalls dependencies), then restarts the process so
    the new code takes effect. Takes no parameters. The download + restart run on a
    short delay in a daemon thread so this response can flush to the client first;
    if the install fails, the current code keeps running (see the log)."""
    if os.environ.get("CTRLPI_RELOAD") == "1":
        raise HTTPException(
            status_code=409,
            detail="Upgrade is unavailable while running in --reload (dev) mode: it ends "
                   "in an in-place re-exec that collides with uvicorn's reload supervisor "
                   "and wedges the server. Upgrade/restart the dev server manually instead.",
        )
    log_to_file("[CONFIG] Upgrade requested via /upgrade")
    threading.Thread(target=_upgrade_and_restart, daemon=True).start()
    return {"status": "upgrading"}


if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="Raspberry Pi GPIO REST API Server")
    parser.add_argument("--api-key", type=str, help="Set the Api-Key for authenticated routes (runtime only unless --save is given)")
    parser.add_argument("--save", action="store_true", help="Persist any provided --api-key to the JSON config file")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Binding host address (default: 0.0.0.0)")
    parser.add_argument("--local", action="store_true", help="Bind to localhost only (127.0.0.1); overrides --host")
    parser.add_argument("--port", type=int, default=8314, help="Binding port number (default: 8314)")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload on file changes (development mode)")
    parser.add_argument("--fastapi-docs-enabled", action="store_true", help="Enable the interactive /docs and /redoc pages (runtime only unless --save is given)")
    args = parser.parse_args()

    host = "127.0.0.1" if args.local else args.host

    if not _CONFIG_EXISTED_AT_STARTUP:
        _first_time_setup()

    if args.api_key:
        manager.config.api_key = args.api_key

    if args.fastapi_docs_enabled:
        manager.config.settings.docs_enabled = True

    if args.save:
        updates = {}
        if args.api_key:
            updates["api_key"] = args.api_key
        if args.fastapi_docs_enabled:
            updates["docs_enabled"] = True   # root alias -> settings.docs_enabled
        if updates:
            update_config_file(updates)

    if args.reload:
        os.environ["CTRLPI_RELOAD"] = "1"
        uvicorn.run("main:app", host=host, port=args.port,
                    reload=True, reload_delay=2, reload_excludes=["*.log", "config*.json", "status.json"])
    else:
        uvicorn.run(app, host=host, port=args.port)
