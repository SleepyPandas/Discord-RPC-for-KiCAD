from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

REPO_ROOT = Path(__file__).resolve().parent
PACKAGE_METADATA_PATH = REPO_ROOT / "package-metadata.json"
PACKAGE_ARTIFACTS_DIR = REPO_ROOT / "pcm-artifacts"
REPOSITORY_JSON_PATH = REPO_ROOT / "repository.json"
PACKAGES_JSON_PATH = REPO_ROOT / "packages.json"
PLUGIN_SOURCE_DIR = REPO_ROOT / "kicad_plugin"
ICON_SOURCE_PATH = REPO_ROOT / "icon.png"
IGNORED_PLUGIN_PARTS = {"__pycache__"}
IGNORED_PLUGIN_SUFFIXES = {".pyc", ".pyo"}
DISALLOWED_ARCHIVE_METADATA_FIELDS = {
    "download_url",
    "download_sha256",
    "download_size",
    "install_size",
}
REQUIRED_PLUGIN_FILES = {
    "__init__.py",
    "config_io.py",
    "preferences.py",
    "runtime.py",
    "shared_config.py",
    "watcher.pyw",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build KiCad PCM package artifacts for this repository."
    )
    parser.add_argument(
        "--base-url",
        default="https://raw.githubusercontent.com/SleepyPandas/Discord-RPC-for-KiCAD/main",
        help="Raw base URL where repository.json, packages.json, and PCM artifacts are hosted.",
    )
    return parser.parse_args()


def read_package_metadata() -> dict:
    return json.loads(PACKAGE_METADATA_PATH.read_text(encoding="utf-8"))


def validate_package_metadata(metadata: dict) -> None:
    versions = metadata.get("versions")
    if not isinstance(versions, list) or not versions:
        raise ValueError("package-metadata.json must define at least one version entry.")

    if len(versions) != 1:
        raise ValueError(
            "build_pcm.py currently supports exactly one version in package-metadata.json."
        )

    version_entry = versions[0]
    if not isinstance(version_entry, dict):
        raise ValueError("The first versions entry in package-metadata.json must be an object.")

    runtime = version_entry.get("runtime")
    if runtime not in {"swig", "ipc"}:
        raise ValueError(
            "package-metadata.json versions[0].runtime must be explicitly set to 'swig' or 'ipc'."
        )

    disallowed_fields = sorted(
        key for key in DISALLOWED_ARCHIVE_METADATA_FIELDS if key in version_entry
    )
    if disallowed_fields:
        fields = ", ".join(disallowed_fields)
        raise ValueError(
            f"package-metadata.json versions[0] must not include generated fields: {fields}."
        )


def validate_plugin_source() -> None:
    if not PLUGIN_SOURCE_DIR.is_dir():
        raise ValueError(f"Plugin source directory is missing: {PLUGIN_SOURCE_DIR}")

    missing_files = [
        filename
        for filename in sorted(REQUIRED_PLUGIN_FILES)
        if not (PLUGIN_SOURCE_DIR / filename).is_file()
    ]
    if missing_files:
        names = ", ".join(missing_files)
        raise ValueError(f"Plugin source directory is missing required files: {names}")


def get_version(metadata: dict) -> str:
    return str(metadata["versions"][0]["version"])


def get_package_archive_name(metadata: dict) -> str:
    return f"discord-rpc-for-kicad-v{get_version(metadata)}-pcm.zip"


def iter_package_source_paths() -> list[tuple[Path, Path]]:
    package_sources: list[tuple[Path, Path]] = []
    for source_path in sorted(PLUGIN_SOURCE_DIR.rglob("*")):
        if not source_path.is_file():
            continue
        if any(part in IGNORED_PLUGIN_PARTS for part in source_path.parts):
            continue
        if source_path.suffix in IGNORED_PLUGIN_SUFFIXES:
            continue

        package_sources.append(
            (source_path, Path("plugins") / source_path.relative_to(PLUGIN_SOURCE_DIR))
        )

    return package_sources


def copy_package_sources(staging_root: Path) -> int:
    install_size = 0

    for source_path, destination_relative_path in iter_package_source_paths():
        destination_path = staging_root / destination_relative_path
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, destination_path)
        install_size += destination_path.stat().st_size

    if ICON_SOURCE_PATH.exists():
        destination_path = staging_root / "resources" / "icon.png"
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ICON_SOURCE_PATH, destination_path)
        install_size += destination_path.stat().st_size

    return install_size


def write_package_metadata(staging_root: Path, metadata: dict) -> int:
    destination_path = staging_root / "metadata.json"
    destination_path.write_text(
        json.dumps(metadata, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return destination_path.stat().st_size


def create_package_archive(staging_root: Path, archive_path: Path) -> None:
    archive_path.parent.mkdir(parents=True, exist_ok=True)

    with ZipFile(archive_path, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(staging_root.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(staging_root).as_posix())


def sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file_handle:
        for chunk in iter(lambda: file_handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def remove_stale_archives(current_archive_name: str) -> None:
    PACKAGE_ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    for archive_path in PACKAGE_ARTIFACTS_DIR.glob("discord-rpc-for-kicad-v*-pcm.zip"):
        if archive_path.name == current_archive_name:
            continue
        archive_path.unlink(missing_ok=True)


def build_packages_document(
    metadata: dict,
    archive_name: str,
    archive_sha256: str,
    archive_size: int,
    install_size: int,
    base_url: str,
) -> dict:
    package_entry = copy.deepcopy(metadata)
    version_entry = package_entry["versions"][0]
    version_entry["download_url"] = f"{base_url}/pcm-artifacts/{archive_name}"
    version_entry["download_sha256"] = archive_sha256
    version_entry["download_size"] = archive_size
    version_entry["install_size"] = install_size
    return {"packages": [package_entry]}


def write_json(path: Path, document: dict) -> None:
    path.write_text(
        json.dumps(document, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def build_repository_document(
    metadata: dict,
    packages_sha256: str,
    update_timestamp: int,
    update_time_utc: str,
    base_url: str,
) -> dict:
    maintainer = metadata.get("maintainer") or metadata["author"]
    return {
        "$schema": "https://go.kicad.org/pcm/schemas/v2#/definitions/Repository",
        "maintainer": maintainer,
        "name": "Discord RPC for KiCad repository",
        "packages": {
            "sha256": packages_sha256,
            "update_time_utc": update_time_utc,
            "update_timestamp": update_timestamp,
            "url": f"{base_url}/packages.json",
        },
        "schema_version": 2,
    }


def main() -> None:
    args = parse_args()
    base_url = args.base_url.rstrip("/")
    metadata = read_package_metadata()
    validate_package_metadata(metadata)
    validate_plugin_source()
    archive_name = get_package_archive_name(metadata)
    archive_path = PACKAGE_ARTIFACTS_DIR / archive_name

    with tempfile.TemporaryDirectory() as temp_dir:
        staging_root = Path(temp_dir)
        install_size = copy_package_sources(staging_root)
        install_size += write_package_metadata(staging_root, metadata)
        create_package_archive(staging_root, archive_path)

    archive_size = archive_path.stat().st_size
    archive_sha256 = sha256_hex(archive_path)

    packages_document = build_packages_document(
        metadata=metadata,
        archive_name=archive_name,
        archive_sha256=archive_sha256,
        archive_size=archive_size,
        install_size=install_size,
        base_url=base_url,
    )
    write_json(PACKAGES_JSON_PATH, packages_document)

    now = datetime.now(timezone.utc)
    repository_document = build_repository_document(
        metadata=metadata,
        packages_sha256=sha256_hex(PACKAGES_JSON_PATH),
        update_timestamp=int(now.timestamp()),
        update_time_utc=now.strftime("%Y-%m-%d %H:%M:%S"),
        base_url=base_url,
    )
    write_json(REPOSITORY_JSON_PATH, repository_document)
    remove_stale_archives(archive_name)

    print(f"Built {archive_path.relative_to(REPO_ROOT)}")
    print(f"Updated {PACKAGES_JSON_PATH.name} and {REPOSITORY_JSON_PATH.name}")


if __name__ == "__main__":
    main()
