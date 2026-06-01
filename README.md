# PAT Signage (DSM) — Digital Signage Display System

Edge-device firmware for **PAT digital signage** (DSM). It runs on a display node
(e.g. Raspberry Pi + HDMI screen), receives media tasks from the
`Dashobard-DSM` backend over MQTT, plays them on a schedule, and renders flood
**alerts** immediately when a sensor crosses a threshold.

Built on Django + paho-mqtt, managed by [uv](https://docs.astral.sh/uv/), and
driven by the `pat-sig` CLI (mirrors the `pat-smart` sensor firmware).

---

## Architecture

```
Dashobard-DSM (backend)                         PAT Signage device (this)
  pat-sig/{deviceId}/data    ──MQTT──▶  download media (preload, no playback)
  pat-sig/{deviceId}/alert   ──MQTT──▶  play NOW / clear  (override the screen)
  pat-sig/{deviceId}/status  ──MQTT──▶  status commands (SKIPING, ARCHIVED, ...)
  pat-sig/{deviceId}/action  ◀──MQTT──  device reports (downloaded/playing/
                                        requeue/completed) — durable outbox
```

- **Scheduler** plays `PUBLICRELATION` tasks inside their daily date/time window.
- **Alerts** (`ALERTHIGHT/MEDIUM/LOW`) take over the screen the moment `/alert`
  arrives and block PR tasks until cleared (priority gate, survives reboot).
- **Store-and-forward**: every report is persisted (`outbox_report`) and resent
  when the broker/backend comes back — nothing is lost on outage.

---

## Requirements

- Python ≥ 3.11
- [uv](https://docs.astral.sh/uv/)
- An MQTT broker reachable from the device (VerneMQ)
- Linux with systemd (for service install)

---

## Quick start

```bash
# one-shot installer (uv + deps + init + service)
./scripts/install.sh
```

Or step by step:

```bash
uv sync                 # install dependencies + the pat-sig entry point
uv run pat-sig init     # write .env (DEVICE_ID, DSM_ID, MQTT host/port/TLS)
uv run pat-sig install  # install the systemd service
uv run pat-sig start    # start it
uv run pat-sig status   # check it
```

---

## CLI

| Command | Description |
|---|---|
| `pat-sig init` | Write device config `.env` (DEVICE_ID, DSM_ID, MQTT settings) |
| `pat-sig run` | Run the display server (gunicorn, production). `--dev` uses Django's runserver; `--host`, `--port`, `--no-migrate` |
| `pat-sig install` | Install the `pat-sig` backend **and** `pat-sig-kiosk` (Chrome) systemd services (`--host`, `--port`, `--no-kiosk`) |
| `pat-sig start` | Start both services (backend + kiosk) |
| `pat-sig stop` | Stop both services |
| `pat-sig restart` | Restart both services |
| `pat-sig status [--kiosk]` | Show service status (add `--kiosk` for the kiosk) |
| `pat-sig logs [-f] [--kiosk]` | Show service logs (journalctl) |
| `pat-sig uninstall` | Stop, disable and remove both services |

### Kiosk (Chrome fullscreen)

`pat-sig install` also installs **`pat-sig-kiosk.service`** which launches Google
Chrome / Chromium in `--kiosk` mode pointing at the local display server
(`http://localhost:{port}/`). It:

- waits for the backend to be reachable before starting (`ExecStartPre`),
- runs in the graphical session (`DISPLAY=:0`),
- restarts automatically if Chrome is closed.

Requires a desktop/X server on the device and `google-chrome-stable` or
`chromium`. Skip it with `pat-sig install --no-kiosk`.

---

## Configuration

`pat-sig init` writes `src/pat_sig/module/dsm/.env`:

| Key | Example | Meaning |
|---|---|---|
| `DEVICE_ID` | `PAT-D1F5CBEF8D8` | Device id; defines the MQTT topic prefix `pat-sig/{DEVICE_ID}/…` |
| `DSM_ID` | `15ee984e-…` | The DSM (signage) UUID in the backend |
| `MQTT_BROKER` | `localhost` | Broker host |
| `MQTT_PORT` | `1883` | Broker port |
| `MQTT_TLS_ENABLED` | `false` | Enable TLS |

---

## Topics

| Topic | Direction | Purpose |
|---|---|---|
| `pat-sig/{id}/data` | backend → device | Pre-download task media (no playback) |
| `pat-sig/{id}/alert` | backend → device | Play alert now (`action:"play"`) / stop (`action:"clear"`) |
| `pat-sig/{id}/status` | backend → device | Set task status (e.g. `SKIPING`, `ARCHIVED`) |
| `pat-sig/{id}/action` | device → backend | Report `downloaded` / `playing` / `requeue` / `completed` |

---

## Production vs development server

`pat-sig run` serves the app with **gunicorn** (production WSGI). It is pinned
to **a single worker** on purpose: the MQTT client and the scheduler are started
once in `home/apps.py` `ready()`, so more than one worker would spawn duplicate
MQTT clients/schedulers and double-fire alerts and history rows.

```bash
uv run pat-sig run                      # gunicorn, --workers 1 (production)
uv run pat-sig run --dev                # Django runserver (local dev only)
uv run pat-sig run --host 0.0.0.0 --port 8000
```

For horizontal scaling you would move the MQTT client + scheduler into a
separate process (a management command) rather than `apps.ready()`, then run
gunicorn with multiple workers for HTTP only.

## Development

```bash
uv sync
uv run pat-sig run --dev --host 127.0.0.1 --port 8000   # migrate + runserver
```

The Django project lives in `src/pat_sig/module/dsm/` (SQLite local state). The
MQTT client and scheduler start automatically via `home/apps.py` `ready()`.
