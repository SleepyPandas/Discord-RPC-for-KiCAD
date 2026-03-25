# KICAD Discord Rich Presence

A simple project that updates Discord Rich Presence based on what you are doing in KiCad.

## Install

To install a plugin using a third-party repository in KiCad 10:

1. Copy the repository URL.
2. Open KiCad 10 and launch the Plugin and Content Manager.
3. Open the repository settings from the gear icon.
4. Add the repository URL and save it.
5. Install the plugin from the list and apply the pending changes.

## Privacy Preferences

After installation, open the PCB Editor and go to `Tools` -> `External Plugins` -> `Discord RPC Preferences`.

The preferences dialog lets you:

- Enable or disable Privacy Mode.
- Choose the replacement text shown in Discord when the project name is hidden.

Saving these settings updates `config.json`, which is still the source of truth for the standalone bridge.

## Restart Requirement

The bridge reads `config.json` only when it starts. After changing Privacy Mode from the KiCad preferences dialog, restart the Discord RPC bridge for the new setting to apply.

## Manual Configuration

If you prefer, you can still edit `config.json` directly:

- `hide_filename`: hides the active project name in Discord.
- `hidden_project_text`: replacement text to show when privacy mode is enabled.