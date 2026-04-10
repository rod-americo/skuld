#!/usr/bin/env python3
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Set

VERSION = "0.3.0"
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._@-]*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SHELL_SAFE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
SKULD_HOME = Path(os.environ.get("SKULD_HOME", Path.home() / ".local/share/skuld"))
REGISTRY_FILE = SKULD_HOME / "services.json"
RUNTIME_STATS_FILE = Path(os.environ.get("SKULD_RUNTIME_STATS_FILE", "/var/lib/skuld/journal_stats.json"))
DEFAULT_ENV_FILE = Path(".env")
USE_ENV_SUDO = True
FORCE_TABLE_ASCII = False
FORCE_TABLE_UNICODE = False
SYSTEMD_UNIT_STARTED_MESSAGE_ID = "39f53479d3a045ac8e11786248231fbf"
SORT_CHOICES = ("id", "name", "cpu", "memory")
VALID_SCOPES = ("system", "user")
SCOPE_ALIASES = {"system": "system", "root": "system", "user": "user"}


@dataclass
class ManagedService:
    name: str
    scope: str
    exec_cmd: str
    description: str
    display_name: str = ""
    schedule: str = ""
    working_dir: str = ""
    user: str = ""
    restart: str = "on-failure"
    timer_persistent: bool = True
    id: int = 0


@dataclass
class DiscoverableService:
    index: int
    scope: str
    name: str
    service_state: str
    timer_state: str


def ensure_storage() -> None:
    SKULD_HOME.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_FILE.exists():
        REGISTRY_FILE.write_text("[]", encoding="utf-8")


def load_dotenv(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    env: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def get_sudo_password() -> Optional[str]:
    if not USE_ENV_SUDO:
        return None

    from_env = os.environ.get("SKULD_SUDO_PASSWORD")
    if from_env:
        return from_env

    env_path_override = os.environ.get("SKULD_ENV_FILE")
    candidate_files = []
    if env_path_override:
        candidate_files.append(Path(env_path_override))
    candidate_files.extend(
        [
            Path.cwd() / DEFAULT_ENV_FILE,
            Path(__file__).resolve().parent / DEFAULT_ENV_FILE,
            SKULD_HOME / ".env",
        ]
    )

    for env_path in candidate_files:
        if not env_path.exists():
            continue
        value = load_dotenv(env_path).get("SKULD_SUDO_PASSWORD")
        if value:
            return value
    return None


def normalize_scope(value: str) -> str:
    scope = (value or "system").strip().lower()
    normalized = SCOPE_ALIASES.get(scope)
    if not normalized:
        raise ValueError(f"Invalid scope '{value}'. Use 'system' or 'user'.")
    return normalized


def scope_sort_value(scope: str) -> int:
    return 0 if normalize_scope(scope) == "system" else 1


def managed_service_key(name: str, scope: str) -> tuple:
    return (normalize_scope(scope), name)


def managed_sort_key(service: ManagedService) -> tuple:
    return (service.name.lower(), scope_sort_value(service.scope), service.id)


def format_scoped_name(name: str, scope: str) -> str:
    return f"{normalize_scope(scope)}:{name}"


def split_scope_token(token: str) -> tuple[Optional[str], str]:
    raw = (token or "").strip()
    if ":" not in raw:
        return None, raw
    maybe_scope, remainder = raw.split(":", 1)
    normalized = SCOPE_ALIASES.get(maybe_scope.strip().lower())
    if not normalized or not remainder.strip():
        return None, raw
    return normalized, remainder.strip()


def load_registry() -> List[ManagedService]:
    ensure_storage()
    raw_text = REGISTRY_FILE.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid registry JSON at {REGISTRY_FILE}: {exc}") from exc
    if not isinstance(data, list):
        raise RuntimeError(f"Invalid registry format at {REGISTRY_FILE}: root must be an array.")

    services: List[ManagedService] = []
    changed = False
    known_keys = {
        "name",
        "scope",
        "exec_cmd",
        "description",
        "display_name",
        "schedule",
        "working_dir",
        "user",
        "restart",
        "timer_persistent",
        "id",
    }
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid registry entry #{idx}: expected object.")
        unknown = set(item.keys()) - known_keys
        if unknown:
            changed = True
        normalized = {
            "name": str(item.get("name", "")).strip(),
            "scope": normalize_scope(str(item.get("scope", "system"))),
            "exec_cmd": str(item.get("exec_cmd", "")).strip(),
            "description": str(item.get("description", "")).strip(),
            "display_name": str(item.get("display_name", item.get("name", ""))).strip(),
            "schedule": str(item.get("schedule", "")).strip(),
            "working_dir": str(item.get("working_dir", "")).strip(),
            "user": str(item.get("user", "")).strip(),
            "restart": str(item.get("restart", "on-failure")).strip() or "on-failure",
            "timer_persistent": parse_bool(str(item.get("timer_persistent", True))),
            "id": parse_int(str(item.get("id", 0))),
        }
        if not normalized["name"] or not normalized["exec_cmd"] or not normalized["description"]:
            raise RuntimeError(
                f"Invalid registry entry #{idx}: fields 'name', 'exec_cmd' and 'description' are required."
            )
        validate_name(normalized["name"])
        validate_name(normalized["display_name"] or normalized["name"])
        if not normalized["display_name"]:
            normalized["display_name"] = normalized["name"]
        if normalized != {k: item.get(k) for k in known_keys if k in item}:
            changed = True
        services.append(ManagedService(**normalized))

    used_ids = set()
    next_id = 1
    for svc in services:
        if svc.id <= 0 or svc.id in used_ids:
            while next_id in used_ids:
                next_id += 1
            svc.id = next_id
            changed = True
        used_ids.add(svc.id)

    display_names = set()
    for svc in services:
        if svc.display_name in display_names:
            raise RuntimeError(f"Duplicate display name in registry: '{svc.display_name}'.")
        display_names.add(svc.display_name)

    normalized_services = sorted(services, key=managed_sort_key)
    if normalized_services != services:
        changed = True
    canonical_text = json.dumps([asdict(s) for s in normalized_services], indent=2, ensure_ascii=False) + "\n"
    if changed or raw_text != canonical_text:
        REGISTRY_FILE.write_text(canonical_text, encoding="utf-8")
    return normalized_services


def save_registry(services: List[ManagedService]) -> None:
    ensure_storage()
    ordered = sorted(services, key=managed_sort_key)
    REGISTRY_FILE.write_text(json.dumps([asdict(s) for s in ordered], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def upsert_registry(service: ManagedService) -> None:
    services = load_registry()
    service_key = managed_service_key(service.name, service.scope)
    for existing_service in services:
        if existing_service.display_name != service.display_name:
            continue
        existing_key = managed_service_key(existing_service.name, existing_service.scope)
        if existing_service.id == service.id or existing_key == service_key:
            continue
        raise RuntimeError(f"Display name '{service.display_name}' is already in use.")
    by_name = {managed_service_key(s.name, s.scope): s for s in services}
    existing = by_name.get(service_key)
    if service.id <= 0 and existing:
        service.id = existing.id
    if service.id <= 0:
        max_id = max((s.id for s in services), default=0)
        service.id = max_id + 1
    by_name[service_key] = service
    save_registry(sorted(by_name.values(), key=managed_sort_key))


def remove_registry(name: str, scope: str) -> None:
    key = managed_service_key(name, scope)
    services = [s for s in load_registry() if managed_service_key(s.name, s.scope) != key]
    save_registry(services)


def find_managed_by_name(name: str) -> List[ManagedService]:
    return [svc for svc in load_registry() if svc.name == name]


def get_managed(name: str, scope: Optional[str] = None) -> Optional[ManagedService]:
    matches = find_managed_by_name(name)
    if scope is not None:
        normalized_scope = normalize_scope(scope)
        for svc in matches:
            if svc.scope == normalized_scope:
                return svc
        return None
    if len(matches) == 1:
        return matches[0]
    return None


def get_managed_by_display_name(display_name: str) -> Optional[ManagedService]:
    for svc in load_registry():
        if svc.display_name == display_name:
            return svc
    return None


def get_managed_by_id(service_id: int) -> Optional[ManagedService]:
    for svc in load_registry():
        if svc.id == service_id:
            return svc
    return None


def require_managed(name: str, scope: Optional[str] = None) -> ManagedService:
    svc = get_managed(name, scope=scope)
    if not svc:
        raise RuntimeError(
            f"'{name}' is not in the skuld registry. "
            "Only services tracked by skuld can be monitored."
        )
    return svc


def err(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)


def info(msg: str) -> None:
    print(f"[skuld] {msg}")


def ok(msg: str) -> None:
    print(f"[ok] {msg}")


def is_tty() -> bool:
    return sys.stdout.isatty()


def supports_unicode_output() -> bool:
    if FORCE_TABLE_ASCII:
        return False
    if FORCE_TABLE_UNICODE:
        return True
    if not is_tty():
        return False
    term = (os.environ.get("TERM") or "").strip().lower()
    if term == "dumb":
        return False
    encoding = (sys.stdout.encoding or "").upper()
    if "UTF-8" in encoding or "UTF8" in encoding:
        return True
    locale_text = " ".join(
        [
            os.environ.get("LC_ALL", ""),
            os.environ.get("LC_CTYPE", ""),
            os.environ.get("LANG", ""),
        ]
    ).upper()
    return "UTF-8" in locale_text or "UTF8" in locale_text


def colorize(text: str, color: str) -> str:
    if not is_tty():
        return text
    palette = {
        "green": "\033[32m",
        "red": "\033[31m",
        "yellow": "\033[33m",
        "cyan": "\033[36m",
        "gray": "\033[90m",
        "reset": "\033[0m",
    }
    return f"{palette.get(color, '')}{text}{palette['reset']}"


def visible_len(text: str) -> int:
    return len(ANSI_RE.sub("", text))


def parse_first_float(text: str) -> float:
    match = re.search(r"\d+(?:\.\d+)?", ANSI_RE.sub("", text or ""))
    if not match:
        return -1.0
    try:
        return float(match.group(0))
    except ValueError:
        return -1.0


def service_sort_key(sort_by: str, row: Dict[str, object]) -> tuple:
    if sort_by == "name":
        return (str(row["name"]).lower(), int(row["id"]))
    if sort_by == "cpu":
        return (-parse_first_float(str(row["cpu"])), str(row["name"]).lower(), int(row["id"]))
    if sort_by == "memory":
        return (-parse_first_float(str(row["memory"])), str(row["name"]).lower(), int(row["id"]))
    return (int(row["id"]),)


def resolve_sort_arg(args: Optional[argparse.Namespace]) -> str:
    sort_by = getattr(args, "sort", "id") if args is not None else "id"
    return sort_by if sort_by in SORT_CHOICES else "id"


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError("Invalid name. Use [a-zA-Z0-9._@-] and start with a letter/number.")


def ensure_display_name_available(display_name: str, current_id: Optional[int] = None) -> None:
    validate_name(display_name)
    for svc in load_registry():
        if svc.display_name != display_name:
            continue
        if current_id is not None and svc.id == current_id:
            return
        raise RuntimeError(f"Display name '{display_name}' is already in use.")


def normalize_service_name(value: str) -> str:
    raw = (value or "").strip()
    if raw.endswith(".service"):
        raw = raw[:-8]
    elif raw.endswith(".timer"):
        raw = raw[:-6]
    validate_name(raw)
    return raw


def normalize_target_token(value: str) -> tuple[Optional[str], str]:
    scope, raw_name = split_scope_token(value)
    return scope, normalize_service_name(raw_name)


def suggest_display_name(value: str) -> str:
    raw = normalize_service_name(value)
    tokens = [part for part in raw.split(".") if part]
    if len(tokens) >= 2 and tokens[-1].isdigit():
        while tokens and tokens[-1].isdigit():
            tokens.pop()
    if len(tokens) >= 2:
        return "-".join(tokens[-2:])
    return raw


def prompt_display_name(target: str, suggested: str) -> str:
    if not sys.stdin.isatty():
        return suggested
    value = input(f"Display name for {target} [{suggested}]: ").strip()
    chosen = value or suggested
    validate_name(chosen)
    return chosen


def resolve_name_arg(args: argparse.Namespace, required: bool = True) -> Optional[str]:
    positional = getattr(args, "name", None)
    flag_value = getattr(args, "name_flag", None)
    if positional and flag_value and positional != flag_value:
        raise RuntimeError(f"Conflicting names provided: positional='{positional}' and --name='{flag_value}'.")
    name = flag_value or positional
    if required and not name:
        raise RuntimeError("Service name is required. Use NAME or --name NAME.")
    return name


def resolve_managed_from_token(token: str) -> Optional[ManagedService]:
    raw_token = (token or "").strip()
    svc = get_managed_by_display_name(raw_token)
    if svc:
        return svc
    if raw_token.isdigit():
        return get_managed_by_id(int(raw_token))
    scope, name = normalize_target_token(raw_token)
    if scope is not None:
        return get_managed(name, scope=scope)
    matches = find_managed_by_name(name)
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = ", ".join(format_scoped_name(item.name, item.scope) for item in sorted(matches, key=managed_sort_key))
        raise RuntimeError(
            f"Managed service '{name}' is ambiguous across scopes. "
            f"Use an id, display name, or one of: {choices}."
        )
    return None


def resolve_managed_arg(args: argparse.Namespace, required: bool = True) -> Optional[ManagedService]:
    positional = getattr(args, "name", None)
    name_flag = getattr(args, "name_flag", None)
    id_flag = getattr(args, "id_flag", None)

    if positional and name_flag and positional != name_flag:
        raise RuntimeError(
            f"Conflicting targets provided: positional='{positional}' and --name='{name_flag}'."
        )

    token = name_flag or positional
    by_token = None
    if token:
        by_token = resolve_managed_from_token(token)
        if not by_token:
            raise RuntimeError(f"Managed service '{token}' not found (name or id).")

    by_id = None
    if id_flag is not None:
        by_id = get_managed_by_id(id_flag)
        if not by_id:
            raise RuntimeError(f"Managed service id '{id_flag}' not found.")

    if by_token and by_id and by_token.id != by_id.id:
        raise RuntimeError(
            f"Conflicting targets provided: '{token}' resolves to id={by_token.id}, "
            f"but --id={id_flag}."
        )

    svc = by_id or by_token
    if required and not svc:
        raise RuntimeError("Service target is required. Use NAME/ID, --name NAME, or --id ID.")
    return svc


def resolve_managed_many_arg(args: argparse.Namespace) -> List[ManagedService]:
    positional_tokens = getattr(args, "targets", None) or []
    name_flag = getattr(args, "name_flag", None)
    id_flag = getattr(args, "id_flag", None)

    tokens: List[str] = list(positional_tokens)
    if name_flag:
        tokens.append(name_flag)
    if id_flag is not None:
        tokens.append(str(id_flag))

    if not tokens:
        raise RuntimeError("At least one service target is required. Use NAME/ID, --name NAME, or --id ID.")

    resolved: List[ManagedService] = []
    seen_ids = set()
    for token in tokens:
        svc = resolve_managed_from_token(token)
        if not svc:
            raise RuntimeError(f"Managed service '{token}' not found (name or id).")
        if svc.id in seen_ids:
            continue
        seen_ids.add(svc.id)
        resolved.append(svc)
    return resolved


def resolve_lines_arg(args: argparse.Namespace, default: int = 100) -> int:
    lines_flag = getattr(args, "lines", None)
    lines_pos = getattr(args, "lines_pos", None)
    if lines_flag is not None:
        return lines_flag
    if lines_pos is not None:
        return lines_pos
    return default


def run(cmd: List[str], check: bool = True, capture: bool = False, input_text: Optional[str] = None) -> subprocess.CompletedProcess:
    kwargs = {"text": True}
    if capture:
        kwargs["capture_output"] = True
    if input_text is not None:
        kwargs["input"] = input_text
    proc = subprocess.run(cmd, **kwargs)
    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(shlex.quote(c) for c in cmd)}")
    return proc


def run_sudo(cmd: List[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    pwd = get_sudo_password()
    full = ["sudo"] + cmd
    if pwd:
        full = ["sudo", "-S", "-k", "-p", ""] + cmd
        return run(full, check=check, capture=capture, input_text=pwd + "\n")
    return run(full, check=check, capture=capture)


def warn_env_sudo_usage() -> None:
    if get_sudo_password():
        info("Warning: using SKULD_SUDO_PASSWORD from env/.env. Keep this for short-lived local use only.")


def sudo_check(_args: argparse.Namespace) -> None:
    warn_env_sudo_usage()
    password = get_sudo_password()
    if password:
        proc = run(["sudo", "-S", "-k", "-p", "", "true"], check=False, capture=True, input_text=password + "\n")
    else:
        proc = run(["sudo", "-n", "true"], check=False, capture=True)
    if proc.returncode == 0:
        ok("sudo is available.")
        return
    details = (proc.stderr or proc.stdout or "").strip()
    raise RuntimeError(f"sudo is not available non-interactively. {details}".strip())


def sudo_run_command(args: argparse.Namespace) -> None:
    command = list(args.command or [])
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RuntimeError("Use: skuld sudo run -- <command> [args...]")
    warn_env_sudo_usage()
    proc = run_sudo(command, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"sudo command failed with exit code {proc.returncode}.")


def journal_permission_hint(stderr_text: str) -> bool:
    lower = stderr_text.lower()
    return (
        "not seeing messages from other users and the system" in lower
        or "permission denied" in lower
    )


def require_systemctl() -> None:
    try:
        run(["systemctl", "--version"], check=True, capture=True)
    except Exception as exc:
        raise RuntimeError("systemctl not found. This tool requires Linux with systemd.") from exc


def systemctl_command(scope: str, args: List[str]) -> List[str]:
    cmd = ["systemctl"]
    if normalize_scope(scope) == "user":
        cmd.append("--user")
    return cmd + args


def journalctl_command(scope: str, args: List[str]) -> List[str]:
    cmd = ["journalctl"]
    if normalize_scope(scope) == "user":
        cmd.append("--user")
    return cmd + args


def run_systemctl_action(scope: str, args: List[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = systemctl_command(scope, args)
    if normalize_scope(scope) == "system":
        return run_sudo(cmd, check=check, capture=capture)
    return run(cmd, check=check, capture=capture)


def unit_exists(unit: str, scope: str = "system") -> bool:
    show = systemctl_show(unit, ["LoadState"], scope=scope)
    load_state = (show.get("LoadState", "") or "").strip().lower()
    return bool(load_state and load_state != "not-found")


def unit_active(unit: str, scope: str = "system") -> str:
    proc = run(systemctl_command(scope, ["is-active", unit]), check=False, capture=True)
    status = (proc.stdout or "").strip()
    return status if status else "inactive"


def display_unit_state(status: str) -> str:
    if status == "activating":
        return "running"
    return status


def format_bytes(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw in ("[not set]", "n/a"):
        return "-"
    try:
        num = int(raw)
    except ValueError:
        return "-"
    if num < 0:
        return "-"
    size_gb = num / (1024.0 ** 3)
    return f"{size_gb:.2f}GB"


def format_cpu_nsec(value: str) -> str:
    raw = (value or "").strip()
    if not raw or raw in ("[not set]", "n/a"):
        return "-"
    try:
        nsec = int(raw)
    except ValueError:
        return "-"
    if nsec < 0:
        return "-"
    seconds = nsec / 1_000_000_000
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = int(seconds // 60)
    rem = seconds - (minutes * 60)
    if minutes < 60:
        return f"{minutes}m{rem:.0f}s"
    hours = int(minutes // 60)
    rem_min = minutes % 60
    return f"{hours}h{rem_min}m"


def format_duration_human(seconds: int) -> str:
    if seconds < 0:
        return "-"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours:02d}h {minutes:02d}m"
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def read_host_overview() -> Dict[str, str]:
    uptime = "-"
    try:
        raw = Path("/proc/uptime").read_text(encoding="utf-8").split()[0]
        uptime = format_duration_human(int(float(raw)))
    except Exception:
        pass

    cpu = "-"
    try:
        load1, load5, load15 = os.getloadavg()
        cores = max(1, os.cpu_count() or 1)
        load_pct = int((load1 / cores) * 100)
        cpu = f"{load1:.2f} {load5:.2f} {load15:.2f} ({load_pct}%)"
    except Exception:
        pass

    memory = "-"
    try:
        meminfo: Dict[str, int] = {}
        for raw in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if ":" not in raw:
                continue
            key, val = raw.split(":", 1)
            tokens = val.strip().split()
            if not tokens:
                continue
            meminfo[key.strip()] = int(tokens[0]) * 1024  # kB -> bytes
        total = meminfo.get("MemTotal", 0)
        available = meminfo.get("MemAvailable", 0)
        used = max(0, total - available)
        if total > 0:
            pct = int((used / total) * 100)
            memory = f"{format_bytes(str(used))}/{format_bytes(str(total))} ({pct}%)"
    except Exception:
        pass

    return {
        "uptime": uptime,
        "cpu(load1/5/15)": cpu,
        "memory": memory,
    }


def parse_int(value: str) -> int:
    try:
        num = int((value or "").strip())
    except ValueError:
        return 0
    return num if num > 0 else 0


def read_proc_cpu_nsec(pid: int) -> Optional[int]:
    if pid <= 0:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").strip()
        end = stat.rfind(")")
        if end < 0:
            return None
        fields = stat[end + 2 :].split()
        if len(fields) < 13:
            return None
        utime_ticks = int(fields[11])
        stime_ticks = int(fields[12])
        clk_tck = int(os.sysconf("SC_CLK_TCK"))
        if clk_tck <= 0:
            return None
        total_nsec = int(((utime_ticks + stime_ticks) / clk_tck) * 1_000_000_000)
        return total_nsec if total_nsec >= 0 else None
    except Exception:
        return None


def read_proc_memory_bytes(pid: int) -> Optional[int]:
    if pid <= 0:
        return None
    try:
        for raw in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if not raw.startswith("VmRSS:"):
                continue
            tokens = raw.split()
            if len(tokens) < 2:
                return None
            value_kb = int(tokens[1])
            if value_kb < 0:
                return None
            return value_kb * 1024
    except Exception:
        return None
    return None


def format_gpu_mib(value: int) -> str:
    if value <= 0:
        return "0MB"
    if value < 1024:
        return f"{value}MB"
    gib = value / 1024
    text = f"{gib:.1f}".rstrip("0").rstrip(".")
    return f"{text}GB"


def read_gpu_memory_by_pid() -> Optional[Dict[int, int]]:
    cmd = [
        "nvidia-smi",
        "--query-compute-apps=pid,used_gpu_memory",
        "--format=csv,noheader,nounits",
    ]
    try:
        proc = run(cmd, check=False, capture=True)
    except FileNotFoundError:
        return None
    if proc.returncode != 0:
        return None

    by_pid: Dict[int, int] = {}
    output = (proc.stdout or "").strip()
    if not output:
        return by_pid

    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",", 1)]
        if len(parts) != 2:
            continue
        pid = parse_int(parts[0])
        if pid <= 0:
            continue
        try:
            used_mib = int(parts[1])
        except ValueError:
            continue
        if used_mib < 0:
            continue
        by_pid[pid] = by_pid.get(pid, 0) + used_mib
    return by_pid


def read_unit_usage(service_unit: str, scope: str = "system", gpu_memory_by_pid: Optional[Dict[int, int]] = None) -> Dict[str, str]:
    if not unit_exists(service_unit, scope=scope):
        return {"cpu": "-", "memory": "-", "gpu": "-"}
    show = systemctl_show(service_unit, ["CPUUsageNSec", "MemoryCurrent", "MainPID"], scope=scope)
    pid = parse_int(show.get("MainPID", ""))
    gpu_usage = "-"
    if gpu_memory_by_pid is not None:
        gpu_usage = format_gpu_mib(gpu_memory_by_pid.get(pid, 0) if pid > 0 else 0)
    cpu_usage = format_cpu_nsec(show.get("CPUUsageNSec", ""))
    if cpu_usage == "-" and pid > 0:
        proc_cpu_nsec = read_proc_cpu_nsec(pid)
        if proc_cpu_nsec is not None:
            cpu_usage = format_cpu_nsec(str(proc_cpu_nsec))
    memory_usage = format_bytes(show.get("MemoryCurrent", ""))
    if memory_usage == "-" and pid > 0:
        proc_memory = read_proc_memory_bytes(pid)
        if proc_memory is not None:
            memory_usage = format_bytes(str(proc_memory))
    return {
        "cpu": cpu_usage,
        "memory": memory_usage,
        "gpu": gpu_usage,
    }


def get_main_pid(service_unit: str, scope: str = "system") -> int:
    if not unit_exists(service_unit, scope=scope):
        return 0
    show = systemctl_show(service_unit, ["MainPID"], scope=scope)
    return parse_int(show.get("MainPID", ""))


def read_unit_pids(service_unit: str, scope: str = "system") -> List[int]:
    if not unit_exists(service_unit, scope=scope):
        return []
    show = systemctl_show(service_unit, ["MainPID", "ControlGroup"], scope=scope)
    pids: Set[int] = set()
    main_pid = parse_int(show.get("MainPID", ""))
    if main_pid > 0:
        pids.add(main_pid)

    control_group = (show.get("ControlGroup", "") or "").strip()
    if not control_group or control_group == "/":
        return sorted(pids)

    cg_rel = control_group[1:] if control_group.startswith("/") else control_group
    candidates = [
        Path("/sys/fs/cgroup") / cg_rel / "cgroup.procs",
        Path("/sys/fs/cgroup/systemd") / cg_rel / "cgroup.procs",
        Path("/sys/fs/cgroup") / cg_rel / "tasks",
        Path("/sys/fs/cgroup/systemd") / cg_rel / "tasks",
    ]
    for candidate in candidates:
        try:
            for raw in candidate.read_text(encoding="utf-8").splitlines():
                pid = parse_int(raw)
                if pid > 0:
                    pids.add(pid)
            if pids:
                break
        except Exception:
            continue
    return sorted(pids)


def parse_listen_ports_from_ss(output: str, pids: Set[int]) -> List[str]:
    ports: List[str] = []
    seen = set()
    if not pids:
        return ports
    for line in output.splitlines():
        match = re.search(r"pid=(\d+),", line)
        if not match:
            continue
        pid = parse_int(match.group(1))
        if pid not in pids:
            continue
        parts = line.split()
        if len(parts) < 6:
            continue
        local = parts[4]
        proto = parts[0].lower()
        if proto not in ("tcp", "tcp6", "udp", "udp6"):
            continue
        port = ""
        if "[" in local and "]:" in local:
            # [::]:8000
            port = local.rsplit("]:", 1)[-1]
        elif ":" in local:
            # 0.0.0.0:8000 or *:53
            port = local.rsplit(":", 1)[-1]
        if not port or not port.isdigit():
            continue
        proto_tag = "tcp" if "tcp" in proto else "udp"
        tag = f"{port}/{proto_tag}"
        if tag not in seen:
            seen.add(tag)
            ports.append(tag)
    return sorted(ports)


def summarize_ports(ports: List[str], max_items: int = 2) -> str:
    if not ports:
        return "-"
    if len(ports) <= max_items:
        return ",".join(ports)
    shown = ",".join(ports[:max_items])
    return f"{shown}+{len(ports) - max_items}"


def read_socket_inodes_for_pid(pid: int) -> Set[str]:
    inodes: Set[str] = set()
    if pid <= 0:
        return inodes
    fd_dir = Path(f"/proc/{pid}/fd")
    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except Exception:
                continue
            if target.startswith("socket:[") and target.endswith("]"):
                inodes.add(target[8:-1])
    except Exception:
        return set()
    return inodes


def parse_proc_net_ports(path: Path, proto: str, socket_inodes: Set[str]) -> Set[str]:
    tags: Set[str] = set()
    if not socket_inodes:
        return tags
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return tags
    for raw in lines[1:]:
        parts = raw.split()
        if len(parts) < 10:
            continue
        local = parts[1]
        state = parts[3]
        inode = parts[9]
        if inode not in socket_inodes:
            continue
        if proto == "tcp" and state != "0A":
            continue
        if ":" not in local:
            continue
        _, port_hex = local.split(":", 1)
        try:
            port = int(port_hex, 16)
        except ValueError:
            continue
        if port <= 0:
            continue
        tags.add(f"{port}/{proto}")
    return tags


def read_unit_ports_from_proc(pid: int) -> List[str]:
    inodes = read_socket_inodes_for_pid(pid)
    if not inodes:
        return []
    tags: Set[str] = set()
    tags.update(parse_proc_net_ports(Path(f"/proc/{pid}/net/tcp"), "tcp", inodes))
    tags.update(parse_proc_net_ports(Path(f"/proc/{pid}/net/tcp6"), "tcp", inodes))
    tags.update(parse_proc_net_ports(Path(f"/proc/{pid}/net/udp"), "udp", inodes))
    tags.update(parse_proc_net_ports(Path(f"/proc/{pid}/net/udp6"), "udp", inodes))
    return sorted(tags)


def read_unit_ports_from_proc_pids(pids: List[int]) -> List[str]:
    tags: Set[str] = set()
    for pid in pids:
        tags.update(read_unit_ports_from_proc(pid))
    return sorted(tags)


def read_unit_ports(service_unit: str, scope: str = "system") -> str:
    pids = read_unit_pids(service_unit, scope=scope)
    if not pids:
        return "-"

    cmd = ["ss", "-ltnup"]
    output = ""
    try:
        proc = run(cmd, check=False, capture=True)
        output = (proc.stdout or "")
    except FileNotFoundError:
        output = ""
    ports = parse_listen_ports_from_ss(output, set(pids))
    if not ports:
        ports = read_unit_ports_from_proc_pids(pids)

    # Some systems omit process ownership details without sudo but do not emit
    # an explicit permission error. If no port is detected for a live PID,
    # retry with sudo.
    needs_sudo = not ports
    if needs_sudo:
        try:
            proc = run_sudo(cmd, check=False, capture=True)
            output = (proc.stdout or "")
            ports = parse_listen_ports_from_ss(output, set(pids))
        except Exception:
            ports = []

    return summarize_ports(ports)


def render_table(headers: List[str], rows: List[List[str]]) -> None:
    if not rows:
        return
    widths = [visible_len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], visible_len(cell))

    unicode_box = supports_unicode_output()
    if unicode_box:
        box = {
            "top_left": "╭",
            "top_mid": "┬",
            "top_right": "╮",
            "mid_left": "├",
            "mid_mid": "┼",
            "mid_right": "┤",
            "bottom_left": "╰",
            "bottom_mid": "┴",
            "bottom_right": "╯",
            "vertical": "│",
            "fill": "─",
        }
    else:
        box = {
            "top_left": "+",
            "top_mid": "+",
            "top_right": "+",
            "mid_left": "+",
            "mid_mid": "+",
            "mid_right": "+",
            "bottom_left": "+",
            "bottom_mid": "+",
            "bottom_right": "+",
            "vertical": "|",
            "fill": "-",
        }

    def hline(left: str, middle: str, right: str, fill: str) -> str:
        return left + middle.join(fill * (w + 2) for w in widths) + right

    def format_row(cells: List[str]) -> str:
        padded = []
        for i, cell in enumerate(cells):
            pad = widths[i] - visible_len(cell)
            padded.append(f" {cell}{' ' * max(0, pad)} ")
        return box["vertical"] + box["vertical"].join(padded) + box["vertical"]

    print(hline(box["top_left"], box["top_mid"], box["top_right"], box["fill"]))
    print(format_row(headers))
    print(hline(box["mid_left"], box["mid_mid"], box["mid_right"], box["fill"]))
    for row in rows:
        print(format_row(row))
    print(hline(box["bottom_left"], box["bottom_mid"], box["bottom_right"], box["fill"]))


def render_host_panel() -> None:
    overview = read_host_overview()
    render_table(list(overview.keys()), [list(overview.values())])
    print()


def load_runtime_stats() -> Dict[str, Dict[str, int]]:
    try:
        data = json.loads(RUNTIME_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    services = data.get("services")
    if not isinstance(services, dict):
        return {}

    normalized: Dict[str, Dict[str, int]] = {}
    for name, item in services.items():
        if not isinstance(name, str) or not isinstance(item, dict):
            continue
        executions = parse_int(str(item.get("executions", 0)))
        restarts = parse_int(str(item.get("restarts", 0)))
        normalized[name] = {
            "executions": max(0, executions),
            "restarts": max(0, restarts),
        }
    return normalized


def format_restarts_exec(name: str, runtime_stats: Dict[str, Dict[str, int]]) -> str:
    item = runtime_stats.get(name)
    if not item:
        return "-"
    return f"{item.get('restarts', 0)}/{item.get('executions', 0)}"


def clip_text(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def read_timer_schedule(name: str, scope: str = "system") -> str:
    timer_unit = f"{name}.timer"
    show = systemctl_show(timer_unit, ["OnCalendar"], scope=scope)
    schedule = (show.get("OnCalendar", "") or "").strip()
    if schedule:
        return schedule
    directives = parse_unit_directives(systemctl_cat(timer_unit, scope=scope))
    return (directives.get("OnCalendar", "") or "").strip()


def read_timer_persistent(name: str, scope: str = "system", default: bool = True) -> bool:
    timer_unit = f"{name}.timer"
    if not unit_exists(timer_unit, scope=scope):
        return default
    show = systemctl_show(timer_unit, ["Persistent"], scope=scope)
    value = (show.get("Persistent", "") or "").strip()
    if value:
        return parse_bool(value, default=default)
    directives = parse_unit_directives(systemctl_cat(timer_unit, scope=scope))
    raw = (directives.get("Persistent", "") or "").strip()
    if raw:
        return parse_bool(raw, default=default)
    return default


def read_timer_next_run(name: str, scope: str = "system") -> str:
    timer_unit = f"{name}.timer"
    show = systemctl_show(timer_unit, ["NextElapseUSecRealtime"], scope=scope)
    value = (show.get("NextElapseUSecRealtime", "") or "").strip()
    if not value or value.lower() == "n/a":
        return "-"
    return value


def read_timer_last_run(name: str, scope: str = "system") -> str:
    timer_unit = f"{name}.timer"
    show = systemctl_show(timer_unit, ["LastTriggerUSec"], scope=scope)
    value = (show.get("LastTriggerUSec", "") or "").strip()
    if not value or value.lower() == "n/a":
        return "-"
    return value


def schedule_for_display(svc: ManagedService) -> str:
    if svc.schedule:
        return svc.schedule
    return read_timer_schedule(svc.name, scope=svc.scope)


def sync_registry_from_systemd(target: Optional[ManagedService] = None) -> int:
    services = load_registry()
    changed = 0
    target_key = managed_service_key(target.name, target.scope) if target else None
    updated: List[ManagedService] = []

    for svc in services:
        if target_key and managed_service_key(svc.name, svc.scope) != target_key:
            updated.append(svc)
            continue

        new_svc = ManagedService(**asdict(svc))
        svc_unit = f"{svc.name}.service"
        timer_unit = f"{svc.name}.timer"

        if unit_exists(svc_unit, scope=svc.scope):
            show_svc = systemctl_show(svc_unit, ["Description", "WorkingDirectory", "User", "Restart"], scope=svc.scope)
            if not new_svc.description and show_svc.get("Description"):
                new_svc.description = show_svc["Description"]
            if not new_svc.working_dir and show_svc.get("WorkingDirectory"):
                new_svc.working_dir = show_svc["WorkingDirectory"]
            if not new_svc.user and show_svc.get("User"):
                new_svc.user = show_svc["User"]
            if (not new_svc.restart or new_svc.restart == "on-failure") and show_svc.get("Restart"):
                new_svc.restart = show_svc["Restart"]

        if unit_exists(timer_unit, scope=svc.scope):
            if not new_svc.schedule:
                new_svc.schedule = read_timer_schedule(svc.name, scope=svc.scope)
            new_svc.timer_persistent = read_timer_persistent(
                svc.name,
                scope=svc.scope,
                default=new_svc.timer_persistent,
            )

        if asdict(new_svc) != asdict(svc):
            changed += 1
            updated.append(new_svc)
        else:
            updated.append(svc)

    if changed:
        save_registry(updated)
    return changed


def systemctl_show(unit: str, props: List[str], scope: str = "system") -> Dict[str, str]:
    cmd = systemctl_command(scope, ["show", unit, "--no-pager"])
    for p in props:
        cmd.extend(["-p", p])
    proc = run(cmd, check=False, capture=True)
    result: Dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        result[k] = v
    return result


def systemctl_cat(unit: str, scope: str = "system") -> str:
    proc = run(systemctl_command(scope, ["cat", unit, "--no-pager"]), check=False, capture=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def list_discoverable_services_for_scope(scope: str) -> List[DiscoverableService]:
    proc = run(
        systemctl_command(scope, ["list-unit-files", "--type=service", "--type=timer", "--no-legend", "--no-pager"]),
        check=False,
        capture=True,
    )
    if proc.returncode != 0:
        return []
    discovered: Dict[str, Dict[str, str]] = {}
    for raw in (proc.stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        unit_name = parts[0].strip()
        state = parts[1].strip()
        if unit_name.endswith(".service"):
            base_name = unit_name[:-8]
            kind = "service"
        elif unit_name.endswith(".timer"):
            base_name = unit_name[:-6]
            kind = "timer"
        else:
            continue
        if not NAME_RE.match(base_name):
            continue
        entry = discovered.setdefault(base_name, {"service": "", "timer": ""})
        entry[kind] = state

    names = sorted(name for name, states in discovered.items() if states.get("service"))
    result: List[DiscoverableService] = []
    for name in names:
        states = discovered[name]
        result.append(
            DiscoverableService(
                index=0,
                scope=normalize_scope(scope),
                name=name,
                service_state=states.get("service", "") or "-",
                timer_state=states.get("timer", "") or "n/a",
            )
        )
    return result


def list_discoverable_services() -> List[DiscoverableService]:
    require_systemctl()
    entries = list_discoverable_services_for_scope("system") + list_discoverable_services_for_scope("user")
    entries.sort(key=lambda item: (item.name.lower(), scope_sort_value(item.scope)))
    for idx, entry in enumerate(entries, start=1):
        entry.index = idx
    return entries


def render_discoverable_services_hint(empty_registry_note: bool = True) -> None:
    if empty_registry_note:
        print("No services tracked by skuld.")
    entries = list_discoverable_services()
    if not entries:
        print("No systemd services were found.")
        return
    print("Available systemd services (system + user):")
    for entry in entries:
        print(
            f"  {entry.index}. [{entry.scope}] {entry.name}  "
            f"service={entry.service_state}  timer={entry.timer_state}"
        )
    print()
    print("Use: skuld track <id ...>, skuld track <service ...>, or skuld track <system:name|user:name ...>")


def resolve_discoverable_target_by_name(name: str, scope: Optional[str], entries: List[DiscoverableService]) -> DiscoverableService:
    matches = [entry for entry in entries if entry.name == name and (scope is None or entry.scope == scope)]
    known_scopes = {entry.scope for entry in matches}
    if scope is None:
        for candidate_scope in VALID_SCOPES:
            if candidate_scope in known_scopes:
                continue
            if unit_exists(f"{name}.service", scope=candidate_scope):
                matches.append(
                    DiscoverableService(
                        index=0,
                        scope=candidate_scope,
                        name=name,
                        service_state="-",
                        timer_state="n/a",
                    )
                )
    elif not matches and unit_exists(f"{name}.service", scope=scope):
        matches.append(
            DiscoverableService(
                index=0,
                scope=scope,
                name=name,
                service_state="-",
                timer_state="n/a",
            )
        )

    if not matches:
        if scope is not None:
            raise RuntimeError(f"Service '{name}.service' does not exist in the {scope} systemd catalog.")
        raise RuntimeError(f"Service '{name}.service' does not exist in systemd.")
    if len(matches) > 1:
        scopes = ", ".join(format_scoped_name(name, item.scope) for item in sorted(matches, key=lambda item: scope_sort_value(item.scope)))
        raise RuntimeError(f"Service '{name}' exists in multiple scopes. Use one of: {scopes}.")
    return matches[0]


def resolve_discoverable_targets(targets: List[str]) -> List[DiscoverableService]:
    entries = list_discoverable_services()
    by_index = {entry.index: entry for entry in entries}
    resolved: List[DiscoverableService] = []
    seen: Set[tuple] = set()
    for raw_target in targets:
        token = (raw_target or "").strip()
        if not token:
            continue
        entry: Optional[DiscoverableService]
        if token.isdigit():
            entry = by_index.get(int(token))
            if not entry:
                raise RuntimeError(f"Catalog id '{token}' not found.")
        else:
            scope, name = normalize_target_token(token)
            entry = resolve_discoverable_target_by_name(name, scope, entries)
        key = (entry.scope, entry.name)
        if key in seen:
            continue
        seen.add(key)
        resolved.append(entry)
    if not resolved:
        raise RuntimeError("Use: skuld track <id ...>, skuld track <service ...>, or skuld track <system:name|user:name ...>")
    return resolved


def parse_unit_directives(unit_text: str) -> Dict[str, str]:
    directives: Dict[str, str] = {}
    for raw in unit_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        directives[k] = v
    return directives


def parse_bool(value: str, default: bool = True) -> bool:
    if value is None:
        return default
    v = value.strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def _render_services_table(compact: bool, sort_by: str = "id") -> None:
    require_systemctl()
    sync_registry_from_systemd()
    services = list(load_registry())
    if not services:
        render_discoverable_services_hint()
        return

    rows: List[Dict[str, object]] = []
    gpu_memory_by_pid = read_gpu_memory_by_pid()
    runtime_stats = load_runtime_stats()
    print()
    render_host_panel()
    for svc in services:
        s_unit = f"{svc.name}.service"
        t_unit = f"{svc.name}.timer"
        s_state_raw = unit_active(s_unit, scope=svc.scope) if unit_exists(s_unit, scope=svc.scope) else "missing"
        t_state_raw = unit_active(t_unit, scope=svc.scope) if unit_exists(t_unit, scope=svc.scope) else "n/a"
        s_state_display = display_unit_state(s_state_raw)
        t_state_display = display_unit_state(t_state_raw)
        schedule = schedule_for_display(svc) or "-"
        usage = read_unit_usage(s_unit, scope=svc.scope, gpu_memory_by_pid=gpu_memory_by_pid)
        if s_state_raw == "active":
            s_state = colorize("active", "green")
        elif s_state_raw == "activating":
            s_state = colorize(s_state_display, "green")
        elif s_state_raw == "inactive":
            s_state = colorize("inactive", "yellow")
        else:
            s_state = colorize(s_state_display, "red")
        if t_state_raw == "active":
            t_state = colorize("active", "green")
        elif t_state_raw == "activating":
            t_state = colorize(t_state_display, "green")
        elif t_state_raw == "inactive":
            t_state = colorize("inactive", "yellow")
        elif t_state_raw == "n/a":
            t_state = colorize("n/a", "gray")
        else:
            t_state = colorize(t_state_display, "red")
        rows.append(
            {
                "id": svc.id,
                "name": svc.display_name,
                "scope": svc.scope,
                "service": s_state,
                "timer": t_state,
                "cpu": usage["cpu"],
                "memory": usage["memory"],
                "ports": read_unit_ports(s_unit, scope=svc.scope),
            }
        )
    ordered_rows = sorted(rows, key=lambda row: service_sort_key(sort_by, row))
    render_table(
        ["id", "name", "scope", "service", "timer", "cpu", "memory", "ports"],
        [
            [
                str(row["id"]),
                str(row["name"]),
                str(row["scope"]),
                str(row["service"]),
                str(row["timer"]),
                str(row["cpu"]),
                str(row["memory"]),
                str(row["ports"]),
            ]
            for row in ordered_rows
        ],
    )
    print()


def list_services(args: argparse.Namespace) -> None:
    _render_services_table(compact=False, sort_by=resolve_sort_arg(args))


def list_services_compact(sort_by: str = "id") -> None:
    _render_services_table(compact=True, sort_by=sort_by)


def catalog(_args: argparse.Namespace) -> None:
    render_discoverable_services_hint(empty_registry_note=False)


def exec_now(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    require_systemctl()
    run_systemctl_action(svc.scope, ["start", f"{name}.service"])
    ok(f"Execution started: {name}.service ({svc.display_name}, scope={svc.scope})")


def managed_has_schedule(svc: ManagedService) -> bool:
    return bool((svc.schedule or "").strip())


def timer_unit_exists(name: str, scope: str = "system") -> bool:
    return unit_exists(f"{name}.timer", scope=scope)


def managed_uses_timer(svc: ManagedService) -> bool:
    return managed_has_schedule(svc) and timer_unit_exists(svc.name, scope=svc.scope)


def apply_action_for_managed(svc: ManagedService, action: str) -> None:
    name = svc.name
    service_unit = f"{name}.service"
    timer_unit = f"{name}.timer"
    uses_timer = managed_uses_timer(svc)

    if uses_timer:
        proc = run_systemctl_action(svc.scope, [action, timer_unit], check=False, capture=True)
        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip()
            msg = f"Failed to {action} {timer_unit}."
            if details:
                msg = f"{msg} {details}"
            raise RuntimeError(msg)
        ok(f"{action} -> {timer_unit} ({svc.scope})")
        return

    proc = run_systemctl_action(svc.scope, [action, service_unit], check=False, capture=True)
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        msg = f"Failed to {action} {service_unit}."
        if details:
            msg = f"{msg} {details}"
        raise RuntimeError(msg)
    ok(f"{action} -> {service_unit} ({svc.scope})")


def start_stop(args: argparse.Namespace, action: str) -> None:
    services = resolve_managed_many_arg(args)
    require_systemctl()
    for svc in services:
        apply_action_for_managed(svc, action)


def restart(args: argparse.Namespace) -> None:
    start_stop(args, "restart")


def status(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    require_systemctl()
    print(f"[skuld] {svc.display_name} -> {format_scoped_name(name, svc.scope)}")
    run(systemctl_command(svc.scope, ["status", f"{name}.service", "--no-pager"]), check=False)
    run(systemctl_command(svc.scope, ["status", f"{name}.timer", "--no-pager"]), check=False)


def logs(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    lines = resolve_lines_arg(args, default=100)
    require_systemctl()
    unit = f"{name}.timer" if args.timer else f"{name}.service"
    cmd = journalctl_command(svc.scope, ["-u", unit, "-n", str(lines)])
    output_mode = "cat" if args.plain else args.output
    cmd.extend(["-o", output_mode])
    if args.since:
        cmd.extend(["--since", args.since])
    if args.follow:
        cmd.append("-f")
        # For streaming mode, avoid capture so output is shown in real time.
        probe_cmd = [c for c in cmd if c != "-f"] + ["-n", "1", "--no-pager"]
        probe = run(probe_cmd, check=False, capture=True)
        probe_err = (probe.stderr or "").lower()
        needs_sudo = svc.scope == "system" and (
            "not seeing messages from other users and the system" in probe_err
            or "permission denied" in probe_err
        )
        if needs_sudo:
            run_sudo(cmd, check=False)
        else:
            run(cmd, check=False)
        return
    else:
        cmd.append("--no-pager")
    proc = run(cmd, check=False, capture=True)
    stderr = (proc.stderr or "").strip()
    stdout = (proc.stdout or "").strip()

    permission_hint = svc.scope == "system" and journal_permission_hint(stderr)
    if permission_hint:
        proc = run_sudo(cmd, check=False, capture=True)
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()

    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)


def count_unit_starts(unit: str, scope: str = "system", since: Optional[str] = None, boot: bool = False) -> int:
    cmd = journalctl_command(
        scope,
        [
            "-u",
            unit,
            f"MESSAGE_ID={SYSTEMD_UNIT_STARTED_MESSAGE_ID}",
            "-o",
            "json",
            "--no-pager",
        ],
    )
    if since:
        cmd.extend(["--since", since])
    if boot:
        cmd.append("-b")

    proc = run(cmd, check=False, capture=True)
    stderr = (proc.stderr or "").strip()
    stdout = proc.stdout or ""
    if normalize_scope(scope) == "system" and journal_permission_hint(stderr):
        proc = run_sudo(cmd, check=False, capture=True)
        stderr = (proc.stderr or "").strip()
        stdout = proc.stdout or ""

    lines = [line for line in stdout.splitlines() if line.strip()]
    return len(lines)


def read_restart_count(name: str, scope: str = "system") -> str:
    service_unit = f"{name}.service"
    if not unit_exists(service_unit, scope=scope):
        return "-"
    show = systemctl_show(service_unit, ["NRestarts"], scope=scope)
    raw = (show.get("NRestarts", "") or "").strip()
    return raw if raw else "-"


def stats(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    require_systemctl()
    sync_registry_from_systemd(svc)
    service_unit = f"{name}.service"
    executions = count_unit_starts(service_unit, scope=svc.scope, since=args.since, boot=args.boot)
    restarts = read_restart_count(name, scope=svc.scope)

    print(f"name: {svc.display_name}")
    print(f"target: {format_scoped_name(name, svc.scope)}")
    print(f"scope: {svc.scope}")
    print(f"service_unit: {service_unit}")
    if args.boot:
        print("window: current boot")
    elif args.since:
        print(f"window: since {args.since}")
    else:
        print("window: all retained journal entries")
    print(f"executions: {executions}")
    print(f"restarts: {restarts}")


def track(args: argparse.Namespace) -> None:
    require_systemctl()
    targets = list(args.targets or [])
    if not targets:
        raise RuntimeError("Use: skuld track <id ...>, skuld track <service ...>, or skuld track <system:name|user:name ...>")
    if args.alias and len(targets) != 1:
        raise RuntimeError("--alias can only be used when tracking exactly one service.")

    resolved = resolve_discoverable_targets(targets)
    for entry in resolved:
        name = entry.name
        suggested = suggest_display_name(name)
        target_label = format_scoped_name(name, entry.scope)
        alias = (args.alias or prompt_display_name(target_label, suggested)).strip()
        ensure_display_name_available(alias)
        if get_managed(name, scope=entry.scope):
            raise RuntimeError(f"'{target_label}' is already tracked in skuld.")

        service_unit = f"{name}.service"
        timer_unit = f"{name}.timer"
        service_text = systemctl_cat(service_unit, scope=entry.scope)
        directives = parse_unit_directives(service_text)
        exec_line = directives.get("ExecStart", "")
        if exec_line.startswith("/bin/bash -lc "):
            exec_line = exec_line[len("/bin/bash -lc "):].strip()
            if len(exec_line) >= 2 and exec_line[0] == exec_line[-1] and exec_line[0] in ("'", '"'):
                exec_line = exec_line[1:-1]
        if not exec_line:
            exec_line = service_unit

        show_service = systemctl_show(
            service_unit,
            ["Description", "WorkingDirectory", "User", "Restart"],
            scope=entry.scope,
        )
        schedule = ""
        timer_persistent = True
        if unit_exists(timer_unit, scope=entry.scope):
            show_timer = systemctl_show(timer_unit, ["OnCalendar", "Persistent"], scope=entry.scope)
            schedule = show_timer.get("OnCalendar", "") or ""
            timer_persistent = parse_bool(show_timer.get("Persistent", "true"), default=True)

        upsert_registry(
            ManagedService(
                name=name,
                scope=entry.scope,
                exec_cmd=exec_line,
                description=show_service.get("Description", f"Tracked service: {name}"),
                display_name=alias,
                schedule=schedule,
                working_dir=show_service.get("WorkingDirectory", "") or "",
                user=show_service.get("User", "") or "",
                restart=show_service.get("Restart", "on-failure") or "on-failure",
                timer_persistent=timer_persistent,
            )
        )
        ok(f"Tracked '{target_label}' as '{alias}'.")


def rename(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    new_name = (args.new_name or "").strip()
    ensure_display_name_available(new_name, current_id=svc.id)
    if svc.display_name == new_name:
        info("No changes detected.")
        return
    upsert_registry(
        ManagedService(
            name=svc.name,
            scope=svc.scope,
            exec_cmd=svc.exec_cmd,
            description=svc.description,
            display_name=new_name,
            schedule=svc.schedule,
            working_dir=svc.working_dir,
            user=svc.user,
            restart=svc.restart,
            timer_persistent=svc.timer_persistent,
            id=svc.id,
        )
    )
    ok(f"Renamed '{svc.display_name}' to '{new_name}'.")


def untrack(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    remove_registry(svc.name, svc.scope)
    ok(f"Removed '{svc.display_name}' from the skuld registry.")


def doctor(_args: argparse.Namespace) -> None:
    require_systemctl()
    sync_registry_from_systemd()
    services = load_registry()
    if not services:
        print("No services tracked by skuld.")
        return

    issues = 0
    for svc in services:
        service_unit = f"{svc.name}.service"
        timer_unit = f"{svc.name}.timer"
        line_prefix = f"[{svc.display_name}|{format_scoped_name(svc.name, svc.scope)}]"

        if not unit_exists(service_unit, scope=svc.scope):
            print(f"{line_prefix} ERROR missing service unit ({service_unit})")
            issues += 1
        else:
            st = unit_active(service_unit, scope=svc.scope)
            print(f"{line_prefix} service={display_unit_state(st)}")

        has_timer = bool(svc.schedule)
        runtime_schedule = read_timer_schedule(svc.name, scope=svc.scope)
        if not has_timer and runtime_schedule:
            print(
                f"{line_prefix} WARN registry schedule is empty, "
                f"but timer OnCalendar is '{runtime_schedule}'"
            )
            issues += 1
        if has_timer and not unit_exists(timer_unit, scope=svc.scope):
            print(f"{line_prefix} ERROR expected timer is missing ({timer_unit})")
            issues += 1
        if (not has_timer) and unit_exists(timer_unit, scope=svc.scope):
            print(f"{line_prefix} WARN timer exists, but registry has no schedule")
            issues += 1

        if unit_exists(service_unit, scope=svc.scope):
            cat = parse_unit_directives(systemctl_cat(service_unit, scope=svc.scope))
            current_exec = cat.get("ExecStart", "")
            if svc.exec_cmd and svc.exec_cmd not in current_exec:
                print(f"{line_prefix} WARN ExecStart differs from registry")
                issues += 1

    if issues == 0:
        ok("doctor: no issues found.")
    else:
        err(f"doctor: found {issues} issue(s).")


def describe(args: argparse.Namespace) -> None:
    target = resolve_managed_arg(args)
    name = target.name
    require_systemctl()
    sync_registry_from_systemd(target)
    svc = require_managed(name, scope=target.scope)
    service_unit = f"{name}.service"
    timer_unit = f"{name}.timer"

    show_service = systemctl_show(
        service_unit,
        ["Id", "Description", "ActiveState", "SubState", "FragmentPath", "MainPID"],
        scope=svc.scope,
    )
    show_timer = systemctl_show(
        timer_unit,
        ["Id", "ActiveState", "SubState", "NextElapseUSecRealtime", "LastTriggerUSec"],
        scope=svc.scope,
    ) if unit_exists(timer_unit, scope=svc.scope) else {}

    print(f"name: {svc.display_name}")
    print(f"target: {format_scoped_name(svc.name, svc.scope)}")
    print(f"scope: {svc.scope}")
    print(f"description: {svc.description}")
    print(f"exec: {svc.exec_cmd}")
    print(f"working_dir: {svc.working_dir or '-'}")
    print(f"user: {svc.user or '-'}")
    print(f"restart: {svc.restart}")
    print(f"schedule: {svc.schedule or '-'}")
    print(f"timer_persistent: {svc.timer_persistent}")
    print("---")
    print(f"service_active: {show_service.get('ActiveState', 'unknown')}")
    print(f"service_substate: {show_service.get('SubState', 'unknown')}")
    print(f"main_pid: {show_service.get('MainPID', '-')}")
    print(f"fragment: {show_service.get('FragmentPath', '-')}")
    if show_timer:
        print(f"timer_active: {show_timer.get('ActiveState', 'unknown')}")
        print(f"timer_substate: {show_timer.get('SubState', 'unknown')}")
        print(f"next_run: {show_timer.get('NextElapseUSecRealtime', '-')}")
        print(f"last_trigger: {show_timer.get('LastTriggerUSec', '-')}")
    else:
        print("timer: n/a")


def sync(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args, required=False)
    require_systemctl()
    changed = sync_registry_from_systemd(svc)
    if changed == 0:
        ok("Registry is already up to date.")
    else:
        ok(f"Registry updated for {changed} service(s).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skuld", description="CLI for tracking and operating systemd services")
    p.add_argument(
        "--no-env-sudo",
        action="store_true",
        help="Disable SKULD_SUDO_PASSWORD from env/.env and use regular sudo behavior",
    )
    p.add_argument("--ascii", action="store_true", help="Force ASCII table borders")
    p.add_argument("--unicode", action="store_true", help="Force Unicode table borders")
    p.add_argument("--sort", choices=SORT_CHOICES, default="id", help="Sort service views by id, name, cpu, or memory")
    sub = p.add_subparsers(dest="command", required=False)

    l = sub.add_parser("list", help="List services tracked by skuld")
    l.add_argument("--sort", choices=SORT_CHOICES, default="id", help="Sort by id, name, cpu, or memory")
    l.set_defaults(func=list_services)

    ct = sub.add_parser("catalog", help="Show the current system + user systemd discovery catalog")
    ct.set_defaults(func=catalog)

    tr = sub.add_parser("track", help="Track systemd services from the current catalog or by service name")
    tr.add_argument(
        "targets",
        nargs="+",
        help="Catalog ids or service names (example: 1 4 nginx sshd.service user:syncthing)",
    )
    tr.add_argument("--alias", help="Friendly name shown by skuld")
    tr.set_defaults(func=track)

    rn = sub.add_parser("rename", help="Change the display name of a tracked service")
    rn.add_argument("name", nargs="?")
    rn.add_argument("new_name")
    rn.add_argument("--name", dest="name_flag")
    rn.add_argument("--id", dest="id_flag", type=int)
    rn.set_defaults(func=rename)

    ut = sub.add_parser("untrack", help="Remove a service from the skuld registry without touching systemd")
    ut.add_argument("name", nargs="?")
    ut.add_argument("--name", dest="name_flag")
    ut.add_argument("--id", dest="id_flag", type=int)
    ut.set_defaults(func=untrack)

    e = sub.add_parser("exec", help="Execute a service immediately")
    e.add_argument("name", nargs="?")
    e.add_argument("--name", dest="name_flag")
    e.add_argument("--id", dest="id_flag", type=int)
    e.set_defaults(func=exec_now)

    s = sub.add_parser("start", help="Start one or more services")
    s.add_argument("targets", nargs="*", help="Service target(s): managed NAME and/or ID")
    s.add_argument("--name", dest="name_flag")
    s.add_argument("--id", dest="id_flag", type=int)
    s.set_defaults(func=lambda a: start_stop(a, "start"))

    st = sub.add_parser("stop", help="Stop one or more services")
    st.add_argument("targets", nargs="*", help="Service target(s): managed NAME and/or ID")
    st.add_argument("--name", dest="name_flag")
    st.add_argument("--id", dest="id_flag", type=int)
    st.set_defaults(func=lambda a: start_stop(a, "stop"))

    rs = sub.add_parser("restart", help="Restart one or more services")
    rs.add_argument("targets", nargs="*", help="Service target(s): managed NAME and/or ID")
    rs.add_argument("--name", dest="name_flag")
    rs.add_argument("--id", dest="id_flag", type=int)
    rs.set_defaults(func=restart)

    ps = sub.add_parser("status", help="Service/timer status")
    ps.add_argument("name", nargs="?")
    ps.add_argument("--name", dest="name_flag")
    ps.add_argument("--id", dest="id_flag", type=int)
    ps.set_defaults(func=status)

    lg = sub.add_parser("logs", help="Show logs from journalctl")
    lg.add_argument("name", nargs="?")
    lg.add_argument("lines_pos", nargs="?", type=int)
    lg.add_argument("--name", dest="name_flag")
    lg.add_argument("--id", dest="id_flag", type=int)
    lg.add_argument("--lines", type=int, default=None)
    lg.add_argument("--follow", action="store_true", help="Follow logs in real time")
    lg.add_argument("--folow", dest="follow", action="store_true", help=argparse.SUPPRESS)
    lg.add_argument("--since", help="journalctl time filter (example: '1 hour ago')")
    lg.add_argument("--timer", action="store_true", help="Read .timer logs instead of .service")
    lg.add_argument("--output", default="short", help="journalctl output mode (e.g. short, short-iso, cat, json)")
    lg.add_argument("--plain", action="store_true", help="Shortcut for --output cat (message only)")
    lg.set_defaults(func=logs)

    stt = sub.add_parser("stats", help="Show execution/restart counters for a tracked service")
    stt.add_argument("name", nargs="?")
    stt.add_argument("--name", dest="name_flag")
    stt.add_argument("--id", dest="id_flag", type=int)
    stt.add_argument("--since", help="journalctl time filter (example: '24 hours ago')")
    stt.add_argument("--boot", action="store_true", help="Count entries from current boot only")
    stt.set_defaults(func=stats)

    dr = sub.add_parser("doctor", help="Check registry/systemd inconsistencies")
    dr.set_defaults(func=doctor)

    ds = sub.add_parser("describe", help="Show details for a tracked service")
    ds.add_argument("name", nargs="?")
    ds.add_argument("--name", dest="name_flag")
    ds.add_argument("--id", dest="id_flag", type=int)
    ds.set_defaults(func=describe)

    sy = sub.add_parser("sync", help="Backfill missing registry fields from systemd")
    sy.add_argument("name", nargs="?", help="Sync only one managed service")
    sy.add_argument("--name", dest="name_flag", help="Sync only one managed service")
    sy.add_argument("--id", dest="id_flag", type=int, help="Sync only one managed service by id")
    sy.set_defaults(func=sync)

    v = sub.add_parser("version", help="Show version")
    v.set_defaults(func=lambda _a: print(VERSION))

    sd = sub.add_parser("sudo", help="Helpers for one-off sudo usage")
    sd_sub = sd.add_subparsers(dest="sudo_command", required=True)

    sd_check = sd_sub.add_parser("check", help="Check whether sudo can run non-interactively")
    sd_check.set_defaults(func=sudo_check)

    sd_run = sd_sub.add_parser("run", help="Run one command through sudo")
    sd_run.add_argument("command", nargs=argparse.REMAINDER)
    sd_run.set_defaults(func=sudo_run_command)

    return p


def main() -> int:
    global USE_ENV_SUDO, FORCE_TABLE_ASCII, FORCE_TABLE_UNICODE
    argv = list(sys.argv)
    known_commands = {"list", "catalog", "track", "rename", "untrack", "exec", "start", "stop", "restart", "status", "logs", "stats", "doctor", "describe", "sync", "sudo", "version"}
    parser = build_parser()
    args = parser.parse_args(argv[1:])
    USE_ENV_SUDO = not args.no_env_sudo
    FORCE_TABLE_ASCII = bool(args.ascii)
    FORCE_TABLE_UNICODE = bool(args.unicode)
    if FORCE_TABLE_ASCII and FORCE_TABLE_UNICODE:
        parser.error("choose only one of --ascii or --unicode")
    try:
        if getattr(args, "command", None) != "version":
            load_registry()
        if not getattr(args, "command", None):
            list_services_compact(resolve_sort_arg(args))
            print("Quick help: skuld track <id ...> | skuld <id|name> exec/start/stop/restart/status/logs/stats/describe/rename/untrack")
            print()
            return 0

        args.func(args)
        if args.command in {"track", "rename", "untrack", "exec", "start", "stop", "restart", "sync"}:
            print()
            list_services_compact(resolve_sort_arg(args))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
