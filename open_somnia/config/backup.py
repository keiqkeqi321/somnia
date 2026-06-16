from __future__ import annotations

import time
from pathlib import Path

from open_somnia.storage.common import atomic_write_text

BACKUP_DIRNAME = "config_backups"
LAST_GOOD_SUFFIX = ".last_good"
BROKEN_SUFFIX = ".broken"
TIMESTAMP_SUFFIX = ".bak"
DEFAULT_BACKUP_KEEP = 10


def backup_dir_for_config(config_path: Path) -> Path:
    return config_path.parent / BACKUP_DIRNAME


def _backup_stem(config_path: Path) -> str:
    return config_path.name


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def last_good_path(config_path: Path) -> Path:
    return backup_dir_for_config(config_path) / f"{_backup_stem(config_path)}{LAST_GOOD_SUFFIX}"


def save_last_good(config_path: Path) -> Path | None:
    if not config_path.exists() or not config_path.is_file():
        return None
    target = last_good_path(config_path)
    atomic_write_text(target, config_path.read_text(encoding="utf-8"))
    return target


def save_timestamp_backup(config_path: Path, *, suffix: str = TIMESTAMP_SUFFIX) -> Path | None:
    if not config_path.exists() or not config_path.is_file():
        return None
    backup_dir = backup_dir_for_config(config_path)
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = _timestamp()
    base_name = f"{_backup_stem(config_path)}.{timestamp}{suffix}"
    target = backup_dir / base_name
    counter = 1
    while target.exists():
        target = backup_dir / f"{_backup_stem(config_path)}.{timestamp}.{counter}{suffix}"
        counter += 1
    atomic_write_text(target, config_path.read_text(encoding="utf-8"))
    return target


def save_broken_backup(config_path: Path) -> Path | None:
    return save_timestamp_backup(config_path, suffix=BROKEN_SUFFIX)


def restore_last_good(config_path: Path) -> bool:
    source = last_good_path(config_path)
    if not source.exists() or not source.is_file():
        return False
    atomic_write_text(config_path, source.read_text(encoding="utf-8"))
    return True


def list_config_backups(config_path: Path) -> list[Path]:
    backup_dir = backup_dir_for_config(config_path)
    if not backup_dir.exists():
        return []
    stem = _backup_stem(config_path)
    return sorted(
        [path for path in backup_dir.iterdir() if path.is_file() and path.name.startswith(f"{stem}.")],
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def prune_config_backups(config_path: Path, *, keep: int = DEFAULT_BACKUP_KEEP) -> None:
    timestamped = [
        path
        for path in list_config_backups(config_path)
        if path.name.endswith(TIMESTAMP_SUFFIX) or path.name.endswith(BROKEN_SUFFIX)
    ]
    for path in timestamped[max(0, int(keep)) :]:
        try:
            path.unlink()
        except OSError:
            continue


def write_config_text(config_path: Path, text: str, *, backup_existing: bool = True) -> None:
    if backup_existing:
        save_timestamp_backup(config_path)
        prune_config_backups(config_path)
    atomic_write_text(config_path, text)


def remove_config_file(config_path: Path, *, backup_existing: bool = True) -> None:
    if not config_path.exists():
        return
    if backup_existing:
        save_timestamp_backup(config_path)
        prune_config_backups(config_path)
    config_path.unlink()
