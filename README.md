# Skuld

Skuld is a Python CLI/TUI for creating and managing `systemd` services and timers with a local registry.

It is designed to monitor only services that were created or explicitly adopted by Skuld.

## Features

- Create `.service` units and optional `.timer` units.
- Persist managed service metadata in a local JSON registry.
- Start, stop, restart, execute-now, inspect status, and read logs via `journalctl`.
- Adopt existing `systemd` services into the Skuld registry.
- Run `doctor` checks to detect registry/unit mismatches.
- Lightweight terminal UI (`skuld tui`) for quick operations.

## Requirements

- Linux with `systemd` (`systemctl` + `journalctl`).
- Python 3.9+.
- `sudo` privileges for unit installation/removal.

No external Python packages are required.

## Installation

```bash
chmod +x /Users/rodrigo/Skuld/skuld
```

Optional: place it on your `PATH`.

```bash
sudo ln -s /Users/rodrigo/Skuld/skuld /usr/local/bin/skuld
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

### List managed services

```bash
skuld list
```

### Execute immediately

```bash
skuld exec --name my-job
```

### Start/Stop/Restart

```bash
skuld start --name my-worker
skuld stop --name my-worker
skuld restart --name my-worker
```

### Logs (`journalctl`)

```bash
skuld logs --name my-worker --lines 200
skuld logs --name my-worker --follow
skuld logs --name my-job --timer --since "1 hour ago"
```

### Describe

```bash
skuld describe --name my-worker
```

### Edit

```bash
skuld edit --name my-worker --exec "python /opt/app/new_worker.py"
skuld edit --name my-job --schedule "*-*-* 03:00:00"
skuld edit --name my-job --clear-schedule
```

### Adopt an existing service

```bash
skuld adopt --name existing-service
```

### Doctor

```bash
skuld doctor
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
- `e`: execute now
- `s`: start
- `t`: stop
- `R`: restart
- `d`: show description hint

## Command Help

```bash
skuld --help
```
