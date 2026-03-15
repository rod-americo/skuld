#!/bin/sh
set -eu

name="${1:-skuld-smoke-trigger}"

printf '[%s] %s fired (pid=%s cwd=%s)\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" \
  "$name" \
  "$$" \
  "$(pwd)"
