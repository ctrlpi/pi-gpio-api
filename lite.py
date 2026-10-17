#!/usr/bin/env python3
"""
A lightweight single-file Python server that exposes Raspberry Pi GPIO control through a
REST API, without dependencies on FastAPI or Pydantic.
"""

import os, sys, json, time, threading, socket, logging, re, secrets, shutil, subprocess, shlex, signal
import platform as _platform
from collections import deque
from datetime import datetime, timedelta
from typing import Optional, Union, Dict, Any

# Redact api_key/session_id values from URLs before they reach the access log
def _strip_qs_params(s):
    s = re.sub(r'\?(api_key|session_id)=[^&\s"]*&', '?', s, flags=re.IGNORECASE)
    s = re.sub(r'\?(api_key|session_id)=[^&\s"]*', '', s, flags=re.IGNORECASE)
    s = re.sub(r'&(api_key|session_id)=[^&\s"]*', '', s, flags=re.IGNORECASE)
    return s

try:
    import requests
except ModuleNotFoundError as e:
    print(f"Error: missing package '{e.name}'.")
    print("  Run: pip install -r requirements.txt")
    print("  or use: run.sh to create/enable a virtual environment (venv) and start")
    raise SystemExit(1)

VERSION = "0.9.26"  # agent software version (kept in lockstep with pico-gpio-api); surfaced in /config/read
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


# Lowercase incoming strings so enum-like fields (type, pullup, value, ...) are case-insensitive
def _norm_str(v):
    if not isinstance(v, str): return v
    return v.lower()

INIT_VALUES = (0, 1, "last")

def _norm_init(v):
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
    if v is True:  return 1
    if v is False: return 0
    if isinstance(v, float):
        if v != int(v):
            return v
        v = int(v)
    if isinstance(v, int):
        return 1 if v != 0 else 0
    return v

def _check_init(v):
    if v is not None and v not in INIT_VALUES:
        raise ValueError('init must be 0, 1 or "last" (or "on"/"off"/"restore"/"none"), got ' + repr(v))
    return v

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
    return _text_fields(out)

# Keys, names, URLs and scripts are text: a number sent for one is stored as text
# (4242 -> "4242"), so a key typed as digits never locks the agent.
def _as_text(v):
    return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v

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
_CONFIG_EXISTED_AT_STARTUP = os.path.exists(CONFIG_FILE)

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
            days = manager.config.get("settings", {}).get("log_days", 7)
            cutoff = datetime.now() - (timedelta(hours=1) if days == 0 else timedelta(days=days))
            _rewrite_log(cutoff)
        except Exception as e:
            log_to_file(f"[Log] Hourly cleanup failed: {e}")

def log_to_file(message: str):
    try:
        if manager and not manager.config.get("settings", {}).get("logs_enabled", True):
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
    non-inheritable (PEP 446), so the listening socket is closed by the
    kernel during execv and the new process can rebind the port immediately -
    no 'address already in use' race, no external supervisor needed."""
    time.sleep(0.5)
    log_to_file("[CONFIG] Restarting process now")
    os.execv(sys.executable, [sys.executable] + sys.argv)

def _reboot_device():
    """Reboots the physical device. Requires passwordless sudo for `reboot`
    (e.g. a sudoers entry: `<user> ALL=(ALL) NOPASSWD: /sbin/reboot`).
    Falls back to a soft (in-process) restart if device reboot fails."""
    time.sleep(0.5)
    log_to_file("[CONFIG] Rebooting device now")
    try:
        subprocess.run(["sudo", "reboot"], check=True)
    except Exception as e:
        log_to_file(f"[CONFIG] Reboot failed: {e}; falling back to process restart")
        _restart_process()

def _upgrade_and_restart():
    """Run install.sh --upgrade (re-downloads the release files and reinstalls
    dependencies into the venv), then re-exec the process so the freshly-downloaded
    code takes effect. Runs in a daemon thread so the /upgrade response can flush
    first. If install.sh fails, the process is left running the current code."""
    time.sleep(0.5)
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
def _locked(fn):
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)
    return wrapper

def _get_lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
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


class GPIOManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._revert_token: Dict[str, int] = {}
        self._last_edge: Dict[str, float] = {}
        self.config: dict = self._load_config()
        self.status: Dict[str, int] = self._load_status()
        self.devices: Dict[str, Any] = {}
        self.watches: Dict[str, bool] = {}
        self._initialize_pins()

    @property
    def api_key(self): return self.config.get("api_key", "your-secret-key")

    def _build_config(self, data: dict) -> dict:
        if not isinstance(data, dict):
            data = {}
        data.setdefault("api_key", "your-secret-key")
        data.setdefault("name", None)
        data.setdefault("notifications", {})
        data.setdefault("bridges", {})
        data.setdefault("settings", {})
        data.setdefault("sensors", {})
        data.setdefault("gpios", {})

        data["notifications"].setdefault("webhook", None)
        data["notifications"].setdefault("webhook_key", None)
        data["bridges"].setdefault("matter", None)
        data["bridges"].setdefault("matter_key", None)
        data["bridges"].setdefault("homekit", None)
        data["bridges"].setdefault("homekit_key", None)
        data["bridges"].setdefault("homebridge", None)
        data["bridges"].setdefault("homebridge_key", None)
        data["settings"].setdefault("docs_enabled", False)
        data["settings"].setdefault("logs_enabled", True)
        data["settings"].setdefault("log_days", 7)

        for gpio, pin_data in list(data["gpios"].items()):
            if not isinstance(pin_data, dict):
                pin_data = {}
                data["gpios"][gpio] = pin_data
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
            if "init" in pin_data and pin_data["init"] is not None:
                pin_data["init"] = _norm_init(pin_data["init"])

        settings = data["settings"]
        try:
            settings["log_days"] = max(0, min(int(settings["log_days"]), 30))
        except (KeyError, TypeError, ValueError):
            settings["log_days"] = 7
        return data

    def _load_config(self) -> dict:
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

    def _save_config(self, conf: Optional[dict] = None, path: str = None):
        _atomic_write_json(path or CONFIG_FILE, conf or self.config)

    def _load_status(self) -> dict:
        if os.path.exists(STATUS_FILE):
            with open(STATUS_FILE, "r") as f:
                try: return json.load(f)
                except Exception: return {}
        return {}

    def _save_status(self):
        # Store the logical value (what /gpio/read reports), not the raw gpiozero level,
        # so `init: "last"` compares like with like. get_pin_info defines "the value".
        self.status = {g: self.get_pin_info(g)["value"] for g in list(self.devices)}
        _atomic_write_json(STATUS_FILE, self.status)

    # Brings up the gpiozero device for a configured pin per its type/init/watch settings.
    # Retries because claiming a GPIO can fail briefly, e.g. right after a close.
    # `default_new_output_to_zero` (set by config_pin()): a pin that becomes an output with
    # no init starts LOW. A pin that is already an output keeps its current level.
    def _init_pin(self, gpio: str, pin_conf: dict, default_new_output_to_zero: bool = False):
        for attempt in range(5):
            try:
                pin_type = pin_conf.get("type")
                init = pin_conf.get("init")
                reversed_flag = pin_conf.get("reversed", False)

                if pin_type == "vcc":
                    self._setup_output(gpio, False, True)
                elif pin_type == "gnd":
                    self._setup_output(gpio, False, False)
                elif pin_type == "output":
                    if init == 1:
                        self._setup_output(gpio, reversed_flag, True)
                    elif init == 0:
                        self._setup_output(gpio, reversed_flag, False)
                    elif init == "last":
                        last_val = int(self.devices[gpio].value) if gpio in self.devices else self.status.get(gpio)
                        self._setup_output(gpio, reversed_flag, bool(last_val) if last_val is not None else False)
                    elif default_new_output_to_zero:
                        self._setup_output(gpio, reversed_flag, False)
                    else:
                        self._setup_output(gpio, reversed_flag, None)
                elif pin_type == "input" or (pin_type is None and init is not None):
                    self._setup_input(gpio, pin_conf.get("pullup"))
                    if init is not None and pin_conf.get("watched"):
                        # Compare against the value the API reports (pull-up and
                        # `reversed` applied), not the raw gpiozero level.
                        ref = init if init != "last" else self.status.get(gpio)
                        if ref is not None and self.get_pin_info(gpio)["value"] != ref:
                            self._trigger_webhook(gpio)

                if pin_conf.get("watched") and pin_type not in ("vcc", "gnd"):
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
        for gpio, pin_conf in list(self.config.get("gpios", {}).items()):
            self._init_pin(gpio, pin_conf)

    def _is_static(self, gpio: str) -> bool:
        conf = self.config.get("gpios", {}).get(gpio)
        return conf is not None and conf.get("type") in ("vcc", "gnd")

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

    # gpiozero's lgpio backend keeps one edge registration for the whole chip, and closing
    # an input tears it down, so every other watched input silently stops firing. Only
    # recreating their devices restores it. Called by the paths that close an input
    # without creating another (_setup_input already recreates).
    def _rearm_watches(self, exclude: str):
        for gpio in list(self.watches):
            if gpio == exclude or not isinstance(self.devices.get(gpio), DigitalInputDevice):
                continue
            conf = self.config.get("gpios", {}).get(gpio)
            self._setup_input(gpio, conf.get("pullup") if conf else None)

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
        return next((g for g, c in self.config.get("gpios", {}).items()
                     if c.get("name") and c["name"].lower() == name_or_gpio.lower()), None)

    # Builds the response for a pin read. `value` is the logical state (what the caller
    # configured/expects); `level` is the raw physical pin state - they differ only when
    # `reversed` (active-low) is set, and `level` is included only when it's informative
    def get_pin_info(self, gpio: str, show_level: bool = False) -> dict:
        gpio = str(gpio)
        conf = self.config.get("gpios", {}).get(gpio, {})
        pin_conf_type = conf.get("type")
        if gpio not in self.devices:
            if pin_conf_type == "vcc": self._setup_output(gpio, False, True)
            elif pin_conf_type == "gnd": self._setup_output(gpio, False, False)
            elif pin_conf_type == "output": self._setup_output(gpio, conf.get("reversed", False), False)
            else: self._setup_input(gpio, conf.get("pullup"))
        dev = self.devices[gpio]
        pin_type = pin_conf_type if pin_conf_type in ("vcc", "gnd") else ("output" if isinstance(dev, DigitalOutputDevice) else "input")
        reversed_flag = bool(conf.get("reversed")) if pin_type not in ("vcc", "gnd") else False
        dev_val = int(dev.value)
        if pin_type == "output":
            value = dev_val
            level = (1 - dev_val) if reversed_flag else dev_val
        elif pin_type == "input":
            pulled_up = conf.get("pullup") not in ("down", "none")
            level = (1 - dev_val) if pulled_up else dev_val
            value = (1 - level) if reversed_flag else level
        else:  # vcc / gnd - no reversal
            value = dev_val
            level = dev_val
        info = {
            "gpio": int(gpio),
            "value": value,
            "type": pin_type,
        }
        if show_level or reversed_flag:
            info["level"] = level
        if conf.get("name"):
            info["name"] = conf["name"]
        if conf.get("init") is not None:
            info["init"] = conf["init"]
        if pin_type == "input" and conf.get("pullup") is not None:
            info["pullup"] = conf["pullup"]
        if conf.get("max") is not None:
            info["max"] = conf["max"]
        if pin_type not in ("vcc", "gnd"):
            info["reversed"] = bool(conf.get("reversed", False))
        if self.watches.get(gpio):
            info["watched"] = True
        return info

    @_locked
    def read_pin(self, name_or_gpio: str) -> Any:
        if name_or_gpio.lower() == "all": return {g: self.get_pin_info(g) for g in self.config.get("gpios", {})}
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
        conf = self.config.setdefault("gpios", {}).setdefault(gpio, {})
        # Writing to an UNCONFIGURED pin auto-configures it as an output. A pin already
        # configured as anything else (e.g. an input sensor) is never silently flipped -
        # that's a 400, matching the vcc/gnd guard above.
        if not isinstance(dev, DigitalOutputDevice):
            if conf.get("type") not in (None, "output"):
                raise ValueError(f"Pin {name_or_gpio} is configured as {conf.get('type')} and cannot be written")
            self._setup_output(gpio, conf.get("reversed", False))
            dev = self.devices[gpio]
            conf["type"] = "output"
            conf.pop("pullup", None)
            self._save_config()
            log_to_file(f"[GPIO] GPIO {gpio} configured as Output")

        # Capture the pre-write level to revert to, and bump this pin's token so any
        # pending duration-revert is cancelled - a new write supersedes it (otherwise a
        # stale revert fires later and clobbers the new value).
        prev = int(dev.value)
        token = self._revert_token.get(gpio, 0) + 1
        self._revert_token[gpio] = token

        # "toggle" flips the current state; "on"/"off" are word spellings of 1/0. Any
        # other string is rejected below.
        sval = str(value).lower()
        if sval == "toggle":
            dev.toggle()
            log_to_file(f"[GPIO] GPIO {gpio} toggled to {int(dev.value)}")
        elif sval in ("on", "off"):
            val_int = 1 if sval == "on" else 0
            if val_int == 1: dev.on()
            else: dev.off()
            log_to_file(f"[GPIO] GPIO {gpio} set to {int(dev.value)}")
        else:
            try:
                val_int = 1 if int(value) != 0 else 0  # any nonzero value counts as "1" (some systems use -1 for true)
            except (ValueError, TypeError):
                raise ValueError("value must be 0, 1, 'on', 'off' or 'toggle'")
            if val_int == 1: dev.on()
            else: dev.off()
            log_to_file(f"[GPIO] GPIO {gpio} set to {int(dev.value)}")

        self._save_status()
        if self.watches.get(gpio):
            self._trigger_webhook(gpio)

        # The pin's configured `max` caps (and, if no duration was given, supplies) the duration
        effective_duration = duration
        if conf.get("max"):
            if effective_duration is None: effective_duration = conf["max"]
            elif effective_duration > conf["max"]: effective_duration = conf["max"]

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
                        d.value = restore  # restore the pre-write level, not a blind toggle
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
        if "name" in update:
            update["name"] = _as_text(update["name"])
        if not name_or_gpio.isdigit():
            raise ValueError(f"GPIO number required, got: {name_or_gpio}")
        gpio = name_or_gpio
        self._require_valid_gpio(gpio)
        # Reconfiguring a pin supersedes any pending duration-revert for it.
        self._revert_token[gpio] = self._revert_token.get(gpio, 0) + 1

        # type="remove" deletes the pin's config/device/watch entirely
        if update.get("type") == "remove":
            self.watches.pop(gpio, None)
            self.config.get("gpios", {}).pop(gpio, None)
            if gpio in self.devices:
                self.devices[gpio].close()
                del self.devices[gpio]
            self._save_config()
            log_to_file(f"[GPIO] GPIO {gpio} configuration removed")
            return {"status": "removed"}

        current = dict(self.config.setdefault("gpios", {}).get(gpio, {}))
        was_output_before = current.get("type") == "output"
        original_type = current.get("type")
        effective_type = update.get("type") or current.get("type")

        if "type" in update and update["type"] not in ("input", "output", "vcc", "gnd", "remove"):
            raise ValueError(f"Invalid type '{update['type']}'; must be input, output, vcc, gnd, or remove")

        # Only an input carries a pullup at all - every other type has it cleared below.
        if "pullup" in update and effective_type in (None, "input"):
            pullup_val = _norm_str(update["pullup"])
            if pullup_val not in ("up", "down", "none"):
                raise ValueError("pullup must be 'up', 'down', or 'none'")
            self._require_valid_pullup(gpio, pullup_val)

        for k, v in update.items():
            # Ignore fields that don't apply to the (new) effective type
            if k == "max" and effective_type == "input":
                continue
            if k == "reversed" and effective_type in ("vcc", "gnd"):
                continue
            if k == "type":
                current["type"] = _norm_str(v)
            elif k == "pullup":
                current["pullup"] = _norm_str(v)
            elif k == "init":
                current["init"] = _check_init(_norm_init(v))
            elif k == "max":
                current["max"] = float(v) if v is not None else None
            elif k == "reversed":
                current["reversed"] = bool(v) if v is not None else False
            elif k == "name":
                current["name"] = str(v) if v is not None else None
            elif k == "watched":
                current["watched"] = bool(v) if v is not None else None

        # Clear out any leftover fields that don't make sense for the new type
        if effective_type in ("vcc", "gnd"):
            current.pop("pullup", None)
            current["reversed"] = False
            current.pop("watched", None)
            self.watches.pop(gpio, None)
        elif effective_type == "output":
            current.pop("pullup", None)
            if not current.get("max"):
                current.pop("max", None)
        elif effective_type == "input":
            current.pop("max", None)

        if current.get("watched") is not True:
            current.pop("watched", None)

        # A stale `init` from a previous type means something different for the new
        # type (e.g. an output's startup value misread as an input's expected sensor
        # reading) - drop it on an actual type change unless this same call supplies
        # a fresh one.
        if effective_type != original_type and "init" not in update:
            current.pop("init", None)

        self.config.setdefault("gpios", {})[gpio] = current
        self._save_config()
        self._init_pin(gpio, current, default_new_output_to_zero=(effective_type == "output" and not was_output_before))
        log_to_file(f"[GPIO] GPIO {gpio} configured: {update}")
        return self.get_pin_info(gpio)

    @_locked
    def scan(self) -> dict: return {g: self.get_pin_info(g, show_level=True) for g in self.config.get("gpios", {})}

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
        notif = self.config.get("notifications", {})
        bridges = self.config.get("bridges", {})
        return {
            "watched": {g: self.get_pin_info(g) for g, watched in self.watches.items() if watched},
            "notifications": {"webhook": notif.get("webhook") or ""},
            "bridges": {
                "matter": bridges.get("matter") or "",
                "homekit": bridges.get("homekit") or "",
                "homebridge": bridges.get("homebridge") or "",
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
        notif = self.config.get("notifications", {})
        bridges = self.config.get("bridges", {})
        settings = self.config.get("settings", {})
        return {
            "name": self.config.get("name") or _default_name(),
            "agent": self._agent_info(),
            "notifications": {
                "webhook": notif.get("webhook") or "",
                "webhook_key": notif.get("webhook_key") or "",
            },
            "bridges": {
                "matter": bridges.get("matter") or "",
                "matter_key": bridges.get("matter_key") or "",
                "homekit": bridges.get("homekit") or "",
                "homekit_key": bridges.get("homekit_key") or "",
                "homebridge": bridges.get("homebridge") or "",
                "homebridge_key": bridges.get("homebridge_key") or "",
            },
            "settings": {
                "docs_enabled": bool(settings.get("docs_enabled", False)),
                "logs_enabled": bool(settings.get("logs_enabled", True)),
                "log_days": int(settings.get("log_days", 7)),
            },
            "sensors": {name: {k: v for k, v in conf.items() if v is not None}
                        for name, conf in self.config.get("sensors", {}).items()},
            "gpios": {gpio: {k: v for k, v in conf.items() if v is not None}
                      for gpio, conf in self.config.get("gpios", {}).items()},
        }

    # --- Sensors ---
    SENSOR_TIMEOUT = 15.0
    # Runs one sensor's script and returns its output nested under the sensor's name
    # (the JSON shapes are in the README). Re-validates the script on every read: one
    # that fails reads as {name: "blocked: <reason>"}, a missing or silent one as
    # {name: "<reason>"}.
    SENSOR_OUTPUT_CAP = 2048   # max chars read from a script's stdout

    @_locked
    def config_sensor(self, name: str, update: dict) -> dict:
        if update.get("remove"):
            existed = self.config.setdefault("sensors", {}).pop(name, None) is not None
            self._save_config()
            log_to_file(f"[Sensor] {name} removed")
            return {"status": "removed" if existed else "not found"}
        script = _as_text(update.get("script"))
        if not script:
            raise ValueError("Configure 'script', or 'remove': true to delete the sensor")
        err = _validate_sensor_script(script)
        if err:
            raise ValueError(err)
        self.config.setdefault("sensors", {})[name] = {"script": script}
        self._save_config()
        log_to_file(f"[Sensor] {name} configured: {script}")
        return {"name": name, "script": script}

    def _read_sensor_value(self, name: str, conf: dict) -> dict:
        script_val = conf.get("script") if isinstance(conf, dict) else None
        err = _validate_sensor_script(script_val)
        if err:
            return {name: f"blocked: {err}"}
        script, *args = shlex.split(script_val)
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
        conf = self.config.get("sensors", {}).get(name)
        if conf is None:
            raise ValueError(f"Sensor not found: {name}")
        return self._read_sensor_value(name, conf)

    @_locked
    def read_sensors(self) -> dict:
        result = {}
        for name, conf in self.config.get("sensors", {}).items():
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
                    self.config["api_key"] = v
            elif k in ("notifications", "bridges"):
                group = self.config.setdefault(k, {})
                for gk, gv in (v or {}).items():
                    if gv is not None:
                        group[gk] = None if gv == "" else gv
            elif k == "settings":
                settings = self.config.setdefault("settings", {})
                for sk, sv in (v or {}).items():
                    if sv is None:
                        continue
                    if sk == "log_days":
                        days = max(0, min(int(sv), 30))
                        settings["log_days"] = days
                        if days == 0:
                            clear_log_file()
                    else:
                        settings[sk] = sv
            elif v is not None:
                self.config[k] = None if v == "" else v
        self._save_config()
        return self.get_app_config()

    # Replaces the whole config file (POST /config/load); an empty dict resets
    # everything to defaults while preserving the current API key
    @_locked
    def load_config(self, data: dict) -> None:
        self._backup_config()
        data = _normalize_config(data) if isinstance(data, dict) else {}
        data.setdefault("api_key", self.api_key)
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

    def get_webhook_url(self): return self.config.get("notifications", {}).get("webhook") or ""

    @_locked
    def set_webhook_url(self, url: str):
        self.config.setdefault("notifications", {})["webhook"] = url
        self._save_config()
        return {"webhook_url": url or ""}

    # Every place a watched event should be delivered, as (url, headers) pairs:
    # the general-purpose webhook_url plus each smart-home bridge's own callback
    # field. A bridge sets only its own field, so bridges never overwrite each other.
    def _notify_targets(self) -> list:
        targets = []
        notif = self.config.get("notifications", {})
        if notif.get("webhook"):
            key = notif.get("webhook_key")
            targets.append((notif["webhook"], {"Api-Key": key} if key else {}))
        bridges = self.config.get("bridges", {})
        for field in ("matter", "homekit", "homebridge"):
            url = bridges.get(field)
            key = bridges.get(field + "_key")
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
        payload = {"agent": self.config.get("name") or _default_name(), **self.get_pin_info(gpio)}
        for url, headers in targets:
            log_to_file(f"[Webhook] GPIO {gpio} → {url}")
            threading.Thread(target=self._post_webhook,
                             args=(url, payload, headers), daemon=True).start()

    WEBHOOK_RETRIES = 2
    WEBHOOK_RETRY_DELAY = 30.0

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

    WEBHOOK_MIN_INTERVAL = 0.2

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
# runs - this just fills in the hostname as the agent name. No prompt: the agent
# starts unattended either way. Interactive/-y setup happens in install.sh.
def _first_time_setup():
    manager.config["name"] = _default_name()
    update_config_file({"name": manager.config["name"]})
    print(f"Created {CONFIG_FILE} - API key '{manager.api_key}', name '{manager.config['name']}'.")


manager = GPIOManager()
threading.Thread(target=_log_cleanup_loop, daemon=True).start()


# --- HTTP Server Engine (socket-based, standard library only) ---

_STATUS_TEXT = {
    200: "OK", 400: "Bad Request", 403: "Forbidden",
    404: "Not Found", 413: "Payload Too Large", 422: "Unprocessable Entity",
    500: "Internal Server Error",
}

# Sends a status line with no body: the empty 404 every unknown route or pin gets.
def _send_empty(conn, status=404):
    header = (
        f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Error')}\r\n"
        f"Content-Length: 0\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        conn.sendall(header)
    except Exception:
        pass

# Sends a JSON response. Every response closes the connection (no keep-alive).
def _send_json(conn, data, status=200):
    body = json.dumps(data).encode("utf-8")
    header = (
        f"HTTP/1.1 {status} {_STATUS_TEXT.get(status, 'Error')}\r\n"
        f"Content-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    try:
        conn.sendall(header + body)
    except Exception:
        pass

# Reads one request: the headers, then the body up to its Content-Length (a chunked
# body goes to _recv_chunked). Returns "OVERSIZED" past MAX_REQUEST_BYTES, and whatever
# arrived when the client stalls for 5 s or hangs up.
def _recv_request(conn):
    data = b""
    conn.settimeout(5)
    while True:
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, OSError):
            break
        if not chunk:
            break
        data += chunk
        sep = data.find(b"\r\n\r\n")
        if sep != -1:
            cl = 0
            chunked = False
            for line in data[:sep].decode("utf-8", "replace").split("\r\n")[1:]:
                low = line.lower()
                if low.startswith("content-length:"):
                    try:
                        cl = int(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
                elif low.startswith("transfer-encoding:") and "chunked" in low:
                    chunked = True
            if chunked:
                return _recv_chunked(conn, data, sep)
            if cl > MAX_REQUEST_BYTES or len(data) > MAX_REQUEST_BYTES + 4096:
                return "OVERSIZED"
            if len(data) - sep - 4 >= cl:
                return data
    return data

def _recv_chunked(conn, data, sep):
    """Decode a chunked body (no Content-Length) up to MAX_REQUEST_BYTES. Returns the
    headers plus the decoded body, or "OVERSIZED". Never hands back a partial body:
    /config/load treats an empty body as {}, which resets the whole config. A body that
    can't be fully read comes back as an unparseable one, so it is refused, never applied."""
    head, buf, body = data[:sep + 4], data[sep + 4:], b""
    bad = head + b"\x00"    # fails JSON parsing: answered 422, never treated as {}
    while True:
        line_end = buf.find(b"\r\n")
        if line_end != -1:
            try:
                size = int(buf[:line_end].split(b";")[0].strip(), 16)
            except ValueError:
                return bad
            if size == 0:
                return head + body
            if len(body) + size > MAX_REQUEST_BYTES:
                return "OVERSIZED"
            if len(buf) >= line_end + 2 + size + 2:
                body += buf[line_end + 2:line_end + 2 + size]
                buf = buf[line_end + 2 + size + 2:]
                continue
        elif len(buf) > 64:
            return bad      # no chunk-size line in sight: malformed framing
        try:
            chunk = conn.recv(4096)
        except (socket.timeout, OSError):
            return bad
        if not chunk:
            return bad
        buf += chunk

# Splits a raw request into (method, path, query, headers, body). body is {} when the
# request has none and None when it isn't a JSON object, which the caller answers 422.
# Returns None for a malformed request line.
def _parse_request(raw):
    try:
        sep = raw.find(b"\r\n\r\n")
        if sep == -1:
            return None
        header_text = raw[:sep].decode("utf-8", "replace")
        lines = header_text.split("\r\n")
        parts = lines[0].split(" ", 2)
        if len(parts) < 2:
            return None
        method = parts[0].upper()
        full_path = parts[1]
        path, _, query = full_path.partition("?")
        hdrs = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                hdrs[k.strip().lower()] = v.strip()
        body = {}
        tail = raw[sep + 4:]
        if tail:
            try:
                parsed = json.loads(tail.decode("utf-8"))
                body = parsed if isinstance(parsed, dict) else None
            except Exception:
                body = None
        return method, path, query, hdrs, body
    except Exception:
        return None

# Serves one connection on its own thread: read, check the Api-Key (every route but
# /hello), dispatch on method and path, answer, close. Same routes, status codes and
# JSON as main.py; an unknown route or pin answers an empty 404.
def handle_request(conn, manager: GPIOManager):
    try:
        raw = _recv_request(conn)
        if raw == "OVERSIZED":
            log_to_file(f"[API] Rejected request - body over {MAX_REQUEST_BYTES} bytes")
            _send_json(conn, {"error": "Request body too large"}, 413)
            return

        if not raw:
            return

        parsed = _parse_request(raw)
        if not parsed:
            _send_json(conn, {"error": "Bad request"}, 400)
            return

        method, path, query, hdrs, body = parsed

        if manager.config.get("settings", {}).get("logs_enabled", True) and not (method == "GET" and path == "/hello"):
            q_str = f"?{query}" if query else ""
            log_to_file(f"[API] Received {method} {path}{_strip_qs_params(q_str)}")

        # /hello is unauthenticated
        if method == "GET" and path == "/hello":
            _send_json(conn, {"name": manager.config.get("name") or _default_name()})
            return

        # Api-Key validation
        api_key = hdrs.get("api-key")
        expected_key = manager.api_key
        if not api_key or not secrets.compare_digest(api_key.encode("utf-8"), expected_key.encode("utf-8")):
            _send_json(conn, {"error": "Invalid or missing Api-Key header"}, 403)
            return

        # Body parse failure
        if body is None and method in ("POST", "PUT", "PATCH"):
            _send_json(conn, {"error": "Request body failed validation"}, 422)
            return

        # Routes
        if method == "GET" and path == "/gpio/read":
            _send_json(conn, manager.read_pin("all"))
        elif method == "GET" and path.startswith("/gpio/read/"):
            pin_name = path[len("/gpio/read/"):]
            try:
                _send_json(conn, manager.read_pin(pin_name))
            except ValueError as e:
                _send_empty(conn, 404)
        elif method == "POST" and path.startswith("/gpio/write/"):
            pin_name = path[len("/gpio/write/"):]
            if not isinstance(body, dict) or "value" not in body or body.get("value") is None:
                _send_json(conn, {"error": "Request body failed validation"}, 422)
                return
            try:
                _send_json(conn, manager.write_pin(pin_name, body.get("value"), body.get("duration")))
            except ValueError as e:
                _send_json(conn, {"error": str(e)}, 400)
        elif method == "POST" and path.startswith("/gpio/config/"):
            pin_name = path[len("/gpio/config/"):]
            if not isinstance(body, dict):
                _send_json(conn, {"error": "Request body failed validation"}, 422)
                return
            try:
                _send_json(conn, manager.config_pin(pin_name, body))
            except ValueError as e:
                _send_json(conn, {"error": str(e)}, 400)
        elif method == "GET" and path == "/gpio/watched":
            _send_json(conn, manager.get_watched_pins())
        elif method == "POST" and path.startswith("/sensor/config/"):
            sensor_name = path[len("/sensor/config/"):]
            if not isinstance(body, dict):
                _send_json(conn, {"error": "Request body failed validation"}, 422)
                return
            try:
                _send_json(conn, manager.config_sensor(sensor_name, body))
            except ValueError as e:
                _send_json(conn, {"error": str(e)}, 400)
        elif method == "GET" and path == "/sensor/read":
            _send_json(conn, manager.read_sensors())
        elif method == "GET" and path.startswith("/sensor/read/"):
            sensor_name = path[len("/sensor/read/"):]
            try:
                _send_json(conn, manager.read_sensor(sensor_name))
            except ValueError as e:
                _send_empty(conn, 404)
        elif method == "GET" and path == "/gpio/scan":
            if not IS_PI:
                _send_json(conn, {"agent": manager._agent_info(), "error": "Platform does not support GPIOs"}, 422)
            else:
                _send_json(conn, {"agent": manager._agent_info(), "gpios": manager.scan_all()})
        elif method == "GET" and path == "/logs":
            try:
                with open(LOG_FILE, "r") as f:
                    lines = deque(f, maxlen=50)
                _send_json(conn, {"logs": [l.rstrip("\n") for l in lines]})
            except FileNotFoundError:
                _send_json(conn, {"logs": []})
        elif method == "GET" and path == "/config/read":
            _send_json(conn, manager.get_app_config())
        elif method == "POST" and path == "/config/update":
            if not isinstance(body, dict):
                _send_json(conn, {"error": "Request body failed validation"}, 422)
                return
            _send_json(conn, manager.set_app_config(body))
        elif method == "POST" and path == "/config/load":
            load_data = body if isinstance(body, dict) else {}
            try:
                manager.load_config(load_data)
                _send_json(conn, {"status": "success"})
            except Exception as e:
                _send_json(conn, {"error": str(e)}, 500)
        elif method == "POST" and path == "/restart":
            reboot = bool(body.get("reboot", False)) if isinstance(body, dict) else False
            if reboot and IS_PI:
                log_to_file("[CONFIG] Reboot requested via /restart")
                _send_json(conn, {"status": "rebooting"})
                conn.close()
                threading.Thread(target=_reboot_device, daemon=True).start()
                return
            else:
                log_to_file(f"[CONFIG] Restart requested via /restart (reboot={reboot})")
                _send_json(conn, {"status": "restarting"})
                conn.close()
                threading.Thread(target=_restart_process, daemon=True).start()
                return
        elif method == "POST" and path == "/upgrade":
            log_to_file("[CONFIG] Upgrade requested via /upgrade")
            _send_json(conn, {"status": "upgrading"})
            conn.close()
            threading.Thread(target=_upgrade_and_restart, daemon=True).start()
            return
        else:
            _send_empty(conn, 404)
    except Exception as e:
        try:
            _send_json(conn, {"error": str(e)}, 500)
        except Exception:
            pass
    finally:
        try:
            conn.close()
        except Exception:
            pass


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


# Sends the boot webhook ("Up and running") to every configured target, then accepts
# connections forever, one thread per request.
def run_server(host: str, port: int):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((host, port))
    server.listen(128)

    def _banner():
        # The public-IP lookup can take up to 5 s - don't hold up startup for a banner.
        private, public = _get_ips()
        log_to_file(f"[SERVER] Private: http://{private}:{port} | Public: http://{public}:{port}")
        print(f"INFO:     Private: http://{private}:{port}")
        print(f"INFO:     Public:  http://{public}:{port}")
    threading.Thread(target=_banner, daemon=True).start()

    boot_targets = manager._notify_targets()
    if boot_targets:
        payload = {"agent": manager.config.get("name") or _default_name(), "note": "Up and running"}
        for url, headers in boot_targets:
            log_to_file(f"[Webhook] Boot → {url}")
            threading.Thread(target=GPIOManager._post_webhook,
                             args=(url, payload, headers), daemon=True).start()

    print(f"INFO:     Uvicorn running on http://{host}:{port} (Press CTRL+C to quit)" if False
          else f"INFO:     Lite server listening on http://{host}:{port}")

    while True:
        try:
            client_conn, _ = server.accept()
            t = threading.Thread(target=handle_request, args=(client_conn, manager), daemon=True)
            t.start()
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            log_to_file(f"[ERROR] Server accept error: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Raspberry Pi GPIO REST API Server (Lite)")
    parser.add_argument("--api-key", type=str, help="Set the Api-Key for authenticated routes (runtime only unless --save is given)")
    parser.add_argument("--save", action="store_true", help="Persist any provided --api-key to the JSON config file")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Binding host address (default: 0.0.0.0)")
    parser.add_argument("--local", action="store_true", help="Bind to localhost only (127.0.0.1); overrides --host")
    parser.add_argument("--port", type=int, default=8314, help="Binding port number (default: 8314)")
    parser.add_argument("--lite", action="store_true", help="Run lite version (default for lite.py)")
    parser.add_argument("--reload", action="store_true", help="Ignored in lite mode")
    parser.add_argument("--fastapi-docs-enabled", action="store_true", help="Ignored in lite mode")
    args = parser.parse_args()

    host = "127.0.0.1" if args.local else args.host

    if not _CONFIG_EXISTED_AT_STARTUP:
        _first_time_setup()

    if args.api_key:
        manager.config["api_key"] = args.api_key

    if args.save:
        updates = {}
        if args.api_key:
            updates["api_key"] = args.api_key
        if updates:
            update_config_file(updates)

    run_server(host, args.port)
