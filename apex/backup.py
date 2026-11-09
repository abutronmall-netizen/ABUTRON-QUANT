"""Journal backup and restore — a snapshot an operator can actually verify.

Two rules, both learned the hard way from desks that had "a copy":

1. **Copy the database, not the file.** `sqlite3.Connection.backup()` produces a
   consistent snapshot while the engine is writing; `cp apex.db` does not, and a
   -wal file left behind makes the copy look complete and be wrong.
2. **Prove the copy.** Every snapshot gets a sidecar manifest with row counts and
   a sha256 of the file. A restore refuses a snapshot whose hash no longer
   matches, because "restore from backup" is the worst possible moment to
   discover the backup is a half-written file.

Backups are named `apex-YYYYmmdd-HHMMSS.db`; `keep` prunes the oldest beyond the
retention you ask for. Nothing here talks to a broker and nothing touches the
live journal except to read it.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from typing import Optional

MANIFEST_SUFFIX = ".json"
TABLES = ("orders", "trades", "equity", "signals", "events", "runtime_state")


class BackupError(RuntimeError):
    pass


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _counts(path: str) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True)
    try:
        out: dict[str, int] = {}
        for table in TABLES:
            try:
                out[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.DatabaseError:
                continue
        return out
    finally:
        conn.close()


def create_backup(db_path: str, out_dir: str = "backups", keep: int = 14,
                  now: Optional[float] = None, label: str = "") -> dict:
    """Write a consistent snapshot plus manifest; prune old snapshots."""
    if not os.path.exists(db_path):
        raise BackupError(f"no journal at {db_path}")
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now if now is not None else time.time()))
    name = f"apex-{stamp}{('-' + label) if label else ''}.db"
    dest = os.path.join(out_dir, name)

    src = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)                 # consistent even while the engine writes
            dst.execute("PRAGMA journal_mode=DELETE")   # one self-contained file
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    manifest = {
        "source": os.path.abspath(db_path),
        "created_ts": time.time(),
        "sha256": _sha256(dest),
        "size_bytes": os.path.getsize(dest),
        "counts": _counts(dest),
    }
    with open(dest + MANIFEST_SUFFIX, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    pruned = _prune(out_dir, keep)
    return {"path": dest, "manifest": dest + MANIFEST_SUFFIX, "pruned": pruned, **manifest}


def _prune(out_dir: str, keep: int) -> int:
    if keep is None or keep <= 0:
        return 0
    files = sorted(f for f in os.listdir(out_dir)
                   if f.startswith("apex-") and f.endswith(".db"))
    excess = files[:-keep] if len(files) > keep else []
    for name in excess:
        for path in (os.path.join(out_dir, name), os.path.join(out_dir, name + MANIFEST_SUFFIX)):
            try:
                os.remove(path)
            except OSError:
                pass
    return len(excess)


def restore_backup(backup_path: str, db_path: str, force: bool = False) -> dict:
    """Restore a snapshot into `db_path`, verifying the manifest when present."""
    if not os.path.exists(backup_path):
        raise BackupError(f"no backup at {backup_path}")
    manifest_path = backup_path + MANIFEST_SUFFIX
    manifest = {}
    if os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError) as exc:
            raise BackupError(f"unreadable manifest {manifest_path}: {exc}") from exc
        expected = str(manifest.get("sha256") or "")
        if expected and _sha256(backup_path) != expected:
            raise BackupError("backup does not match its manifest (sha256) — refusing to restore")
    if os.path.exists(db_path) and not force:
        raise BackupError(f"{db_path} exists; pass --force to overwrite it")

    parent = os.path.dirname(os.path.abspath(db_path))
    os.makedirs(parent, exist_ok=True)
    # Remove stale write-ahead files: a restored main file beside an old -wal is
    # how a "restored" journal silently keeps rows from the run you replaced.
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(db_path + suffix)
        except OSError:
            pass

    src = sqlite3.connect(f"file:{os.path.abspath(backup_path)}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(db_path)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()

    return {"source": os.path.abspath(backup_path), "path": os.path.abspath(db_path),
            "verified": bool(manifest.get("sha256")), "counts": _counts(db_path)}
