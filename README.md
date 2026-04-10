# Skuld

Skuld is a Python CLI for tracking local services, assigning human-friendly names, and giving you one operational view of what matters.

It is intentionally narrow:

- Skuld tracks services that already exist in `systemd` or `launchd`.
- Skuld does not create, edit, or remove service definitions anymore.
- Skuld operates only on services that you explicitly placed in its registry.

The entrypoint `./skuld` dispatches by platform:

- Linux: `skuld_linux.py`
- macOS: `skuld_macos.py`

## Named After

**Skuld** is one of the three Norns in Norse mythology, alongside **Urdr** (the past) and **Verdandi** (the present).
She is tied to the future: what is owed, what is bound to happen, what is next in the thread of events.

That symbolism maps directly to this project.

- A timer is a promise to the future.
- A scheduled service is an obligation waiting for its time.
- The registry is the ledger of what must still happen.

Skuld does not try to own everything in your machine.
It watches only what you intentionally place in its care, then keeps those services visible, operable, and auditable.

## Why Skuld in the AI era

AI can generate service files, timers, and `sudo` commands faster than ever.
Skuld does not need to be the author of those units.
Its value is the local control layer around what you chose to watch:

- friendly naming
- stable operational commands
- lightweight metrics
- one registry for the services you actually care about

## Features

- Linux backend via `systemd`.
- macOS backend via `launchd`.
- Persist tracked service metadata in a local JSON registry.
- Discover available services and track them from a catalog.
- Track existing services and assign a display alias.
- Start, stop, restart, execute-now, inspect status, and read logs.
- Run `doctor` checks to detect registry/backend mismatches.
- Run one-off privileged commands through `skuld sudo run -- ...`.
- Show CPU, memory, and listening ports in `skuld` and `skuld list`.

## Requirements

- Linux with `systemd` (`systemctl` + `journalctl`), or macOS with `launchd` (`launchctl`).
- Python 3.9+.
- `sudo` privileges when you want to inspect privileged resources or operate services that require elevation.

No external Python packages are required.

## Installation

```bash
git clone git@github.com:rod-americo/skuld.git
cd skuld
chmod +x ./skuld
```

Run from project root:

```bash
./skuld --help
```

`./skuld` dispatches to the internal backend automatically.

Optional: place it on your `PATH`.

```bash
sudo ln -s "$(pwd)/skuld" /usr/local/bin/skuld
```

## Security Note

You can set `SKULD_SUDO_PASSWORD` in `.env` (or environment variables), but this is not recommended for production systems.
When present, Skuld runs `sudo` non-interactively.

Lookup order:

1. `SKULD_SUDO_PASSWORD` from process environment
2. `SKULD_ENV_FILE` path (if set)
3. `.env` in current working directory
4. `.env` next to the `skuld` script
5. `~/.local/share/skuld/.env` (Linux) or `~/Library/Application Support/skuld/.env` (macOS)

To force regular interactive `sudo` and ignore env/.env password:

```bash
skuld --no-env-sudo <command> ...
```

Example `.env`:

```env
SKULD_SUDO_PASSWORD=your_password_here
```

For one-off agent usage, prefer:

```bash
skuld sudo check
skuld sudo run -- systemctl daemon-reload
```

This avoids exporting the password back into shell history or command output.

## Registry

Skuld stores tracked services in:

`~/.local/share/skuld/services.json`

On macOS, Skuld stores tracked services in:

`~/Library/Application Support/skuld/services.json`

Only services present in this file can be operated by:

- `exec`
- `start`
- `stop`
- `restart`
- `status`
- `logs`
- `describe`
- `sync --name <service>`
- `rename`
- `untrack`

On startup, Skuld normalizes this file automatically with canonical keys, stable ordering, pretty JSON, trailing newline, and valid unique IDs. This keeps hand-edited registries consistent.

## Usage

### Core commands

```bash
skuld
skuld list
skuld catalog
skuld track ...
skuld rename ...
skuld untrack ...
skuld exec ...
skuld start ...
skuld stop ...
skuld restart ...
skuld status ...
skuld logs ...
skuld stats ...
skuld describe ...
skuld doctor
skuld sync
skuld sudo check
skuld sudo run -- <command>
```

### Track an existing service

```bash
skuld track nginx
skuld track sshd.service --alias access-ssh
skuld track user:syncthing --alias sync-home
skuld catalog
skuld track 1 4 22
```

On Linux, `track` inspects the existing `.service` and optional same-name `.timer`, then stores:

- the real target name used by the backend
- the systemd scope used by the backend (`system` or `user`)
- a friendly `display_name` used by Skuld
- basic metadata used by `describe`, `stats`, and `doctor`

When there are no tracked services yet, `skuld` shows a numbered catalog from both `systemctl list-unit-files` and `systemctl --user list-unit-files`. You can also reopen that catalog later with `skuld catalog`.

If the same service name exists in both scopes, use an explicit target such as `system:nginx` or `user:nginx`.

On macOS, the first run without tracked services shows a numbered catalog from `launchctl list`. You can track from that catalog directly:

```bash
skuld
skuld catalog
skuld track 1 4 22
skuld track com.apple.Finder --alias finder
```

When `--alias` is omitted, Skuld asks interactively for a friendly name for each selected service. Press `Enter` to accept the suggested default.

### Rename a tracked service

```bash
skuld rename nginx edge-proxy
skuld rename 3 nightly-sync
```

### Untrack without touching the backend

```bash
skuld untrack edge-proxy
skuld untrack 3
```

If you need to create or edit units, do that outside Skuld, then track the result here.

Typical tracked service lifecycle:

```bash
skuld start --name my-daemon
skuld status --name my-daemon
skuld logs --name my-daemon --follow
skuld stop --name my-daemon
```

For scheduled jobs and daemons, action routing is automatic:

- Linux:
  `start/stop/restart` act on `.timer` only when the managed service has a real schedule and an installed `.timer` unit; otherwise they act on `.service`.
- macOS:
  `start/stop/restart` act on the `launchd` job itself.
- To run a scheduled job immediately, use `exec <name>`.

### List tracked services

```bash
skuld
skuld list
skuld --sort cpu
skuld list --sort memory
```

- `skuld` and `skuld list` show the same operational view:
  `id | name | service | timer | cpu | memory | ports`
- Both accept `--sort id|name|cpu|memory`. The default is `id`. `cpu` and `memory` sort descending.
- After operational commands like `track`, `rename`, `untrack`, `exec`, `start`, `stop`, `restart`, and `sync`, Skuld refreshes using the compact view.
- `ports` is resolved from all PIDs in the service cgroup (not only `MainPID`), so wrapper processes like `npm start` still show the app listening port.
- Both views include a top host panel with:
  `uptime | cpu(load1/5/15) | memory`
- Table borders use Unicode automatically when supported by your terminal. You can override:
  - `skuld --ascii`
  - `skuld --unicode`

On macOS:

- `ports` is `-` for jobs without listening sockets.

On Linux, to keep runtime execution counters fresh via `systemd` (outside the Skuld registry), run:

```bash
./scripts/install_runtime_stats_timer.sh --registry "$HOME/.local/share/skuld/services.json"
```

Running `skuld` without arguments shows: `id | name | service | timer | cpu | memory | ports`.

### Execute immediately

```bash
skuld exec --name my-job
skuld exec my-job
```

### Start/Stop/Restart

```bash
skuld start --name my-worker
skuld start my-worker
skuld start 2 4 5
skuld stop --name my-worker
skuld stop my-worker
skuld stop 2 4 5
skuld restart --name my-worker
skuld restart my-worker
skuld restart api-worker 7
```

### Logs

```bash
skuld logs --name my-worker --lines 200
skuld logs my-worker 200
skuld logs --name my-worker --follow
skuld logs --name my-job --timer --since "1 hour ago"
skuld logs 3 --plain
skuld logs 3 --output short-iso
```

On Linux, `--plain` uses `journalctl -o cat` (message only, no timestamp/host/process prefix).

On macOS, logs are file-based:

- `LaunchDaemon`: `/Library/Application Support/skuld/logs/<name>/stdout.log`
- `LaunchDaemon`: `/Library/Application Support/skuld/logs/<name>/stderr.log`
- `LaunchAgent`: `~/Library/Application Support/skuld/logs/<name>/stdout.log`
- `LaunchAgent`: `~/Library/Application Support/skuld/logs/<name>/stderr.log`

`--since` is currently Linux-only.

### Execution stats

On Linux, Skuld uses journal extraction to count service executions and `systemd` counters for restarts.

On macOS, Skuld uses its own event files.

```bash
skuld stats --name my-worker
skuld stats my-worker --since "24 hours ago"
skuld stats my-worker --boot
```

### Describe

```bash
skuld describe --name my-worker
skuld describe my-worker
```

### Doctor

```bash
skuld doctor
```

### Sync registry

```bash
skuld sync
skuld sync --name my-worker
skuld sync my-worker
```

## Command Help

```bash
skuld --help
```

## Project Docs

- `CONTRIBUTING.md`: contribution workflow and expectations
- `CHANGELOG.md`: notable changes

## License

This project is licensed under the MIT License.
See the `LICENSE` file for details.
