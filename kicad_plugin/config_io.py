from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HIDDEN_PROJECT_TEXT = "Working on a generic project"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class ConfigError(RuntimeError):
    """Raised when the bridge config file cannot be read or written safely."""


@dataclass(frozen=True)
class PrivacySettings:
    hide_filename: bool = False
    hidden_project_text: str = DEFAULT_HIDDEN_PROJECT_TEXT


def get_config_path() -> Path:
    return CONFIG_PATH


def _normalize_hidden_text(value: str) -> str:
    cleaned_value = value.strip()
    if not cleaned_value:
        raise ConfigError("Replacement text cannot be empty.")
    return cleaned_value


def _load_config_document(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        raise ConfigError(f"Config file was not found: {config_path}")

    try:
        raw_data = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Config file is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read config file: {exc}") from exc

    if not isinstance(raw_data, dict):
        raise ConfigError("Config file must contain a JSON object.")

    return raw_data


def load_privacy_settings(config_path: Path | None = None) -> PrivacySettings:
    resolved_path = config_path or get_config_path()
    raw_config = _load_config_document(resolved_path)
    hidden_text = str(
        raw_config.get("hidden_project_text", DEFAULT_HIDDEN_PROJECT_TEXT)
    ).strip() or DEFAULT_HIDDEN_PROJECT_TEXT

    return PrivacySettings(
        hide_filename=bool(raw_config.get("hide_filename", False)),
        hidden_project_text=hidden_text,
    )


def save_privacy_settings(
    settings: PrivacySettings, config_path: Path | None = None
) -> Path:
    resolved_path = config_path or get_config_path()
    raw_config = _load_config_document(resolved_path)
    raw_config["hide_filename"] = bool(settings.hide_filename)
    raw_config["hidden_project_text"] = _normalize_hidden_text(
        settings.hidden_project_text
    )

    try:
        resolved_path.write_text(
            json.dumps(raw_config, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise ConfigError(f"Unable to write config file: {exc}") from exc

    return resolved_path
