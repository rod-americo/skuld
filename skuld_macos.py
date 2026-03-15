#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import os
import plistlib
import pwd
import readline
import re
import shlex
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

VERSION = "0.3.0"
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._@-]*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
SHELL_SAFE_RE = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")
SCHEDULE_PROMPT_SENTINEL = "__SKULD_PROMPT_SCHEDULE__"
DEFAULT_ENV_FILE = Path(".env")
SKULD_HOME = Path(os.environ.get("SKULD_HOME", Path.home() / "Library/Application Support/skuld"))
REGISTRY_FILE = SKULD_HOME / "services.json"
RUNTIME_STATS_FILE = SKULD_HOME / "runtime_stats.json"
USE_ENV_SUDO = True
FORCE_TABLE_ASCII = False
FORCE_TABLE_UNICODE = False
WEEKDAY_MAP = {
    "sun": 0,
    "mon": 1,
    "tue": 2,
    "wed": 3,
    "thu": 4,
    "fri": 5,
    "sat": 6,
}


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
    backend: str = "launchd"
    scope: str = "daemon"
    log_dir: str = ""


def ensure_storage() -> None:
    SKULD_HOME.mkdir(parents=True, exist_ok=True)
    if not REGISTRY_FILE.exists():
        REGISTRY_FILE.write_text("[]", encoding="utf-8")
    if not RUNTIME_STATS_FILE.exists():
        RUNTIME_STATS_FILE.write_text('{"services": {}}\n', encoding="utf-8")


def load_dotenv(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    env: Dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def get_sudo_password() -> Optional[str]:
    if not USE_ENV_SUDO:
        return None
    from_env = os.environ.get("SKULD_SUDO_PASSWORD")
    if from_env:
        return from_env
    env_path_override = os.environ.get("SKULD_ENV_FILE")
    candidates = []
    if env_path_override:
        candidates.append(Path(env_path_override))
    candidates.extend(
        [
            Path.cwd() / DEFAULT_ENV_FILE,
            Path(__file__).resolve().parent / DEFAULT_ENV_FILE,
            SKULD_HOME / ".env",
        ]
    )
    for candidate in candidates:
        if not candidate.exists():
            continue
        value = load_dotenv(candidate).get("SKULD_SUDO_PASSWORD")
        if value:
            return value
    return None


def parse_bool(value: str, default: bool = True) -> bool:
    raw = (value or "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return default


def parse_int(value: str) -> int:
    try:
        num = int((value or "").strip())
    except ValueError:
        return 0
    return num if num > 0 else 0


def validate_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError("Invalid name. Use [a-zA-Z0-9._@-] and start with a letter/number.")


def resolve_scope(value: str) -> str:
    scope = (value or "daemon").strip().lower()
    if scope not in {"daemon", "agent"}:
        raise RuntimeError("Invalid scope. Use 'daemon' or 'agent'.")
    return scope


def resolve_name_arg(args: argparse.Namespace, required: bool = True) -> Optional[str]:
    positional = getattr(args, "name", None)
    flag_value = getattr(args, "name_flag", None)
    if positional and flag_value and positional != flag_value:
        raise RuntimeError(f"Conflicting names provided: positional='{positional}' and --name='{flag_value}'.")
    name = flag_value or positional
    if required and not name:
        raise RuntimeError("Service name is required. Use NAME or --name NAME.")
    return name


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


def info(msg: str) -> None:
    print(f"[skuld] {msg}")


def ok(msg: str) -> None:
    print(f"[ok] {msg}")


def err(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)


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
    password = get_sudo_password()
    full = ["sudo"] + cmd
    if password:
        full = ["sudo", "-S", "-k", "-p", ""] + cmd
        return run(full, check=check, capture=capture, input_text=password + "\n")
    return run(full, check=check, capture=capture)


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
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


def format_bytes_from_kib(kib: int) -> str:
    return format_bytes(str(kib * 1024))


def format_bytes(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
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


def render_table(headers: List[str], rows: List[List[str]]) -> None:
    if not rows:
        return
    widths = [visible_len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], visible_len(cell))
    if supports_unicode_output():
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

    def hline(left: str, middle: str, right: str) -> str:
        return left + middle.join(box["fill"] * (w + 2) for w in widths) + right

    def format_row(cells: List[str]) -> str:
        padded = []
        for i, cell in enumerate(cells):
            pad = widths[i] - visible_len(cell)
            padded.append(f" {cell}{' ' * max(0, pad)} ")
        return box["vertical"] + box["vertical"].join(padded) + box["vertical"]

    print(hline(box["top_left"], box["top_mid"], box["top_right"]))
    print(format_row(headers))
    print(hline(box["mid_left"], box["mid_mid"], box["mid_right"]))
    for row in rows:
        print(format_row(row))
    print(hline(box["bottom_left"], box["bottom_mid"], box["bottom_right"]))


def prompt_schedule_edit(current: str) -> str:
    get_hook = getattr(readline, "get_startup_hook", None)
    previous_hook = get_hook() if callable(get_hook) else None
    try:
        if current:
            readline.set_startup_hook(lambda: readline.insert_text(current))
        value = input("Schedule: ").strip()
    finally:
        readline.set_startup_hook(previous_hook)
    return value if value else current


def current_user_name() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def current_user_home() -> Path:
    return Path.home()


def home_for_user(user: str) -> Path:
    return Path(pwd.getpwnam(user).pw_dir)


def service_label(name: str) -> str:
    return f"io.skuld.{name}"


def plist_path_for_service(service: ManagedService) -> Path:
    if service.scope == "agent":
        return current_user_home() / "Library/LaunchAgents" / f"{service_label(service.name)}.plist"
    return Path("/Library/LaunchDaemons") / f"{service_label(service.name)}.plist"


def jobs_root_for_scope(scope: str) -> Path:
    if scope == "agent":
        return SKULD_HOME / "jobs"
    return Path("/Library/Application Support/skuld/jobs")


def logs_root_for_scope(scope: str) -> Path:
    if scope == "agent":
        return SKULD_HOME / "logs"
    return Path("/Library/Application Support/skuld/logs")


def events_root_for_scope(scope: str) -> Path:
    if scope == "agent":
        return SKULD_HOME / "events"
    return Path("/Library/Application Support/skuld/events")


def log_dir_for_service(name: str, scope: str) -> Path:
    return logs_root_for_scope(scope) / name


def event_file_for_service(name: str, scope: str) -> Path:
    return events_root_for_scope(scope) / f"{name}.jsonl"


def wrapper_script_for_service(name: str, scope: str) -> Path:
    return jobs_root_for_scope(scope) / f"{name}.sh"


def normalize_service(item: Dict[str, object]) -> ManagedService:
    scope = resolve_scope(str(item.get("scope", "daemon")))
    name = str(item.get("name", "")).strip()
    log_dir = str(item.get("log_dir", "")).strip() or str(log_dir_for_service(name, scope))
    return ManagedService(
        name=name,
        exec_cmd=str(item.get("exec_cmd", "")).strip(),
        description=str(item.get("description", "")).strip(),
        schedule=str(item.get("schedule", "")).strip(),
        working_dir=str(item.get("working_dir", "")).strip(),
        user=str(item.get("user", "")).strip(),
        restart=str(item.get("restart", "on-failure")).strip() or "on-failure",
        timer_persistent=parse_bool(str(item.get("timer_persistent", True))),
        id=parse_int(str(item.get("id", 0))),
        backend="launchd",
        scope=scope,
        log_dir=log_dir,
    )


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
    for idx, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise RuntimeError(f"Invalid registry entry #{idx}: expected object.")
        svc = normalize_service(item)
        if not svc.name or not svc.exec_cmd or not svc.description:
            raise RuntimeError(
                f"Invalid registry entry #{idx}: fields 'name', 'exec_cmd' and 'description' are required."
            )
        validate_name(svc.name)
        if svc.scope == "agent" and svc.user:
            raise RuntimeError(f"Invalid registry entry #{idx}: 'user' is only valid for daemon scope.")
        services.append(svc)
        normalized = asdict(svc)
        if normalized != {k: item.get(k) for k in normalized.keys() if k in item}:
            changed = True
    used_ids = set()
    next_id = 1
    for svc in services:
        if svc.id <= 0 or svc.id in used_ids:
            while next_id in used_ids:
                next_id += 1
            svc.id = next_id
            changed = True
        used_ids.add(svc.id)
    ordered = sorted(services, key=lambda s: (s.name.lower(), s.id))
    canonical = json.dumps([asdict(s) for s in ordered], indent=2, ensure_ascii=False) + "\n"
    if changed or raw_text != canonical:
        REGISTRY_FILE.write_text(canonical, encoding="utf-8")
    return ordered


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
    save_registry(list(by_name.values()))


def remove_registry(name: str) -> None:
    save_registry([svc for svc in load_registry() if svc.name != name])


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
            f"Conflicting targets provided: '{token}' resolves to id={by_token.id}, but --id={id_flag}."
        )
    svc = by_id or by_token
    if required and not svc:
        raise RuntimeError("Service target is required. Use NAME/ID, --name NAME, or --id ID.")
    return svc


def resolve_managed_many_arg(args: argparse.Namespace) -> List[ManagedService]:
    tokens = list(getattr(args, "targets", None) or [])
    name_flag = getattr(args, "name_flag", None)
    id_flag = getattr(args, "id_flag", None)
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


def require_supported_scope_user(scope: str, user: str) -> None:
    if scope == "agent" and user:
        raise RuntimeError("--user is only supported with --scope daemon on macOS.")


def ensure_directory(path: Path, scope: str) -> None:
    if scope == "daemon":
        run_sudo(["mkdir", "-p", str(path)])
    else:
        path.mkdir(parents=True, exist_ok=True)


def write_text_file(path: Path, content: str, scope: str, executable: bool = False) -> None:
    ensure_directory(path.parent, scope)
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as handle:
        handle.write(content)
        tmp_path = Path(handle.name)
    try:
        if scope == "daemon":
            run_sudo(["cp", str(tmp_path), str(path)])
            if executable:
                run_sudo(["chmod", "755", str(path)])
        else:
            tmp_path.replace(path)
            if executable:
                path.chmod(0o755)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_plist_file(path: Path, content: Dict[str, object], scope: str) -> None:
    ensure_directory(path.parent, scope)
    with tempfile.NamedTemporaryFile("wb", delete=False) as handle:
        plistlib.dump(content, handle)
        tmp_path = Path(handle.name)
    try:
        if scope == "daemon":
            run_sudo(["cp", str(tmp_path), str(path)])
            run_sudo(["chmod", "644", str(path)])
        else:
            tmp_path.replace(path)
            path.chmod(0o644)
    finally:
        tmp_path.unlink(missing_ok=True)


def rm_file(path: Path, scope: str) -> None:
    if scope == "daemon":
        run_sudo(["rm", "-f", str(path)], check=False)
    else:
        path.unlink(missing_ok=True)


def launchctl_cmd(scope: str, args: List[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    if scope == "daemon":
        return run_sudo(["launchctl"] + args, check=check, capture=capture)
    return run(["launchctl"] + args, check=check, capture=capture)


def domain_target(scope: str) -> str:
    if scope == "agent":
        return f"gui/{os.getuid()}"
    return "system"


def service_target(service: ManagedService) -> str:
    return f"{domain_target(service.scope)}/{service_label(service.name)}"


def service_loaded(service: ManagedService) -> bool:
    proc = launchctl_cmd(service.scope, ["list", service_label(service.name)], check=False, capture=True)
    return proc.returncode == 0


def parse_launchctl_kv(text: str) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        match = re.match(r'"?([A-Za-z0-9_]+)"?\s*=\s*("?)(.*?)\2;?$', line)
        if not match:
            continue
        result[match.group(1)] = match.group(3)
    return result


def launchctl_service_info(service: ManagedService) -> Dict[str, str]:
    proc = launchctl_cmd(service.scope, ["list", service_label(service.name)], check=False, capture=True)
    if proc.returncode != 0:
        return {}
    return parse_launchctl_kv(proc.stdout or "")


def read_pid(service: ManagedService) -> int:
    return parse_int(launchctl_service_info(service).get("PID", "0"))


def restart_policy_to_keepalive(value: str) -> object:
    policy = (value or "on-failure").strip().lower()
    if policy in {"no", "never"}:
        return False
    if policy == "always":
        return True
    return {"SuccessfulExit": False}


def restart_policy_allows_restart(value: str) -> bool:
    policy = (value or "on-failure").strip().lower()
    return policy not in {"no", "never"}


def parse_schedule(schedule: str) -> Tuple[Optional[str], object]:
    value = (schedule or "").strip()
    if not value:
        return None, None
    match = re.match(r"^\*-\*-\* \*:00/(\d{1,2}):00$", value)
    if match:
        minutes = int(match.group(1))
        if minutes <= 0 or minutes > 59:
            raise RuntimeError("Unsupported --schedule interval. Use minutes between 1 and 59.")
        return "StartInterval", minutes * 60
    match = re.match(r"^\*-\*-\* \*:(\d{2}):(\d{2})$", value)
    if match:
        minute = int(match.group(1))
        second = int(match.group(2))
        if second != 0:
            raise RuntimeError("Unsupported --schedule seconds. macOS schedule subset requires :00 seconds.")
        return "StartCalendarInterval", {"Minute": minute}
    match = re.match(r"^\*-\*-\* (\d{2}):(\d{2}):(\d{2})$", value)
    if match:
        hour, minute, second = map(int, match.groups())
        if second != 0:
            raise RuntimeError("Unsupported --schedule seconds. macOS schedule subset requires :00 seconds.")
        return "StartCalendarInterval", {"Hour": hour, "Minute": minute}
    match = re.match(r"^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) \*-\*-\* (\d{2}):(\d{2}):(\d{2})$", value)
    if match:
        weekday, hour, minute, second = match.groups()
        if int(second) != 0:
            raise RuntimeError("Unsupported --schedule seconds. macOS schedule subset requires :00 seconds.")
        return "StartCalendarInterval", {"Weekday": WEEKDAY_MAP[weekday.lower()], "Hour": int(hour), "Minute": int(minute)}
    match = re.match(r"^\*-\*-(\d{2}) (\d{2}):(\d{2}):(\d{2})$", value)
    if match:
        day, hour, minute, second = map(int, match.groups())
        if second != 0:
            raise RuntimeError("Unsupported --schedule seconds. macOS schedule subset requires :00 seconds.")
        return "StartCalendarInterval", {"Day": day, "Hour": hour, "Minute": minute}
    raise RuntimeError(
        "Unsupported --schedule for macOS. Supported subset: "
        "'*-*-* *:00/15:00', '*-*-* *:05:00', '*-*-* 02:30:00', "
        "'Mon *-*-* 08:00:00', '*-*-01 00:01:00'."
    )


def compute_next_run(schedule: str, now: Optional[dt.datetime] = None) -> str:
    if not schedule:
        return "-"
    now = now or dt.datetime.now().astimezone()
    sched_type, data = parse_schedule(schedule)
    if sched_type == "StartInterval":
        seconds = int(data)
        epoch = int(now.timestamp())
        next_epoch = ((epoch // seconds) + 1) * seconds
        return dt.datetime.fromtimestamp(next_epoch, tz=now.tzinfo).strftime("%Y-%m-%d %H:%M")
    if sched_type != "StartCalendarInterval":
        return "-"
    info = dict(data)
    for offset in range(1, 366 * 2):
        candidate_date = now.date() + dt.timedelta(days=offset // 1440)
        for minute_of_day in range(1440):
            candidate = dt.datetime.combine(
                candidate_date,
                dt.time(hour=minute_of_day // 60, minute=minute_of_day % 60, tzinfo=now.tzinfo),
            )
            if candidate <= now:
                continue
            if "Minute" in info and candidate.minute != info["Minute"]:
                continue
            if "Hour" in info and candidate.hour != info["Hour"]:
                continue
            if "Day" in info and candidate.day != info["Day"]:
                continue
            if "Weekday" in info:
                candidate_weekday = (candidate.weekday() + 1) % 7
                if candidate_weekday != info["Weekday"]:
                    continue
            if "Month" in info and candidate.month != info["Month"]:
                continue
            return candidate.strftime("%Y-%m-%d %H:%M")
    return "-"


def format_event_timestamp(value: str) -> str:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return value


def read_event_stats(service: ManagedService) -> Dict[str, object]:
    event_path = event_file_for_service(service.name, service.scope)
    executions = 0
    last_run = "-"
    last_exit_status = "-"
    if not event_path.exists():
        return {
            "executions": 0,
            "restarts": 0,
            "last_run": "-",
            "last_exit_status": "-",
        }
    for raw in event_path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if item.get("event") == "start":
            executions += 1
            last_run = format_event_timestamp(str(item.get("ts", "-")))
        elif item.get("event") == "end":
            last_exit_status = str(item.get("exit_status", "-"))
    restarts = max(0, executions - 1) if (not service.schedule and restart_policy_allows_restart(service.restart)) else 0
    return {
        "executions": executions,
        "restarts": restarts,
        "last_run": last_run,
        "last_exit_status": last_exit_status,
    }


def update_runtime_stats(service: ManagedService) -> Dict[str, Dict[str, object]]:
    ensure_storage()
    try:
        payload = json.loads(RUNTIME_STATS_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {"services": {}}
    if not isinstance(payload, dict):
        payload = {"services": {}}
    services = payload.get("services")
    if not isinstance(services, dict):
        services = {}
        payload["services"] = services
    stats = read_event_stats(service)
    services[service.name] = stats
    RUNTIME_STATS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return services


def format_restarts_exec(service: ManagedService, runtime_stats: Dict[str, Dict[str, object]]) -> str:
    item = runtime_stats.get(service.name)
    if not item:
        return "-"
    return f"{item.get('restarts', 0)}/{item.get('executions', 0)}"


def build_wrapper_script(service: ManagedService) -> str:
    event_file = event_file_for_service(service.name, service.scope)
    event_dir = event_file.parent
    cmd = shell_quote_pretty(service.exec_cmd)
    return (
        "#!/bin/zsh\n"
        "set +e\n"
        f"EVENT_FILE={shell_quote_pretty(str(event_file))}\n"
        f"mkdir -p {shell_quote_pretty(str(event_dir))}\n"
        "ts_start=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        'printf \'{"ts":"%s","event":"start","pid":%s}\\n\' "$ts_start" "$$" >> "$EVENT_FILE"\n'
        "exit_code=0\n"
        f"/bin/zsh -lc {cmd}\n"
        "exit_code=$?\n"
        "ts_end=$(date -u +%Y-%m-%dT%H:%M:%SZ)\n"
        'printf \'{"ts":"%s","event":"end","exit_status":%s}\\n\' "$ts_end" "$exit_code" >> "$EVENT_FILE"\n'
        "exit \"$exit_code\"\n"
    )


def build_environment_variables(service: ManagedService) -> Dict[str, str]:
    env = {}
    if service.scope == "daemon" and service.user:
        home = str(home_for_user(service.user))
        env["HOME"] = home
        env["USER"] = service.user
        env["LOGNAME"] = service.user
        env["PATH"] = f"{home}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    else:
        home = str(current_user_home())
        env["HOME"] = home
        env["USER"] = current_user_name()
        env["LOGNAME"] = current_user_name()
        env["PATH"] = f"{home}/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def build_plist(service: ManagedService) -> Dict[str, object]:
    schedule_type, schedule_value = parse_schedule(service.schedule) if service.schedule else (None, None)
    log_dir = Path(service.log_dir or log_dir_for_service(service.name, service.scope))
    wrapper_path = wrapper_script_for_service(service.name, service.scope)
    plist: Dict[str, object] = {
        "Label": service_label(service.name),
        "ProgramArguments": [str(wrapper_path)],
        "StandardOutPath": str(log_dir / "stdout.log"),
        "StandardErrorPath": str(log_dir / "stderr.log"),
        "WorkingDirectory": service.working_dir or str(current_user_home()),
        "EnvironmentVariables": build_environment_variables(service),
        "ProcessType": "Background",
    }
    if service.scope == "daemon" and service.user:
        plist["UserName"] = service.user
    if schedule_type:
        plist[schedule_type] = schedule_value
        if service.timer_persistent:
            plist["RunAtLoad"] = True
    else:
        plist["RunAtLoad"] = True
        keepalive = restart_policy_to_keepalive(service.restart)
        if keepalive:
            plist["KeepAlive"] = keepalive
    return plist


def install_service_files(service: ManagedService) -> None:
    log_dir = Path(service.log_dir or log_dir_for_service(service.name, service.scope))
    ensure_directory(log_dir, service.scope)
    ensure_directory(event_file_for_service(service.name, service.scope).parent, service.scope)
    write_text_file(wrapper_script_for_service(service.name, service.scope), build_wrapper_script(service), service.scope, executable=True)
    write_plist_file(plist_path_for_service(service), build_plist(service), service.scope)


def bootstrap_service(service: ManagedService) -> None:
    if service_loaded(service):
        launchctl_cmd(service.scope, ["enable", service_target(service)], check=False)
        return
    proc = launchctl_cmd(
        service.scope,
        ["bootstrap", domain_target(service.scope), str(plist_path_for_service(service))],
        check=False,
        capture=True,
    )
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to bootstrap {service.name}. {details}".strip())
    launchctl_cmd(service.scope, ["enable", service_target(service)], check=False)


def bootout_service(service: ManagedService) -> None:
    launchctl_cmd(service.scope, ["bootout", service_target(service)], check=False)


def kickstart_service(service: ManagedService, kill_existing: bool = False) -> subprocess.CompletedProcess:
    args = ["kickstart"]
    if kill_existing:
        args.append("-k")
    args.append(service_target(service))
    return launchctl_cmd(service.scope, args, check=False, capture=True)


def sync_registry_from_launchd(name: Optional[str] = None) -> int:
    services = load_registry()
    changed = 0
    target_names = {name} if name else None
    updated: List[ManagedService] = []
    for svc in services:
        if target_names and svc.name not in target_names:
            updated.append(svc)
            continue
        new_svc = ManagedService(**asdict(svc))
        path = plist_path_for_service(svc)
        if path.exists():
            with path.open("rb") as handle:
                plist = plistlib.load(handle)
            new_svc.working_dir = str(plist.get("WorkingDirectory", new_svc.working_dir))
            new_svc.user = str(plist.get("UserName", new_svc.user))
            new_svc.log_dir = str(Path(str(plist.get("StandardOutPath", new_svc.log_dir))).parent)
        if asdict(new_svc) != asdict(svc):
            changed += 1
            updated.append(new_svc)
        else:
            updated.append(svc)
    if changed:
        save_registry(updated)
    return changed


def create(args: argparse.Namespace) -> None:
    validate_name(args.name)
    scope = resolve_scope(args.scope)
    require_supported_scope_user(scope, args.user or "")
    desc = args.description or f"Skuld service: {args.name}"
    service = ManagedService(
        name=args.name,
        exec_cmd=args.exec,
        description=desc,
        schedule=args.schedule or "",
        working_dir=args.working_dir or "",
        user=args.user or "",
        restart=args.restart,
        timer_persistent=args.timer_persistent,
        scope=scope,
        log_dir=str(log_dir_for_service(args.name, scope)),
    )
    if get_managed(service.name):
        raise RuntimeError(f"'{service.name}' is already registered in skuld.")
    install_service_files(service)
    bootstrap_service(service)
    upsert_registry(service)
    update_runtime_stats(service)
    ok(f"Service '{service.name}' created and registered.")


def managed_uses_schedule(service: ManagedService) -> bool:
    return bool(service.schedule)


def apply_action_for_managed(service: ManagedService, action: str) -> None:
    if action == "start":
        bootstrap_service(service)
        if not managed_uses_schedule(service):
            proc = kickstart_service(service, kill_existing=False)
            if proc.returncode != 0:
                details = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"Failed to start {service.name}. {details}".strip())
        ok(f"start -> {service.name}")
        return
    if action == "stop":
        bootout_service(service)
        ok(f"stop -> {service.name}")
        return
    if action == "restart":
        bootout_service(service)
        bootstrap_service(service)
        if not managed_uses_schedule(service):
            proc = kickstart_service(service, kill_existing=True)
            if proc.returncode != 0:
                details = (proc.stderr or proc.stdout or "").strip()
                raise RuntimeError(f"Failed to restart {service.name}. {details}".strip())
        ok(f"restart -> {service.name}")
        return
    raise RuntimeError(f"Unsupported action: {action}")


def start_stop(args: argparse.Namespace, action: str) -> None:
    for service in resolve_managed_many_arg(args):
        apply_action_for_managed(service, action)


def restart(args: argparse.Namespace) -> None:
    start_stop(args, "restart")


def exec_now(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    bootstrap_service(service)
    proc = kickstart_service(service, kill_existing=False)
    if proc.returncode != 0:
        details = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to execute {service.name}. {details}".strip())
    ok(f"Execution started: {service.name}")


def status(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    info_map = launchctl_service_info(service)
    print(f"name: {service.name}")
    print(f"label: {service_label(service.name)}")
    print(f"scope: {service.scope}")
    print(f"domain: {domain_target(service.scope)}")
    print(f"loaded: {'yes' if info_map else 'no'}")
    print(f"pid: {info_map.get('PID', '-') if info_map else '-'}")
    print(f"last_exit_status: {info_map.get('LastExitStatus', '-') if info_map else '-'}")
    print(f"plist: {plist_path_for_service(service)}")


def tail_file(path: Path, lines: int, follow: bool) -> None:
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(path))
    run(cmd, check=False)


def logs(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    if args.since:
        raise RuntimeError("--since is not supported on macOS yet. Logs are read from files.")
    if args.timer:
        info("--timer has no effect on macOS. launchd uses a single plist/job.")
    lines = resolve_lines_arg(args, default=100)
    log_dir = Path(service.log_dir)
    stdout_path = log_dir / "stdout.log"
    stderr_path = log_dir / "stderr.log"
    if not stdout_path.exists() and not stderr_path.exists():
        print("No logs found.")
        return
    print(f"==> {stdout_path}")
    if stdout_path.exists():
        tail_file(stdout_path, lines, args.follow)
    print()
    print(f"==> {stderr_path}")
    if stderr_path.exists():
        tail_file(stderr_path, lines, args.follow)


def read_cpu_memory(pid: int) -> Dict[str, str]:
    if pid <= 0:
        return {"cpu": "-", "memory": "-"}
    proc = run(["ps", "-o", "%cpu=", "-o", "rss=", "-p", str(pid)], check=False, capture=True)
    output = (proc.stdout or "").strip()
    if not output:
        return {"cpu": "-", "memory": "-"}
    parts = output.split()
    if len(parts) < 2:
        return {"cpu": "-", "memory": "-"}
    cpu = parts[0]
    try:
        memory_kib = int(parts[1])
    except ValueError:
        memory_kib = 0
    return {"cpu": f"{cpu}%", "memory": format_bytes_from_kib(memory_kib)}


def read_ports(pid: int) -> str:
    if pid <= 0:
        return "-"
    proc = run(["lsof", "-Pan", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-iUDP"], check=False, capture=True)
    tags: Set[str] = set()
    for raw in (proc.stdout or "").splitlines()[1:]:
        line = raw.strip()
        tcp_match = re.search(r"TCP .*:(\d+) \(LISTEN\)", line)
        if tcp_match:
            tags.add(f"{tcp_match.group(1)}/tcp")
        udp_match = re.search(r"UDP .*:(\d+)$", line)
        if udp_match:
            tags.add(f"{udp_match.group(1)}/udp")
    if not tags:
        return "-"
    ports = sorted(tags)
    if len(ports) <= 2:
        return ",".join(ports)
    return f"{','.join(ports[:2])}+{len(ports) - 2}"


def read_host_overview() -> Dict[str, str]:
    uptime = "-"
    proc = run(["sysctl", "-n", "kern.boottime"], check=False, capture=True)
    match = re.search(r"sec = (\d+)", proc.stdout or "")
    if match:
        boot_time = int(match.group(1))
        uptime = format_duration_human(max(0, int(dt.datetime.now().timestamp()) - boot_time))
    cpu = "-"
    try:
        load1, load5, load15 = os.getloadavg()
        cores = max(1, os.cpu_count() or 1)
        pct = int((load1 / cores) * 100)
        cpu = f"{load1:.2f} {load5:.2f} {load15:.2f} ({pct}%)"
    except Exception:
        pass
    memory = "-"
    total_proc = run(["sysctl", "-n", "hw.memsize"], check=False, capture=True)
    vm_proc = run(["vm_stat"], check=False, capture=True)
    try:
        total = int((total_proc.stdout or "0").strip())
        page_size_match = re.search(r"page size of (\d+) bytes", vm_proc.stdout or "")
        page_size = int(page_size_match.group(1)) if page_size_match else 4096
        pages_free = 0
        pages_inactive = 0
        pages_speculative = 0
        for raw in (vm_proc.stdout or "").splitlines():
            if ":" not in raw:
                continue
            key, value = raw.split(":", 1)
            count = int(value.strip().strip("."))
            if key.startswith("Pages free"):
                pages_free = count
            elif key.startswith("Pages inactive"):
                pages_inactive = count
            elif key.startswith("Pages speculative"):
                pages_speculative = count
        available = (pages_free + pages_inactive + pages_speculative) * page_size
        used = max(0, total - available)
        if total > 0:
            pct = int((used / total) * 100)
            memory = f"{format_bytes(str(used))}/{format_bytes(str(total))} ({pct}%)"
    except Exception:
        pass
    return {"uptime": uptime, "cpu(load1/5/15)": cpu, "memory": memory}


def render_host_panel() -> None:
    overview = read_host_overview()
    render_table(list(overview.keys()), [list(overview.values())])
    print()


def _render_services_table(compact: bool) -> None:
    sync_registry_from_launchd()
    services = sorted(load_registry(), key=lambda s: s.name.lower())
    if not services:
        print("No services managed by skuld.")
        return
    runtime_stats: Dict[str, Dict[str, object]] = {}
    rows: List[List[str]] = []
    print()
    render_host_panel()
    for service in services:
        runtime_stats[service.name] = read_event_stats(service)
        pid = read_pid(service)
        usage = read_cpu_memory(pid)
        loaded = service_loaded(service)
        kind = "timer" if service.schedule else "daemon"
        if loaded and pid > 0:
            service_state = colorize("active", "green")
        elif loaded:
            service_state = colorize("loaded", "yellow")
        else:
            service_state = colorize("inactive", "yellow")
        timer_state = colorize("scheduled", "green") if service.schedule and loaded else (colorize("inactive", "yellow") if service.schedule else colorize("n/a", "gray"))
        stats = runtime_stats[service.name]
        rows.append(
            [
                str(service.id),
                service.name,
                kind,
                service_state,
                timer_state,
                compute_next_run(service.schedule),
                format_restarts_exec(service, runtime_stats),
                str(stats.get("last_run", "-")),
                service.schedule or "-",
                usage["cpu"],
                usage["memory"],
                read_ports(pid),
            ]
        )
    if compact:
        compact_rows = [[row[0], row[1], row[2], row[3], row[4], row[9], row[10]] for row in rows]
        render_table(["id", "name", "kind", "service", "timer", "cpu", "memory"], compact_rows)
    else:
        render_table(
            ["id", "name", "kind", "service", "timer", "next_run", "r/e", "last_run", "schedule", "cpu", "memory", "ports"],
            rows,
        )
    print()


def list_services(_args: argparse.Namespace) -> None:
    _render_services_table(compact=False)


def list_services_compact() -> None:
    _render_services_table(compact=True)


def stats(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    item = update_runtime_stats(service)[service.name]
    print(f"name: {service.name}")
    print(f"scope: {service.scope}")
    print(f"window: all retained event entries")
    print(f"executions: {item.get('executions', 0)}")
    print(f"restarts: {item.get('restarts', 0)}")
    print(f"last_run: {item.get('last_run', '-')}")
    print(f"last_exit_status: {item.get('last_exit_status', '-')}")


def build_recreate_command(service: ManagedService) -> str:
    lines = [
        "skuld create \\",
        f"  --name {shell_quote_pretty(service.name)} \\",
        f"  --description {shell_quote_pretty(service.description)} \\",
        f"  --scope {service.scope} \\",
    ]
    if service.working_dir:
        lines.append(f"  --working-dir {shell_quote_pretty(service.working_dir)} \\")
    if service.user:
        lines.append(f"  --user {shell_quote_pretty(service.user)} \\")
    lines.append(f"  --restart {shell_quote_pretty(service.restart)} \\")
    lines.append(f"  --exec {shell_quote_pretty(service.exec_cmd)}")
    if service.schedule:
        lines[-1] = lines[-1] + " \\"
        lines.append(f"  --schedule {shell_quote_pretty(service.schedule)} \\")
        lines.append("  --timer-persistent" if service.timer_persistent else "  --no-timer-persistent")
    return "\n".join(lines)


def recreate(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    print(build_recreate_command(service))


def remove(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    bootout_service(service)
    rm_file(plist_path_for_service(service), service.scope)
    rm_file(wrapper_script_for_service(service.name, service.scope), service.scope)
    if args.purge:
        log_dir = Path(service.log_dir)
        if service.scope == "daemon":
            run_sudo(["rm", "-rf", str(log_dir)], check=False)
            run_sudo(["rm", "-f", str(event_file_for_service(service.name, service.scope))], check=False)
        else:
            if log_dir.exists():
                for child in log_dir.iterdir():
                    child.unlink(missing_ok=True)
                log_dir.rmdir()
            event_file_for_service(service.name, service.scope).unlink(missing_ok=True)
        remove_registry(service.name)
    ok(f"Removed: {service.name} (purge={args.purge})")


def doctor(_args: argparse.Namespace) -> None:
    services = load_registry()
    if not services:
        print("No services managed by skuld.")
        return
    issues = 0
    for service in services:
        prefix = f"[{service.name}]"
        plist_path = plist_path_for_service(service)
        if not plist_path.exists():
            print(f"{prefix} ERROR missing plist ({plist_path})")
            issues += 1
        else:
            print(f"{prefix} plist=ok")
        if not wrapper_script_for_service(service.name, service.scope).exists():
            print(f"{prefix} ERROR missing wrapper script")
            issues += 1
        loaded = service_loaded(service)
        print(f"{prefix} loaded={'yes' if loaded else 'no'}")
        if service.scope == "agent" and service.user:
            print(f"{prefix} ERROR agent scope cannot store user")
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
    scope: Optional[str] = None,
) -> bool:
    new_scope = resolve_scope(scope or current.scope)
    new_exec = exec_cmd if exec_cmd is not None else current.exec_cmd
    new_description = description if description is not None else current.description
    new_workdir = working_dir if working_dir is not None else current.working_dir
    new_user = user if user is not None else current.user
    new_restart = restart if restart is not None else current.restart
    new_schedule = schedule if schedule is not None else current.schedule
    if clear_schedule:
        new_schedule = ""
    new_timer_persistent = current.timer_persistent if timer_persistent is None else timer_persistent
    require_supported_scope_user(new_scope, new_user)
    updated = ManagedService(
        name=current.name,
        exec_cmd=new_exec,
        description=new_description,
        schedule=new_schedule,
        working_dir=new_workdir,
        user=new_user,
        restart=new_restart,
        timer_persistent=new_timer_persistent,
        id=current.id,
        backend="launchd",
        scope=new_scope,
        log_dir=str(log_dir_for_service(current.name, new_scope)),
    )
    if asdict(updated) == asdict(current):
        return False
    bootout_service(current)
    install_service_files(updated)
    bootstrap_service(updated)
    upsert_registry(updated)
    return True


def edit(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    schedule = args.schedule
    if schedule == SCHEDULE_PROMPT_SENTINEL and not args.clear_schedule:
        schedule = prompt_schedule_edit(service.schedule)
    changed = apply_managed_update(
        service,
        exec_cmd=args.exec,
        description=args.description,
        working_dir=args.working_dir,
        user=args.user,
        restart=args.restart,
        schedule=schedule,
        timer_persistent=args.timer_persistent,
        clear_schedule=args.clear_schedule,
        scope=args.scope,
    )
    if not changed:
        info("No changes detected.")
        return
    ok(f"Service '{service.name}' updated.")


def describe(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args)
    if not service:
        raise RuntimeError("Service target is required.")
    info_map = launchctl_service_info(service)
    stats_map = read_event_stats(service)
    print(f"name: {service.name}")
    print(f"description: {service.description}")
    print(f"exec: {service.exec_cmd}")
    print(f"scope: {service.scope}")
    print(f"user: {service.user or '-'}")
    print(f"working_dir: {service.working_dir or '-'}")
    print(f"restart: {service.restart}")
    print(f"schedule: {service.schedule or '-'}")
    print(f"timer_persistent: {service.timer_persistent}")
    print(f"log_dir: {service.log_dir}")
    print("---")
    print(f"loaded: {'yes' if info_map else 'no'}")
    print(f"pid: {info_map.get('PID', '-') if info_map else '-'}")
    print(f"last_exit_status: {info_map.get('LastExitStatus', '-') if info_map else '-'}")
    print(f"next_run: {compute_next_run(service.schedule)}")
    print(f"last_run: {stats_map.get('last_run', '-')}")
    print(f"plist: {plist_path_for_service(service)}")


def sync(args: argparse.Namespace) -> None:
    service = resolve_managed_arg(args, required=False)
    name = service.name if service else None
    changed = sync_registry_from_launchd(name)
    if changed == 0:
        ok("Registry is already up to date.")
    else:
        ok(f"Registry updated for {changed} service(s).")


def adopt(_args: argparse.Namespace) -> None:
    raise RuntimeError("adopt is not supported on macOS.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="skuld", description="CLI for managing launchd services")
    parser.add_argument(
        "--no-env-sudo",
        action="store_true",
        help="Disable SKULD_SUDO_PASSWORD from env/.env and use regular sudo behavior",
    )
    parser.add_argument("--ascii", action="store_true", help="Force ASCII table borders")
    parser.add_argument("--unicode", action="store_true", help="Force Unicode table borders")
    sub = parser.add_subparsers(dest="command", required=False)

    create_parser = sub.add_parser("create", help="Create and install a launchd job")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--exec", required=True, help="Program command")
    create_parser.add_argument("--description")
    create_parser.add_argument("--working-dir")
    create_parser.add_argument("--user")
    create_parser.add_argument("--restart", default="on-failure")
    create_parser.add_argument("--schedule", help="Supported subset of systemd-like schedule expressions")
    create_parser.add_argument("--timer-persistent", action=argparse.BooleanOptionalAction, default=True)
    create_parser.add_argument("--scope", choices=["daemon", "agent"], default="daemon")
    create_parser.set_defaults(func=create)

    list_parser = sub.add_parser("list", help="List services managed by skuld")
    list_parser.set_defaults(func=list_services)

    exec_parser = sub.add_parser("exec", help="Execute a service immediately")
    exec_parser.add_argument("name", nargs="?")
    exec_parser.add_argument("--name", dest="name_flag")
    exec_parser.add_argument("--id", dest="id_flag", type=int)
    exec_parser.set_defaults(func=exec_now)

    start_parser = sub.add_parser("start", help="Start one or more services")
    start_parser.add_argument("targets", nargs="*", help="Service target(s): managed NAME and/or ID")
    start_parser.add_argument("--name", dest="name_flag")
    start_parser.add_argument("--id", dest="id_flag", type=int)
    start_parser.set_defaults(func=lambda a: start_stop(a, "start"))

    stop_parser = sub.add_parser("stop", help="Stop one or more services")
    stop_parser.add_argument("targets", nargs="*", help="Service target(s): managed NAME and/or ID")
    stop_parser.add_argument("--name", dest="name_flag")
    stop_parser.add_argument("--id", dest="id_flag", type=int)
    stop_parser.set_defaults(func=lambda a: start_stop(a, "stop"))

    restart_parser = sub.add_parser("restart", help="Restart one or more services")
    restart_parser.add_argument("targets", nargs="*", help="Service target(s): managed NAME and/or ID")
    restart_parser.add_argument("--name", dest="name_flag")
    restart_parser.add_argument("--id", dest="id_flag", type=int)
    restart_parser.set_defaults(func=restart)

    status_parser = sub.add_parser("status", help="Service status")
    status_parser.add_argument("name", nargs="?")
    status_parser.add_argument("--name", dest="name_flag")
    status_parser.add_argument("--id", dest="id_flag", type=int)
    status_parser.set_defaults(func=status)

    logs_parser = sub.add_parser("logs", help="Show logs from files")
    logs_parser.add_argument("name", nargs="?")
    logs_parser.add_argument("lines_pos", nargs="?", type=int)
    logs_parser.add_argument("--name", dest="name_flag")
    logs_parser.add_argument("--id", dest="id_flag", type=int)
    logs_parser.add_argument("--lines", type=int, default=None)
    logs_parser.add_argument("--follow", action="store_true", help="Follow logs in real time")
    logs_parser.add_argument("--folow", dest="follow", action="store_true", help=argparse.SUPPRESS)
    logs_parser.add_argument("--since", help="Not supported on macOS file logs")
    logs_parser.add_argument("--timer", action="store_true", help="No effect on macOS; kept for CLI compatibility")
    logs_parser.add_argument("--output", default="short", help="Ignored on macOS file logs")
    logs_parser.add_argument("--plain", action="store_true", help="Ignored on macOS file logs")
    logs_parser.set_defaults(func=logs)

    stats_parser = sub.add_parser("stats", help="Show execution/restart counters for a managed service")
    stats_parser.add_argument("name", nargs="?")
    stats_parser.add_argument("--name", dest="name_flag")
    stats_parser.add_argument("--id", dest="id_flag", type=int)
    stats_parser.add_argument("--since", help="Ignored on macOS event stats")
    stats_parser.add_argument("--boot", action="store_true", help="Ignored on macOS event stats")
    stats_parser.set_defaults(func=stats)

    remove_parser = sub.add_parser("remove", help="Remove jobs")
    remove_parser.add_argument("name", nargs="?")
    remove_parser.add_argument("--name", dest="name_flag")
    remove_parser.add_argument("--id", dest="id_flag", type=int)
    remove_parser.add_argument("--purge", action="store_true")
    remove_parser.set_defaults(func=remove)

    adopt_parser = sub.add_parser("adopt", help="Not supported on macOS")
    adopt_parser.add_argument("name", nargs="?")
    adopt_parser.add_argument("--name", dest="name_flag")
    adopt_parser.set_defaults(func=adopt)

    doctor_parser = sub.add_parser("doctor", help="Check registry/launchd inconsistencies")
    doctor_parser.set_defaults(func=doctor)

    edit_parser = sub.add_parser("edit", help="Edit a managed service definition")
    edit_parser.add_argument("name", nargs="?")
    edit_parser.add_argument("--name", dest="name_flag")
    edit_parser.add_argument("--id", dest="id_flag", type=int)
    edit_parser.add_argument("--exec")
    edit_parser.add_argument("--description")
    edit_parser.add_argument("--working-dir")
    edit_parser.add_argument("--user")
    edit_parser.add_argument("--restart")
    edit_parser.add_argument(
        "--schedule",
        nargs="?",
        const=SCHEDULE_PROMPT_SENTINEL,
        help="Supported subset of systemd-like schedule expressions. If omitted, opens interactive edit.",
    )
    edit_parser.add_argument("--clear-schedule", action="store_true")
    edit_parser.add_argument("--timer-persistent", action=argparse.BooleanOptionalAction, default=None)
    edit_parser.add_argument("--scope", choices=["daemon", "agent"])
    edit_parser.set_defaults(func=edit)

    describe_parser = sub.add_parser("describe", help="Show details for a managed service")
    describe_parser.add_argument("name", nargs="?")
    describe_parser.add_argument("--name", dest="name_flag")
    describe_parser.add_argument("--id", dest="id_flag", type=int)
    describe_parser.set_defaults(func=describe)

    recreate_parser = sub.add_parser("recreate", help="Print equivalent skuld create command for a managed service")
    recreate_parser.add_argument("name", nargs="?")
    recreate_parser.add_argument("--name", dest="name_flag")
    recreate_parser.add_argument("--id", dest="id_flag", type=int)
    recreate_parser.set_defaults(func=recreate)

    sync_parser = sub.add_parser("sync", help="Backfill missing registry fields from launchd")
    sync_parser.add_argument("name", nargs="?", help="Sync only one managed service")
    sync_parser.add_argument("--name", dest="name_flag", help="Sync only one managed service")
    sync_parser.add_argument("--id", dest="id_flag", type=int, help="Sync only one managed service by id")
    sync_parser.set_defaults(func=sync)

    version_parser = sub.add_parser("version", help="Show version")
    version_parser.set_defaults(func=lambda _args: print(VERSION))

    return parser


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
        "--scope",
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
        if args.command in {"create", "exec", "start", "stop", "restart", "remove", "edit", "sync"}:
            print()
            list_services(argparse.Namespace())
        return 0
    except RuntimeError as exc:
        err(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
