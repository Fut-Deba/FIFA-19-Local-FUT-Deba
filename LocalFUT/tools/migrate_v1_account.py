#!/usr/bin/env python3
"""Explicitly copy one historical root account into the isolated v1 profile."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path


PROFILE_ID = "v1-3865658"
RECEIPT_NAME = "V1_ACCOUNT_MIGRATION.json"
COPY_CHUNK_SIZE = 1024 * 1024


class MigrationError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(COPY_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_sqlite(path: Path) -> None:
    try:
        connection = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            rows = [str(row[0]) for row in connection.execute(
                "PRAGMA integrity_check")]
        finally:
            connection.close()
    except sqlite3.Error as error:
        raise MigrationError("SQLite validation failed for %s: %s" %
                             (path, error)) from error
    if rows != ["ok"]:
        raise MigrationError("SQLite integrity check failed for %s: %s" %
                             (path, "; ".join(rows)))


def _require_quiet_source(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            raise MigrationError(
                "historical database has a SQLite sidecar; close every old "
                "LocalFUT19 process before migrating: %s" % sidecar)


def _copy_verified(source: Path, destination: Path,
                   expected_hash: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending = destination.with_name(destination.name + ".pending")
    if pending.exists():
        if not pending.is_file() or sha256(pending) != expected_hash:
            raise MigrationError(
                "unexpected pending migration file; no file was replaced: %s" %
                pending)
    else:
        try:
            with source.open("rb") as reader, pending.open("xb") as writer:
                for chunk in iter(lambda: reader.read(COPY_CHUNK_SIZE), b""):
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        except OSError as error:
            raise MigrationError("could not create verified copy %s: %s" %
                                 (pending, error)) from error
    if sha256(pending) != expected_hash:
        raise MigrationError("copy hash verification failed: %s" % pending)
    _validate_sqlite(pending)
    os.replace(pending, destination)


def _write_receipt(path: Path, payload: dict) -> None:
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(
        "utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise MigrationError(
                "existing migration receipt does not match the verified copy: "
                "%s" % path)
        return
    pending = path.with_name(path.name + ".pending")
    if pending.exists():
        if pending.read_bytes() != encoded:
            raise MigrationError(
                "unexpected pending migration receipt; no file was replaced: "
                "%s" % pending)
    else:
        with pending.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    os.replace(pending, path)


def migrate_historical_v1_account(data_root: Path | str,
                                  account_mode: str) -> dict:
    root = Path(data_root).expanduser().resolve()
    mode = str(account_mode or "").strip().upper()
    if mode not in {"NORMAL", "RTG"}:
        raise MigrationError("account mode must be NORMAL or RTG")
    filename = "fut19-rtg.sqlite3" if mode == "RTG" else "fut19-local.sqlite3"
    profile_name = PROFILE_ID + ("-rtg" if mode == "RTG" else "")
    source = root / filename
    destination = root / "profiles" / profile_name / filename

    if not source.is_file() or source.is_symlink():
        raise MigrationError("historical v1 account was not found: %s" % source)
    _require_quiet_source(source)
    _validate_sqlite(source)
    source_hash = sha256(source)

    # The historical account is never the working input.  A verified,
    # content-addressed backup is published first and every later copy is made
    # from that backup, so a failed retry cannot consume or alter the source.
    backup = (root / "migration-backups" / PROFILE_ID /
              ("%s-%s.sqlite3" % (mode.lower(), source_hash)))
    if backup.exists():
        if (not backup.is_file() or backup.is_symlink() or
                sha256(backup) != source_hash):
            raise MigrationError("existing backup failed hash verification: %s" %
                                 backup)
        _validate_sqlite(backup)
    else:
        _copy_verified(source, backup, source_hash)
    backup_hash = sha256(backup)
    if sha256(source) != source_hash:
        raise MigrationError(
            "historical database changed while its backup was being verified")

    status = "migrated"
    if destination.exists():
        if (not destination.is_file() or destination.is_symlink() or
                sha256(destination) != source_hash):
            raise MigrationError(
                "v1 profile already contains a different account; no file was "
                "replaced: %s" % destination)
        _validate_sqlite(destination)
        status = "already-migrated"
    else:
        _copy_verified(backup, destination, backup_hash)
    destination_hash = sha256(destination)

    receipt_payload = {
        "schemaVersion": 1,
        "operation": "historical-v1-account-copy",
        "accountMode": mode,
        "profileId": profile_name,
        "source": {"path": os.fspath(source), "sha256": source_hash},
        "backup": {"path": os.fspath(backup), "sha256": backup_hash},
        "destination": {
            "path": os.fspath(destination),
            "sha256": destination_hash,
        },
    }
    _write_receipt(destination.parent / RECEIPT_NAME, receipt_payload)
    return {"status": status, **receipt_payload}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--account-mode", choices=("NORMAL", "RTG"),
                        required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = migrate_historical_v1_account(
            args.data_root, args.account_mode)
    except MigrationError as error:
        print("ERROR: %s" % error, file=sys.stderr)
        return 2
    print("Historical v1 account migration: %s" % result["status"])
    print("Source SHA-256     : %s" % result["source"]["sha256"])
    print("Backup SHA-256     : %s" % result["backup"]["sha256"])
    print("Destination SHA-256: %s" % result["destination"]["sha256"])
    print("Backup             : %s" % result["backup"]["path"])
    print("Destination        : %s" % result["destination"]["path"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
