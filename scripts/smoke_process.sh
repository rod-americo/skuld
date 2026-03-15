#!/bin/sh
set -eu

name="${1:-skuld-smoke-process}"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1"
}

cleanup() {
  log "$name received shutdown signal"
  exit 0
}

trap cleanup INT TERM

log "$name started (pid=$$)"

while :; do
  log "$name heartbeat"
  sleep 5
done
