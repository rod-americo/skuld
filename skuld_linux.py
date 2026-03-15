#!/usr/bin/env python3
import argparse
import json
import os
import pwd
import re
import readline
import shlex
import subprocess
import sys
import tempfile
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
SCHEDULE_PROMPT_SENTINEL = "__SKULD_PROMPT_SCHEDULE__"
SYSTEMD_UNIT_STARTED_MESSAGE_ID = "39f53479d3a045ac8e11786248231fbf"


@dataclass
class ManagedService:
    name: str
    exec_cmd: str
    description: str
    schedule: str = ""
    working_dir: str = ""
    user: str = ""
    restart: str = "on-failure"
    timer_persistent: bool = True
    id: int = 0


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
        "exec_cmd",
        "description",
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
            "exec_cmd": str(item.get("exec_cmd", "")).strip(),
            "description": str(item.get("description", "")).strip(),
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
    normalized_services = sorted(services, key=lambda s: (s.name.lower(), s.id))
    if normalized_services != services:
        changed = True
    canonical_text = json.dumps([asdict(s) for s in normalized_services], indent=2, ensure_ascii=False) + "\n"
    if changed or raw_text != canonical_text:
        REGISTRY_FILE.write_text(canonical_text, encoding="utf-8")
    return normalized_services


def save_registry(services: List[ManagedService]) -> None:
    ensure_storage()
    ordered = sorted(services, key=lambda s: (s.name.lower(), s.id))
    REGISTRY_FILE.write_text(json.dumps([asdict(s) for s in ordered], indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def upsert_registry(service: ManagedService) -> None:
    services = load_registry()
    by_name = {s.name: s for s in services}
    existing = by_name.get(service.name)
    if service.id <= 0 and existing:
        service.id = existing.id
    if service.id <= 0:
        max_id = max((s.id for s in services), default=0)
        service.id = max_id + 1
    by_name[service.name] = service
    save_registry(sorted(by_name.values(), key=lambda s: s.name.lower()))


def remove_registry(name: str) -> None:
    services = [s for s in load_registry() if s.name != name]
    save_registry(services)


def get_managed(name: str) -> Optional[ManagedService]:
    for svc in load_registry():
        if svc.name == name:
            return svc
    return None


def get_managed_by_id(service_id: int) -> Optional[ManagedService]:
    for svc in load_registry():
        if svc.id == service_id:
            return svc
    return None


def require_managed(name: str) -> ManagedService:
    svc = get_managed(name)
    if not svc:
        raise RuntimeError(
            f"'{name}' is not in the skuld registry. "
            "Only services created or adopted via skuld can be monitored."
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


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError("Invalid name. Use [a-zA-Z0-9._@-] and start with a letter/number.")


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
    svc = get_managed(token)
    if svc:
        return svc
    if token.isdigit():
        return get_managed_by_id(int(token))
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


def prompt_schedule_edit(current: str) -> str:
    get_hook = getattr(readline, "get_startup_hook", None)
    previous_hook = get_hook() if callable(get_hook) else None
    try:
        if current:
            readline.set_startup_hook(lambda: readline.insert_text(current))
        value = input("Schedule (OnCalendar): ").strip()
    finally:
        readline.set_startup_hook(previous_hook)
    if value:
        return value
    return current


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


def unit_exists(unit: str) -> bool:
    show = systemctl_show(unit, ["LoadState"])
    load_state = (show.get("LoadState", "") or "").strip().lower()
    return bool(load_state and load_state != "not-found")


def unit_active(unit: str) -> str:
    proc = run(["systemctl", "is-active", unit], check=False, capture=True)
    status = (proc.stdout or "").strip()
    return status if status else "inactive"


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
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num)
    idx = 0
    while size >= 1024 and idx < len(units) - 1:
        size /= 1024.0
        idx += 1
    if idx == 0:
        return f"{int(size)}{units[idx]}"
    return f"{size:.1f}{units[idx]}"


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


def read_unit_usage(name: str, gpu_memory_by_pid: Optional[Dict[int, int]] = None) -> Dict[str, str]:
    service_unit = f"{name}.service"
    if not unit_exists(service_unit):
        return {"cpu": "-", "memory": "-", "gpu": "-"}
    show = systemctl_show(service_unit, ["CPUUsageNSec", "MemoryCurrent", "MainPID"])
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


def get_main_pid(name: str) -> int:
    unit = f"{name}.service"
    if not unit_exists(unit):
        return 0
    show = systemctl_show(unit, ["MainPID"])
    return parse_int(show.get("MainPID", ""))


def read_unit_pids(name: str) -> List[int]:
    unit = f"{name}.service"
    if not unit_exists(unit):
        return []
    show = systemctl_show(unit, ["MainPID", "ControlGroup"])
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


def read_unit_ports(name: str) -> str:
    pids = read_unit_pids(name)
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


def shell_quote_pretty(value: str) -> str:
    if value == "":
        return '""'
    if SHELL_SAFE_RE.match(value):
        return value
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )
    escaped = escaped.replace("\n", "\\n")
    return f'"{escaped}"'


def read_timer_schedule(name: str) -> str:
    timer_unit = f"{name}.timer"
    show = systemctl_show(timer_unit, ["OnCalendar"])
    schedule = (show.get("OnCalendar", "") or "").strip()
    if schedule:
        return schedule
    directives = parse_unit_directives(systemctl_cat(timer_unit))
    return (directives.get("OnCalendar", "") or "").strip()


def read_timer_persistent(name: str, default: bool = True) -> bool:
    timer_unit = f"{name}.timer"
    if not unit_exists(timer_unit):
        return default
    show = systemctl_show(timer_unit, ["Persistent"])
    value = (show.get("Persistent", "") or "").strip()
    if value:
        return parse_bool(value, default=default)
    directives = parse_unit_directives(systemctl_cat(timer_unit))
    raw = (directives.get("Persistent", "") or "").strip()
    if raw:
        return parse_bool(raw, default=default)
    return default


def read_timer_next_run(name: str) -> str:
    timer_unit = f"{name}.timer"
    show = systemctl_show(timer_unit, ["NextElapseUSecRealtime"])
    value = (show.get("NextElapseUSecRealtime", "") or "").strip()
    if not value or value.lower() == "n/a":
        return "-"
    return value


def read_timer_last_run(name: str) -> str:
    timer_unit = f"{name}.timer"
    show = systemctl_show(timer_unit, ["LastTriggerUSec"])
    value = (show.get("LastTriggerUSec", "") or "").strip()
    if not value or value.lower() == "n/a":
        return "-"
    return value


def schedule_for_display(svc: ManagedService) -> str:
    if svc.schedule:
        return svc.schedule
    return read_timer_schedule(svc.name)


def sync_registry_from_systemd(name: Optional[str] = None) -> int:
    services = load_registry()
    changed = 0
    target_names = {name} if name else None
    updated: List[ManagedService] = []

    for svc in services:
        if target_names and svc.name not in target_names:
            updated.append(svc)
            continue

        new_svc = ManagedService(**asdict(svc))
        svc_unit = f"{svc.name}.service"
        timer_unit = f"{svc.name}.timer"

        if unit_exists(svc_unit):
            show_svc = systemctl_show(svc_unit, ["Description", "WorkingDirectory", "User", "Restart"])
            if not new_svc.description and show_svc.get("Description"):
                new_svc.description = show_svc["Description"]
            if not new_svc.working_dir and show_svc.get("WorkingDirectory"):
                new_svc.working_dir = show_svc["WorkingDirectory"]
            if not new_svc.user and show_svc.get("User"):
                new_svc.user = show_svc["User"]
            if (not new_svc.restart or new_svc.restart == "on-failure") and show_svc.get("Restart"):
                new_svc.restart = show_svc["Restart"]

        if unit_exists(timer_unit):
            if not new_svc.schedule:
                new_svc.schedule = read_timer_schedule(svc.name)
            new_svc.timer_persistent = read_timer_persistent(svc.name, default=new_svc.timer_persistent)

        if asdict(new_svc) != asdict(svc):
            changed += 1
            updated.append(new_svc)
        else:
            updated.append(svc)

    if changed:
        save_registry(updated)
    return changed


def systemctl_show(unit: str, props: List[str]) -> Dict[str, str]:
    cmd = ["systemctl", "show", unit, "--no-pager"]
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


def systemctl_cat(unit: str) -> str:
    proc = run(["systemctl", "cat", unit, "--no-pager"], check=False, capture=True)
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


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


def write_systemd_file(target: str, content: str) -> None:
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as tf:
        tf.write(content)
        tmp_path = tf.name
    try:
        run_sudo(["cp", tmp_path, target])
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def systemd_env_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def infer_home_for_user(user: str) -> str:
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return f"/home/{user}"


def render_user_environment(user: str) -> str:
    if not user:
        return ""
    home = infer_home_for_user(user)
    home_q = systemd_env_quote(home)
    user_q = systemd_env_quote(user)
    path_q = systemd_env_quote(f"{home}/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return (
        f'Environment="HOME={home_q}"\n'
        f'Environment="USER={user_q}"\n'
        f'Environment="LOGNAME={user_q}"\n'
        f'Environment="PATH={path_q}"\n'
    )


def render_service(name: str, description: str, exec_cmd: str, working_dir: str, user: str, restart: str) -> str:
    wd_line = f"WorkingDirectory={working_dir}\n" if working_dir else ""
    user_line = f"User={user}\n" if user else ""
    env_lines = render_user_environment(user)
    return (
        "[Unit]\n"
        f"Description={description}\n"
        "After=network.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        "CPUAccounting=yes\n"
        "MemoryAccounting=yes\n"
        f"ExecStart=/bin/bash -lc {shlex.quote(exec_cmd)}\n"
        f"Restart={restart}\n"
        f"{user_line}{env_lines}{wd_line}"
        "\n"
        "[Install]\n"
        "WantedBy=multi-user.target\n"
    )


def render_timer(name: str, schedule: str, persistent: bool) -> str:
    p = "true" if persistent else "false"
    return (
        "[Unit]\n"
        f"Description=Timer for {name}.service\n\n"
        "[Timer]\n"
        f"OnCalendar={schedule}\n"
        f"Persistent={p}\n"
        f"Unit={name}.service\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def create(args: argparse.Namespace) -> None:
    require_systemctl()
    validate_name(args.name)

    desc = args.description or f"Skuld service: {args.name}"
    service_content = render_service(
        name=args.name,
        description=desc,
        exec_cmd=args.exec,
        working_dir=args.working_dir or "",
        user=args.user or "",
        restart=args.restart,
    )
    service_file = f"/etc/systemd/system/{args.name}.service"

    info(f"Creating {service_file}")
    write_systemd_file(service_file, service_content)

    if args.schedule:
        timer_content = render_timer(args.name, args.schedule, args.timer_persistent)
        timer_file = f"/etc/systemd/system/{args.name}.timer"
        info(f"Creating {timer_file}")
        write_systemd_file(timer_file, timer_content)

    run_sudo(["systemctl", "daemon-reload"])
    if args.schedule:
        # Timer jobs should be enabled via .timer only.
        run_sudo(["systemctl", "disable", f"{args.name}.service"], check=False)
        run_sudo(["systemctl", "enable", "--now", f"{args.name}.timer"])
    else:
        run_sudo(["systemctl", "enable", f"{args.name}.service"])

    upsert_registry(
        ManagedService(
            name=args.name,
            exec_cmd=args.exec,
            description=desc,
            schedule=args.schedule or "",
            working_dir=args.working_dir or "",
            user=args.user or "",
            restart=args.restart,
            timer_persistent=args.timer_persistent,
        )
    )
    ok(f"Service '{args.name}' created and registered.")


def _render_services_table(compact: bool) -> None:
    require_systemctl()
    sync_registry_from_systemd()
    services = sorted(load_registry(), key=lambda s: s.name.lower())
    if not services:
        print("No services managed by skuld.")
        return

    rows: List[List[str]] = []
    gpu_memory_by_pid = read_gpu_memory_by_pid()
    runtime_stats = load_runtime_stats()
    print()
    render_host_panel()
    for svc in services:
        s_unit = f"{svc.name}.service"
        t_unit = f"{svc.name}.timer"
        s_state_raw = unit_active(s_unit) if unit_exists(s_unit) else "missing"
        t_state_raw = unit_active(t_unit) if unit_exists(t_unit) else "n/a"
        schedule = schedule_for_display(svc) or "-"
        usage = read_unit_usage(svc.name, gpu_memory_by_pid)
        kind = "timer" if schedule != "-" else "daemon"
        if s_state_raw == "active":
            s_state = colorize("active", "green")
        elif s_state_raw == "inactive":
            s_state = colorize("inactive", "yellow")
        else:
            s_state = colorize(s_state_raw, "red")
        if t_state_raw == "active":
            t_state = colorize("active", "green")
        elif t_state_raw == "inactive":
            t_state = colorize("inactive", "yellow")
        elif t_state_raw == "n/a":
            t_state = colorize("n/a", "gray")
        else:
            t_state = colorize(t_state_raw, "red")
        rows.append(
            [
                str(svc.id),
                svc.name,
                kind,
                s_state,
                t_state,
                read_timer_next_run(svc.name),
                format_restarts_exec(svc.name, runtime_stats),
                read_timer_last_run(svc.name),
                schedule,
                usage["cpu"],
                usage["memory"],
                usage["gpu"],
                read_unit_ports(svc.name),
            ]
        )
    if compact:
        compact_rows = [[r[0], r[1], r[2], r[3], r[4], r[9], r[10]] for r in rows]
        render_table(["id", "name", "kind", "service", "timer", "cpu", "memory"], compact_rows)
    else:
        render_table(
            ["id", "name", "kind", "service", "timer", "next_run", "r/e", "last_run", "schedule", "cpu", "memory", "gpu", "ports"],
            rows,
        )
    print()


def list_services(_args: argparse.Namespace) -> None:
    _render_services_table(compact=False)


def list_services_compact() -> None:
    _render_services_table(compact=True)


def exec_now(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    require_systemctl()
    run_sudo(["systemctl", "start", f"{name}.service"])
    ok(f"Execution started: {name}.service")


def managed_uses_timer(svc: ManagedService) -> bool:
    return bool(svc.schedule) or unit_exists(f"{svc.name}.timer")


def apply_action_for_managed(svc: ManagedService, action: str) -> None:
    name = svc.name
    service_unit = f"{name}.service"
    timer_unit = f"{name}.timer"
    uses_timer = managed_uses_timer(svc)

    if uses_timer:
        proc = run_sudo(["systemctl", action, timer_unit], check=False, capture=True)
        if proc.returncode != 0:
            details = (proc.stderr or proc.stdout or "").strip()
            msg = f"Failed to {action} {timer_unit}."
            if details:
                msg = f"{msg} {details}"
            raise RuntimeError(msg)
        ok(f"{action} -> {timer_unit}")
        return

    proc = run_sudo(["systemctl", action, service_unit], check=False, capture=True)
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        msg = f"Failed to {action} {service_unit}."
        if details:
            msg = f"{msg} {details}"
        raise RuntimeError(msg)
    ok(f"{action} -> {service_unit}")


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
    run(["systemctl", "status", f"{name}.service", "--no-pager"], check=False)
    run(["systemctl", "status", f"{name}.timer", "--no-pager"], check=False)


def logs(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    lines = resolve_lines_arg(args, default=100)
    require_systemctl()
    unit = f"{name}.timer" if args.timer else f"{name}.service"
    cmd = ["journalctl", "-u", unit, "-n", str(lines)]
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
        needs_sudo = (
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

    permission_hint = journal_permission_hint(stderr)
    if permission_hint:
        proc = run_sudo(cmd, check=False, capture=True)
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()

    if stdout:
        print(stdout)
    if stderr:
        print(stderr, file=sys.stderr)


def count_unit_starts(unit: str, since: Optional[str] = None, boot: bool = False) -> int:
    cmd = [
        "journalctl",
        "-u",
        unit,
        f"MESSAGE_ID={SYSTEMD_UNIT_STARTED_MESSAGE_ID}",
        "-o",
        "json",
        "--no-pager",
    ]
    if since:
        cmd.extend(["--since", since])
    if boot:
        cmd.append("-b")

    proc = run(cmd, check=False, capture=True)
    stderr = (proc.stderr or "").strip()
    stdout = proc.stdout or ""
    if journal_permission_hint(stderr):
        proc = run_sudo(cmd, check=False, capture=True)
        stderr = (proc.stderr or "").strip()
        stdout = proc.stdout or ""

    lines = [line for line in stdout.splitlines() if line.strip()]
    return len(lines)


def read_restart_count(name: str) -> str:
    service_unit = f"{name}.service"
    if not unit_exists(service_unit):
        return "-"
    show = systemctl_show(service_unit, ["NRestarts"])
    raw = (show.get("NRestarts", "") or "").strip()
    return raw if raw else "-"


def stats(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    require_systemctl()
    sync_registry_from_systemd(name)
    service_unit = f"{name}.service"
    executions = count_unit_starts(service_unit, since=args.since, boot=args.boot)
    restarts = read_restart_count(name)

    print(f"name: {name}")
    print(f"service_unit: {service_unit}")
    if args.boot:
        print("window: current boot")
    elif args.since:
        print(f"window: since {args.since}")
    else:
        print("window: all retained journal entries")
    print(f"executions: {executions}")
    print(f"restarts: {restarts}")


def recreate(args: argparse.Namespace) -> None:
    target = resolve_managed_arg(args)
    name = target.name
    sync_registry_from_systemd(name)
    svc = require_managed(name)
    print(build_recreate_command(svc))
    if svc.user:
        print()
        print(
            "# Note: with --user, Skuld also injects "
            'Environment="HOME=...", "USER=...", "LOGNAME=...", and "PATH=..." in the generated unit.'
        )


def build_recreate_command(svc: ManagedService) -> str:
    lines = [
        "skuld create \\",
        f"  --name {shell_quote_pretty(svc.name)} \\",
        f"  --description {shell_quote_pretty(svc.description)} \\",
    ]
    if svc.working_dir:
        lines.append(f"  --working-dir {shell_quote_pretty(svc.working_dir)} \\")
    if svc.user:
        lines.append(f"  --user {shell_quote_pretty(svc.user)} \\")
    lines.append(f"  --restart {shell_quote_pretty(svc.restart)} \\")
    lines.append(f"  --exec {shell_quote_pretty(svc.exec_cmd)}")
    if svc.schedule:
        lines[-1] = lines[-1] + " \\"
        lines.append(f"  --schedule {shell_quote_pretty(svc.schedule)} \\")
        lines.append("  --timer-persistent" if svc.timer_persistent else "  --no-timer-persistent")
    return "\n".join(lines)


def remove(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    require_systemctl()

    service_unit = f"{name}.service"
    timer_unit = f"{name}.timer"

    # Stop first to avoid lingering runtime when removing daemon units.
    run_sudo(["systemctl", "stop", timer_unit], check=False)
    run_sudo(["systemctl", "stop", service_unit], check=False)

    # If still active for any reason (restart policies/races), force kill.
    if unit_active(service_unit) == "active":
        run_sudo(["systemctl", "kill", service_unit], check=False)
        run_sudo(["systemctl", "stop", service_unit], check=False)

    run_sudo(["systemctl", "disable", timer_unit], check=False)
    run_sudo(["systemctl", "disable", service_unit], check=False)
    run_sudo(["rm", "-f", f"/etc/systemd/system/{name}.service"])
    run_sudo(["rm", "-f", f"/etc/systemd/system/{name}.timer"])
    run_sudo(["systemctl", "daemon-reload"])
    run_sudo(["systemctl", "reset-failed", service_unit], check=False)
    run_sudo(["systemctl", "reset-failed", timer_unit], check=False)

    if args.purge:
        remove_registry(name)
    ok(f"Removed: {name} (purge={args.purge})")


def adopt(args: argparse.Namespace) -> None:
    name = resolve_name_arg(args)
    require_systemctl()
    validate_name(name)
    if get_managed(name):
        raise RuntimeError(f"'{name}' is already registered in skuld.")

    service_unit = f"{name}.service"
    timer_unit = f"{name}.timer"
    if not unit_exists(service_unit):
        raise RuntimeError(f"Service '{service_unit}' does not exist in systemd.")

    service_text = systemctl_cat(service_unit)
    directives = parse_unit_directives(service_text)
    exec_line = directives.get("ExecStart", "")
    if exec_line.startswith("/bin/bash -lc "):
        exec_line = exec_line[len("/bin/bash -lc "):].strip()
        if len(exec_line) >= 2 and exec_line[0] == exec_line[-1] and exec_line[0] in ("'", '"'):
            exec_line = exec_line[1:-1]
    if not exec_line:
        raise RuntimeError("Could not infer ExecStart for adopt.")

    show_service = systemctl_show(service_unit, ["Description", "WorkingDirectory", "User", "Restart"])
    schedule = ""
    timer_persistent = True
    if unit_exists(timer_unit):
        show_timer = systemctl_show(timer_unit, ["OnCalendar", "Persistent"])
        schedule = show_timer.get("OnCalendar", "") or ""
        timer_persistent = parse_bool(show_timer.get("Persistent", "true"), default=True)

    upsert_registry(
        ManagedService(
            name=name,
            exec_cmd=exec_line,
            description=show_service.get("Description", f"Skuld service: {name}"),
            schedule=schedule,
            working_dir=show_service.get("WorkingDirectory", "") or "",
            user=show_service.get("User", "") or "",
            restart=show_service.get("Restart", "on-failure") or "on-failure",
            timer_persistent=timer_persistent,
        )
    )
    ok(f"Service '{name}' adopted into the skuld registry.")


def doctor(_args: argparse.Namespace) -> None:
    require_systemctl()
    sync_registry_from_systemd()
    services = load_registry()
    if not services:
        print("No services managed by skuld.")
        return

    issues = 0
    for svc in services:
        service_unit = f"{svc.name}.service"
        timer_unit = f"{svc.name}.timer"
        line_prefix = f"[{svc.name}]"

        if not unit_exists(service_unit):
            print(f"{line_prefix} ERROR missing service unit ({service_unit})")
            issues += 1
        else:
            st = unit_active(service_unit)
            print(f"{line_prefix} service={st}")

        has_timer = bool(svc.schedule)
        runtime_schedule = read_timer_schedule(svc.name)
        if not has_timer and runtime_schedule:
            print(
                f"{line_prefix} WARN registry schedule is empty, "
                f"but timer OnCalendar is '{runtime_schedule}'"
            )
            issues += 1
        if has_timer and not unit_exists(timer_unit):
            print(f"{line_prefix} ERROR expected timer is missing ({timer_unit})")
            issues += 1
        if (not has_timer) and unit_exists(timer_unit):
            print(f"{line_prefix} WARN timer exists, but registry has no schedule")
            issues += 1

        if unit_exists(service_unit):
            cat = parse_unit_directives(systemctl_cat(service_unit))
            current_exec = cat.get("ExecStart", "")
            if svc.exec_cmd and svc.exec_cmd not in current_exec:
                print(f"{line_prefix} WARN ExecStart differs from registry")
                issues += 1

    if issues == 0:
        ok("doctor: no issues found.")
    else:
        err(f"doctor: found {issues} issue(s).")


def apply_managed_update(
    current: ManagedService,
    *,
    exec_cmd: Optional[str] = None,
    description: Optional[str] = None,
    working_dir: Optional[str] = None,
    user: Optional[str] = None,
    restart: Optional[str] = None,
    schedule: Optional[str] = None,
    timer_persistent: Optional[bool] = None,
    clear_schedule: bool = False,
) -> bool:
    name = current.name
    new_exec = exec_cmd if exec_cmd is not None else current.exec_cmd
    new_description = description if description is not None else current.description
    new_workdir = working_dir if working_dir is not None else current.working_dir
    new_user = user if user is not None else current.user
    new_restart = restart if restart is not None else current.restart
    new_schedule = schedule if schedule is not None else current.schedule
    if clear_schedule:
        new_schedule = ""
    new_timer_persistent = current.timer_persistent if timer_persistent is None else timer_persistent

    if (
        new_exec == current.exec_cmd
        and new_description == current.description
        and new_workdir == current.working_dir
        and new_user == current.user
        and new_restart == current.restart
        and new_schedule == current.schedule
        and new_timer_persistent == current.timer_persistent
    ):
        return False

    service_content = render_service(
        name=name,
        description=new_description,
        exec_cmd=new_exec,
        working_dir=new_workdir,
        user=new_user,
        restart=new_restart,
    )
    write_systemd_file(f"/etc/systemd/system/{name}.service", service_content)

    timer_path = f"/etc/systemd/system/{name}.timer"
    if new_schedule:
        timer_content = render_timer(name, new_schedule, new_timer_persistent)
        write_systemd_file(timer_path, timer_content)
    else:
        run_sudo(["rm", "-f", timer_path], check=False)

    run_sudo(["systemctl", "daemon-reload"])
    if new_schedule:
        # Timer jobs should be enabled via .timer only.
        run_sudo(["systemctl", "disable", f"{name}.service"], check=False)
        run_sudo(["systemctl", "enable", f"{name}.timer"], check=False)
    else:
        run_sudo(["systemctl", "enable", f"{name}.service"], check=False)
        run_sudo(["systemctl", "disable", f"{name}.timer"], check=False)

    upsert_registry(
        ManagedService(
            name=name,
            exec_cmd=new_exec,
            description=new_description,
            schedule=new_schedule,
            working_dir=new_workdir,
            user=new_user,
            restart=new_restart,
            timer_persistent=new_timer_persistent,
            id=current.id,
        )
    )
    return True


def edit(args: argparse.Namespace) -> None:
    svc = resolve_managed_arg(args)
    name = svc.name
    require_systemctl()
    current = require_managed(name)
    schedule = args.schedule
    if schedule == SCHEDULE_PROMPT_SENTINEL and not args.clear_schedule:
        schedule = prompt_schedule_edit(current.schedule)

    changed = apply_managed_update(
        current,
        exec_cmd=args.exec,
        description=args.description,
        working_dir=args.working_dir,
        user=args.user,
        restart=args.restart,
        schedule=schedule,
        timer_persistent=args.timer_persistent,
        clear_schedule=args.clear_schedule,
    )
    if not changed:
        info("No changes detected.")
        return
    ok(f"Service '{name}' updated.")


def describe(args: argparse.Namespace) -> None:
    target = resolve_managed_arg(args)
    name = target.name
    require_systemctl()
    sync_registry_from_systemd(name)
    svc = require_managed(name)
    service_unit = f"{name}.service"
    timer_unit = f"{name}.timer"

    show_service = systemctl_show(
        service_unit,
        ["Id", "Description", "ActiveState", "SubState", "FragmentPath", "MainPID"],
    )
    show_timer = systemctl_show(
        timer_unit,
        ["Id", "ActiveState", "SubState", "NextElapseUSecRealtime", "LastTriggerUSec"],
    ) if unit_exists(timer_unit) else {}

    print(f"name: {svc.name}")
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
    name = svc.name if svc else None
    require_systemctl()
    changed = sync_registry_from_systemd(name)
    if changed == 0:
        ok("Registry is already up to date.")
    else:
        ok(f"Registry updated for {changed} service(s).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="skuld", description="CLI for managing systemd services")
    p.add_argument(
        "--no-env-sudo",
        action="store_true",
        help="Disable SKULD_SUDO_PASSWORD from env/.env and use regular sudo behavior",
    )
    p.add_argument("--ascii", action="store_true", help="Force ASCII table borders")
    p.add_argument("--unicode", action="store_true", help="Force Unicode table borders")
    sub = p.add_subparsers(dest="command", required=False)

    c = sub.add_parser("create", help="Create and install .service and optional .timer")
    c.add_argument("--name", required=True)
    c.add_argument("--exec", required=True, help="ExecStart command")
    c.add_argument("--description")
    c.add_argument("--working-dir")
    c.add_argument("--user")
    c.add_argument("--restart", default="on-failure")
    c.add_argument("--schedule", help="Timer OnCalendar expression")
    c.add_argument("--timer-persistent", action=argparse.BooleanOptionalAction, default=True)
    c.set_defaults(func=create)

    l = sub.add_parser("list", help="List services managed by skuld")
    l.set_defaults(func=list_services)

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

    stt = sub.add_parser("stats", help="Show execution/restart counters for a managed service")
    stt.add_argument("name", nargs="?")
    stt.add_argument("--name", dest="name_flag")
    stt.add_argument("--id", dest="id_flag", type=int)
    stt.add_argument("--since", help="journalctl time filter (example: '24 hours ago')")
    stt.add_argument("--boot", action="store_true", help="Count entries from current boot only")
    stt.set_defaults(func=stats)

    rm = sub.add_parser("remove", help="Remove units")
    rm.add_argument("name", nargs="?")
    rm.add_argument("--name", dest="name_flag")
    rm.add_argument("--id", dest="id_flag", type=int)
    rm.add_argument("--purge", action="store_true")
    rm.set_defaults(func=remove)

    ad = sub.add_parser("adopt", help="Adopt an existing systemd service into skuld registry")
    ad.add_argument("name", nargs="?")
    ad.add_argument("--name", dest="name_flag")
    ad.set_defaults(func=adopt)

    dr = sub.add_parser("doctor", help="Check registry/systemd inconsistencies")
    dr.set_defaults(func=doctor)

    ed = sub.add_parser("edit", help="Edit a managed service definition")
    ed.add_argument("name", nargs="?")
    ed.add_argument("--name", dest="name_flag")
    ed.add_argument("--id", dest="id_flag", type=int)
    ed.add_argument("--exec")
    ed.add_argument("--description")
    ed.add_argument("--working-dir")
    ed.add_argument("--user")
    ed.add_argument("--restart")
    ed.add_argument(
        "--schedule",
        nargs="?",
        const=SCHEDULE_PROMPT_SENTINEL,
        help="Timer OnCalendar expression. If omitted, opens interactive edit with current value.",
    )
    ed.add_argument("--clear-schedule", action="store_true")
    ed.add_argument("--timer-persistent", action=argparse.BooleanOptionalAction, default=None)
    ed.set_defaults(func=edit)

    ds = sub.add_parser("describe", help="Show details for a managed service")
    ds.add_argument("name", nargs="?")
    ds.add_argument("--name", dest="name_flag")
    ds.add_argument("--id", dest="id_flag", type=int)
    ds.set_defaults(func=describe)

    rc = sub.add_parser("recreate", help="Print equivalent skuld create command for a managed service")
    rc.add_argument("name", nargs="?")
    rc.add_argument("--name", dest="name_flag")
    rc.add_argument("--id", dest="id_flag", type=int)
    rc.set_defaults(func=recreate)

    sy = sub.add_parser("sync", help="Backfill missing registry fields from systemd")
    sy.add_argument("name", nargs="?", help="Sync only one managed service")
    sy.add_argument("--name", dest="name_flag", help="Sync only one managed service")
    sy.add_argument("--id", dest="id_flag", type=int, help="Sync only one managed service by id")
    sy.set_defaults(func=sync)

    v = sub.add_parser("version", help="Show version")
    v.set_defaults(func=lambda _a: print(VERSION))

    return p


def main() -> int:
    global USE_ENV_SUDO, FORCE_TABLE_ASCII, FORCE_TABLE_UNICODE
    argv = list(sys.argv)
    known_commands = {"create", "list", "exec", "start", "stop", "restart", "status", "logs", "stats", "remove", "adopt", "doctor", "edit", "describe", "recreate", "sync", "version"}
    edit_flags = {
        "--exec",
        "--description",
        "--working-dir",
        "--user",
        "--restart",
        "--schedule",
        "--clear-schedule",
        "--timer-persistent",
        "--no-timer-persistent",
    }
    if len(argv) > 2 and not argv[1].startswith("-") and argv[1] not in known_commands:
        if any(flag in argv[2:] for flag in edit_flags):
            argv = [argv[0], "edit", argv[1], *argv[2:]]
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
            list_services_compact()
            print("Quick help: skuld <id|name> commands: exec/start/stop/restart/status/logs/stats/describe/edit/remove")
            print()
            return 0

        args.func(args)
        if args.command in {"create", "exec", "start", "stop", "restart", "remove", "adopt", "edit", "sync"}:
            print()
            list_services(argparse.Namespace())
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
