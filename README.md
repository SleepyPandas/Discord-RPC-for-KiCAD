# KiCad Discord Rich Presence

This project publishes Discord Rich Presence updates based on what you are doing in KiCad 10.

## Install The KiCad Plugin

KiCad 10 expects a PCM repository URL, not the normal GitHub repository page.

Add this repository to the Plugin and Content Manager:

`https://raw.githubusercontent.com/SleepyPandas/Discord-RPC-for-KiCAD/main/repository.json`

Then install `Discord RPC for KiCad` from the list and apply the pending changes.

## What The PCM Package Installs

The KiCad PCM package installs the KiCad-side preferences plugin.

The standalone Discord Rich Presence bridge is still a separate Python process that you run outside KiCad.

## Run The Bridge

1. Install the Python dependencies with `pip install -r requirements.txt`.
2. Start the bridge with `python main.py`.

## Privacy Preferences

After installation, open the PCB Editor and go to `Tools` -> `External Plugins` -> `Discord RPC Preferences`.

The preferences dialog lets you:

- Enable or disable Privacy Mode.
- Choose the replacement text shown in Discord when the project name is hidden.

## Shared Configuration

The plugin and the standalone bridge now share the same user config file.

On Windows the config file is stored at:

`%APPDATA%\kicad\discord-rpc-for-kicad\config.json`

If an older repo-local `config.json` exists, it is copied there automatically the first time the plugin or bridge runs.

## Restart Requirement

The bridge reads the config when it starts. After changing Privacy Mode from the KiCad preferences dialog, restart the Discord RPC bridge for the new setting to apply.

## Rebuild The PCM Files

If you change the plugin files or package metadata, regenerate the KiCad PCM artifacts with:

`python build_pcm.py`

This rebuilds:

- `repository.json`
- `packages.json`
- `pcm-artifacts/discord-rpc-for-kicad-v1.0.1-pcm.zip`