# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**`README.md` is done.** Do not edit it without the user's explicit confirmation first, even for small wording/formatting fixes.

## What This Is

A Python server for Raspberry Pi that exposes GPIO pin control over a FastAPI REST API. 

## Setup & Running

```bash
# Recommended — handles venv, installs deps, starts server (pass --reload for auto-reload)
./run.sh [--reload]

# Manual
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8314

# Set and persist API key on startup
python main.py --api-key your-secret-key --save
```

## Architecture

Everything lives in **`main.py`**. 

1. **GPIO hardware layer**: Detects Pi at import, falls back to minimal mock classes off-Pi.
2. **Pydantic models**: Request/Response models and configurations.
3. **Logging**: Daemon thread (`_log_cleanup_loop`) manages `agent.log` retention.
4. **`GPIOManager`**: Singleton managing business logic, pin lifecycle, and webhooks.
5. **Persistent State**: 
   - `config.json` (agent, pins, sensors, webhooks).
   - `config-backup.json` (automatic fallback).
   - `status.json` (last known output values, restored on reboot).

Sensor scripts live in **`scripts/`**. The API executes them directly.

## Key Design Logic

- **Duration clamping**: If `max` is set on a pin and no duration is given, `max` becomes the effective duration. Handled via a daemon thread.
- **Write auto-configure**: `write_pin()` configures a pin as `output` only if it is unconfigured. Writing to an `input` pin raises a 400 error to prevent accidental sensor overrides.
- **Output config defaults**: Explicitly configuring a new `output` pin starts it at LOW (`0`) unless an `init` value is provided. 
- **Removing an output pin**: Setting `type=remove` switches the pin to an un-pulled `input` before deleting it, ensuring it doesn't stay actively driven.
- **Config persistence rules**: Configurations only store fields applicable to their type. For example, `pullup` is cleared on an `output` pin, and `watched` is only stored if true. 
- **Watch/webhook flow**: `watch_pin` registers callbacks that POST pin state JSON to all configured targets in daemon threads. Delivery failures retry up to `WEBHOOK_RETRIES` times, delayed by `WEBHOOK_RETRY_DELAY`.
- **Sensors**: Scripts run via `["sh", scripts/<file>, *args]` with a 15-second timeout. Filenames must match `_SCRIPT_NAME_RE` and stay confined to the `scripts/` directory for security.
- **`/hello` endpoint**: Bypasses auth and logging entirely to serve as a fast, quiet polling endpoint.
- **Log retention**: Controlled by `config.log_days`. Setting to `0` clears the log immediately.
- **Restart/reboot (`/restart`)**: Process restarts use `os.execv` to preserve PID and bind port 8314 instantly (since Python 3.4+ sockets are non-inheritable). A full device reboot via `{"reboot": true}` is supported on Pi devices with passwordless sudo.

## Authentication

All endpoints except `/hello` require the `Api-Key: <key>` header. The default fallback is `"your-secret-key"`. Keys are transmitted in plaintext (no TLS), meant for trusted LANs only.

## Feature Flags

Stored in `config.settings`, runtime-togglable via `POST /config/update`:
- `docs_enabled`: Toggles `/docs`, `/redoc`, and `/openapi.json` (default: `false`).
- `logs_enabled`: Toggles writing to `agent.log` (default: `true`).
- `log_days`: Retention in days (0-30, default: `7`).
