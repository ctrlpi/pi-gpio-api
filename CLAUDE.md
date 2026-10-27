# CLAUDE.md - pi-gpio-api

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## The README is locked

`README.md` is done. Do not edit it without the user's explicit confirmation, even for a small wording or formatting fix, and even to keep it in sync with another project's README.

## What This Is

A Python server for Raspberry Pi that exposes GPIO pin control over a FastAPI REST API. 

## Setup & Running

```bash
# Recommended: handles venv, installs deps, starts server (pass --reload for auto-reload)
./run.sh [--reload]

# Manual
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8314

# Set and persist API key on startup
python main.py --api-key your-secret-key --save
```

## Tests

`python test.py` (CI runs it on Ubuntu after `pip install -r requirements.txt`). It switches to `venv/` by itself when the
outer Python lacks the packages. The same suite runs twice, against `lite.py` and against `main.py` (skipped when
FastAPI/Pydantic are missing): each starts on a free loopback port with mock GPIO, inside a temporary folder so
`config.json`, `status.json` and `agent.log` never touch the checkout. A test sensor script is staged in `scripts/` and
removed afterwards. Live tests against a real Pi are `api-tester`'s job.

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

Sensor scripts live in **`scripts/`**. The API executes them directly, and `/script/*` manages the files there.

### `lite.py` (the Lite version)

A second, self-contained server with the same REST API and no FastAPI/Pydantic/uvicorn: a plain socket accept loop, one thread per connection, hand-written request parsing (`_recv_request`, `_parse_request`) and routing in `handle_request`. `./run.sh --lite` starts it, and `run.sh` falls back to it automatically whenever `fastapi`/`pydantic` don't import; `install.sh` asks Lite or Full (`--lite`/`--full`). It must keep **the same wire behaviour as `main.py`** (routes, status codes, JSON shapes), so the same `api-tester` suite runs against both: a change to one almost always needs the same change in the other.

**Request bodies** follow `main.py`'s `CheckApiMiddleware`: a `Content-Length` over `MAX_REQUEST_BYTES` (16 KB) is answered `413`, and a chunked body (no `Content-Length`) is decoded by `_recv_chunked` up to the same cap, `413` past it. A partial or unreadable body must never reach a route as an empty one, since `/config/load` applies an empty body as `{}`, a full config reset.

## Key Design Logic

- **Text fields stay text**: a number sent for a key, name, URL or script (`api_key`, `name`, `notifications.*`, `bridges.*`, a pin's `name`, a sensor's `script`) is stored as text, so `4242` becomes `"4242"` and a key typed as digits never locks the agent out. `_as_text()` / `_text_fields()` run in `_normalize_config()` (every `/config/update` and `/config/load`) and in `config_pin()` / `config_sensor()`. In `main.py` the request models also take numbers as text (`coerce_numbers_to_str`), since pydantic would otherwise refuse them with 422. `test_numbers_stored_as_text` covers it.
- **Duration clamping**: If `max` is set on a pin and no duration is given, `max` becomes the effective duration. Handled via a daemon thread.
- **Write auto-configure**: `write_pin()` configures a pin as `output` only if it is unconfigured. Writing to an `input` pin raises a 400 error to prevent accidental sensor overrides.
- **Output config defaults**: Explicitly configuring a new `output` pin starts it at LOW (`0`) unless an `init` value is provided. 
- **Removing an output pin**: Setting `type=remove` switches the pin to an un-pulled `input` before deleting it, ensuring it doesn't stay actively driven.
- **Config persistence rules**: Configurations only store fields applicable to their type. For example, `pullup` is cleared on an `output` pin, and `watched` is only stored if true. 
- **Watch/webhook flow**: `watch_pin` registers callbacks; `_trigger_webhook()` queues the pin state JSON for every configured target on `WebhookSender` (the boot notice goes the same way). Each target URL gets one `_WebhookTarget`: its own worker thread, its own kept-alive `requests.Session`, and a queue, so its events go out in order and a dead target never delays another. Headers (the target's own `Api-Key`) go on each request, never on the session, so no key reaches another target. A connection error on a session that has delivered before (a kept-alive socket the target closed) is resent at once on a fresh connection; any other failure is queued again `WEBHOOK_RETRY_DELAY` (30 s) later, up to `WEBHOOK_RETRIES` (2) times, with the same `[WEBHOOK] POST to ... failed` log lines. A queue holds at most `QUEUE_MAX` (100) events (the oldest is dropped and logged), and a target idle for `IDLE_SECS` (120 s) closes its connection and its worker exits, so URLs that are no longer configured leave nothing behind. Why: a fresh HTTPS connection is slow on a small board. Measured on a Pi 1 A+, one cost ~3 s and three at once ~9 s each, past the ~10 s the hooks server allows for a handshake (`api-tester` test 32 failed there in full runs), while a reused one costs ~0.2 s. `TestWebhookSenderLite` / `TestWebhookSenderMain` cover order and one connection, per-target keys, the retry, the resend after a dropped connection and the idle exit.
- **Sensors**: Scripts run via `["sh", scripts/<file>, *args]` (a `.py` file via `[sys.executable, scripts/<file>, *args]`, the agent's own Python) with a 15-second timeout. Filenames must match `_SCRIPT_NAME_RE` and stay confined to the `scripts/` directory for security.
- **Scripts (`/script/list`, `/script/read`, `/script/write`, `/script/run`)**: module-level `list_scripts()` / `read_script()` / `write_script()` / `run_script()` over `SENSOR_SCRIPTS_DIR`, the same block in `main.py` and `lite.py`, raising `ScriptError(status, message)`. Names use `_SCRIPT_NAME_RE` (no `/`, `\`, leading dot or dash) and `SCRIPT_NAME_MAX` (32), and a subfolder is `400` ("not a regular file"). A run is a sensor run without the JSON parsing: `{name, exit_code, output}` (stdout, `SCRIPT_OUTPUT_CAP` 2 KB, trailing whitespace trimmed), `504` after `SCRIPT_TIMEOUT` (15 s), and it does not take the manager lock. Writes go through a hidden `.<name>.tmp` and `os.replace`, and create `scripts/` when it's missing. `write_script()` answers `403` while `api_key` is `your-secret-key` and while `scripts/write.lock` exists (any content). A write may create `write.lock` (nothing else ever does, install included), and from then on every write, the lock's own included, is refused, so `/script/write` can't lift it; a shell can (`./install.sh --unlock` deletes it and installs nothing), and so can a script run through `/script/run`, since runs still work while locked (api-tester test 59 unlocks that way). `write.lock` in any case is never run (`_is_lock_file`, since macOS maps `WRITE.LOCK` onto it). A wrong body type is `422` in both servers (pydantic in `main.py`, by hand in `lite.py`). The `test_script_*` tests cover it.
- **`/hello` endpoint**: Bypasses auth and logging entirely to serve as a fast, quiet polling endpoint.
- **Log retention**: Controlled by `config.log_days`. Setting to `0` clears the log immediately.
- **Restart/reboot (`/restart`)**: Process restarts use `os.execv` to preserve PID and bind port 8314 instantly (since Python 3.4+ sockets are non-inheritable). A full device reboot via `{"reboot": true}` is supported on Pi devices with passwordless sudo; if device reboot fails, it falls back to an in-process restart.

## Authentication

All endpoints except `/hello` require the `Api-Key: <key>` header. The default fallback is `"your-secret-key"`. Keys are transmitted in plaintext (no TLS), meant for trusted LANs only. A usable key is non-empty text (`_valid_key()`; a number counts, as text). An empty, null or non-text `api_key` counts as no key: `config.json` then falls back to the default, `/config/load` keeps the current key, and `/config/update` ignores it. An empty or missing `Api-Key` header is always refused. `test_bad_api_key_*` cover it.

## Feature Flags

Stored in `config.settings`, runtime-togglable via `POST /config/update`:
- `docs_enabled`: Toggles `/docs`, `/redoc`, and `/openapi.json` (default: `false`).
- `logs_enabled`: Toggles writing to `agent.log` (default: `true`).
- `log_days`: Retention in days (0-30, default: `7`).

## Open items (future)

- **A shorter first webhook retry** (e.g. 3 s, then 30 s), so a delivery whose first attempt fails still lands within seconds. Not done, because: (1) "retried twice, 30 s apart" is the shared rule in the root `CLAUDE.md`, and `pico-gpio-api` and `esp32-gpio-api` use 30 s too, so it would have to change in all three agents and in their locked READMEs; (2) a bridge answers `401` until it has pushed its new key, which it does off the first rejected event, so a 3 s retry may still be refused (the 30 s one lands); (3) a POST the server received whose reply was lost would be resent inside a test window, and `api-tester` tests 32/33 count deliveries exactly. Revisit only if the per-target kept-alive sender is not enough on the slowest Pis.
