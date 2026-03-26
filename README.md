# KiCad Discord Rich Presence

This project publishes Discord Rich Presence updates based on what you are doing in KiCad 10.

## Install The KiCad Plugin

KiCad 10 expects a PCM repository URL, not the normal GitHub repository page.

Add this repository to the Plugin and Content Manager:

`https://raw.githubusercontent.com/SleepyPandas/Discord-RPC-for-KiCAD/main/repository.json`

Then install `Discord RPC for KiCad` from the list and apply the pending changes.

## What The PCM Package Installs

The KiCad PCM package installs a self-contained KiCad plugin that starts Discord Rich Presence automatically after KiCad reloads the plugin on startup.

No separate bridge process or manual `pip install` step is required for normal KiCad use.

## Privacy Preferences

After installation, open the PCB Editor and go to `Tools` -> `External Plugins` -> `Discord RPC Preferences`.

The preferences dialog lets you:

- Enable or disable Privacy Mode.
- Choose the replacement text shown in Discord when the project name is hidden.

## Shared Configuration

The plugin stores its settings in the same user config file location used by the standalone bridge.

On Windows the config file is stored at:

`%APPDATA%\kicad\discord-rpc-for-kicad\config.json`

If an older repo-local `config.json` exists, it is copied there automatically the first time the plugin or bridge runs.

## Applying Preference Changes

Changes from the preferences dialog are picked up automatically while KiCad stays open.

## Rebuild The PCM Files

If you change the plugin files or package metadata, regenerate the KiCad PCM artifacts with:

`python build_pcm.py`

This rebuilds:

- `repository.json`
- `packages.json`
- `pcm-artifacts/discord-rpc-for-kicad-v1.0.0-pcm.zip`