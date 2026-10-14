# Running Mission Control in Docker

## Build

```bash
cd ~/agent-mission-control
docker compose build
```

Base image is `ubuntu:26.04` (matches this host's OS, not a generic
`python:slim`) with `python3.11` installed explicitly — the systemd unit
runs the real service under `python3.11` (`cgi`, used by `server.py`, was
removed from the stdlib in Python 3.13+), so the container pins the same
interpreter rather than trusting whatever `python3` resolves to.

## Run

```bash
docker compose up -d
# same as: systemctl --user status agent-mission-control
docker compose logs -f
```

Reach it exactly like the bare-metal service: `http://127.0.0.1:51763/`.

## Why `network_mode: host`

`server.py` hardcodes `HOST = "127.0.0.1"` on purpose (see its module
docstring) — this is an operator console for one machine, never meant to be
reachable off-box. A normal Docker bridge + `-p 51763:51763` would force the
app to listen on `0.0.0.0` *inside* the container for port publishing to
work, which quietly widens that guarantee. `network_mode: host` keeps the
container's `127.0.0.1` identical to the host's loopback, so the
localhost-only guarantee holds exactly as it does running `server.py`
directly. This only works on Linux hosts, which is what this is deployed on.

## Why `~/.hermes` is bind-mounted, not baked into the image

Two features shell out to the real Hermes install rather than doing
anything themselves:

- `POST /api/chat/send` spawns the `hermes` CLI for a live agent turn.
- The Kanban tab and the swdev-demo bridge (`kanban_bridge.py`,
  `swdev_demo_bridge.py`) spawn whichever Hermes venv python under
  `~/.hermes/installs/*/environments/` is newest, importing
  `hermes_cli.kanban_db` from it.

Vendoring a full Hermes install into the image would duplicate and drift
from the real one. Instead the image bind-mounts the host's `~/.hermes` in
at the identical path (`user: "1000:1000"` + `HOME=/home/hermes` in the
Dockerfile make `os.path.expanduser("~/.hermes")` resolve the same way it
does bare-metal) and executes the host's own venv binaries from inside the
container. This is a best-effort compatibility shim, not a hard guarantee:
if a future host OS upgrade or a Hermes venv with compiled C extensions
ever breaks under the container's glibc, that's this bind-mount strategy's
known failure mode — rebuild the image against the new host OS release.

## Known limitations vs. bare metal

- **FAOS desktop-activity probes** (`jv_watcher.py`: `xprintidle`, `xclip`,
  `wmctrl`) read the host's X session. There is none inside the container,
  so those FAOS panels degrade gracefully to "unknown" rather than erroring.
- Everything that reads Hermes/project state read-only (Overview, Agents,
  Builds, History, Schedule, Docs) works identically to bare metal, since
  it's the same code reading the same bind-mounted files.
