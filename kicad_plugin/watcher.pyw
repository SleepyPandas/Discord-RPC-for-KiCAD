from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parent
VENDOR_DIR = PLUGIN_DIR / "vendor"
if str(VENDOR_DIR) not in sys.path:
    sys.path.insert(0, str(VENDOR_DIR))
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from pypresence import Presence
from pypresence.types import StatusDisplayType
from shared_config import get_config_directory, get_config_path, load_config_document

try:
    import pcbnew  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only available inside KiCad's Python.
    pcbnew = None  # type: ignore[assignment]

DEFAULT_APP_NAME = "KiCad"
DEFAULT_HIDDEN_TEXT = "Working on a generic project"
DETAILS_TEXT_LIMIT = 48
STATE_TEXT_LIMIT = 48
CONFIG_PATH = get_config_path()
CONFIG_DIRECTORY = get_config_directory()
WATCHER_LOG_PATH = CONFIG_DIRECTORY / "watcher.log"
WATCHER_STATE_PATH = CONFIG_DIRECTORY / "watcher-state.json"
LEGACY_CONFIG_PATH = PLUGIN_DIR.parent / "config.json"
WATCHER_MUTEX_NAME = "Local\\DiscordRpcForKiCadWatcher"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
ERROR_ALREADY_EXISTS = 183
SCHEMATIC_CUSTOM_LABEL_PATTERN = re.compile(
    r'\((?:global_label|hierarchical_label|label)\s+"([^"]+)"'
)
WINDOW_TITLE_SPLIT_PATTERN = re.compile(r"\s+(?:-|\u2013|\u2014)\s+")

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
mutex_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    ctypes.POINTER(wintypes.DWORD),
]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
mutex_kernel32.CreateMutexW.restype = wintypes.HANDLE
mutex_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
mutex_kernel32.CloseHandle.restype = wintypes.BOOL
_WATCHER_MUTEX_HANDLE: int | None = None


class PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_ulong),
        ("cntUsage", ctypes.c_ulong),
        ("th32ProcessID", ctypes.c_ulong),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", ctypes.c_ulong),
        ("cntThreads", ctypes.c_ulong),
        ("th32ParentProcessID", ctypes.c_ulong),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", ctypes.c_ulong),
        ("szExeFile", ctypes.c_char * 260),
    ]


class EditorType(str, Enum):
    GENERIC = "generic"
    PCB = "pcb"
    SCHEMATIC = "schematic"


@dataclass(frozen=True)
class AppConfig:
    discord_client_id: str
    hide_filename: bool = False
    hidden_project_text: str = DEFAULT_HIDDEN_TEXT
    poll_interval_seconds: int = 5
    idle_threshold_seconds: int = 300
    large_image: str = ""
    large_text: str = ""
    log_level: str = "INFO"

    @classmethod
    def load(cls) -> "AppConfig":
        raw_config = load_config_document(
            CONFIG_PATH,
            legacy_candidates=(LEGACY_CONFIG_PATH,),
        )
        client_id = str(raw_config.get("discord_client_id", "")).strip()
        if not client_id or client_id == "YOUR_DISCORD_APPLICATION_CLIENT_ID":
            raise ValueError(f"{CONFIG_PATH} must contain a valid Discord application client ID.")
        return cls(
            discord_client_id=client_id,
            hide_filename=bool(raw_config.get("hide_filename", False)),
            hidden_project_text=str(
                raw_config.get("hidden_project_text", DEFAULT_HIDDEN_TEXT)
            ).strip()
            or DEFAULT_HIDDEN_TEXT,
            poll_interval_seconds=max(3, int(raw_config.get("poll_interval_seconds", 5))),
            idle_threshold_seconds=max(60, int(raw_config.get("idle_threshold_seconds", 300))),
            large_image=str(raw_config.get("large_image", "")).strip(),
            large_text=str(raw_config.get("large_text", "")).strip(),
            log_level=str(raw_config.get("log_level", "INFO")).strip().upper() or "INFO",
        )


@dataclass(frozen=True)
class WindowInfo:
    process_name: str
    process_id: int | None
    title: str
    editor: EditorType | None


@dataclass(frozen=True)
class ActivitySnapshot:
    editor: EditorType
    display_name: str
    project_name: str
    project_path: str | None
    window_title: str
    state_text: str
    fingerprint: str


class WatcherStateWriter:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._last_payload: dict[str, Any] | None = None

    def write(
        self,
        status: str,
        *,
        snapshot: ActivitySnapshot | None = None,
        details: str | None = None,
        state: str | None = None,
        message: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {"status": status, "pid": os.getpid()}
        if message:
            payload["message"] = message
        if snapshot is not None:
            payload.update(
                {
                    "editor": snapshot.editor.value,
                    "project_name": snapshot.project_name,
                    "display_name": snapshot.display_name,
                    "project_path": snapshot.project_path,
                    "window_title": snapshot.window_title,
                    "snapshot_state": snapshot.state_text,
                    "fingerprint": snapshot.fingerprint,
                }
            )
        if details is not None:
            payload["details"] = details
        if state is not None:
            payload["state"] = state

        if payload == self._last_payload:
            return

        serialized = json.dumps(
            {**payload, "timestamp": int(time.time())},
            indent=2,
            ensure_ascii=True,
        ) + "\n"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(serialized, encoding="utf-8")
        self._last_payload = dict(payload)
        logging.info("Watcher state=%s details=%s state=%s", status, details or "", state or "")


class DiscordRpcClient:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._rpc: Presence | None = None
        self._connected = False
        self._last_payload: dict[str, Any] | None = None

    def set_config(self, config: AppConfig) -> None:
        if config.discord_client_id != self._config.discord_client_id:
            self.reset()
        self._config = config

    def ensure_connected(self) -> bool:
        if self._connected and self._rpc is not None:
            return True
        try:
            self._rpc = Presence(self._config.discord_client_id)
            self._rpc.connect()
            self._connected = True
            self._last_payload = None
            logging.info("Connected to Discord RPC.")
            return True
        except Exception as exc:
            self._rpc = None
            self._connected = False
            logging.debug("Discord RPC connection failed: %s", exc)
            return False

    def publish(self, payload: dict[str, Any]) -> None:
        if payload == self._last_payload:
            return
        if not self.ensure_connected():
            return
        try:
            assert self._rpc is not None
            self._rpc.update(**payload)
            self._last_payload = dict(payload)
        except Exception as exc:
            logging.warning("Discord RPC update failed: %s", exc)
            self.reset()

    def clear(self) -> None:
        if not self._connected or self._rpc is None:
            self._last_payload = None
            return
        try:
            self._rpc.clear()
        except Exception as exc:
            logging.debug("Discord clear failed: %s", exc)
        finally:
            self._last_payload = None

    def reset(self) -> None:
        if self._rpc is not None:
            try:
                self._rpc.close()
            except Exception:
                pass
        self._rpc = None
        self._connected = False
        self._last_payload = None


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if any(getattr(handler, "_discord_rpc_watcher", False) for handler in root_logger.handlers):
        return
    WATCHER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(WATCHER_LOG_PATH, encoding="utf-8")
    handler._discord_rpc_watcher = True  # type: ignore[attr-defined]
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
    root_logger.addHandler(handler)


def acquire_single_instance() -> bool:
    global _WATCHER_MUTEX_HANDLE
    handle = mutex_kernel32.CreateMutexW(None, False, WATCHER_MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        mutex_kernel32.CloseHandle(handle)
        return False
    _WATCHER_MUTEX_HANDLE = handle
    return True


def release_single_instance() -> None:
    global _WATCHER_MUTEX_HANDLE
    if _WATCHER_MUTEX_HANDLE is None:
        return
    mutex_kernel32.CloseHandle(_WATCHER_MUTEX_HANDLE)
    _WATCHER_MUTEX_HANDLE = None


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def get_window_text(hwnd: wintypes.HWND) -> str:
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value.strip()


def get_process_name(process_id: int | None) -> str:
    if process_id is None:
        return ""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not handle:
        return ""
    try:
        buffer_size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(buffer_size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(buffer_size)):
            return ""
        return Path(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def is_kicad_process_name(process_name: str) -> bool:
    return process_name.lower() in {"kicad.exe", "kicad", "pcbnew.exe", "pcbnew", "eeschema.exe", "eeschema"}


def detect_active_window() -> WindowInfo | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    resolved_process_id = int(process_id.value) if process_id.value else None
    process_name = get_process_name(resolved_process_id)
    title = get_window_text(hwnd)
    lowered_title = title.lower()
    editor = None
    if is_kicad_process_name(process_name):
        if any(marker in lowered_title for marker in ("schematic editor", "eeschema", ".kicad_sch")):
            editor = EditorType.SCHEMATIC
        elif any(marker in lowered_title for marker in ("pcb editor", "pcbnew", ".kicad_pcb")):
            editor = EditorType.PCB
    return WindowInfo(process_name=process_name, process_id=resolved_process_id, title=title, editor=editor)


def any_kicad_running() -> bool:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return False
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        if not kernel32.Process32First(snapshot, ctypes.byref(entry)):
            return False
        while True:
            if is_kicad_process_name(entry.szExeFile.decode(errors="ignore").lower()):
                return True
            if not kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                return False
    finally:
        kernel32.CloseHandle(snapshot)


def clean_project_name(value: str) -> str:
    return value.strip().lstrip("*").strip()


def is_editor_title_fragment(value: str) -> bool:
    lowered_value = value.lower()
    return any(
        marker in lowered_value
        for marker in (
            "pcb editor",
            "pcbnew",
            "schematic editor",
            "eeschema",
            "project manager",
            "kicad",
        )
    )


def shorten_display_name(name: str, config: AppConfig) -> str:
    if config.hide_filename:
        return config.hidden_project_text
    return clean_project_name(name) or "Untitled Project"


def truncate_presence_text(value: str, limit: int) -> str:
    cleaned_value = re.sub(r"\s+", " ", value).strip()
    if len(cleaned_value) <= limit:
        return cleaned_value
    if limit <= 3:
        return cleaned_value[:limit]
    return f"{cleaned_value[: limit - 3].rstrip()}..."


def format_compact_count(value: int) -> str:
    absolute_value = abs(value)
    if absolute_value < 1000:
        return str(value)

    scaled_value = value / 1000
    suffix = "k"
    if absolute_value >= 1_000_000:
        scaled_value = value / 1_000_000
        suffix = "M"

    compact_value = f"{scaled_value:.1f}".rstrip("0").rstrip(".")
    return f"{compact_value}{suffix}"


def get_file_mtime_ns(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def extract_project_name_from_window_title(title: str) -> str:
    trimmed_title = clean_project_name(title)
    if not trimmed_title:
        return "Untitled Project"
    title_parts = [
        clean_project_name(part)
        for part in WINDOW_TITLE_SPLIT_PATTERN.split(trimmed_title)
        if clean_project_name(part)
    ]
    if not title_parts:
        return "Untitled Project"
    non_editor_parts = [part for part in title_parts if not is_editor_title_fragment(part)]
    if non_editor_parts:
        return non_editor_parts[0]
    return title_parts[0]


def find_path_in_title(title: str, extension: str) -> Path | None:
    match = re.search(rf"([A-Za-z]:\\[^|]+?{re.escape(extension)})", title)
    if not match:
        return None
    candidate = Path(match.group(1))
    return candidate if candidate.exists() else None


def iter_kicad_version_directories() -> list[Path]:
    root = Path(os.environ.get("APPDATA", "")) / "kicad"
    if not root.is_dir():
        return []
    return sorted((path for path in root.iterdir() if path.is_dir()), reverse=True)


def collect_matching_paths(value: Any, suffix: str) -> list[Path]:
    matches: list[Path] = []
    if isinstance(value, dict):
        for nested_value in value.values():
            matches.extend(collect_matching_paths(nested_value, suffix))
    elif isinstance(value, list):
        for nested_value in value:
            matches.extend(collect_matching_paths(nested_value, suffix))
    elif isinstance(value, str) and value.lower().endswith(suffix.lower()):
        candidate = Path(value)
        if candidate.is_file():
            matches.append(candidate)
    return matches


def load_recent_kicad_paths(json_filename: str, suffix: str) -> list[Path]:
    collected_paths: list[Path] = []
    seen_paths: set[Path] = set()
    for version_dir in iter_kicad_version_directories():
        json_path = version_dir / json_filename
        if not json_path.is_file():
            continue
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for candidate in collect_matching_paths(data, suffix):
            if candidate in seen_paths:
                continue
            seen_paths.add(candidate)
            collected_paths.append(candidate)
    return collected_paths


def find_recent_paths_by_project_name(
    project_name: str,
    json_filename: str,
    suffix: str,
) -> list[Path]:
    normalized_project_name = clean_project_name(project_name).casefold()
    if not normalized_project_name:
        return []

    exact_matches: list[Path] = []
    partial_matches: list[Path] = []
    for candidate in load_recent_kicad_paths(json_filename, suffix):
        candidate_stem = clean_project_name(candidate.stem).casefold()
        if candidate_stem == normalized_project_name:
            exact_matches.append(candidate)
        elif normalized_project_name in candidate_stem:
            partial_matches.append(candidate)

    return exact_matches or partial_matches


def discover_project_dir_from_title(title: str) -> Path | None:
    project_name = extract_project_name_from_window_title(title)
    if not project_name or project_name == "Untitled Project":
        return None
    search_roots = [
        Path(os.environ.get("USERPROFILE", "")) / "Documents" / "GitHub KiCAD",
        Path(os.environ.get("USERPROFILE", "")) / "Documents" / "KiCad Projects",
        Path(os.environ.get("USERPROFILE", "")) / "Documents",
    ]
    for base in search_roots:
        if not base.is_dir():
            continue
        for candidate in base.rglob(f"{project_name}.kicad_pro"):
            return candidate.parent
    return None


def discover_pcb_file(window: WindowInfo) -> Path | None:
    title_path = find_path_in_title(window.title, ".kicad_pcb")
    if title_path is not None:
        return title_path
    project_name = extract_project_name_from_window_title(window.title)
    recent_matches = find_recent_paths_by_project_name(
        project_name,
        "pcbnew.json",
        ".kicad_pcb",
    )
    if recent_matches:
        return recent_matches[0]
    project_dir = discover_project_dir_from_title(window.title)
    if project_dir is None:
        return None
    named_candidate = project_dir / f"{project_name}.kicad_pcb"
    if named_candidate.is_file():
        return named_candidate
    for candidate in sorted(project_dir.glob("*.kicad_pcb")):
        return candidate
    return None


def discover_schematic_files(window: WindowInfo) -> list[Path]:
    title_path = find_path_in_title(window.title, ".kicad_sch")
    if title_path is not None:
        return [title_path]
    project_name = extract_project_name_from_window_title(window.title)
    recent_matches = find_recent_paths_by_project_name(
        project_name,
        "eeschema.json",
        ".kicad_sch",
    )
    if recent_matches:
        return recent_matches
    project_dir = discover_project_dir_from_title(window.title)
    if project_dir is None:
        return []
    return sorted(
        path
        for path in project_dir.rglob("*.kicad_sch")
        if ".history" not in path.parts
    )


def build_pcb_state_text(pcb_path: Path | None) -> str:
    if pcb_path is None or pcbnew is None:
        return "Editing PCB"

    try:
        board = pcbnew.LoadBoard(str(pcb_path))
    except Exception as exc:
        logging.debug("Unable to load PCB details from %s: %s", pcb_path, exc)
        return "Editing PCB"

    try:
        layers = board.GetCopperLayerCount()
    except Exception:
        layers = None

    try:
        footprint_count = len(list(board.GetFootprints()))
    except Exception:
        footprint_count = None

    try:
        all_tracks = list(board.GetTracks())
        via_count = sum(1 for track in all_tracks if isinstance(track, pcbnew.PCB_VIA))
        track_count = max(0, len(all_tracks) - via_count)
    except Exception:
        track_count = None
        via_count = None

    try:
        drawing_count = len(list(board.GetDrawings()))
    except Exception:
        drawing_count = 0

    try:
        zone_count = int(board.GetAreaCount())
    except Exception:
        zone_count = 0

    item_count = None
    if footprint_count is not None and track_count is not None and via_count is not None:
        item_count = footprint_count + track_count + via_count + drawing_count + zone_count

    stats: list[str] = []
    if item_count is not None:
        stats.append(f"{format_compact_count(item_count)} Items")
    if track_count is not None:
        stats.append(f"{format_compact_count(track_count)} Tracks")
    if via_count is not None:
        stats.append(f"{format_compact_count(via_count)} Vias")
    if layers is not None:
        stats.append(f"{layers} Layers")

    if stats:
        return " | ".join(stats)

    return "Editing PCB"


def count_custom_labels_in_schematics(schematic_files: list[Path]) -> int | None:
    if not schematic_files:
        return None
    custom_labels: set[str] = set()
    readable_file_seen = False
    for schematic_file in schematic_files:
        try:
            content = schematic_file.read_text(encoding="utf-8", errors="ignore")
            readable_file_seen = True
        except OSError:
            continue
        for match in SCHEMATIC_CUSTOM_LABEL_PATTERN.finditer(content):
            label_value = match.group(1).strip()
            if label_value:
                custom_labels.add(label_value)
    return len(custom_labels) if readable_file_seen else None


def build_snapshot(config: AppConfig) -> ActivitySnapshot | None:
    window = detect_active_window()
    if window is None:
        return build_background_snapshot() if any_kicad_running() else None
    if window.editor is EditorType.PCB:
        return build_pcb_snapshot(window, config)
    if window.editor is EditorType.SCHEMATIC:
        return build_schematic_snapshot(window, config)
    if is_kicad_process_name(window.process_name) or any_kicad_running():
        return build_background_snapshot()
    return None


def build_background_snapshot() -> ActivitySnapshot:
    return ActivitySnapshot(
        editor=EditorType.GENERIC,
        display_name=DEFAULT_APP_NAME,
        project_name=DEFAULT_APP_NAME,
        project_path=None,
        window_title="",
        state_text="Open in background",
        fingerprint="kicad-background-open",
    )


def build_pcb_snapshot(window: WindowInfo, config: AppConfig) -> ActivitySnapshot:
    pcb_path = discover_pcb_file(window)
    project_name = extract_project_name_from_window_title(window.title)
    if project_name == "Untitled Project" and pcb_path is not None:
        project_name = pcb_path.stem or project_name
    state_text = build_pcb_state_text(pcb_path)
    return ActivitySnapshot(
        editor=EditorType.PCB,
        display_name=shorten_display_name(project_name, config),
        project_name=project_name,
        project_path=str(pcb_path) if pcb_path is not None else None,
        window_title=window.title,
        state_text=state_text,
        fingerprint=sha1_text(f"{window.title}|{state_text}|{pcb_path}|{get_file_mtime_ns(pcb_path)}|pcb"),
    )


def build_schematic_snapshot(window: WindowInfo, config: AppConfig) -> ActivitySnapshot:
    schematic_files = discover_schematic_files(window)
    project_name = extract_project_name_from_window_title(window.title)
    if project_name == "Untitled Project" and schematic_files:
        project_name = schematic_files[0].stem or project_name
    custom_net_count = count_custom_labels_in_schematics(schematic_files)
    state_text = (
        f"Editing | {custom_net_count} Total Nets"
        if custom_net_count is not None
        else "Editing"
    )
    fingerprint = sha1_text(
        "|".join([window.title, state_text, *(f"{path}:{get_file_mtime_ns(path)}" for path in schematic_files)])
    )
    return ActivitySnapshot(
        editor=EditorType.SCHEMATIC,
        display_name=shorten_display_name(project_name, config),
        project_name=project_name,
        project_path=str(schematic_files[0]) if schematic_files else None,
        window_title=window.title,
        state_text=state_text,
        fingerprint=fingerprint,
    )


def build_presence_details(snapshot: ActivitySnapshot) -> str:
    if snapshot.editor is EditorType.GENERIC:
        return "KiCad"
    editor_label = "PCB" if snapshot.editor is EditorType.PCB else "SCH"
    return truncate_presence_text(
        f"KiCad {editor_label}: {snapshot.display_name}",
        DETAILS_TEXT_LIMIT,
    )


def build_presence_state(snapshot: ActivitySnapshot, is_idle: bool) -> str:
    if snapshot.editor is EditorType.GENERIC:
        return "Open in background"
    if not is_idle:
        return truncate_presence_text(snapshot.state_text, STATE_TEXT_LIMIT)
    return truncate_presence_text(f"Idle | {snapshot.state_text}", STATE_TEXT_LIMIT)


def main() -> int:
    if not acquire_single_instance():
        return 0

    state_writer = WatcherStateWriter(WATCHER_STATE_PATH)
    discord_client: DiscordRpcClient | None = None
    last_snapshot: ActivitySnapshot | None = None
    last_change_time = time.monotonic()
    session_start_timestamp: int | None = None
    sleep_seconds = 5

    try:
        while True:
            try:
                config = AppConfig.load()
                configure_logging(config.log_level)
                sleep_seconds = config.poll_interval_seconds
                if discord_client is None:
                    discord_client = DiscordRpcClient(config)
                else:
                    discord_client.set_config(config)

                snapshot = build_snapshot(config)
                if snapshot is None:
                    if discord_client is not None:
                        discord_client.clear()
                    last_snapshot = None
                    last_change_time = time.monotonic()
                    session_start_timestamp = None
                    state_writer.write("waiting_for_kicad")
                else:
                    if snapshot != last_snapshot:
                        last_snapshot = snapshot
                        last_change_time = time.monotonic()
                        if session_start_timestamp is None:
                            session_start_timestamp = int(time.time())

                    is_idle = (time.monotonic() - last_change_time) >= config.idle_threshold_seconds
                    payload: dict[str, Any] = {
                        "name": DEFAULT_APP_NAME,
                        "status_display_type": StatusDisplayType.NAME,
                        "details": build_presence_details(snapshot),
                        "state": build_presence_state(snapshot, is_idle),
                        "start": session_start_timestamp or int(time.time()),
                    }
                    if config.large_image:
                        payload["large_image"] = config.large_image
                    if config.large_text:
                        payload["large_text"] = config.large_text
                    if discord_client is not None:
                        discord_client.publish(payload)
                    state_writer.write(
                        "active",
                        snapshot=snapshot,
                        details=str(payload["details"]),
                        state=str(payload["state"]),
                    )
            except Exception as exc:
                logging.exception("Unexpected error in watcher loop.")
                state_writer.write("error", message=str(exc))
                if discord_client is not None:
                    discord_client.reset()
            time.sleep(sleep_seconds)
    finally:
        if discord_client is not None:
            discord_client.clear()
            discord_client.reset()
        release_single_instance()


if __name__ == "__main__":
    raise SystemExit(main())
