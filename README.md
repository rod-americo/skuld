# Skuld

Skuld is a Python CLI/TUI for creating and managing `systemd` services and timers with a local registry.

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

## Features

- Create `.service` units and optional `.timer` units.
- Persist managed service metadata in a local JSON registry.
- Start, stop, restart, execute-now, inspect status, and read logs via `journalctl`.
- Adopt existing `systemd` services into the Skuld registry.
- Run `doctor` checks to detect registry/unit mismatches.
- Backfill missing registry fields from systemd with `skuld sync`.
- Generate an equivalent `skuld create` command from an existing managed service with `skuld recreate`.
- Show CPU, memory, and listening ports in `skuld list` and in the TUI table.
- Lightweight terminal UI (`skuld tui`) for quick operations.

## Requirements

- Linux with `systemd` (`systemctl` + `journalctl`).
- Python 3.9+.
- `sudo` privileges for unit installation/removal.

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
5. `~/.local/share/skuld/.env`

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

## Usage

### Create a service

```bash
skuld create \
  --name my-worker \
  --exec "python /opt/app/worker.py" \
  --working-dir /opt/app \
  --restart on-failure
```

### Create a scheduled service (timer)

```bash
skuld create \
  --name my-job \
  --exec "python /opt/app/job.py" \
  --schedule "*-*-* *:00/15:00" \
  --timer-persistent
```

### Timer schedules (`OnCalendar`) quick reference

Skuld passes `--schedule` directly to `systemd` `OnCalendar`.

Common patterns:

- Every 15 minutes: `*-*-* *:00/15:00`
- Every hour at minute 05: `*-*-* *:05:00`
- Every day at 02:30: `*-*-* 02:30:00`
- Every Monday at 08:00: `Mon *-*-* 08:00:00`
- First day of each month at 00:01: `*-*-01 00:01:00`
- Specific date/time: `2026-03-15 14:00:00`

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
This creates only a `.service` unit (no `.timer`).

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

For timer jobs and daemons, action routing is automatic:

- Timer job (`.timer` exists): `start/stop/restart` act on `.timer`.
- Daemon (no timer): `start/stop/restart` act on `.service`.
- To run a timer job immediately, use `exec <timer-job>` (starts `.service` once).
- To pause future scheduled runs, use `stop <timer-job>`.
- To resume scheduled runs, use `start <timer-job>`.

### List managed services

```bash
skuld list
```

`skuld list` output includes: `id | name | kind | service | timer | schedule | cpu time | memory | ports`.

### Execute immediately

```bash
skuld exec --name my-job
skuld exec my-job
```

### Start/Stop/Restart

```bash
skuld start --name my-worker
skuld start my-worker
skuld stop --name my-worker
skuld stop my-worker
skuld restart --name my-worker
skuld restart my-worker
```

### Logs (`journalctl`)

```bash
skuld logs --name my-worker --lines 200
skuld logs my-worker 200
skuld logs --name my-worker --follow
skuld logs --name my-job --timer --since "1 hour ago"
skuld logs 3 --plain
skuld logs 3 --output short-iso
```

`--plain` uses `journalctl -o cat` (message only, no timestamp/host/process prefix).

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

This prints an equivalent `skuld create ...` command based on current registry/systemd data.

### Edit

```bash
skuld edit --name my-worker --exec "python /opt/app/new_worker.py"
skuld edit my-worker --exec "python /opt/app/new_worker.py"
skuld edit --name my-job --schedule "*-*-* 03:00:00"
skuld edit --name my-job --clear-schedule
```

### Adopt an existing service

```bash
skuld adopt --name existing-service
skuld adopt existing-service
```

### Doctor

```bash
skuld doctor
```

### Sync registry from systemd

```bash
skuld sync
skuld sync --name my-worker
skuld sync my-worker
```

### Remove units

```bash
skuld remove --name my-worker
skuld remove --name my-worker --purge
```

### TUI

```bash
skuld tui
```

TUI keys:

- `q`: quit
- `r`: refresh
- `j`/`k` or arrows: navigate
- `Enter`: open service details panel
- `e`: execute now
- `s`: start
- `t`: stop
- `R`: restart
- `d`: show description hint

Inside details panel:

- `q`: back to list
- `e`: edit `exec` command
- `c`: edit/clear `schedule`
- `r`: show `recreate` command
- `x`: execute immediately
- `s` / `t` / `R`: start, stop, restart

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
