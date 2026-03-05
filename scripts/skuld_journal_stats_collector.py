#!/usr/bin/env python3
import argparse
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

SYSTEMD_UNIT_STARTED_MESSAGE_ID = "39f53479d3a045ac8e11786248231fbf"


def run_capture(cmd: List[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, check=False)


def read_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except Exception:
        return "-"


def read_boot_started_at() -> str:
    try:
        for raw in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if raw.startswith("btime "):
                epoch = int(raw.split()[1])
                return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except Exception:
        pass
    return "-"


def load_managed_names(registry_path: Path) -> List[str]:
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    names: List[str] = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return names


def read_restart_count(service_unit: str) -> int:
    proc = run_capture(["systemctl", "show", service_unit, "--no-pager", "-p", "NRestarts", "--value"])
    if proc.returncode != 0:
        return 0
    raw = (proc.stdout or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return 0
    return max(0, value)


def count_executions_since_boot(service_unit: str) -> int:
    proc = run_capture(
        [
            "journalctl",
            "-u",
            service_unit,
            "-b",
            f"MESSAGE_ID={SYSTEMD_UNIT_STARTED_MESSAGE_ID}",
            "-o",
            "json",
            "--no-pager",
        ]
    )
    if proc.returncode != 0:
        return 0
    return sum(1 for line in (proc.stdout or "").splitlines() if line.strip())


def collect(registry_path: Path) -> Dict[str, Dict[str, int]]:
    names = load_managed_names(registry_path)
    stats: Dict[str, Dict[str, int]] = {}
    for name in names:
        unit = f"{name}.service"
        stats[name] = {
            "restarts": read_restart_count(unit),
            "executions": count_executions_since_boot(unit),
        }
    return stats


def write_json_atomic(path: Path, payload: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), encoding="utf-8") as tf:
        json.dump(payload, tf, indent=2, ensure_ascii=False)
        tf.write("\n")
        tmp = Path(tf.name)
    tmp.chmod(0o644)
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Skuld restart/execution counters since last boot.")
    parser.add_argument("--registry", required=True, help="Path to skuld services registry JSON.")
    parser.add_argument("--output", default="/var/lib/skuld/journal_stats.json", help="Target stats JSON file.")
    args = parser.parse_args()

    registry_path = Path(args.registry)
    output_path = Path(args.output)
    stats = collect(registry_path)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "boot_id": read_boot_id(),
        "boot_started_at": read_boot_started_at(),
        "services": stats,
    }
    write_json_atomic(output_path, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
