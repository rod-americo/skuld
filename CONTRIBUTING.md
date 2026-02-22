# Contributing to Skuld

Thanks for considering a contribution.

Skuld exists to reduce friction when managing Linux services and timers with `systemd`, while keeping operations explicit and auditable.

## Scope and Principles

- Keep the tool practical and production-oriented.
- Prefer clear behavior over magical automation.
- Preserve the core model:
  - daemon -> control `.service`
  - timer job -> control `.timer`
  - immediate run -> `exec` on `.service`
- Keep backward compatibility when possible.

## Local Development

```bash
git clone git@github.com:rod-americo/skuld.git
cd skuld
chmod +x ./skuld
python3 -m py_compile ./skuld
./skuld --help
```

## Pull Request Expectations

- One logical change per PR.
- Include:
  - purpose
  - key changes
  - exact test/validation commands used
  - behavior/risk notes (especially for start/stop/restart/edit/recreate)
- Update `README.md` when commands or UX behavior change.

## Coding Notes

- Python standard library preferred unless a new dependency is clearly necessary.
- Keep CLI messages and docs in English.
- Do not introduce machine-specific paths in docs (`/Users/...`, `/home/...` hardcoded examples).
- Avoid destructive actions by default.

