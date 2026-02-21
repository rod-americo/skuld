# AGENTS.md

This file defines collaboration and coding behavior for contributors and AI agents working on Skuld.

## Project Personality

- Be direct, practical, and technical.
- Prefer clear implementation over abstract discussion.
- Keep communication concise and actionable.
- Explain tradeoffs when proposing changes.

## Engineering Principles

- Favor standard library solutions unless an external dependency is clearly justified.
- Keep the CLI stable and backward-compatible when possible.
- Treat `systemd` operations as high-impact: prefer explicit commands and clear errors.
- Preserve the rule: Skuld only manages services in its registry.

## Code Style

- Use English for:
  - Function names
  - Variable names
  - CLI argument names
  - Help text and user-facing messages
  - Comments and documentation
- Keep functions focused and small.
- Avoid unnecessary abstraction.
- Prefer explicit errors over silent failures.

## Safety Rules

- Do not run destructive commands without explicit user intent.
- Do not remove existing units or registry entries unless requested.
- Warn users when using `.env` sudo password support.
- Never log secrets.

## Testing Expectations

- Run syntax checks before finalizing changes:
  - `python3 -m py_compile ./skuld`
- Validate CLI interface changes with:
  - `./skuld --help`
  - `... <subcommand> --help` for new commands
- If `systemd` is unavailable in the environment, state this clearly.

## Docs Expectations

- Update `README.md` when commands or behavior change.
- Keep examples runnable and realistic.
- Prefer absolute paths in this repository's documentation examples.
