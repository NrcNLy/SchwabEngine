import os
import shutil
import time
from pathlib import Path

DIR_LOCAL = Path(r"C:\Projects\schwab_engine\docs")
DIR_DRIVE = Path(r"G:\My Drive\Financial\SchwabEngine\docs")

IGNORE_FILENAMES = {
    "desktop.ini",
    "thumbs.db",
    ".ds_store",
    "icon\r",
}
IGNORE_EXTENSIONS = {".tmp", ".crdownload", ".part"}
MTIME_TOLERANCE = 1.0

def is_ignored(rel_path: Path) -> bool:
    name_lower = rel_path.name.lower()
    if name_lower in IGNORE_FILENAMES:
        return True
    if rel_path.suffix.lower() in IGNORE_EXTENSIONS:
        return True
    if name_lower.startswith(".~") or name_lower.startswith("~$"):
        return True
    return False

def get_relative_file_map(base_dir: Path) -> dict[Path, float]:
    file_map = {}
    if not base_dir.exists():
        return file_map
    for root, _, files in os.walk(base_dir):
        for f in files:
            full_path = Path(root) / f
            rel_path = full_path.relative_to(base_dir)
            if not is_ignored(rel_path):
                try:
                    file_map[rel_path] = full_path.stat().st_mtime
                except OSError:
                    continue
    return file_map

def copy_file(src: Path, dst: Path, action_label: str):
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[{action_label}] {src.name}")

def sync_bidirectional():
    DIR_LOCAL.mkdir(parents=True, exist_ok=True)
    DIR_DRIVE.mkdir(parents=True, exist_ok=True)

    local_files = get_relative_file_map(DIR_LOCAL)
    drive_files = get_relative_file_map(DIR_DRIVE)
    all_rel_paths = set(local_files.keys()).union(set(drive_files.keys()))

    synced_to_drive = 0
    synced_to_local = 0
    in_sync = 0

    print("=" * 65)
    print("BIDIRECTIONAL DOCS SYNC")
    print(f"Local: {DIR_LOCAL}")
    print(f"Drive: {DIR_DRIVE}")
    print("=" * 65)

    for rel_path in sorted(all_rel_paths):
        local_path = DIR_LOCAL / rel_path
        drive_path = DIR_DRIVE / rel_path
        in_local = rel_path in local_files
        in_drive = rel_path in drive_files

        if in_local and not in_drive:
            copy_file(local_path, drive_path, "NEW -> DRIVE")
            synced_to_drive += 1
        elif in_drive and not in_local:
            copy_file(drive_path, local_path, "NEW -> LOCAL")
            synced_to_local += 1
        else:
            mtime_local = local_files[rel_path]
            mtime_drive = drive_files[rel_path]
            if mtime_local - mtime_drive > MTIME_TOLERANCE:
                copy_file(local_path, drive_path, "UPDATE -> DRIVE")
                synced_to_drive += 1
            elif mtime_drive - mtime_local > MTIME_TOLERANCE:
                copy_file(drive_path, local_path, "UPDATE -> LOCAL")
                synced_to_local += 1
            else:
                in_sync += 1

    print("-" * 65)
    print(f"Result: {synced_to_drive} to Drive | {synced_to_local} to Local | {in_sync} in sync")
    print("=" * 65)

if __name__ == "__main__":
    sync_bidirectional()
