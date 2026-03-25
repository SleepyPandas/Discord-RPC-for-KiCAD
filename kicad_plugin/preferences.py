from __future__ import annotations

from typing import Any

from .config_io import ConfigError, PrivacySettings, save_privacy_settings

try:
    import wx  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - only available inside KiCad.
    wx = None  # type: ignore[assignment]


class PrivacyPreferencesDialog:
    def __init__(self, parent: Any, initial_settings: PrivacySettings) -> None:
        if wx is None:
            raise RuntimeError("wxPython is required to show the preferences dialog.")

        self._dialog = wx.Dialog(parent, title="Discord RPC Preferences")
        self._updated_settings: PrivacySettings | None = None

        panel = wx.Panel(self._dialog)
        description = wx.StaticText(
            panel,
            label=(
                "Choose whether Discord should hide the active KiCad project name."
            ),
        )
        description.Wrap(380)

        self._privacy_checkbox = wx.CheckBox(
            panel, label="Enable Privacy Mode (hide project name)"
        )
        self._privacy_checkbox.SetValue(initial_settings.hide_filename)
        self._privacy_checkbox.Bind(wx.EVT_CHECKBOX, self._on_toggle_privacy_mode)

        replacement_label = wx.StaticText(
            panel, label="Replacement text shown in Discord"
        )
        self._replacement_text = wx.TextCtrl(
            panel,
            value=initial_settings.hidden_project_text,
            size=(400, -1),
        )

        button_sizer = self._dialog.CreateSeparatedButtonSizer(wx.OK | wx.CANCEL)
        save_button = self._dialog.FindWindowById(wx.ID_OK)
        if save_button is not None:
            save_button.SetLabel("Save")
            save_button.Bind(wx.EVT_BUTTON, self._on_save)

        content_sizer = wx.BoxSizer(wx.VERTICAL)
        content_sizer.Add(description, 0, wx.ALL | wx.EXPAND, 12)
        content_sizer.Add(self._privacy_checkbox, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 12)
        content_sizer.Add(replacement_label, 0, wx.LEFT | wx.RIGHT, 12)
        content_sizer.Add(
            self._replacement_text,
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM | wx.EXPAND,
            12,
        )
        if button_sizer is not None:
            content_sizer.Add(button_sizer, 0, wx.ALL | wx.EXPAND, 12)

        panel.SetSizer(content_sizer)

        dialog_sizer = wx.BoxSizer(wx.VERTICAL)
        dialog_sizer.Add(panel, 1, wx.EXPAND)
        self._dialog.SetSizerAndFit(dialog_sizer)
        self._dialog.SetMinSize((460, self._dialog.GetSize().Height))

    def _on_toggle_privacy_mode(self, _event: Any) -> None:
        self._replacement_text.Enable(self._privacy_checkbox.GetValue())

    def _on_save(self, _event: Any) -> None:
        replacement_text = self._replacement_text.GetValue().strip()
        if not replacement_text:
            wx.MessageBox(
                "Replacement text cannot be empty.",
                "Discord RPC Preferences",
                wx.OK | wx.ICON_WARNING,
                parent=self._dialog,
            )
            return

        self._updated_settings = PrivacySettings(
            hide_filename=self._privacy_checkbox.GetValue(),
            hidden_project_text=replacement_text,
        )
        self._dialog.EndModal(wx.ID_OK)

    def show_modal(self) -> int:
        self._on_toggle_privacy_mode(None)
        return self._dialog.ShowModal()

    def get_updated_settings(self) -> PrivacySettings | None:
        return self._updated_settings

    def destroy(self) -> None:
        self._dialog.Destroy()


def show_preferences_dialog(parent: Any = None) -> None:
    if wx is None:
        raise RuntimeError("wxPython is required to show the preferences dialog.")

    saved_path = None
    try:
        from .config_io import load_privacy_settings

        initial_settings = load_privacy_settings()
    except ConfigError as exc:
        wx.MessageBox(
            str(exc),
            "Discord RPC Preferences",
            wx.OK | wx.ICON_ERROR,
            parent=parent,
        )
        return

    dialog = PrivacyPreferencesDialog(parent, initial_settings)
    try:
        if dialog.show_modal() != wx.ID_OK:
            return

        updated_settings = dialog.get_updated_settings()
        if updated_settings is None:
            return

        saved_path = save_privacy_settings(updated_settings)
    except ConfigError as exc:
        wx.MessageBox(
            str(exc),
            "Discord RPC Preferences",
            wx.OK | wx.ICON_ERROR,
            parent=parent,
        )
        return
    finally:
        dialog.destroy()

    wx.MessageBox(
        (
            "Privacy settings saved to:\n"
            f"{saved_path}\n\n"
            "Restart the Discord RPC bridge for changes to apply."
        ),
        "Discord RPC Preferences",
        wx.OK | wx.ICON_INFORMATION,
        parent=parent,
    )
