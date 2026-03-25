from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

CONFIG_DIRECTORY_NAME = "discord-rpc-for-kicad"
CONFIG_FILE_NAME = "config.json"
DEFAULT_CONFIG: dict[str, Any] = {
    "discord_client_id": "1486478009967055059",
    "hide_filename": False,
    "hidden_project_text": "Working on a generic project",
    "poll_interval_seconds": 10,
    "idle_threshold_seconds": 300,
    "large_image": "large_image",
    "large_text": "KiCad 10",
    "log_level": "INFO",
    "auto_enable_kicad_ipc": True,
}


def get_config_directory() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "kicad" / CONFIG_DIRECTORY_NAME

    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "kicad" / CONFIG_DIRECTORY_NAME

    return Path.home() / ".config" / "kicad" / CONFIG_DIRECTORY_NAME


def get_config_path() -> Path:
    return get_config_directory() / CONFIG_FILE_NAME


def get_default_config() -> dict[str, Any]:
    return dict(DEFAULT_CONFIG)


def _normalize_config_document(raw_config: dict[str, Any]) -> dict[str, Any]:
    normalized_config = get_default_config()
    normalized_config.update(raw_config)
    return normalized_config


def _read_json_document(path: Path) -> dict[str, Any]:
    raw_config = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_config, dict):
        raise ValueError(f"Config file must contain a JSON object: {path}")
    return raw_config


def _write_json_document(path: Path, raw_config: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_normalize_config_document(raw_config), indent=2, ensure_ascii=True)
        + "\n",
        encoding="utf-8",
    )
    return path


def ensure_config_exists(legacy_candidates: Iterable[Path] | None = None) -> Path:
    config_path = get_config_path()

    if config_path.exists():
        return config_path

    for legacy_path in legacy_candidates or ():
        if not legacy_path.exists():
            continue

        try:
            raw_config = _read_json_document(legacy_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue

        return _write_json_document(config_path, raw_config)

    return _write_json_document(config_path, get_default_config())


def _bootstrap_config_file(
    config_path: Path,
    legacy_candidates: Iterable[Path] | None = None,
) -> Path:
    for legacy_path in legacy_candidates or ():
        if not legacy_path.exists():
            continue

        try:
            raw_config = _read_json_document(legacy_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue

        return _write_json_document(config_path, raw_config)

    return _write_json_document(config_path, get_default_config())


def load_config_document(
    config_path: Path | None = None,
    legacy_candidates: Iterable[Path] | None = None,
) -> dict[str, Any]:
    resolved_path = config_path or get_config_path()
    if not resolved_path.exists():
        if config_path is None:
            resolved_path = ensure_config_exists(legacy_candidates)
        else:
            resolved_path = _bootstrap_config_file(resolved_path, legacy_candidates)

    return _normalize_config_document(_read_json_document(resolved_path))


def save_config_document(raw_config: dict[str, Any], config_path: Path | None = None) -> Path:
    resolved_path = config_path or get_config_path()
    return _write_json_document(resolved_path, raw_config)
