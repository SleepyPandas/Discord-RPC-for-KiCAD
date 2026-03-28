from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .shared_config import (
    get_config_path as get_shared_config_path,
    load_config_document,
    save_config_document,
)

DEFAULT_HIDDEN_PROJECT_TEXT = "Working on a generic project"


class ConfigError(RuntimeError):
    """Raised when the bridge config file cannot be read or written safely."""


@dataclass(frozen=True)
class PrivacySettings:
    hide_filename: bool = False
    hidden_project_text: str = DEFAULT_HIDDEN_PROJECT_TEXT


def get_config_path() -> Path:
    return get_shared_config_path()


def _normalize_hidden_text(value: str) -> str:
    cleaned_value = value.strip()
    if not cleaned_value:
        raise ConfigError("Replacement text cannot be empty.")
    return cleaned_value


def load_privacy_settings(config_path: Path | None = None) -> PrivacySettings:
    resolved_path = config_path or get_config_path()
    try:
        raw_config = load_config_document(resolved_path)
    except ValueError as exc:
        raise ConfigError(f"Config file is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read config file: {exc}") from exc

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
    try:
        raw_config = load_config_document(resolved_path)
    except ValueError as exc:
        raise ConfigError(f"Config file is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Unable to read config file: {exc}") from exc

    raw_config["hide_filename"] = bool(settings.hide_filename)
    raw_config["hidden_project_text"] = _normalize_hidden_text(
        settings.hidden_project_text
    )

    try:
        return save_config_document(
            raw_config,
            resolved_path,
        )
    except OSError as exc:
        raise ConfigError(f"Unable to write config file: {exc}") from exc
