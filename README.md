# Raspberry Pi GPIO API

[![Platform: Raspberry Pi](https://img.shields.io/badge/platform-Raspberry%20Pi-006400.svg)](#install-on-your-pi)
[![Version 0.9.23](https://img.shields.io/badge/version-0.9.23-blue.svg)](https://github.com/ctrlpi/pi-gpio-api/tags)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Auth: Api-Key](https://img.shields.io/badge/auth-Api--Key-orange.svg)](#authentication)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
![Status: Beta](https://img.shields.io/badge/status-Beta-red.svg)

A Python server that exposes Raspberry Pi GPIO control through an OpenAPI REST API, allowing HTTP clients to trigger outputs and read inputs remotely.

Runs on a Raspberry Pi, secured with API key authentication, GPIO via `gpiozero`.

> Looking for AI-agent / MCP control, Apple HomeKit or Google Home? See the [Related projects](#related-projects) below that drive one or more of these API servers (Pi or Pico) over their REST endpoints.

## Install on your Pi

**Requirements:** Python 3.9+ (any recent Raspberry Pi OS ships one; Bullseye has 3.9, Bookworm 3.11) on any of the following models: **Raspberry Pi 2 / 3 / 4 / 5 / Zero W / Zero 2 W**.

### Option 1: curl (recommended)

One-liner, no git required - downloads the release files into `./pi-gpio-api` and builds the venv:

```bash
curl -fsSL https://raw.githubusercontent.com/ctrlpi/pi-gpio-api/main/install.sh | bash
```

If `config.json` doesn't exist yet, you'll be asked for an agent name and API key (hostname suggested as the default name, `your-secret-key` as the key). Pass `-y` to skip the prompt and take both defaults.

Re-running the downloaded copy directly (`./install.sh`) only reinstalls dependencies
into the existing venv (creating one first if missing) - it won't re-download the files
unless run with `./install.sh --upgrade`.

Once installed, start the server with `./run.sh` (see [Quick Start](#quick-start) below).

### Option 2: git clone

```bash
git clone https://github.com/ctrlpi/pi-gpio-api.git
cd pi-gpio-api
./install.sh
./run.sh
```

Run `./install.sh` first (`-y` to take the defaults). Then start the server with `./run.sh`.

### Run automatically at boot

**Easiest:** on a Pi, `./install.sh` prints a ready-to-paste command that creates a systemd unit (with `User` and the `run.sh` path already filled in) and enables it. 
Just copy-paste what it shows.

To manually do it, set up a **systemd** service to start the server on boot. 

Or use **cron** (one-liner adding to crontab, run from inside the project directory):

```bash
(crontab -l 2>/dev/null; echo "@reboot $(pwd)/run.sh") | crontab -
```


## Quick Start

```bash
# Recommended: starts server, build/fix venv if needed
./run.sh

# Set and persist API key (drop --save to apply to this run only)
./run.sh --api-key your-secret-key --save

# Or run main.py directly (after ./install.sh has built venv)
source venv/bin/activate
python main.py
```


The server binds **`0.0.0.0`** (all interfaces) and port **8314** by default, so it is reachable on your local network; keep it behind a trusted LAN (see the No-TLS note under Authentication). Override with `--host` and `--port`, or pass `--local` to bind `127.0.0.1` only.

Off-Pi (Mac/PC) is also supported, for development and testing: it skips `gpiozero` and `lgpio` and runs against a mock GPIO backend instead.

### GPIO Backend

`main.py` imports only `gpiozero`, which picks its backend at runtime: `lgpio`, then `RPi.GPIO`, then `pigpio`. `install.sh`/`run.sh` accept either of the first two, installed with apt:

| Raspberry Pi OS | Package | Notes |
|---|---|---|
| Bookworm or newer | `sudo apt-get install -y python3-lgpio` | Required on a Pi 5 (RP1); usually already installed |
| Bullseye or older | `sudo apt-get install -y python3-rpi.gpio` | Bullseye has no `python3-lgpio` package at all; usually already installed |


## Try the API

Replace `<pi-host-addr>` with the device address and `your-secret-key` with your API key.

#### 1. Health check

```bash
curl http://<pi-host-addr>:8314/hello
```

#### 2. Read all configured pins

```bash
curl http://<pi-host-addr>:8314/gpio/read -H "Api-Key: your-secret-key"
```

#### 3. Write to a pin

```bash
curl -X POST http://<pi-host-addr>:8314/gpio/write/26 \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"value": 1, "duration": 2}'
```

#### 4. Configure a pin (relay, reversed, max 1 hour)

```bash
curl -X POST http://<pi-host-addr>:8314/gpio/config/26 \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"name": "relay", "type": "output", "reversed": true, "max": 3600}'
```

#### 5. Scan all BCM GPIO hardware pins (Pi only)

```bash
curl http://<pi-host-addr>:8314/gpio/scan -H "Api-Key: your-secret-key"
```

#### 6. Configure a watched input (fires a webhook on change)

```bash
curl -X POST http://<pi-host-addr>:8314/gpio/config/6 \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"type": "input", "watched": true}'
```

#### 7. Change the webhook URL

```bash
curl -X POST http://<pi-host-addr>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"webhook_url": "https://example.com/webhook"}'
```

#### 8. Configure a sensor

```bash
# cpu-temp.sh is the example script install.sh seeds into scripts/
curl -X POST http://<pi-host-addr>:8314/sensor/config/cpu_temp \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"script": "cpu-temp.sh"}'
```

#### 9. Read a sensor

```bash
curl -H "Api-Key: your-secret-key" http://<pi-host-addr>:8314/sensor/read/cpu_temp
```

## Authentication

All endpoints require an `Api-Key` header except `/hello`.

```
Api-Key: your-secret-key
```

> **No TLS.** The server speaks plain HTTP, so the key travels as plaintext on the wire. That's fine on a trusted LAN, but don't expose port 8314 directly to the internet. If you need remote access, tunnel it.

## REST API

Interactive OpenAPI docs are served at `/docs` (Swagger UI) and `/redoc` when the `docs_enabled` config field is on (default: off). Enable it at startup with `--fastapi-docs-enabled` (add `--save` to persist), or toggle it at runtime via `POST /config/update` (no restart needed).

## Health

A single unauthenticated endpoint for discovery and liveness checks; it reports the agent's name.

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/hello` | none | Returns `{"name": "agent-name"}`, used for discovery and health checks. If name is not configured, it will return hostname, or `pi-<model>-<serial4>` (e.g. `pi-4B-1a2b`) when the hostname is still the stock `raspberrypi`. |

## GPIO

The core of the API: read, write, configure, and watch GPIO pins. Every pin is addressed by its BCM number or a configured name alias.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/gpio/read` | Read all configured pins |
| `GET` | `/gpio/read/{name_or_gpio}` | Read a single pin by name alias or GPIO number |
| `POST` | `/gpio/write/{name_or_gpio}` | Write a value to a pin |
| `POST` | `/gpio/config/{name_or_gpio}` | Configure a pin (name, type, init, pullup, max, reversed, watched) |
| `GET` | `/gpio/watched` | List all currently watched pins, the active webhook URL, and the `bridges` callback URLs (URLs only, never the keys) |
| `GET` | `/gpio/scan` | Scan all BCM GPIO pins (Pi hardware only). Returns `level` (the physical state) and `value` (logical). Also returns: `platform`, `os`, `version`, `ip` and `cpu_temperature`. |

### Request Bodies

#### `POST /gpio/write/{name_or_gpio}`

```json
{
  "value": 1,
  "duration": 2.5
}
```

| Field | Values | Description |
|-------|--------|-------------|
| `value` | `0`, `1`, `"on"`, `"off"`, `"toggle"` | Value to write. `"on"`/`"off"` are word spellings of `1`/`0`, `"toggle"` flips the current state. Strings are case-insensitive. A JSON boolean (`true`/`false`) also works, coerced to `1`/`0`. Any other integer counts as `1` if nonzero, `0` only if exactly `0` (so a negative value like `-1`, used by some systems for true, also means "on"). |
| `duration` | float (seconds) | Optional. Pulse the pin for this many seconds, then revert. If the pin has `max` configured, a larger `duration` is clamped to `max`, and a write **without** `duration` uses `max` as the duration (it pulses and reverts, it does not latch). |

Writing to an **unconfigured** pin auto-configures it as an output first. A pin already configured as anything else (`input`, `vcc`, `gnd`) is never silently flipped; the write returns `400`. Reconfigure it as `output` via `/gpio/config` first.

#### `POST /gpio/config/{name_or_gpio}`

```json
{
  "name": "relay",
  "type": "output",
  "init": 0,
  "pullup": "up",
  "reversed": true,
  "max": 5.0,
  "watched": true
}
```

| Field | Values | Description |
|-------|--------|-------------|
| `name` | string | Name alias for the pin, usable in place of GPIO number in all endpoints. |
| `type` | `input`, `output`, `vcc`, `gnd`, `remove` | Pin direction. `vcc`/`gnd` are permanently driven and cannot be written or watched. `remove` deletes the pin config. Changing type clears fields that no longer apply (see below). |
| `init` | `0`, `1`, `"on"`, `"off"`, `"last"`, `"restore"` | Startup behavior, also applied on every live `/gpio/config` call. **Outputs**: drive to value. Inputs: compare to reference and fire on mismatch. `"on"`/`"off"` are word spellings of `1`/`0`, and `"restore"` is a synonym for `"last"`, which uses the value from the last run. Strings are case-insensitive. A JSON boolean (`true`/`false`) also works, coerced to `1`/`0`. Any other integer counts as `1` if nonzero, `0` only if exactly `0` (so `2` and `-1` both mean "on", matching `/gpio/write`). All of this is stored canonically, so `/config/read` and `/gpio/read` always answer `0`, `1` or `"last"`. Unknown words are **rejected**, not stored and ignored. On an input the reference is compared against the pin's reported (logical) value, so `pullup` and `reversed` are already accounted for. If omitted on an output: a pin that's already configured as output keeps its current value unchanged; a pin that's *newly* becoming an output (was `input`, `vcc`, `gnd` or unconfigured) starts at `0`. Cleared automatically when the pin's `type` changes. |
| `pullup` | `"up"`, `"down"`, `"none"` | Pull resistor (inputs only). Cleared automatically when type is set to `output`, `vcc`, or `gnd`. |
| `max` | float (seconds) | Maximum pulse duration (outputs only). Writes with a larger `duration` are clamped to it, and writes with **no** `duration` use it as the duration (see `POST /gpio/write` above). Cleared automatically when type is set to `input` or when type is set to `output` if the value is `0` or unset. |
| `reversed` | boolean | Inverts the logical value. **Output**: writes the boolean flip to the physical GPIO pin (physical HIGH = logical 0). **Input**: flips the reported value. Cleared automatically when type is set to `vcc` or `gnd`. |
| `watched` | boolean | `true` to send a webhook on every write (outputs) or state change (inputs); `false` to stop. Cleared automatically when type is set to `vcc` or `gnd`. Webhooks are only sent when a `webhook_url` is configured. With none set, each watched event is recorded in the agent log instead (see [Webhooks](#webhooks)). |

## Sensors

A **sensor** is a named reading the agent computes on demand by running a script, a way to surface values GPIO pins can't carry (I2C sensors, 1-Wire probes, a `/proc` metric, the output of any script or tool).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/sensor/read` | Read every configured sensor, merged into one JSON object |
| `GET` | `/sensor/read/{name}` | Read one sensor by name |
| `POST` | `/sensor/config/{name}` | Configure a named sensor |

### Request Bodies

#### `POST /sensor/config/{name}`

```json
{
  "script": "cpu-temp.sh"
}
```

Or, to delete the sensor:

```json
{
  "remove": true
}
```

Sensors are pure config plus on-demand reads: no pins, no background polling, no extra persisted state beyond `config.json`.

### Configure

Scripts live in the **`scripts/` folder next to `main.py`**. `install.sh` creates it and seeds it with a one-line `cpu-temp.sh` example; drop your own scripts in beside it (the agent itself never creates the folder - with no folder, every sensor just reads `script not found`).

Scripts can have arguments as `"<file> [args...]"`, where the file is a **bare filename** in `scripts/`: no paths, no `..`, no leading dot, no subfolders. It's always run as `sh scripts/<file> <args>` with a 15-second timeout, and its **stdout** (first 2 KB) is the reading, so it needs no `./` prefix, no executable bit, and nothing on `PATH`. Arguments may contain letters and digits only. Inline shell commands are **not** accepted: nothing is ever parsed by a shell, so pipes, redirects, and command substitution cannot be injected through the API.

`scripts/` is the **only** folder a sensor can run anything from. That folder is hardcoded (no config field or API call can widen it), so a holder of the API key can never point a sensor at an arbitrary file or binary on the device. To surface something outside it (e.g. `/sys/class/thermal/thermal_zone0/temp`), drop a one-line script into `scripts/` that reads it: the restriction is on what the *API* can point at, not on what a script you stage there may itself read. Scripts run with the server's privileges; keep the agent on a trusted LAN, as with the rest of the API.

Reading returns a clear message instead of failing when a script is missing or broken: `{"<name>": "script not found"}`, `{"<name>": "script failed or did not produce output"}` when it exits non-zero or prints nothing, `{"<name>": "script timed out"}` after the 15-second limit, and `{"<name>": "blocked: <reason>"}` when a sensor violates the name/argument rules (possible when sensors arrive via `/config/load` or an edited `config.json`; every read re-validates).

### Read

`GET /sensor/read/{name}` reads one sensor; `GET /sensor/read` reads them all, merged into one object. The output is always JSON, and a reading is **always nested under the sensor's own name** - one key per sensor, whatever the script printed:

- A JSON object with **several fields** nests whole: `{ "<name>": { ... } }`.
- A JSON object with a **single field** contributes just that field's value: `{ "<name>": <value> }` (the field's own key is dropped - a script that prints `{"temp": 47.8}` for a sensor named `cpu_temp` reads back as `{"cpu_temp": 47.8}`).
- Anything else (a non-JSON answer such as a bare number or string) is wrapped the same way; numbers stay numbers - a script printing `47.8` for a sensor named `cpu_temp` reads back as `{ "cpu_temp": 47.8 }` (a number, not a string), and a script printing `ok` for a sensor named `status` reads back as `{ "status": "ok" }`.

Because every sensor owns exactly one key in that merge, two sensors emitting the same field name can never overwrite each other. An unknown sensor name returns `404`.

```bash
curl -H "Api-Key: your-secret-key" http://<pi-host-addr>:8314/sensor/read
# → {"cpu_temp": 47.8, "status": "ok"}
```

## Agent Setup

Read and change the agent's own configuration, and drive process and device lifecycle (restart, upgrade, logs).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/config/read` | Read the agent identity + full config (see response fields below) |
| `POST` | `/config/update` | Update one or more config fields (backs up the current config first) |
| `POST` | `/config/load` | Load a full config from a JSON dict (backs up the current config first; pass `{}` to reset) |
| `POST` | `/upgrade` | Upgrade in place. Takes no body. Runs `install.sh --upgrade` (re-downloads the latest release files and reinstalls dependencies), then restarts the process so the new code takes effect. Returns `{"status": "upgrading"}` immediately; the download + restart run in the background, and the current code keeps running if the install fails (see `agent.log`). Unavailable in `--reload` dev mode (409) |
| `POST` | `/restart` | Restart the server process. Body is optional; pass `{"reboot": true}` to reboot the physical device instead. Only honored on a real Pi, ignored (falls back to a process restart) on Mac/dev machines. Device reboot runs `sudo reboot`, which requires passwordless sudo for the server's user (e.g. a sudoers entry `<user> ALL=(ALL) NOPASSWD: /sbin/reboot`); without it the reboot fails silently |
| `GET` | `/logs` | Last 50 lines of `agent.log` as a JSON array |

### `GET /config/read` response

Returns the agent's identity plus its full config:

| Field | Description |
|-------|-------------|
| `name` | Agent name: configured name, else the hostname, or `pi-<model>-<serial4>` when the hostname is the stock `raspberrypi`. |
| `agent` | Live machine facts, computed on every read and **never stored**, in this order:<br>`platform`: the board model with the marketing words dropped (`Raspberry Pi 5 Model B Rev 1.1` is reported as `Pi 5 B Rev 1.1`)<br>`os`: on a Pi the distro codename and word size, e.g. `Bookworm 64-bit` or `Trixie 32-bit`; the bare system/release pair elsewhere<br>`host`: this machine's hostname<br>`serial`: the board serial from `/proc/cpuinfo`, `""` off-Pi<br>`ip`: this machine's local network address<br>`mac`: the address of the interface carrying that `ip`, wireless or wired, whichever holds the default route; `""` if it can't be determined<br>`wifi`: SSID, `""` when wired or unavailable<br>`signal`: dBm, `null` with no wireless link<br>`cpu_temperature`: Pi only, absent off-Pi<br>`uptime`: a short human string of at most two units, largest first (`"3d 4h"`, `"4h 12m"`, `"12m"`, `"38s"`)<br>`version`: the agent's software version<br>`config_updated`: when `config.json` itself was last written, as UTC `"YYYY-MM-DD HH:MM:SS"` (the file's own mtime, so it answers "when did this agent last change"); `""` if it has never been written<br><br>Sending it back on `/config/update` or `/config/load` is ignored. `version` is kept in lockstep with `pico-gpio-api`, so a matching `version` means matching API behavior. |
| `notifications` | The general-purpose webhook target: `webhook` and `webhook_key`. |
| `bridges` | The three bridge callbacks: `homekit`, `homekit_key`, `matter`, `matter_key`, `homebridge`, `homebridge_key` (see [Bridge callbacks](#bridge-callbacks-bridgeshomekit--bridgesmatter--bridgeshomebridge)). |
| `settings` | Operational toggles: `docs_enabled`, `logs_enabled`, `log_days`. |
| `sensors` | Named-sensor configuration map (see [Sensors](#sensors)). |
| `gpios` | Per-pin configuration map (see [GPIO](#gpio); always the last field). |

### Config fields (`/config/update`)

Send fields **ungrouped, at the top level** - the agent routes each one into its group,
so a later `/config/read` shows them nested:

```json
{
  "name": "my-pi",
  "webhook_url": "https://example.com/webhook",
  "webhook_key": "",
  "matter": "",
  "matter_key": "",
  "homekit": "",
  "homekit_key": "",
  "homebridge": "",
  "homebridge_key": "",
  "docs_enabled": false,
  "logs_enabled": true,
  "log_days": 7
}
```

| Sent at the root | Lands in |
|---|---|
| `webhook_url`, `webhook_key` | `notifications.webhook`, `notifications.webhook_key` |
| `matter`, `matter_key`, `homekit`, `homekit_key`, `homebridge`, `homebridge_key` | `bridges.*` |
| `docs_enabled`, `logs_enabled`, `log_days` | `settings.*` |
| `name`, `api_key` | top level, ungrouped |

The grouped spelling works too (`{"settings": {"log_days": 5}}`), and either way each group
**merges key by key** - setting `log_days` never resets `logs_enabled`, and a bridge pushing
its own URL never clears the other bridge's. `/config/load` accepts both forms as well, and
always writes `config.json` in the grouped shape.

`webhook_key` is a dedicated key for the webhook *target* (see Webhooks below); it is not the agent's own `api_key`. An empty string clears it, same as `webhook_url`.

The live `agent` block is read-only: if it is present in an update or a whole-config load it is dropped rather than stored, so a read-modify-load round trip can post `/config/read` straight back.

Log retention is controlled by the `log_days` config field (default `7`, range `0`-`30`, clamped). An hourly background pass trims `agent.log` down to that many days; setting `log_days` to `0` clears the file immediately and then keeps only the last hour on each pass.

## Errors

Error responses are JSON `{"error": ...}`, except 404 which has an empty body:

| Status | When | Body |
|--------|------|------|
| `400` | Bad write/config (e.g. writing to a pin configured as `input`/`vcc`/`gnd`, invalid value) | `{"error": "<reason>"}` |
| `403` | Missing or wrong `Api-Key` header | `{"error": "Invalid or missing Api-Key header"}` |
| `404` | Unknown pin/name, or unknown route | empty |
| `413` | Request body over 16 KB | `{"error": "Request body too large"}`. Checked before authentication, so an oversized body is refused whatever key it carries. The largest real request is a full `/config/load` (every pin named, with sensors), which is well under it |
| `422` | Request body fails validation | `{"error": [<validation error list>]}`. Special case: `GET /gpio/scan` off-Pi returns `{"agent": {...}, "error": "Platform does not support GPIOs"}` |

## Webhooks

### Payload

When a pin has `watched: true`, the server POSTs the pin's state to `webhook_url` on every write (outputs) or state change (inputs: every edge, the primary use case):

```json
{ "agent": "my-pi", "gpio": 17, "value": 1, "type": "output", "name": "relay" }
```

For reversed pins the payload also includes `level` (the physical GPIO state).

No webhook URL is configured by default. When a watched event occurs with **no target at all** configured (no `webhook_url` and no bridge callback), nothing is sent; the event is recorded in the agent log instead.

### Delivery and retries

A failed delivery is retried **twice more, 30 seconds apart** (3 attempts in total, per target). A
non-2xx answer counts as a failure just like a refused connection - which is what carries an event
across a bridge restart: the bridge answers `401` until it has re-pushed its key onto this agent
(it does that off the first rejected event), so the retry 30s later is the one that lands. Each
attempt is logged, with the reason and whether another is coming.

### Bridge callbacks (`bridges.homekit` / `bridges.matter` / `bridges.homebridge`)

Three extra callback URLs, one owned by each smart-home bridge, sit alongside `webhook_url`:
`homekit` for the standalone `homekit-bridge`, `matter` for `matter-bridge`, and `homebridge`
for the `homebridge-ctrlpi` plugin. They are **additive**: a watched event is POSTed to every
configured target, with the same payload, so a bridge no longer has to take over `webhook_url`.

`homebridge` exists so the Homebridge plugin and the standalone `homekit-bridge` can drive the
same agent at once. Both put pins in the Home app, and while they shared the `homekit` slot,
whichever configured itself last silently stopped the other receiving events.

```bash
curl -X POST http://<pi-host-addr>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"matter": "http://192.168.1.50:8317/webhook?agent=pi"}'
```

`homekit-bridge`, `matter-bridge` and `homebridge-ctrlpi` set these themselves at startup, so
you normally never touch them by hand. An empty string clears one. All three are reported by `/config/read` and
`/gpio/watched` under `bridges` (the latter carries the URLs only, never the keys). The group
**merges key by key**, so setting one bridge's fields never disturbs another's.

Each callback has its own key field, `homekit_key` / `matter_key` / `homebridge_key`, set in
the same call:

```bash
curl -X POST http://<pi-host-addr>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"matter": "http://192.168.1.50:8317/webhook?agent=pi", "matter_key": "Kp3xR9tLmQ7z"}'
```

When set, that key is sent as the `Api-Key` header on that bridge's callbacks (and nowhere
else) so the bridge can tell a real event from one anybody on the LAN made up - it answers
`401` and logs the attempt otherwise. The bridges generate a random 12-character key per
agent on every start and push it here themselves, so a key left behind in this file is
worthless the moment the bridge restarts. As with `webhook_key`, this is never the
agent's own `api_key`: that key can never leak to a bridge. An empty string clears one, which
goes back to unauthenticated callbacks.

### Boot notification

On startup, the server also POSTs a one-off boot notification to every configured target:

```json
{ "agent": "my-pi", "note": "Up and running" }
```

It carries no pin fields; consumers of the webhook feed should treat entries with a `note` field as status messages, not pin events. The same `webhook_key` rule below applies.

### Authentication

If a dedicated `webhook_key` is configured, it is sent as the `Api-Key` header on every webhook POST; if not, no `Api-Key` header is sent at all. The agent's own `api_key` is never sent to the webhook target. The public `https://ctrlpi.com/webhook/test` test endpoint requires no key, it will display whatever key was sent for debugging.

### Testing webhooks

For development and testing, ctrlPi provides a public test endpoint: point `webhook_url` at `https://ctrlpi.com/webhook/test` and monitor the incoming events live at [https://ctrlpi.com/webhook/watch](https://ctrlpi.com/webhook/watch). The feed is public and events are retained only briefly, so use it for verification during setup, not as a production target.

### Changing the URL

Change it by setting `webhook_url` (and optionally `webhook_key`) via `POST /config/update`:

```bash
curl -X POST http://<pi-host-addr>:8314/config/update \
  -H "Api-Key: your-secret-key" -H "Content-Type: application/json" \
  -d '{"webhook_url": "https://example.com/webhook", "webhook_key": "target-key"}'
```

## Files

The state the agent persists to disk, all in the project directory alongside `main.py`. Each is created on first run as needed, so there's nothing to set up by hand.

| File | Contents |
|------|----------|
| `config.json` | API key, pin configs, feature flags, webhook URL. Config backup is written automatically to `config-backup.json` and read on boot if `config.json` exists but fails to parse/validate |
| `status.json` | Last known pin values, restored on reboot when `init: "last"`. Inputs are recorded too, so a watched input's `init: "last"` reference has a previous reading to compare against |
| `agent.log` | Server log, persists across restarts; trimmed to `log_days` by an hourly pass |

## Related projects

- **[`pico-gpio-api`](https://github.com/ctrlpi/pico-gpio-api)**: Raspberry Pi Pico W port of the GPIO REST server, with the same endpoints, wire format, and auth.
- **[`mcp-bridge`](https://github.com/ctrlpi/mcp-bridge)**: MCP server that exposes GPIO control as tools for AI agents (e.g. Claude), executing them over the agents' REST API.
- **`homekit-bridge`** *(coming soon)*: native Apple HomeKit bridge that exposes GPIO pins and sensors as HomeKit accessories, driven over the agents' REST API.
- **`matter-bridge`** *(coming soon)*: Matter bridge that exposes GPIO pins and sensors to any Matter platform (Apple Home, Google Home, Alexa, Home Assistant), driven over the agents' REST API.
- **`gpio-lab`** *(coming soon)*: Web dashboard for monitoring and controlling agents over REST API and MCP.
- **[`pico-bay`](https://github.com/ctrlpi/pico-bay)**: An App and MCP Server for managing Raspberry Pi Pico and ESP32 boards with MicroPython and CircuitPython over USB.

## License

[MIT](LICENSE)
