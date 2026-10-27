#!/usr/bin/env python3
"""
A Python server that exposes Raspberry Pi GPIO control through a REST API,
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
VERSION = "0.9.30p"  # agent software version (kept in lockstep with pico-gpio-api; the "p" marks the full version, lite.py has none); surfaced in /config/read
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
    """Short model tag for the fallback agent name: 'Pi 4 B Rev 1.1' -> '4B',
    'Pi Zero 2 W Rev 1.0' -> 'Zero2W'."""
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

# Text fields take a number as text (a key typed as 4242 is stored as "4242").
_TEXT = ConfigDict(coerce_numbers_to_str=True)

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
    model_config = ConfigDict(validate_assignment=True, coerce_numbers_to_str=True)

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
    model_config = _TEXT
    script: Optional[str] = None

# The general-purpose webhook target, grouped under config.notifications. `webhook_key`
# is a key for the *target*, never the agent's own api_key, and is sent as the Api-Key
# header only when set (see _notify_targets).
class NotificationsConfig(BaseModel):
    model_config = _TEXT
    webhook: Optional[str] = None
    webhook_key: Optional[str] = None

# Callback URLs of the smart-home bridges, one field each, so no bridge competes for
# notifications.webhook. Each has its own key, pushed by the bridge on every start and
# sent back as the Api-Key header; the agent's own api_key is never sent.
class BridgesConfig(BaseModel):
    model_config = _TEXT
    matter: Optional[str] = None
    matter_key: Optional[str] = None
    homekit: Optional[str] = None
    homekit_key: Optional[str] = None
    # homebridge-ctrlpi has its own slot, so it and homekit-bridge both receive every
    # event when a household runs both.
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
    model_config = _TEXT
    api_key: str
    name: Optional[str] = None
    notifications: NotificationsConfig = NotificationsConfig()
    bridges: BridgesConfig = BridgesConfig()
    settings: SettingsConfig = SettingsConfig()
    sensors: Dict[str, SensorConfig] = {}
    gpios: Dict[str, PinConfig] = {}

# Partial-update body for POST /config/update: only fields present are applied, groups
# merge key by key, and any grouped field may also be sent at the root (_ROOT_ALIASES).
class ConfigUpdate(BaseModel):
    model_config = _TEXT
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
    model_config = _TEXT
    script: Optional[str] = None
    remove: Optional[bool] = None

# Body for POST /script/write/{name}: the file's text, or remove=true to delete it.
class ScriptWrite(BaseModel):
    model_config = _TEXT
    content: Optional[str] = None
    remove: Optional[bool] = None

# Body for POST /script/run/{name} (optional): letters-and-digits arguments.
class ScriptRun(BaseModel):
    model_config = _TEXT
    args: Optional[list[str]] = None

# Keys a client may see but never set: `agent` is computed live, so /config/update and
# /config/load drop it instead of saving a stale snapshot.
_READONLY_KEYS = ("agent",)

# Root-level (ungrouped) spelling -> (group, field). Either form is accepted on
# /config/update and /config/load; _normalize_config() folds the root form into its
# group, so config.json is always grouped.
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
    return _text_fields(out)

# Keys, names, URLs and scripts are text: a number sent for one is stored as text
# (4242 -> "4242"), so a key typed as digits never locks the agent.
def _as_text(v):
    return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

# A usable api_key is non-empty text (a number counts, as text). Anything else, empty,
# null or another type, counts as no key: the default on load, the current key on
# /config/load, ignored on /config/update.
def _valid_key(v):
    v = _as_text(v)
    return v if isinstance(v, str) and v else None

def _text_fields(out):
    for k in ("name", "api_key"):
        if k in out:
            out[k] = _as_text(out[k])
    for group in ("notifications", "bridges"):
        if isinstance(out.get(group), dict):
            out[group] = {f: _as_text(v) for f, v in out[group].items()}
    for section, field in (("gpios", "name"), ("sensors", "script")):
        entries = out.get(section)
        for entry in (entries.values() if isinstance(entries, dict) else ()):
            if isinstance(entry, dict) and field in entry:
                entry[field] = _as_text(entry[field])
    return out


# --- Sensor script validation ---
# A bare filename from the fixed `scripts/` folder, so an API-key holder can't run or read
# anything else. Checked on configure (400) and on every read ("blocked: <reason>").

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


# --- Scripts (/script/list, /script/read, /script/write, /script/run) ---
# The files in SENSOR_SCRIPTS_DIR. A name is a bare filename (the sensor rule). Writes are
# refused (403) on the default api_key and while scripts/write.lock exists; a write may
# create the lock but not change or remove it. Runs are not locked.
SCRIPT_LOCK_FILE = "write.lock"
SCRIPT_DEFAULT_KEY = "your-secret-key"
SCRIPT_TIMEOUT = 15.0     # seconds a run may take before it's killed (504)
SCRIPT_OUTPUT_CAP = 2048  # max chars of stdout a run returns
SCRIPT_NAME_MAX = 32      # longest name accepted

class ScriptError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status

def _script_path(name: str) -> str:
    if not _SCRIPT_NAME_RE.fullmatch(name or ""):
        raise ScriptError(400, "script must be a bare filename in the scripts folder")
    if len(name) > SCRIPT_NAME_MAX:
        raise ScriptError(400, f"script name over {SCRIPT_NAME_MAX} characters")
    path = os.path.join(SENSOR_SCRIPTS_DIR, name)
    if os.path.exists(path) and not os.path.isfile(path):
        raise ScriptError(400, "not a regular file")
    return path

# write.lock, in any letter case, is never run (WRITE.LOCK is the same file on a case-insensitive disk).
def _is_lock_file(name: str) -> bool:
    return name.lower() == SCRIPT_LOCK_FILE

def _script_write_state(api_key: str) -> str:
    if os.path.exists(os.path.join(SENSOR_SCRIPTS_DIR, SCRIPT_LOCK_FILE)):
        return "locked"
    if api_key == SCRIPT_DEFAULT_KEY:
        return "default_key"
    return "enabled"

def list_scripts(api_key: str) -> dict:
    try:
        names = sorted(os.listdir(SENSOR_SCRIPTS_DIR))
    except OSError:
        names = []
    scripts = []
    for name in names:
        path = os.path.join(SENSOR_SCRIPTS_DIR, name)
        if _SCRIPT_NAME_RE.fullmatch(name) and os.path.isfile(path):
            st = os.stat(path)
            scripts.append({"name": name, "size": st.st_size,
                            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(st.st_mtime))})
    return {"scripts": scripts, "write": _script_write_state(api_key)}

def read_script(name: str) -> dict:
    path = _script_path(name)
    if not os.path.isfile(path):
        raise ScriptError(404, f"Script not found: {name}")
    # newline="": the text comes back exactly as written, \r\n included.
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return {"name": name, "content": f.read()}

def write_script(name: str, body: dict, api_key: str) -> dict:
    path = _script_path(name)
    state = _script_write_state(api_key)
    if state == "locked":
        raise ScriptError(403, "script writes are locked (write.lock in scripts/)")
    if state == "default_key":
        raise ScriptError(403, "script writes need an api_key other than the default")
    if body.get("remove"):
        existed = os.path.isfile(path)
        if existed:
            os.remove(path)
            log_to_file(f"[Script] {name} removed")
        return {"name": name, "status": "removed" if existed else "not found"}
    content = _as_text(body.get("content"))
    if content is None:
        raise ScriptError(400, "Send 'content' (text), or 'remove': true to delete the script")
    if not isinstance(content, str):
        raise ScriptError(422, "Request body failed validation")
    # Written to a hidden temp file and moved into place, so a run never sees half a script.
    os.makedirs(SENSOR_SCRIPTS_DIR, exist_ok=True)
    tmp = os.path.join(SENSOR_SCRIPTS_DIR, f".{name}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    os.replace(tmp, path)
    size = os.path.getsize(path)
    log_to_file(f"[Script] {name} written ({size} bytes)")
    return {"name": name, "size": size}

def run_script(name: str, body: dict) -> dict:
    path = _script_path(name)
    if _is_lock_file(name):
        raise ScriptError(400, "write.lock is not a script")
    if not os.path.isfile(path):
        raise ScriptError(404, f"Script not found: {name}")
    # A wrong type is 422, as pydantic answers it in main.py; a bad character is 400.
    args = body.get("args") or []
    if not isinstance(args, list):
        raise ScriptError(422, "Request body failed validation")
    args = [_as_text(a) for a in args]
    if any(not isinstance(a, str) for a in args):
        raise ScriptError(422, "Request body failed validation")
    if any(not re.fullmatch(r"[A-Za-z0-9]+", a) for a in args):
        raise ScriptError(400, "script args must be letters and digits only")
    # Run like a sensor: `sh scripts/<file> <args>`, or the agent's own Python for a `.py`
    # file, in its own process group so a timeout kills it with any children.
    runner = sys.executable if name.endswith(".py") else "sh"
    proc = subprocess.Popen([runner, path, *args], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, start_new_session=True)
    try:
        stdout, _ = proc.communicate(timeout=SCRIPT_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        log_to_file(f"[Script] {name} timed out")
        raise ScriptError(504, "script timed out")
    log_to_file(f"[Script] {name} run, exit code {proc.returncode}")
    return {"name": name, "exit_code": proc.returncode, "output": stdout[:SCRIPT_OUTPUT_CAP].rstrip()}


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
    """Re-exec the process in place (same PID, argv, venv, cwd), re-reading config.json.
    The listening socket is non-inheritable (PEP 446), so the new process can rebind
    the port at once."""
    time.sleep(1.0)
    log_to_file("[CONFIG] Restarting process now")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def _reboot_device():
    """Reboots the physical device. Requires passwordless sudo for `reboot`
    (e.g. a sudoers entry: `<user> ALL=(ALL) NOPASSWD: /sbin/reboot`).
    Falls back to a soft (in-process) restart if device reboot fails."""
    time.sleep(1.0)
    log_to_file("[CONFIG] Rebooting device now")
    try:
        subprocess.run(["sudo", "reboot"], check=True)
    except Exception as e:
        log_to_file(f"[CONFIG] Reboot failed: {e}; falling back to process restart")
        _restart_process()


def _upgrade_and_restart():
    """Run install.sh --upgrade, then re-exec so the new code takes effect. On a daemon
    thread so the response goes out first; if install.sh fails, the current code keeps running."""
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

# Serializes GPIOManager's shared state across FastAPI's threadpool, gpiozero callbacks
# and revert timers. RLock because locked methods call each other.
def _locked(fn):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper

def _get_lan_ip() -> str:
    # 1. Primary: UDP socket trick (fastest when default route exists)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass

    # 2. Backup: enumerate interfaces for offline machines / isolated LANs
    try:
        _, _, addrs = socket.gethostbyname_ex(socket.gethostname())
        for a in addrs:
            if not a.startswith("127.") and not a.startswith("169.254."):
                return a
    except Exception:
        pass

    try:
        for res in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = res[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    for cmd in [["ip", "-4", "addr"], ["ifconfig"]]:
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
            for match in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+)", out):
                ip = match.group(1)
                if not ip.startswith("127.") and not ip.startswith("169.254."):
                    return ip
        except Exception:
            pass

    return "127.0.0.1"


# Webhook delivery: one worker thread and one kept-alive requests.Session per target URL.
# A fresh HTTPS connection costs a slow board seconds (a Pi 1 A+ measured ~3 s for one and
# ~9 s each for three at once, past the ~10 s the hooks server allows for a handshake); a
# reused one costs ~0.2 s. Per target, so a dead bridge never holds up another target's
# events, and one worker per target keeps its events in order. Headers go on each request,
# never on the session, so a target's Api-Key cannot reach another target.
class _WebhookTarget:
    def __init__(self, sender, url):
        self.sender, self.url = sender, url
        self.items = []                    # [due, attempt, payload, headers], oldest first
        self.cond = threading.Condition()
        self.session = None
        self.reused = False                # the session has delivered before: its socket may be stale
        threading.Thread(target=self._run, daemon=True).start()

    def put(self, payload, headers):
        with self.cond:
            if len(self.items) >= WebhookSender.QUEUE_MAX:
                self.items.pop(0)
                log_to_file(f"[WEBHOOK] Queue for {self.url} full, dropped the oldest event")
            self.items.append([time.monotonic(), 0, payload, headers])
            self.cond.notify()

    def _post(self, payload, headers):
        if self.session is None:
            self.session, self.reused = requests.Session(), False
        status = self.session.post(self.url, json=payload, headers=headers,
                                   timeout=WebhookSender.TIMEOUT).status_code
        self.reused = True
        return status

    def _reset(self):
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
        self.session, self.reused = None, False

    # One delivery. A connection error on a session that has delivered before is usually a
    # kept-alive socket the target closed, so it is resent at once on a fresh connection;
    # any other failure is queued again WEBHOOK_RETRY_DELAY later, up to WEBHOOK_RETRIES times.
    def _deliver(self, item):
        _, attempt, payload, headers = item
        try:
            try:
                status = self._post(payload, headers)
            except requests.exceptions.ConnectionError as e:
                if not self.reused or isinstance(e, requests.exceptions.Timeout):
                    raise
                self._reset()
                status = self._post(payload, headers)
            if status < 300:
                return
            reason = f"HTTP {status}"
        except Exception as e:
            self._reset()
            reason = str(e)
        retrying = attempt < self.sender.retries
        log_to_file(f"[WEBHOOK] POST to {self.url} failed ({reason})" +
                    (f", retrying in {int(self.sender.retry_delay)}s" if retrying else ", giving up"))
        if retrying:
            with self.cond:
                self.items.append([time.monotonic() + self.sender.retry_delay, attempt + 1, payload, headers])

    def _run(self):
        idle_since = time.monotonic()
        while True:
            with self.cond:
                now = time.monotonic()
                due = next((it for it in self.items if it[0] <= now), None)
                if due is not None:
                    self.items.remove(due)
                elif self.items or now - idle_since < WebhookSender.IDLE_SECS:
                    nxt = min((it[0] for it in self.items), default=idle_since + WebhookSender.IDLE_SECS)
                    self.cond.wait(max(0.05, nxt - now))
                    continue
            if due is None:
                # Idle long enough: leave, unless an event arrived meanwhile. Same lock order
                # as WebhookSender.send (sender lock, then this target's condition).
                with self.sender.lock, self.cond:
                    if self.items:
                        continue
                    self.sender.targets.pop(self.url, None)
                self._reset()
                return
            self._deliver(due)
            idle_since = time.monotonic()


class WebhookSender:
    TIMEOUT = 8.0      # per request (connect and each read)
    QUEUE_MAX = 100    # events waiting per target; the oldest is dropped beyond this
    IDLE_SECS = 120.0  # a target idle this long closes its connection and its worker exits

    def __init__(self, retries, retry_delay):
        self.retries, self.retry_delay = retries, retry_delay
        self.lock = threading.Lock()
        self.targets = {}   # url -> _WebhookTarget

    def send(self, url, payload, headers):
        with self.lock:
            target = self.targets.get(url)
            if target is None:
                target = self.targets[url] = _WebhookTarget(self, url)
            target.put(payload, headers)


class GPIOManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._webhook_sender = WebhookSender(self.WEBHOOK_RETRIES, self.WEBHOOK_RETRY_DELAY)
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
        data["api_key"] = _valid_key(data.get("api_key")) or "your-secret-key"
        # Convert an init of "input"/"clear"/"output" into type + init.
        for gpio, pin_data in data.get("gpios", {}).items():
            old_init = pin_data.get("init")
            if "type" not in pin_data or pin_data["type"] is None:
                if old_init in ("input", "clear"):
                    pin_data["type"] = "input"
                    pin_data["init"] = None
                elif old_init == "output":
                    pin_data["type"] = "output"
                    pin_data["init"] = None
            # Drop what doesn't apply to the pin's type: /config/load and a hand-edited
            # config.json write pins verbatim. None, not a pop: PinConfig.pullup defaults
            # to "up", so a removed key would come back.
            if _norm_str(pin_data.get("type")) in ("output", "vcc", "gnd"):
                pin_data["pullup"] = None
            # GPIO 2/3 always have the hardware pull-up, so a stored "down"/"none" would
            # make /config/read and /gpio/read describe the opposite of the real reading.
            elif str(gpio) in _PI_FIXED_PULLUP_PINS and _norm_str(pin_data.get("pullup")) not in (None, "up"):
                pin_data["pullup"] = "up"
            if pin_data.get("watched") is not True:
                pin_data.pop("watched", None)
        # Clamp log_days to 0-30 here too (/config/load and hand edits skip the API check):
        # a negative value would make every hourly cleanup erase the whole log.
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
        # Store the logical value (what /gpio/read reports), not the raw gpiozero level,
        # so `init: "last"` compares like with like. get_pin_info defines "the value".
        self.status = {g: self.get_pin_info(g)["value"] for g in list(self.devices)}
        _atomic_write_json(STATUS_FILE, self.status)

    # Brings up the gpiozero device for a pin per its type/init/watch settings, retrying
    # briefly. `default_new_output_to_zero` (set by config_pin()): a new output with no
    # init starts LOW; an existing output keeps its level.
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
                        # Compare against the value the API reports (pull-up and
                        # `reversed` applied), not the raw gpiozero level.
                        ref = init if init != "last" else self.status.get(gpio)
                        if ref is not None and self.get_pin_info(gpio)["value"] != ref:
                            self._trigger_webhook(gpio)

                if pin_conf.watched and pin_type not in ("vcc", "gnd"):
                    self.watches[gpio] = True
                    # Wire the input's edge callbacks now - _setup_input ran above, before
                    # this flag was set, so it couldn't have applied the watch itself.
                    self._apply_watch(gpio)
                elif self.watches.pop(gpio, None):
                    # Unwatching: _setup_input above re-attached the edge callbacks, so
                    # detach them now or webhooks keep firing.
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
        # The hardware wins over the stored config: GPIO 2/3 have a fixed pull-up and
        # gpiozero rejects any other pull, so a stored "down"/"none" is ignored here.
        if gpio in _PI_FIXED_PULLUP_PINS: pull = True
        kwargs = {"active_state": True} if pull is None else {}
        self.devices[gpio] = DigitalInputDevice(int(gpio), pull_up=pull, bounce_time=0.05, **kwargs)
        if self.watches.get(gpio): self._apply_watch(gpio)

    # Closing an input tears down lgpio's one shared edge registration, silencing every
    # other watched input; recreating their devices restores it. Used where an input is
    # closed without a replacement (_setup_input recreates on its own).
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
        # other string is rejected by WriteRequest before it reaches here.
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
            # close() releases the pin, so it stops driving. Don't create a temporary
            # input here: closing an input breaks the shared edge registration (see
            # _rearm_watches).
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
        # An init from the previous type means something else for the new one: drop it on
        # a type change unless this call supplies a fresh one.
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
                # `value` equals `level` unless the pin is reversed, so both are sent only then.
                entry = {
                    "gpio": info["gpio"],
                    "level": info["level"],
                    "type": info["type"],
                }
                if info.get("name"):
                    entry["name"] = info["name"]
                if info.get("pullup"):
                    entry["pullup"] = info["pullup"]
                if info.get("reversed") is True:
                    entry["value"] = info["value"]
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
    # is why _normalize_config() drops it from anything posted back to /config/*.
    def _agent_info(self) -> dict:
        local_ip = _get_lan_ip()
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

    # The GET /config/read response. Never includes api_key.
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
    # A sensor runs a script from scripts/; /sensor/read returns its output as JSON.
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

    # Runs one sensor's script; output is nested under the sensor's name (shapes in the
    # README). Re-validated every read: {name: "blocked: <reason>"} or {name: "<reason>"}.
    SENSOR_OUTPUT_CAP = 2048   # max chars read from a script's stdout

    def _read_sensor_value(self, name: str, conf: SensorConfig) -> dict:
        err = _validate_sensor_script(conf.script)
        if err:
            return {name: f"blocked: {err}"}
        # Run as `sh scripts/<file> <args>`, or with the agent's own Python for a `.py` file
        # (no shell parses the string), in its own process group so a timeout kills it with
        # any children.
        script, *args = shlex.split(conf.script)
        path = os.path.join(SENSOR_SCRIPTS_DIR, script)
        if not os.path.isfile(path):
            return {name: "script not found"}
        runner = sys.executable if script.endswith(".py") else "sh"
        proc = subprocess.Popen([runner, path, *args], stdout=subprocess.PIPE,
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

    # POST /config/update: grouped fields may also be sent at the root. Groups merge key by
    # key, api_key changes only on a real value, log_days is clamped to 0-30 (0 wipes the
    # log), and an empty string clears a URL/key field.
    @_locked
    def set_app_config(self, update: dict) -> dict:
        self._backup_config()
        for k, v in _normalize_config(update).items():
            if k == "api_key":
                if _valid_key(v):
                    self.config.api_key = _valid_key(v)
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
        # Keep the current API key when the loaded config has no usable one: /config/read
        # never exposes it, so a read-modify-load round trip would reset it to the default.
        data = _normalize_config(data)
        data["api_key"] = _valid_key(data.get("api_key")) or self.config.api_key
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
    # field. A bridge sets only its own field, so bridges never overwrite each other.
    def _notify_targets(self) -> list:
        targets = []
        if self.config.notifications.webhook:
            # Only authenticate with the dedicated notifications.webhook_key when one is
            # configured - the agent's own api_key must never leak to the target.
            key = self.config.notifications.webhook_key
            targets.append((self.config.notifications.webhook, {"Api-Key": key} if key else {}))
        # Each bridge gets its own bridges.<name>_key, never the agent's api_key; with no
        # key set yet the POST goes unauthenticated and the bridge rejects it.
        for url, key in ((self.config.bridges.matter, self.config.bridges.matter_key),
                         (self.config.bridges.homekit, self.config.bridges.homekit_key),
                         (self.config.bridges.homebridge, self.config.bridges.homebridge_key)):
            if url:
                targets.append((url, {"Api-Key": key} if key else {}))
        return targets

    # Queues the pin's current info for every configured target; WebhookSender delivers it
    # (in order per target, over a kept-alive connection, retried on failure)
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
            self._webhook_sender.send(url, payload, headers)

    WEBHOOK_RETRIES = 2         # extra delivery attempts after the first
    WEBHOOK_RETRY_DELAY = 30.0  # seconds to wait before each of them

    WEBHOOK_MIN_INTERVAL = 0.2   # per-pin edge-webhook debounce, matching the Pico ports

    # Edge-callback entry point (runs on gpiozero's callback thread). gpiozero's own
    # bounce_time (50 ms) still lets a chattering input fire ~20 webhooks/s, each
    # queued for delivery - cap it at one per WEBHOOK_MIN_INTERVAL per pin.
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


# Runs once when config.json doesn't exist yet: names the agent after the hostname.
# No prompt; interactive setup happens in install.sh.
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
    private = _get_lan_ip()
    if private == "127.0.0.1":
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
            manager._webhook_sender.send(url, payload, headers)
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
    """Buffer a chunked request body (no Content-Length) up to `limit`. Returns
    `(messages, oversized)`. An oversized body is answered 413 and never reaches the app,
    since a cut-off body would parse as `{}` and reset the config via /config/load."""
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
# Handlers are plain `def`, so FastAPI runs them in its threadpool and slow GPIO or file
# work never stalls the event loop; GPIOManager's RLock serializes the state changes.

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

@app.get("/script/list", dependencies=[Depends(verify_api_key)])
def script_list():
    return list_scripts(manager.api_key)

@app.get("/script/read/{name}", dependencies=[Depends(verify_api_key)])
def script_read(name: str):
    try:
        return read_script(name)
    except ScriptError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

@app.post("/script/write/{name}", dependencies=[Depends(verify_api_key)])
def script_write(name: str, body: ScriptWrite):
    try:
        return write_script(name, body.model_dump(exclude_unset=True), manager.api_key)
    except ScriptError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

@app.post("/script/run/{name}", dependencies=[Depends(verify_api_key)])
def script_run(name: str, body: Optional[ScriptRun] = None):
    try:
        return run_script(name, body.model_dump(exclude_unset=True) if body else {})
    except ScriptError as e:
        raise HTTPException(status_code=e.status, detail=str(e))

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
    """Restarts the server process. {"reboot": true} reboots the device instead (real Pi
    only; elsewhere it restarts the process), falling back to a restart if the reboot fails.
    Runs after a short delay so this response is sent first."""
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
    """Upgrades in place: runs `install.sh --upgrade`, then restarts so the new code takes
    effect. Runs after a short delay so this response is sent first; if the install fails,
    the current code keeps running (see the log)."""
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
