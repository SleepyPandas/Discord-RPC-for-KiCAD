"""Discord Rich Presence bridge for KiCad 10.

This script is intentionally Windows-first and keeps its runtime simple:
- poll KiCad every 15 seconds
- detect whether the foreground KiCad window is the PCB or Schematic editor
- publish Rich Presence updates through Discord

The official KiCad IPC API currently exposes PCB data directly. For schematic
mode, this script falls back to parsing the project's `.kicad_sch` files.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import re
import time
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import psutil
from kipy import KiCad
from pypresence import Presence
from pypresence.types import StatusDisplayType


CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_HIDDEN_TEXT = "Working on a generic project"
DEFAULT_APP_NAME = "KiCad 10"


user32 = ctypes.windll.user32
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD


class EditorType(str, Enum):
    PCB = "pcb"
    SCHEMATIC = "schematic"


@dataclass(frozen=True)
class AppConfig:
    discord_client_id: str
    hide_filename: bool = False
    hidden_project_text: str = DEFAULT_HIDDEN_TEXT
    poll_interval_seconds: int = 15
    idle_threshold_seconds: int = 300
    large_image: str = ""
    large_text: str = ""
    log_level: str = "INFO"

    @classmethod
    def load(cls, config_path: Path) -> "AppConfig":
        if not config_path.exists():
            raise FileNotFoundError(
                f"Missing config file: {config_path}. Create it from the provided template first."
            )

        raw_config = json.loads(config_path.read_text(encoding="utf-8"))
        client_id = str(raw_config.get("discord_client_id", "")).strip()

        if not client_id or client_id == "YOUR_DISCORD_APPLICATION_CLIENT_ID":
            raise ValueError("config.json must contain a valid Discord application client ID.")

        return cls(
            discord_client_id=client_id,
            hide_filename=bool(raw_config.get("hide_filename", False)),
            hidden_project_text=str(
                raw_config.get("hidden_project_text", DEFAULT_HIDDEN_TEXT)
            ).strip()
            or DEFAULT_HIDDEN_TEXT,
            poll_interval_seconds=max(15, int(raw_config.get("poll_interval_seconds", 15))),
            idle_threshold_seconds=max(60, int(raw_config.get("idle_threshold_seconds", 300))),
            large_image=str(raw_config.get("large_image", "")).strip(),
            large_text=str(raw_config.get("large_text", "")).strip(),
            log_level=str(raw_config.get("log_level", "INFO")).strip().upper() or "INFO",
        )


@dataclass(frozen=True)
class WindowInfo:
    process_name: str
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


@dataclass(frozen=True)
class SchematicStats:
    symbol_count: int
    fingerprint: str


def configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
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


def detect_active_window() -> WindowInfo | None:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))

    try:
        process_name = psutil.Process(process_id.value).name().lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
        process_name = ""

    title = get_window_text(hwnd)
    editor = detect_editor_type(process_name, title)
    return WindowInfo(process_name=process_name, title=title, editor=editor)


def detect_editor_type(process_name: str, title: str) -> EditorType | None:
    if process_name not in {"kicad.exe", "kicad"}:
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


def shorten_display_name(name: str, config: AppConfig) -> str:
    if config.hide_filename:
        return config.hidden_project_text

    cleaned_name = name.strip()
    return cleaned_name or "Untitled Project"


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def leading_token(expression: str) -> str:
    match = re.match(r"^\(\s*([^\s()]+)", expression)
    return match.group(1) if match else ""


def extract_top_level_forms(text: str) -> list[str]:
    forms: list[str] = []
    depth = 0
    form_start: int | None = None
    in_string = False
    escape_next = False
    saw_root = False

    for index, character in enumerate(text):
        if escape_next:
            escape_next = False
            continue

        if in_string:
            if character == "\\":
                escape_next = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
            continue

        if character == "(":
            depth += 1
            if not saw_root:
                saw_root = True
            elif depth == 2 and form_start is None:
                form_start = index
            continue

        if character == ")":
            if depth == 2 and form_start is not None:
                forms.append(text[form_start : index + 1])
                form_start = None
            depth = max(depth - 1, 0)

    return forms


def parse_sheet_file_references(sheet_expression: str) -> list[str]:
    matches = re.findall(r'\(property\s+"Sheet file"\s+"([^"]+)"', sheet_expression)
    if matches:
        return matches

    # Some generated files may collapse the property name without a space.
    return re.findall(r'\(property\s+"Sheetfile"\s+"([^"]+)"', sheet_expression)


def parse_schematic_tree(root_path: Path) -> SchematicStats:
    # KiCad's public IPC API is still PCB-focused, so schematic activity is
    # derived from the saved `.kicad_sch` files instead of live editor objects.
    def walk(current_path: Path, ancestry: tuple[Path, ...]) -> tuple[int, str]:
        normalized_path = current_path.resolve()

        if normalized_path in ancestry:
            logging.warning("Skipping recursive schematic sheet reference: %s", normalized_path)
            return 0, ""

        text = current_path.read_text(encoding="utf-8")
        forms = extract_top_level_forms(text)
        symbol_count = 0
        child_fingerprints: list[str] = []

        for form in forms:
            token = leading_token(form)

            if token == "symbol":
                symbol_count += 1
                continue

            if token != "sheet":
                continue

            for relative_sheet in parse_sheet_file_references(form):
                child_path = (current_path.parent / relative_sheet).resolve()
                if not child_path.exists():
                    logging.warning("Referenced schematic sheet does not exist: %s", child_path)
                    continue

                child_symbols, child_fingerprint = walk(child_path, ancestry + (normalized_path,))
                symbol_count += child_symbols
                child_fingerprints.append(f"{child_path}:{child_fingerprint}")

        file_fingerprint = hashlib.sha1()
        file_fingerprint.update(str(normalized_path).encode("utf-8"))
        file_fingerprint.update(text.encode("utf-8"))
        for child in child_fingerprints:
            file_fingerprint.update(child.encode("utf-8"))

        return symbol_count, file_fingerprint.hexdigest()

    total_symbols, fingerprint = walk(root_path, ())
    return SchematicStats(symbol_count=total_symbols, fingerprint=fingerprint)


def resolve_root_schematic_path(project_path: Path | None) -> Path | None:
    if project_path is None:
        return None

    preferred = project_path.with_suffix(".kicad_sch")
    if preferred.exists():
        return preferred

    matching_schematics = sorted(project_path.parent.glob("*.kicad_sch"))
    if not matching_schematics:
        return None

    for candidate in matching_schematics:
        if candidate.stem == project_path.stem:
            return candidate

    if len(matching_schematics) == 1:
        return matching_schematics[0]

    return None


def resolve_project_name(project: Any, board: Any) -> str:
    project_name = str(getattr(project, "name", "") or "").strip()
    if project_name:
        return project_name

    board_name = str(getattr(board, "name", "") or "").strip()
    if board_name:
        return Path(board_name).stem or board_name

    return "Untitled Project"


class DiscordRpcClient:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.rpc: Presence | None = None
        self.connected = False
        self.last_payload: dict[str, Any] | None = None

    def ensure_connected(self) -> bool:
        if self.connected and self.rpc is not None:
            return True

        try:
            self.rpc = Presence(self.config.discord_client_id)
            self.rpc.connect()
            self.connected = True
            self.last_payload = None
            logging.info("Connected to Discord RPC.")
            return True
        except Exception as exc:
            self.rpc = None
            self.connected = False
            logging.warning("Discord RPC connection failed: %s", exc)
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
            logging.warning("Discord RPC update failed: %s", exc)
            self.reset()

    def clear(self) -> None:
        if not self.connected or self.rpc is None:
            self.last_payload = None
            return

        try:
            self.rpc.clear()
        except Exception as exc:
            logging.debug("Discord clear failed: %s", exc)
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


class KiCadClientManager:
    def __init__(self) -> None:
        self.client: KiCad | None = None

    def ensure_connected(self) -> bool:
        if self.client is not None:
            try:
                self.client.ping()
                return True
            except Exception:
                self.reset()

        try:
            self.client = KiCad(client_name="discord-rpc-for-kicad")
            self.client.check_version()
            logging.info("Connected to KiCad IPC API.")
            return True
        except Exception as exc:
            self.client = None
            logging.debug("KiCad IPC connection failed: %s", exc)
            return False

    def get_board(self) -> Any | None:
        if not self.ensure_connected():
            return None

        try:
            assert self.client is not None
            return self.client.get_board()
        except Exception as exc:
            logging.debug("Unable to retrieve KiCad board state: %s", exc)
            self.reset()
            return None

    def reset(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            except Exception:
                pass

        self.client = None


def build_pcb_snapshot(window: WindowInfo, config: AppConfig, board: Any) -> ActivitySnapshot | None:
    try:
        project = board.get_project()
    except Exception:
        project = None

    project_path_value = str(getattr(project, "path", "") or "").strip()
    project_path = Path(project_path_value) if project_path_value else None
    project_name = resolve_project_name(project, board)
    display_name = shorten_display_name(project_name, config)

    layers = board.get_copper_layer_count()
    footprint_count = len(board.get_footprints())
    via_count = len(board.get_vias())

    # Hashing the full board text catches edits that do not change counts, such as
    # reroutes or moved items, so idle detection is more useful than count-only polling.
    board_fingerprint = sha1_text(board.get_as_string())

    # Routed-net progress is intentionally omitted here because the current
    # public IPC docs do not expose a clear exact remaining-nets metric.
    state_text = f"{layers}-Layer Board | {footprint_count} Parts | {via_count} Vias"
    return ActivitySnapshot(
        editor=EditorType.PCB,
        display_name=display_name,
        project_name=project_name,
        project_path=str(project_path) if project_path else None,
        window_title=window.title,
        state_text=state_text,
        fingerprint=sha1_text(f"{window.title}|{state_text}|{board_fingerprint}"),
    )


def build_schematic_snapshot(
    window: WindowInfo, config: AppConfig, board: Any | None
) -> ActivitySnapshot | None:
    project = None
    project_path: Path | None = None
    project_name = "Untitled Project"

    if board is not None:
        try:
            project = board.get_project()
        except Exception:
            project = None

    project_path_value = str(getattr(project, "path", "") or "").strip()
    if project_path_value:
        project_path = Path(project_path_value)
        project_name = resolve_project_name(project, board)
    elif board is not None:
        project_name = resolve_project_name(project, board)

    schematic_path = resolve_root_schematic_path(project_path)
    symbol_count = 0
    schematic_fingerprint = ""

    if schematic_path and schematic_path.exists():
        stats = parse_schematic_tree(schematic_path)
        symbol_count = stats.symbol_count
        schematic_fingerprint = stats.fingerprint
    else:
        logging.debug("No root schematic file could be resolved from the current KiCad project.")

    display_name = shorten_display_name(project_name, config)
    state_text = f"Placing Symbols & Wiring | {symbol_count} Symbols"

    return ActivitySnapshot(
        editor=EditorType.SCHEMATIC,
        display_name=display_name,
        project_name=project_name,
        project_path=str(project_path) if project_path else None,
        window_title=window.title,
        state_text=state_text,
        fingerprint=sha1_text(f"{window.title}|{state_text}|{schematic_fingerprint}"),
    )


def build_activity_snapshot(
    config: AppConfig, kicad_client: KiCadClientManager
) -> ActivitySnapshot | None:
    window = detect_active_window()
    if window is None or window.editor is None:
        return None

    board = kicad_client.get_board()

    if window.editor is EditorType.PCB:
        if board is None:
            logging.debug("PCB editor is focused, but no board is available from KiCad.")
            return None
        return build_pcb_snapshot(window, config, board)

    return build_schematic_snapshot(window, config, board)


def build_presence_payload(
    snapshot: ActivitySnapshot, is_idle: bool, config: AppConfig
) -> dict[str, Any]:
    details_prefix = (
        "Routing PCB: " if snapshot.editor is EditorType.PCB else "Designing Schematic: "
    )

    payload: dict[str, Any] = {
        "name": DEFAULT_APP_NAME,
        "status_display_type": StatusDisplayType.DETAILS,
        "details": f"{details_prefix}{snapshot.display_name}",
        "state": "Idle - Staring at the screen." if is_idle else snapshot.state_text,
    }

    if config.large_image:
        payload["large_image"] = config.large_image

    if config.large_text:
        payload["large_text"] = config.large_text

    return payload


def main() -> None:
    config = AppConfig.load(CONFIG_PATH)
    configure_logging(config.log_level)

    discord_client = DiscordRpcClient(config)
    kicad_client = KiCadClientManager()

    last_snapshot: ActivitySnapshot | None = None
    last_change_time = time.monotonic()

    logging.info("KiCad Discord Rich Presence bridge started.")

    while True:
        try:
            snapshot = build_activity_snapshot(config, kicad_client)

            if snapshot is None:
                discord_client.clear()
                last_snapshot = None
                last_change_time = time.monotonic()
            else:
                if snapshot != last_snapshot:
                    last_snapshot = snapshot
                    last_change_time = time.monotonic()

                is_idle = (time.monotonic() - last_change_time) >= config.idle_threshold_seconds
                discord_client.publish(build_presence_payload(snapshot, is_idle, config))

        except KeyboardInterrupt:
            logging.info("Stopping KiCad Discord Rich Presence bridge.")
            discord_client.clear()
            discord_client.reset()
            kicad_client.reset()
            break
        except Exception:
            logging.exception("Unexpected error in the main polling loop.")

        time.sleep(config.poll_interval_seconds)


if __name__ == "__main__":
    main()
