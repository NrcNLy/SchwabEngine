#!/usr/bin/env python3
"""
scripts/sync_engine_drive.py
============================
Resilient, one-way (Local -> Google Drive) authoritative mirror for SchwabEngine.

Replaces the legacy ``sync_docs_bidirectional.py``. The local checkout is the source of truth;
nothing is ever copied back from Drive and (unless ``--prune`` is passed) nothing is ever deleted
from Drive.

Target:  G:\\My Drive\\Projects\\SchwabEngine\\   (override with --drive-root / SCHWAB_DRIVE_ROOT)
Mirrors: docs/, state/, data/, config/, strategies/, indicators/, architecture.md, research_context.md

Safety rules
------------
* Exclusions: .git/, .venv/, node_modules/, __pycache__/, logs/*.jsonl, *token*.json, *.vault.json,
  .env*, and temporary / lock artifacts (*~, *.tmp, *.wal, *.shm, *-wal, *-shm, *.pyc).
* SQLite databases (``*.db``) are NEVER raw-copied. Each one is snapshotted with SQLite's online
  backup API into a local temp file, VACUUMed, and published to Drive as ``<stem>_backup.db``
  (``state/trading_journal.db`` -> ``state/trading_journal_backup.db``). This avoids write-lock
  conflicts and torn WAL pages.
* Every file is published through a temp name + atomic ``os.replace`` so Drive never sees a
  half-written file.

Modes
-----
``--once``    single pass, exit code 0 on success (2 when Drive is unreachable, 1 on file errors).
default       asynchronous daemon: poll every ``--poll`` seconds (15), then debounce ``--debounce``
              seconds (2) until the tree is quiet before syncing.
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import logging
import os
import shutil
import socket
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DRIVE_ROOT = Path(r"G:\My Drive\Projects\SchwabEngine")

WHITELIST_DIRS: Tuple[str, ...] = ("docs", "state", "data", "config", "strategies", "indicators")
WHITELIST_ROOT_FILES: Tuple[str, ...] = ("architecture.md", "research_context.md")

EXCLUDED_DIR_NAMES = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".idea", ".vscode",
    ".tmp.driveupload",
})
EXCLUDED_FILE_PATTERNS: Tuple[str, ...] = (
    "*token*.json",
    "*.vault.json",
    ".env*",
    "*~",
    "*.tmp",
    "*.wal",
    "*.shm",
    "*-wal",
    "*-shm",
    "*.db-journal",
    "*.pyc",
    "*.pyo",
    "*.syncpart",
    "desktop.ini",
    "thumbs.db",
    ".ds_store",
    ".~*",
    "~$*",
)
# Relative-path glob patterns (POSIX separators, lower-case) excluded regardless of directory name.
EXCLUDED_REL_PATTERNS: Tuple[str, ...] = (
    "logs/*.jsonl",
    "*/logs/*.jsonl",
)

DB_SUFFIX = ".db"
DB_BACKUP_TOKEN = "_backup"
MTIME_TOLERANCE_S = 2.0
DEBOUNCE_MAX_ROUNDS = 5
SINGLETON_PORT = 47321

logger = logging.getLogger("sync_engine_drive")

# rel-path -> (mtime_ns, size) or, for databases, a composite signature.
Signature = Tuple[int, int]
Snapshot = Dict[str, Signature]


@dataclass
class SyncReport:
    copied: int = 0
    skipped: int = 0
    db_snapshots: int = 0
    pruned: int = 0
    errors: List[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.copied} copied | {self.db_snapshots} db snapshots | {self.skipped} up-to-date | "
            f"{self.pruned} pruned | {len(self.errors)} errors"
        )


# ---------------------------------------------------------------------------------------------
# Filtering & scanning
# ---------------------------------------------------------------------------------------------
def is_excluded_file(rel: Path) -> bool:
    name = rel.name.lower()
    for pat in EXCLUDED_FILE_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return True
    rel_posix = rel.as_posix().lower()
    for pat in EXCLUDED_REL_PATTERNS:
        if fnmatch.fnmatch(rel_posix, pat):
            return True
    return False


def is_database(rel: Path) -> bool:
    return rel.suffix.lower() == DB_SUFFIX and not rel.stem.lower().endswith(DB_BACKUP_TOKEN)


def backup_rel_for(rel: Path) -> Path:
    """state/trading_journal.db -> state/trading_journal_backup.db"""
    return rel.with_name(f"{rel.stem}{DB_BACKUP_TOKEN}{rel.suffix}")


def iter_local_files(local_root: Path) -> Iterator[Tuple[Path, os.stat_result]]:
    """Yields (relative_path, stat) for every whitelisted, non-excluded local file."""
    for name in WHITELIST_ROOT_FILES:
        p = local_root / name
        try:
            st = p.stat()
        except OSError:
            continue
        yield Path(name), st

    for top in WHITELIST_DIRS:
        base = local_root / top
        if not base.is_dir():
            continue
        stack = [base]
        while stack:
            current = stack.pop()
            try:
                entries = list(os.scandir(current))
            except OSError as exc:
                logger.warning("Cannot scan %s: %s", current, exc)
                continue
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.lower() not in EXCLUDED_DIR_NAMES:
                            stack.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    rel = Path(entry.path).relative_to(local_root)
                    if is_excluded_file(rel):
                        continue
                    yield rel, entry.stat(follow_symlinks=False)
                except OSError:
                    continue


def _wal_size(local_root: Path, rel: Path) -> int:
    """
    Size of the database's -wal file (0 when absent) so WAL-only commits trigger a snapshot.

    Deliberately size-based: merely reading a WAL-mode database (including our own backup) touches the
    -shm/-wal mtimes, which would otherwise make every pass look like a fresh change.
    """
    try:
        return (local_root / (rel.as_posix() + "-wal")).stat().st_size
    except OSError:
        return 0


def scan_snapshot(local_root: Path) -> Snapshot:
    snap: Snapshot = {}
    for rel, st in iter_local_files(local_root):
        key = rel.as_posix()
        if is_database(rel):
            snap[key] = (st.st_mtime_ns, st.st_size + (_wal_size(local_root, rel) << 40))
        else:
            snap[key] = (st.st_mtime_ns, st.st_size)
    return snap


# ---------------------------------------------------------------------------------------------
# Publishing primitives
# ---------------------------------------------------------------------------------------------
def _atomic_publish(src: Path, dst: Path, retries: int = 4) -> None:
    """Copies ``src`` to ``dst`` via temp file + os.replace (retrying on Drive/AV file locks)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.syncpart")
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            shutil.copyfile(src, tmp)
            shutil.copystat(src, tmp)
            os.replace(tmp, dst)
            return
        except OSError as exc:
            last_exc = exc
            time.sleep(0.25 * (attempt + 1))
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass
    assert last_exc is not None
    raise last_exc


def _file_up_to_date(local_st: os.stat_result, dst: Path) -> bool:
    try:
        dst_st = dst.stat()
    except OSError:
        return False
    if dst_st.st_size != local_st.st_size:
        return False
    return abs(dst_st.st_mtime - local_st.st_mtime) <= MTIME_TOLERANCE_S


def snapshot_database(src: Path, dst: Path) -> None:
    """
    Writes a consistent, vacuumed copy of the SQLite database ``src`` to ``dst``.

    Uses sqlite3's online backup API against a local temp file (so a read lock is held only for the
    page copy, never across the Drive upload), runs VACUUM on the copy, forces journal_mode=DELETE
    so the artifact is a single self-contained file, then publishes it atomically.
    """
    with tempfile.TemporaryDirectory(prefix="schwab_dbsnap_") as tmp_dir:
        tmp_db = Path(tmp_dir) / dst.name
        try:
            src_conn = sqlite3.connect(f"{src.resolve().as_uri()}?mode=ro", uri=True, timeout=10.0)
        except sqlite3.OperationalError:
            src_conn = sqlite3.connect(str(src), timeout=10.0)
        try:
            dst_conn = sqlite3.connect(str(tmp_db))
            try:
                src_conn.backup(dst_conn)
                dst_conn.execute("PRAGMA journal_mode=DELETE")
                dst_conn.execute("VACUUM")
                dst_conn.commit()
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
        try:
            shutil.copystat(src, tmp_db)
        except OSError:
            pass
        _atomic_publish(tmp_db, dst)


# ---------------------------------------------------------------------------------------------
# Sync pass
# ---------------------------------------------------------------------------------------------
def drive_available(drive_root: Path) -> bool:
    """
    True when the Drive mount is present and the target folder exists or can be created.

    The grandparent of the target (``G:/My Drive`` for the default root) must already exist, which
    prevents creating stray folders on machines without Drive (e.g. the Linux VM, where a Windows
    path is not even absolute).
    """
    if not drive_root.is_absolute():
        return False
    if drive_root.is_dir():
        return True
    if not drive_root.parent.parent.is_dir():
        return False
    try:
        drive_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return True

def sync_once(
    local_root: Path,
    drive_root: Path,
    prune: bool = False,
    dry_run: bool = False,
    previous: Optional[Snapshot] = None,
    current: Optional[Snapshot] = None,
) -> SyncReport:
    """
    One authoritative Local -> Drive pass.

    When ``previous`` and ``current`` snapshots are supplied (daemon mode) only keys whose signature
    changed are examined; otherwise every whitelisted file is compared against its Drive twin.
    """
    report = SyncReport()
    expected_rel: set[str] = set()

    for rel, local_st in iter_local_files(local_root):
        key = rel.as_posix()
        db = is_database(rel)
        dst_rel = backup_rel_for(rel) if db else rel
        expected_rel.add(dst_rel.as_posix())

        if previous is not None and current is not None and previous.get(key) == current.get(key):
            report.skipped += 1
            continue

        src = local_root / rel
        dst = drive_root / dst_rel
        try:
            if db:
                if dry_run:
                    logger.info("[DRY] DB SNAPSHOT %s -> %s", rel, dst_rel)
                else:
                    snapshot_database(src, dst)
                    logger.info("[DB SNAPSHOT] %s -> %s", rel.as_posix(), dst_rel.as_posix())
                report.db_snapshots += 1
            elif _file_up_to_date(local_st, dst):
                report.skipped += 1
            else:
                if dry_run:
                    logger.info("[DRY] COPY %s", rel)
                else:
                    _atomic_publish(src, dst)
                    logger.info("[SYNC -> DRIVE] %s", rel.as_posix())
                report.copied += 1
        except (OSError, sqlite3.Error) as exc:
            msg = f"{rel.as_posix()}: {exc}"
            report.errors.append(msg)
            logger.error("Sync failed for %s", msg)

    if prune:
        report.pruned = _prune_drive(drive_root, expected_rel, dry_run)
    return report


def _prune_drive(drive_root: Path, expected_rel: set[str], dry_run: bool) -> int:
    pruned = 0
    for top in WHITELIST_DIRS:
        base = drive_root / top
        if not base.is_dir():
            continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if d.lower() not in EXCLUDED_DIR_NAMES]
            for fname in files:
                full = Path(root) / fname
                rel = full.relative_to(drive_root)
                if rel.as_posix() in expected_rel or is_excluded_file(rel):
                    continue
                if dry_run:
                    logger.info("[DRY] PRUNE %s", rel)
                else:
                    try:
                        full.unlink()
                        logger.info("[PRUNE] %s", rel.as_posix())
                    except OSError as exc:
                        logger.error("Prune failed for %s: %s", rel, exc)
                        continue
                pruned += 1
    return pruned


# ---------------------------------------------------------------------------------------------
# Daemon
# ---------------------------------------------------------------------------------------------
def _acquire_singleton() -> Optional[socket.socket]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SINGLETON_PORT))
        sock.listen(1)
        return sock
    except OSError:
        sock.close()
        return None


async def run_daemon(
    local_root: Path,
    drive_root: Path,
    poll_s: float,
    debounce_s: float,
    prune: bool,
    stop: Optional[asyncio.Event] = None,
) -> None:
    """
    Polls the whitelisted tree every ``poll_s`` seconds. When a change is detected the loop waits
    ``debounce_s`` and re-scans until two consecutive scans agree (or DEBOUNCE_MAX_ROUNDS is hit, which
    bounds the delay for continuously-written files such as WAL-mode databases), then syncs.
    """
    stop = stop or asyncio.Event()
    last_synced: Optional[Snapshot] = None
    logger.info("Drive sync daemon online: %s -> %s (poll=%ss, debounce=%ss)", local_root, drive_root, poll_s, debounce_s)

    while not stop.is_set():
        try:
            if not await asyncio.to_thread(drive_available, drive_root):
                logger.warning("Drive target unavailable (%s); will retry in %ss.", drive_root, poll_s)
                last_synced = None
            else:
                snap = await asyncio.to_thread(scan_snapshot, local_root)
                if last_synced is None or snap != last_synced:
                    for _ in range(DEBOUNCE_MAX_ROUNDS):
                        await asyncio.sleep(debounce_s)
                        again = await asyncio.to_thread(scan_snapshot, local_root)
                        if again == snap:
                            break
                        snap = again
                    report = await asyncio.to_thread(
                        sync_once, local_root, drive_root, prune, False, last_synced, snap
                    )
                    logger.info("Sync pass complete: %s", report.summary())
                    # Only advance the baseline when everything succeeded so failures are retried.
                    last_synced = snap if not report.errors else last_synced
        except Exception:
            logger.exception("Drive sync cycle failed; continuing.")
        try:
            await asyncio.wait_for(stop.wait(), timeout=poll_s)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------------------------
def _configure_logging(local_root: Path, daemon: bool) -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    if sys.stdout is not None:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(fmt)
        root.addHandler(stream)
    if daemon:
        log_dir = local_root / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            fh = RotatingFileHandler(log_dir / "drive_sync.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError:
            pass


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="One-way Local -> Google Drive mirror for SchwabEngine.")
    p.add_argument("--once", action="store_true", help="Run a single full pass and exit.")
    p.add_argument("--poll", type=float, default=15.0, help="Daemon poll interval in seconds (default 15).")
    p.add_argument("--debounce", type=float, default=2.0, help="Debounce window in seconds (default 2).")
    p.add_argument("--drive-root", type=Path,
                   default=Path(os.environ.get("SCHWAB_DRIVE_ROOT", str(DEFAULT_DRIVE_ROOT))),
                   help="Drive destination root.")
    p.add_argument("--local-root", type=Path, default=PROJECT_ROOT, help="Local project root.")
    p.add_argument("--prune", action="store_true",
                   help="Delete Drive files inside mirrored dirs that no longer exist locally (off by default).")
    p.add_argument("--dry-run", action="store_true", help="Log actions without writing to Drive.")
    p.add_argument("--skip-if-unavailable", action="store_true",
                   help="Exit 0 (instead of 2) when the Drive mount is missing, e.g. on the Linux VM.")
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    local_root = args.local_root.resolve()
    drive_root = args.drive_root
    _configure_logging(local_root, daemon=not args.once)

    if args.once:
        if not drive_available(drive_root):
            level = logging.WARNING if args.skip_if_unavailable else logging.ERROR
            logger.log(level, "Drive target unavailable: %s", drive_root)
            return 0 if args.skip_if_unavailable else 2
        logger.info("=" * 65)
        logger.info("SINGLE-PASS DRIVE SYNC  %s -> %s", local_root, drive_root)
        report = sync_once(local_root, drive_root, prune=args.prune, dry_run=args.dry_run)
        logger.info("Result: %s", report.summary())
        logger.info("=" * 65)
        return 1 if report.errors else 0

    lock = _acquire_singleton()
    if lock is None:
        logger.error("Another sync_engine_drive daemon is already running (port %d); exiting.", SINGLETON_PORT)
        return 3
    try:
        asyncio.run(run_daemon(local_root, drive_root, args.poll, args.debounce, args.prune))
    except KeyboardInterrupt:
        logger.info("Drive sync daemon stopped.")
    finally:
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
