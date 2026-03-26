from __future__ import annotations

import json
import subprocess
import winreg
from pathlib import Path

PLUGIN_PACKAGE_DIR = Path(__file__).resolve().parent
WATCHER_FILENAME = "watcher.pyw"
LEGACY_WATCHER_FILENAME = "_discord_rpc_watcher.pyw"
WATCHER_PATH = PLUGIN_PACKAGE_DIR / WATCHER_FILENAME
LEGACY_WATCHER_PATH = PLUGIN_PACKAGE_DIR / LEGACY_WATCHER_FILENAME
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "DiscordRpcForKiCadWatcher"
DEFAULT_KICAD_PYTHONW = Path(r"D:\Programs\KiCad\bin\pythonw.exe")
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
_BOOTSTRAP_ATTEMPTED = False


def _discover_pythonw_from_kicad_common() -> Path | None:
    kicad_common_root = Path.home() / "AppData" / "Roaming" / "kicad"
    if not kicad_common_root.is_dir():
        return None

    for version_dir in sorted(kicad_common_root.iterdir(), reverse=True):
        common_json = version_dir / "kicad_common.json"
        if not common_json.is_file():
            continue

        try:
            data = json.loads(common_json.read_text(encoding="utf-8"))
        except Exception:
            continue

        interpreter_path = Path(str(data.get("api", {}).get("interpreter_path", "")))
        if interpreter_path.is_file() and interpreter_path.name.lower() == "pythonw.exe":
            return interpreter_path

        pythonw_path = interpreter_path.parent / "pythonw.exe"
        if pythonw_path.is_file():
            return pythonw_path

    return None


def get_pythonw_path() -> Path | None:
    if DEFAULT_KICAD_PYTHONW.is_file():
        return DEFAULT_KICAD_PYTHONW
    return _discover_pythonw_from_kicad_common()


def build_startup_command(pythonw_path: Path, watcher_path: Path) -> str:
    return f'"{pythonw_path}" "{watcher_path}"'


def read_startup_command() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def ensure_startup_registration() -> bool:
    pythonw_path = get_pythonw_path()
    if pythonw_path is None or not WATCHER_PATH.is_file():
        return False

    command = build_startup_command(pythonw_path, WATCHER_PATH)
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_READ | winreg.KEY_WRITE,
        ) as key:
            current_value = None
            try:
                current_value, _ = winreg.QueryValueEx(key, RUN_VALUE_NAME)
            except FileNotFoundError:
                current_value = None

            if current_value != command:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
    except OSError:
        return False

    return True


def remove_legacy_generated_watcher() -> None:
    if not LEGACY_WATCHER_PATH.exists():
        return

    try:
        LEGACY_WATCHER_PATH.unlink()
    except OSError:
        pass


def start_watcher_process() -> bool:
    pythonw_path = get_pythonw_path()
    if pythonw_path is None or not WATCHER_PATH.is_file():
        return False

    try:
        subprocess.Popen(
            [str(pythonw_path), str(WATCHER_PATH)],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
            cwd=str(PLUGIN_PACKAGE_DIR),
        )
        return True
    except OSError:
        return False


def ensure_plugin_bootstrap() -> bool:
    global _BOOTSTRAP_ATTEMPTED
    remove_legacy_generated_watcher()
    registered = ensure_startup_registration()
    started = start_watcher_process()
    _BOOTSTRAP_ATTEMPTED = _BOOTSTRAP_ATTEMPTED or registered or started
    return registered or started


def launch_background_watcher() -> bool:
    return start_watcher_process()


def ensure_runtime_started() -> None:
    ensure_plugin_bootstrap()
