# Skuld

Skuld is a Python CLI for tracking local services, giving them human-friendly names, and exposing useful operational metrics.

The entrypoint `skuld` dispatches internally by platform:

- Linux: `skuld_linux.py`
- macOS: `skuld_macos.py`

It is designed to monitor only services that you explicitly track in the Skuld registry.

## Named After

**Skuld** is one of the three Norns in Norse mythology, alongside **Urdr** (the past) and **Verdandi** (the present).
She is tied to the future: what is owed, what is bound to happen, what is next in the thread of events.

That symbolism maps directly to this project.

- A timer is a promise to the future.
- A scheduled service is an obligation waiting for its time.
- The registry is the ledger of what must still happen.

Skuld does not try to manage everything in your system.
It watches only what you intentionally place in its care, then makes sure those future actions remain visible, repeatable, and accountable.

## Why Skuld in the AI era

AI can generate service files, timers, and `sudo` commands faster than ever.
Skuld does not need to be the author of those units anymore.
Its value is the local control layer around what you chose to watch: friendly naming, stable operational commands, and an auditable view of what matters.

## Features

- Linux backend via `systemd`.
- macOS backend via `launchd`.
- Persist tracked service metadata in a local JSON registry.
- Track existing services and assign a display alias.
- Start, stop, restart, execute-now, inspect status, and read logs.
- Run `doctor` checks to detect registry/backend mismatches.
- Run one-off privileged commands through `skuld sudo run -- ...`.
- Generate an equivalent `skuld create` command from an existing tracked service with `skuld recreate`.
- Show CPU, memory, and listening ports in `skuld list`.

## Requirements

- Linux with `systemd` (`systemctl` + `journalctl`), or macOS with `launchd` (`launchctl`).
- Python 3.9+.
- `sudo` privileges when you want to inspect privileged resources or perform system-level unit/job installation or removal.

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
- `remove`
- `describe`
- `edit`
- `sync --name <service>`
- `rename`
- `untrack`

On startup, Skuld normalizes this file automatically (canonical keys/order, pretty JSON, trailing newline, and valid unique IDs). This helps keep legacy or hand-edited registries consistent.

## Usage

### Track an existing service

```bash
skuld track nginx
skuld track sshd.service --alias access-ssh
skuld catalog
skuld track 1 4 22
```

On Linux, `track` inspects the existing `.service` and optional same-name `.timer`, then stores:

- the real target name used by the backend
- a friendly `display_name` used by Skuld
- basic metadata used by `describe`, `stats`, and `doctor`

When there are no tracked services yet, `skuld` shows a numbered catalog from `systemctl list-unit-files`. You can also reopen that catalog later with `skuld catalog`.

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

### Create a service

`create`, `edit`, `remove`, and `recreate` remain available as legacy commands during the transition, but they are no longer the primary model for Skuld.

```bash
skuld create \
  --name my-worker \
  --exec "python /opt/app/worker.py" \
  --working-dir /opt/app \
  --restart on-failure
```

On macOS, Skuld defaults to `--scope agent`, which maps to `LaunchAgent` and is the right default for user-owned services living under `~/...`.

To create a user-scoped job explicitly:

```bash
skuld create \
  --name my-user-job \
  --exec "python3 $(pwd)/job.py" \
  --scope agent
```

On Linux, when `--user <name>` is provided, Skuld writes `User=<name>` and also injects:

- `Environment="HOME=<user-home>"`
- `Environment="USER=<name>"`
- `Environment="LOGNAME=<name>"`
- `Environment="PATH=<user-home>/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"`

This avoids common runtime issues where services started by `systemd` miss user-scoped environment defaults and accidentally fall back to `/root`.

On macOS, `--user` is not supported. Prefer `--scope agent` for per-user services. Use `--scope daemon` only for system-level jobs that should run without a user identity.

### Create a scheduled service (timer)

```bash
skuld create \
  --name my-job \
  --exec "python /opt/app/job.py" \
  --schedule "*-*-* *:00/15:00" \
  --timer-persistent
```

### Timer schedules (`OnCalendar`) quick reference

On Linux, Skuld passes `--schedule` directly to `systemd` `OnCalendar`.

On macOS, Skuld accepts a documented subset and maps it to `launchd` `StartInterval` or `StartCalendarInterval`.

Common patterns:

- Every 15 minutes: `*-*-* *:00/15:00`
- Every hour at minute 05: `*-*-* *:05:00`
- Every day at 02:30: `*-*-* 02:30:00`
- Every Monday at 08:00: `Mon *-*-* 08:00:00`
- First day of each month at 00:01: `*-*-01 00:01:00`
- Specific date/time: `2026-03-15 14:00:00`

Supported on macOS:

- Every 15 minutes: `*-*-* *:00/15:00`
- Every hour at minute 05: `*-*-* *:05:00`
- Every day at 02:30: `*-*-* 02:30:00`
- Every Monday at 08:00: `Mon *-*-* 08:00:00`
- First day of each month at 00:01: `*-*-01 00:01:00`

Unsupported schedule expressions fail explicitly on macOS.

Useful checks:

```bash
systemd-analyze calendar "*-*-* *:00/15:00"
systemctl list-timers --all
```

If you run `skuld list` and see values like `*-*-02 00:01`, that means:

- any year
- any month
- day 2
- at `00:01`

So it runs monthly, on the 2nd day, at 00:01.

### Daemon mode (long-running services)

To run a service continuously, create it **without** `--schedule`.

- Linux: this creates only a `.service` unit (no `.timer`).
- macOS: this creates a `launchd` job without calendar or interval triggers.

```bash
skuld create \
  --name my-daemon \
  --exec "node /opt/app/server.js" \
  --working-dir /opt/app \
  --restart always
```

Typical daemon lifecycle:

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
- After operational commands like `track`, `rename`, `untrack`, `create`, `exec`, `start`, `stop`, `restart`, `remove`, `edit`, and `sync`, Skuld refreshes using the compact view.
- `ports` is resolved from all PIDs in the service cgroup (not only `MainPID`), so wrapper processes like `npm start` still show the app listening port.
- Both views include a top host panel with:
  `uptime | cpu(load1/5/15) | memory`
- Table borders use Unicode automatically when supported by your terminal. You can override:
  - `skuld --ascii`
  - `skuld --unicode`

On macOS:

- `ports` is `-` for jobs without listening sockets.

On Linux, to enable `r/e` collection every minute via `systemd` (outside the Skuld registry), run:

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

### Recreate command from a tracked service

```bash
skuld recreate --name my-worker
skuld recreate my-worker
skuld recreate 3
```

This prints an equivalent `skuld create ...` command based on current registry/backend data.

On macOS, the recreated command includes `--scope`.

### Edit

```bash
skuld edit --name my-worker --exec "python /opt/app/new_worker.py"
skuld edit my-worker --exec "python /opt/app/new_worker.py"
skuld edit --name my-job --schedule "*-*-* 03:00:00"
skuld edit --name my-job --clear-schedule
skuld edit my-job --schedule
skuld 3 --schedule
```

`skuld edit <name|id> --schedule` (or `skuld <name|id> --schedule`) opens an interactive prompt with the current schedule prefilled for editing. Press `Enter` to apply the resulting value.

### Adopt an existing service

```bash
skuld adopt --name existing-service
skuld adopt existing-service
skuld adopt existing-service --alias my-service
```

`adopt` is now a legacy alias for `track` on Linux only.

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

### Remove units/jobs

```bash
skuld remove --name my-worker
skuld remove --name my-worker --purge
```

`remove` is destructive. Prefer `untrack` when you only want to remove the Skuld mapping.

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
