from __future__ import annotations

import ctypes
import hashlib
import logging
import re
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable

try:
    import pcbnew  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only available inside KiCad.
    pcbnew = None  # type: ignore[assignment]

try:
    import wx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only available inside KiCad.
    wx = None  # type: ignore[assignment]

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
if VENDOR_DIR.is_dir():
    vendor_path = str(VENDOR_DIR)
    if vendor_path not in sys.path:
        sys.path.insert(0, vendor_path)

try:
    from pypresence import Presence
    from pypresence.types import StatusDisplayType
except ImportError:  # pragma: no cover - vendored for the PCM package.
    Presence = None  # type: ignore[assignment]
    StatusDisplayType = None  # type: ignore[assignment]

from .shared_config import get_config_path, load_config_document

LOGGER = logging.getLogger(__name__)
DEFAULT_HIDDEN_TEXT = "Working on a generic project"
DEFAULT_APP_NAME = "KiCad 10"
DETAILS_TEXT_LIMIT = 48
STATE_TEXT_LIMIT = 48
LEGACY_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
CONFIG_PATH = get_config_path()
RUNTIME_ATTRIBUTE_NAME = "_discord_rpc_for_kicad_runtime"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SCHEMATIC_CUSTOM_LABEL_PATTERN = re.compile(
    r'\((?:global_label|hierarchical_label|label)\s+"([^"]+)"'
)

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
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


class EditorType(str, Enum):
    GENERIC = "generic"
    PCB = "pcb"
    SCHEMATIC = "schematic"


@dataclass(frozen=True)
class AppConfig:
    discord_client_id: str
    hide_filename: bool = False
    hidden_project_text: str = DEFAULT_HIDDEN_TEXT
    poll_interval_seconds: int = 10
    idle_threshold_seconds: int = 300
    large_image: str = ""
    large_text: str = ""
    log_level: str = "INFO"

    @classmethod
    def load(cls, config_path: Path) -> "AppConfig":
        raw_config = load_config_document(
            config_path,
            legacy_candidates=(LEGACY_CONFIG_PATH,),
        )
        client_id = str(raw_config.get("discord_client_id", "")).strip()
        if not client_id or client_id == "YOUR_DISCORD_APPLICATION_CLIENT_ID":
            raise ValueError(
                f"{config_path} must contain a valid Discord application client ID."
            )

        return cls(
            discord_client_id=client_id,
            hide_filename=bool(raw_config.get("hide_filename", False)),
            hidden_project_text=str(
                raw_config.get("hidden_project_text", DEFAULT_HIDDEN_TEXT)
            ).strip()
            or DEFAULT_HIDDEN_TEXT,
            poll_interval_seconds=max(3, int(raw_config.get("poll_interval_seconds", 10))),
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


def configure_logging(level_name: str) -> None:
    if logging.getLogger().handlers:
        logging.getLogger().setLevel(getattr(logging, level_name, logging.INFO))
        return

    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )


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
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(buffer_size)
        ):
            return ""
        process_path = buffer.value
        return Path(process_path).name.lower()
    finally:
        kernel32.CloseHandle(handle)


def is_kicad_process_name(process_name: str) -> bool:
    return process_name.lower() in {
        "kicad.exe", "kicad",
        "pcbnew.exe", "pcbnew",
        "eeschema.exe", "eeschema",
    }


def detect_editor_type(process_name: str, title: str) -> EditorType | None:
    if not is_kicad_process_name(process_name):
        return None

    lowered_title = title.lower()
    if any(
        marker in lowered_title
        for marker in ("schematic editor", "eeschema", ".kicad_sch")
    ):
        return EditorType.SCHEMATIC

    if any(marker in lowered_title for marker in ("pcb editor", "pcbnew", ".kicad_pcb")):
        return EditorType.PCB

    return None


def detect_active_window() -> WindowInfo | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    resolved_process_id = int(process_id.value) if process_id.value else None
    process_name = get_process_name(resolved_process_id)
    title = get_window_text(hwnd)

    return WindowInfo(
        process_name=process_name,
        process_id=resolved_process_id,
        title=title,
        editor=detect_editor_type(process_name, title),
    )


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def shorten_display_name(name: str, config: AppConfig) -> str:
    if config.hide_filename:
        return config.hidden_project_text

    cleaned_name = name.strip()
    return cleaned_name or "Untitled Project"


def truncate_presence_text(value: str, limit: int) -> str:
    cleaned_value = re.sub(r"\s+", " ", value).strip()
    if limit <= 0:
        return ""
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


def build_background_snapshot() -> ActivitySnapshot:
    return ActivitySnapshot(
        editor=EditorType.GENERIC,
        display_name=DEFAULT_APP_NAME,
        project_name=DEFAULT_APP_NAME,
        project_path=None,
        window_title="",
        state_text="Idle in background",
        fingerprint="kicad-background-open",
    )


def safe_call(callable_obj: Any, fallback: Any = None) -> Any:
    try:
        return callable_obj()
    except Exception:
        return fallback


def iter_items(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    try:
        return tuple(value)
    except TypeError:
        return ()


def get_current_board() -> Any | None:
    if pcbnew is None:
        return None

    try:
        board = pcbnew.GetBoard()
    except Exception:
        return None

    if board is None:
        return None
    return board


def get_board_file_path(board: Any) -> Path | None:
    file_name = str(safe_call(board.GetFileName, "") or "").strip()
    if not file_name:
        return None
    return Path(file_name)


def resolve_project_name_from_board(board: Any) -> str:
    board_path = get_board_file_path(board)
    if board_path is not None:
        return board_path.stem or "Untitled Project"
    return "Untitled Project"


def get_board_layer_count(board: Any) -> int:
    value = safe_call(board.GetCopperLayerCount)
    if isinstance(value, int):
        return value
    return 0


def get_board_footprint_count(board: Any) -> int:
    for accessor_name in ("Footprints", "GetFootprints", "GetModules"):
        accessor = getattr(board, accessor_name, None)
        if accessor is None:
            continue
        items = iter_items(safe_call(accessor))
        return sum(1 for _ in items)
    return 0


def get_board_via_count(board: Any) -> int:
    tracks_accessor = getattr(board, "Tracks", None) or getattr(board, "GetTracks", None)
    if tracks_accessor is None:
        return 0

    count = 0
    for item in iter_items(safe_call(tracks_accessor)):
        if item is None:
            continue
        class_name = item.__class__.__name__.upper()
        if "VIA" in class_name:
            count += 1
            continue
        type_name_getter = getattr(item, "GetClass", None)
        type_name = str(safe_call(type_name_getter, "") or "").upper()
        if "VIA" in type_name:
            count += 1
    return count


def get_file_mtime_ns(path: Path | None) -> int:
    if path is None or not path.exists():
        return 0
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def build_pcb_snapshot(window: WindowInfo, config: AppConfig, board: Any) -> ActivitySnapshot:
    board_path = get_board_file_path(board)
    project_name = resolve_project_name_from_board(board)
    display_name = shorten_display_name(project_name, config)
    layer_count = get_board_layer_count(board)
    footprint_count = get_board_footprint_count(board)
    via_count = get_board_via_count(board)
    state_text = (
        f"{layer_count} Layers | {format_compact_count(footprint_count)} parts"
        f" | {format_compact_count(via_count)} vias"
    )
    fingerprint = sha1_text(
        f"{window.title}|{board_path}|{get_file_mtime_ns(board_path)}|{state_text}"
    )
    return ActivitySnapshot(
        editor=EditorType.PCB,
        display_name=display_name,
        project_name=project_name,
        project_path=str(board_path) if board_path is not None else None,
        window_title=window.title,
        state_text=state_text,
        fingerprint=fingerprint,
    )


def extract_project_name_from_window_title(title: str) -> str:
    trimmed_title = title.strip()
    if not trimmed_title:
        return "Untitled Project"

    for separator in (" — ", " - "):
        if separator in trimmed_title:
            prefix = trimmed_title.split(separator, 1)[0].strip()
            return prefix or "Untitled Project"

    return trimmed_title


def discover_schematic_files(board: Any, window: WindowInfo) -> list[Path]:
    board_path = get_board_file_path(board) if board is not None else None
    if board_path is not None and board_path.parent.is_dir():
        return sorted(board_path.parent.rglob("*.kicad_sch"))

    title_path_match = re.search(r"([A-Za-z]:\\[^|]+?\.kicad_sch)", window.title)
    if title_path_match:
        title_path = Path(title_path_match.group(1))
        if title_path.exists():
            return [title_path]

    return []


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

    if not readable_file_seen:
        return None
    return len(custom_labels)


def build_schematic_snapshot(
    window: WindowInfo, config: AppConfig, board: Any | None
) -> ActivitySnapshot:
    project_name = (
        resolve_project_name_from_board(board)
        if board is not None
        else extract_project_name_from_window_title(window.title)
    )
    schematic_files = discover_schematic_files(board, window)
    if project_name == "Untitled Project" and schematic_files:
        project_name = schematic_files[0].stem or project_name

    custom_net_count = count_custom_labels_in_schematics(schematic_files)
    if custom_net_count is not None:
        state_text = f"Editing | {format_compact_count(custom_net_count)} Total Nets"
    else:
        state_text = "Editing"

    display_name = shorten_display_name(project_name, config)
    fingerprint_source = "|".join(
        [
            window.title,
            state_text,
            *(
                f"{path}:{get_file_mtime_ns(path)}"
                for path in schematic_files
            ),
        ]
    )
    return ActivitySnapshot(
        editor=EditorType.SCHEMATIC,
        display_name=display_name,
        project_name=project_name,
        project_path=str(schematic_files[0]) if schematic_files else None,
        window_title=window.title,
        state_text=state_text,
        fingerprint=sha1_text(fingerprint_source),
    )


def build_activity_snapshot(config: AppConfig) -> ActivitySnapshot:
    window = detect_active_window()
    board = get_current_board()
    if window is None:
        return build_background_snapshot()

    if window.editor is EditorType.PCB and board is not None:
        return build_pcb_snapshot(window, config, board)

    if window.editor is EditorType.SCHEMATIC:
        return build_schematic_snapshot(window, config, board)

    if is_kicad_process_name(window.process_name):
        return build_background_snapshot()

    # The plugin only runs while KiCad is open, so any non-KiCad foreground app
    # means KiCad is simply in the background.
    return build_background_snapshot()


def build_presence_details(snapshot: ActivitySnapshot) -> str:
    if snapshot.editor is EditorType.GENERIC:
        return "KiCad"

    editor_label = "PCB" if snapshot.editor is EditorType.PCB else "SCH"
    return truncate_presence_text(
        f"{editor_label}: {snapshot.display_name}",
        DETAILS_TEXT_LIMIT,
    )


def build_presence_state(snapshot: ActivitySnapshot, is_idle: bool) -> str:
    if snapshot.editor is EditorType.GENERIC:
        return "Open in background"

    base_state = truncate_presence_text(snapshot.state_text, STATE_TEXT_LIMIT)
    if not is_idle:
        return base_state
    return truncate_presence_text(f"Idle | {snapshot.state_text}", STATE_TEXT_LIMIT)


class DiscordRpcClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.rpc: Presence | None = None
        self.connected = False
        self.last_payload: dict[str, Any] | None = None

    def set_config(self, config: AppConfig) -> None:
        if config.discord_client_id != self.config.discord_client_id:
            self.reset()
        self.config = config

    def ensure_connected(self) -> bool:
        if Presence is None:
            return False

        if self.connected and self.rpc is not None:
            return True

        try:
            self.rpc = Presence(self.config.discord_client_id)
            self.rpc.connect()
            self.connected = True
            self.last_payload = None
            LOGGER.info("Connected to Discord RPC.")
            return True
        except Exception as exc:
            self.rpc = None
            self.connected = False
            LOGGER.debug("Discord RPC connection failed: %s", exc)
            return False

    def publish(self, payload: dict[str, Any]) -> None:
        if payload == self.last_payload:
            return

        if not self.ensure_connected():
            return

        try:
            assert self.rpc is not None
            self.rpc.update(**payload)
            self.last_payload = dict(payload)
        except Exception as exc:
            LOGGER.debug("Discord RPC update failed: %s", exc)
            self.reset()

    def clear(self) -> None:
        if not self.connected or self.rpc is None:
            self.last_payload = None
            return

        try:
            self.rpc.clear()
        except Exception as exc:
            LOGGER.debug("Discord clear failed: %s", exc)
        finally:
            self.last_payload = None

    def reset(self) -> None:
        if self.rpc is not None:
            try:
                self.rpc.close()
            except Exception:
                pass

        self.rpc = None
        self.connected = False
        self.last_payload = None


class PluginRuntime:
    def __init__(self) -> None:
        if wx is None:
            raise RuntimeError("wxPython is required to start the Discord RPC runtime.")

        self._handler = wx.EvtHandler()
        self._timer = wx.Timer(self._handler)
        self._handler.Bind(wx.EVT_TIMER, self._on_timer, self._timer)
        self._started = False
        self._config: AppConfig | None = None
        self._discord_client: DiscordRpcClient | None = None
        self._last_snapshot: ActivitySnapshot | None = None
        self._last_change_time = time.monotonic()
        self._session_start_timestamp = int(time.time())
        self._timer_interval_ms: int | None = None

    def start(self) -> None:
        if self._started:
            return

        self._started = True
        self._tick()

    def _load_config(self) -> AppConfig | None:
        try:
            config = AppConfig.load(CONFIG_PATH)
        except Exception as exc:
            LOGGER.debug("Unable to load Discord RPC config: %s", exc)
            return self._config

        if self._config is None or self._config.log_level != config.log_level:
            configure_logging(config.log_level)

        if self._discord_client is None:
            self._discord_client = DiscordRpcClient(config)
        else:
            self._discord_client.set_config(config)

        self._config = config
        interval_ms = max(3000, config.poll_interval_seconds * 1000)
        if not self._timer.IsRunning() or self._timer_interval_ms != interval_ms:
            self._timer.Start(interval_ms)
            self._timer_interval_ms = interval_ms
        return config

    def _on_timer(self, _event: Any) -> None:
        self._tick()

    def _tick(self) -> None:
        config = self._load_config()
        if config is None or self._discord_client is None or StatusDisplayType is None:
            return

        snapshot = build_activity_snapshot(config)
        if snapshot != self._last_snapshot:
            self._last_snapshot = snapshot
            self._last_change_time = time.monotonic()

        is_idle = (time.monotonic() - self._last_change_time) >= config.idle_threshold_seconds
        payload: dict[str, Any] = {
            "name": DEFAULT_APP_NAME,
            "status_display_type": StatusDisplayType.DETAILS,
            "details": build_presence_details(snapshot),
            "state": build_presence_state(snapshot, is_idle),
            "start": self._session_start_timestamp,
        }
        if config.large_image:
            payload["large_image"] = config.large_image
        if config.large_text:
            payload["large_text"] = config.large_text

        self._discord_client.publish(payload)


def ensure_runtime_started() -> PluginRuntime | None:
    if pcbnew is None or wx is None or Presence is None:
        return None

    app = wx.GetApp()
    if app is None:
        return None

    runtime = getattr(app, RUNTIME_ATTRIBUTE_NAME, None)
    if isinstance(runtime, PluginRuntime):
        runtime.start()
        return runtime

    runtime = PluginRuntime()
    setattr(app, RUNTIME_ATTRIBUTE_NAME, runtime)
    runtime.start()
    return runtime
