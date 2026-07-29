# AGENTS.md — meross-integration

## Quick start

```bash
./dot.sh login            # 1. get Meross key from cloud (interactive)
./dot.sh discover         # 2. scan LAN -> UUIDs/IPs into bridge/config/devices.json
./dot.sh up               # 3. generate config.yml, bring up stack
```

## Commands

| Command | What |
|---------|------|
| `./dot.sh up` | clone bridge repo if missing, create `.env`, render config, `docker compose up` |
| `./dot.sh down` | `docker compose down` |
| `./dot.sh restart` | down + up (regenerates config.yml from template) |
| `./dot.sh logs [service]` | live logs (bridge, webapp, mosquitto) |
| `./dot.sh status` | healthcheck, config existence, homie topic dump |
| `./dot.sh render-config` | generate `bridge/config/config.yml` from template without Docker |
| `./dot.sh test unit` | `pytest` with default markers (~170 tests, no Docker, ~2s) |
| `./dot.sh test int` | bring up test stack + `pytest -m integration` (~15 tests, ~60s) |
| `./dot.sh test hw` | `pytest -m hardware` — touches real devices, opt-in |

## Test structure

- Tests always run **inside the test container** (`docker-compose.test.yml` service `tests`), never with host Python (macOS has 3.9, target is 3.12).
- `pytest.ini` defaults: `-m "not hardware and not integration"` — unit only by default.
- Integration tests bring up a separate Compose project (`gniazdka-test`) with broker, 3 fake devices, real bridge, real webapp. Fakes use `FAKE_DEVICE_KEY=testkey123`.
- Test webapp listens on `127.0.0.1:8081` (production: `:8080`). They conflict — same MQTT client ID `gniazdka-webapp`.
- Hardware tests: `HW_ALLOW_SWITCHING=1 HW_TARGET_UUID=... ./dot.sh test hw` to enable switching.

## Architecture

```
browser ──REST+SSE──> webapp ──homie/#──> mosquitto <──homie/#── bridge ──HTTP──> Meross plugs
                         │
                         └── SQLite (schedules)
```

- **`meross2mqtt/`** — cloned fork of `depau/meross2mqtt`, **never modify** (must stay `git pull`-able). In `.gitignore`. Auto-cloned by `./dot.sh up`.
- **`bridge/config/`** — `config.yml` (generated from `config.yml.tmpl`, **do not edit**), `devices.json` (from `discover` + bridge runtime).
- **`webapp/`** — FastAPI + APScheduler + vanilla JS frontend (no build step).
- **`CONTRACT.md`** — frozen interface spec (MQTT topics, REST API, DB schema, ENV vars). Source of truth for all integration points.

## Gotchas

- **`login` on Linux**: container runs as root, files become root-owned. `dot.sh` adds `--user "$(id -u):$(id -g)"` automatically. On macOS (Docker Desktop) this is not needed.
- **`config.yml` is regenerated** on every `./dot.sh up` / `./dot.sh restart` / `./dot.sh render-config`. Edit the template (`config.yml.tmpl`), not the output.
- **Broker on loopback only** (`127.0.0.1:1883`), no auth. Conscious design for home LAN.
- **Meross cloud needed only once** for key. After `login`, cloud can be disconnected. Plugs are polled via HTTP in LAN.
- **`persistence_file` must NOT be set** in config.yml — `yamldataclassconfig` expects `Path` type and crashes on `str`, causing restart loop. Default resolves to `/config/devices.json` which is correct.
- **`pretty_topic` must NOT be set** in bridge config — webapp identifies devices by UUID from `homie/+/$state`. Use `pretty_name` only.
- **MQTT boolean payloads** must be exact lowercase `true`/`false`. `True`, `1`, `on` are treated as `false` by Homie.
- **`power` name collision**: `switch/power` is boolean (on/off), `electricity/power` is float (watts).
- **SQLite is source of truth** for schedules. APScheduler jobs are rebuilt from DB on every webapp start.
- **Timezone** is `Europe/Warsaw` (from `.env` `TZ`). Critical for cron schedules around DST changes.
- **`dot.sh` targets bash 3.2** (macOS default). No associative arrays, no `${var,,}`.

## ENV (from `.env.example`)

`MEROSS_KEY` — key from cloud (filled by `login`). `LAN_SUBNET` — scan range (default `192.168.1.0/24`). `FAKE_DEVICE_KEY` — for test fakes (default `testkey123`). `POLLING_INTERVAL` — bridge poll interval (default `25`). `BRIDGE_LOG_LEVEL` — bridge log level (default `INFO`).

## Style & conventions

- All UI labels in Polish.
- `asyncio_mode = auto` in pytest.ini — tests can use `async def` directly.
- No `pretty_topic` in bridge config — UUID is the stable device identifier.
- `dot.sh` uses bash variable substitution (not `envsubst`/`sed`) for config.yml templating.
