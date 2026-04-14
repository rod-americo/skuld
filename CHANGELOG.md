# Changelog

All notable changes to this project are documented in this file.

The format is inspired by Keep a Changelog and follows semantic versioning intent.

## [Unreleased]

### Added

- `recreate` command to generate an equivalent `skuld create` command.
- TUI details panel with inline editing for `exec` and `schedule`.
- Stable numeric service IDs and id-based targeting.
- CPU and memory columns in `list` and TUI.

### Changed

- `start/stop/restart` now route actions by managed service type:
  - timer jobs act on `.timer`
  - daemons act on `.service`
- `list` output redesigned with table formatting and clearer status rendering.
- `list` now shows a human-readable `triggers` column and drops the redundant Linux `scope` column.
- `list` now sorts by `name` by default.
- `list` now adapts to terminal width by shortening flexible columns and hiding lower-priority ones when needed.
- Documentation now treats `systemd --user` as the default Linux persistence path and frames `sudo` as optional for `system` scope operations.

### Fixed

- ANSI-aware table alignment when colors are enabled.
- Cleaner shell quoting in `recreate` output.
