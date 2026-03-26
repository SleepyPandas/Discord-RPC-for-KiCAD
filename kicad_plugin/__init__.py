from __future__ import annotations

from typing import Any

from .preferences import show_preferences_dialog
from .runtime import ensure_plugin_bootstrap

try:
    import pcbnew  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only available inside KiCad.
    pcbnew = None  # type: ignore[assignment]

try:
    import wx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only available inside KiCad.
    wx = None  # type: ignore[assignment]


if pcbnew is not None:

    class DiscordRpcPreferencesPlugin(pcbnew.ActionPlugin):
        def defaults(self) -> None:
            self.name = "Discord RPC Preferences"
            self.category = "Preferences"
            self.description = "Configure Discord Rich Presence privacy settings."
            self.show_toolbar_button = False
            self.icon_file_name = ""

        def Run(self) -> None:
            parent: Any = wx.GetActiveWindow() if wx is not None else None
            show_preferences_dialog(parent)


else:

    class DiscordRpcPreferencesPlugin:
        def register(self) -> None:
            return None


def register_plugin() -> DiscordRpcPreferencesPlugin | None:
    plugin: DiscordRpcPreferencesPlugin | None = None
    if pcbnew is not None:
        plugin = DiscordRpcPreferencesPlugin()
        plugin.register()
    return plugin


PLUGIN = register_plugin()
ensure_plugin_bootstrap()
