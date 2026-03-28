# Skuld

Skuld is a Python CLI for creating and managing local services with a registry.

The entrypoint `skuld` dispatches internally by platform:

- Linux: `skuld_linux.py`
- macOS: `skuld_macos.py`

It is designed to monitor only services that were created or explicitly adopted by Skuld.

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

AI can help you generate service files and commands faster, but operational clarity still needs a stable control plane.
Skuld provides that local control layer: explicit ownership, predictable lifecycle commands, and an auditable view of what you chose to run.

## Features

- Linux backend via `systemd`.
- macOS backend via `launchd`.
- Persist managed service metadata in a local JSON registry.
- Start, stop, restart, execute-now, inspect status, and read logs.
- Run `doctor` checks to detect registry/backend mismatches.
- Generate an equivalent `skuld create` command from an existing managed service with `skuld recreate`.
- Show CPU, memory, and listening ports in `skuld list`.

## Requirements

- Linux with `systemd` (`systemctl` + `journalctl`), or macOS with `launchd` (`launchctl`).
- Python 3.9+.
- `sudo` privileges for system-level unit/job installation or removal.

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

## Registry

Skuld stores managed services in:

`~/.local/share/skuld/services.json`

On macOS, Skuld stores managed services in:

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

On startup, Skuld normalizes this file automatically (canonical keys/order, pretty JSON, trailing newline, and valid unique IDs). This helps keep legacy or hand-edited registries consistent.

## Usage

### Create a service

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
  `start/stop/restart` act on `.timer` for scheduled jobs and on `.service` for daemons.
- macOS:
  `start/stop/restart` act on the `launchd` job itself.
- To run a scheduled job immediately, use `exec <name>`.

### List managed services

```bash
skuld
skuld list
```

- `skuld` (without subcommands) shows a compact view:
  `id | name | kind | service | timer | cpu | memory`
- `skuld list` shows the full view:
  `id | name | kind | service | timer | next_run | r/e | last_run | schedule | cpu | memory | ports`
- `ports` is resolved from all PIDs in the service cgroup (not only `MainPID`), so wrapper processes like `npm start` still show the app listening port.
- Both views include a top host panel with:
  `uptime | cpu(load1/5/15) | memory`
- `r/e` means `restarts/executions`.
- Table borders use Unicode automatically when supported by your terminal. You can override:
  - `skuld --ascii`
  - `skuld --unicode`

On macOS:

- `r/e` comes from Skuld's own event files.
- `next_run` is available only for the supported `--schedule` subset.
- `gpu` is omitted from the full table.

On Linux, to enable `r/e` collection every minute via `systemd` (outside the Skuld registry), run:

```bash
./scripts/install_runtime_stats_timer.sh --registry "$HOME/.local/share/skuld/services.json"
```

Running `skuld` without arguments shows a compact table: `id | name | kind | service | timer | cpu | memory`.

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

### Recreate command from a managed service

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
```

`adopt` is currently supported on Linux only.

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
