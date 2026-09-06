"""Verify the workflow migration on a guarded SQLite backup, never the source.

Run from this worktree using a Python environment containing project dependencies.
The generated database is intentionally retained for inspection and recovery.
"""

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4


WORKSPACE = Path(__file__).resolve().parents[1]
OLD_REVISION = "b6d7e8f9a0b1"
NEW_REVISION = "c7e9f1a2b3d4"


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def fingerprint(connection, table, columns):
    selected = ", ".join(quote_identifier(name) for name in columns)
    digest = hashlib.sha256()
    for row in connection.execute(f"SELECT {selected} FROM {quote_identifier(table)} ORDER BY id"):
        digest.update(json.dumps(row, ensure_ascii=True, default=str).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def snapshot(path, original_tables=None, tracked_columns=None):
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as connection:
        quick_check = [row[0] for row in connection.execute("PRAGMA quick_check")]
        if quick_check != ["ok"]:
            raise RuntimeError("The test copy failed SQLite quick_check.")
        tables = original_tables or [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        counts = {name: connection.execute(f"SELECT COUNT(*) FROM {quote_identifier(name)}").fetchone()[0]
                  for name in tables}
        violations = sorted(tuple(row) for row in connection.execute("PRAGMA foreign_key_check"))
        if tracked_columns is None:
            tracked_columns = {name: [row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(name)})")]
                               for name in ("campaigns", "email_reply", "outbound_emails")}
        return {
            "revision": connection.execute("SELECT version_num FROM alembic_version").fetchone()[0],
            "quick_check": "ok", "counts": counts,
            "foreign_key_violations": violations,
            "tracked_columns": tracked_columns,
            "original_content_hashes": {name: fingerprint(connection, name, columns)
                                        for name, columns in tracked_columns.items()},
        }


def verify_preserved(baseline, current, revision):
    if current["revision"] != revision:
        raise RuntimeError(f"Unexpected test-copy revision: {current['revision']}")
    if current["counts"] != baseline["counts"]:
        raise RuntimeError("An original table count changed in the test copy.")
    if current["foreign_key_violations"] != baseline["foreign_key_violations"]:
        raise RuntimeError("Foreign-key violations changed in the test copy.")
    if current["original_content_hashes"] != baseline["original_content_hashes"]:
        raise RuntimeError("Original columns changed in a batch-altered table.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True, help="Explicitly chosen SQLite source; opened read-only.")
    args = parser.parse_args()
    source = args.source.resolve(strict=True)
    instance = (WORKSPACE / "instance").resolve()
    instance.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = (instance / f"migration-check-{stamp}-{uuid4().hex[:8]}.db").resolve()
    if destination.parent != instance or destination == source or destination.exists():
        raise RuntimeError("Refusing an unsafe or existing test-copy destination.")
    source_stat = source.stat()
    free = shutil.disk_usage(instance).free
    if free < source_stat.st_size * 3 + 512 * 1024 * 1024:
        raise RuntimeError("Insufficient free space for a safe backup and migration.")
    print(json.dumps({"event": "copy_start", "source": str(source), "source_bytes": source_stat.st_size,
                      "destination": str(destination), "free_bytes": free}), flush=True)
    # Exclusive creation prevents accidental overwrite if another process races.
    with destination.open("xb"):
        pass
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(destination) as copied:
            original.backup(copied, pages=1024)
    baseline = snapshot(destination)
    if baseline["revision"] != OLD_REVISION:
        raise RuntimeError(f"Expected old source revision {OLD_REVISION}, got {baseline['revision']}; copy retained.")
    print(json.dumps({"event": "baseline", "revision": baseline["revision"], "counts": baseline["counts"],
                      "foreign_key_violation_count": len(baseline["foreign_key_violations"])}), flush=True)

    sys.path.insert(0, str(WORKSPACE))
    from app import create_app
    from extensions import db
    from flask_migrate import check, downgrade, upgrade

    app = create_app({"TESTING": True, "SQLALCHEMY_DATABASE_URI": "sqlite:///" + destination.as_posix(),
                      "SECRET_KEY": "migration-copy-verification-only", "MAIL_SUPPRESS_SEND": True,
                      "OPENAI_API_KEY": None, "BRAVE_API_KEY": None})
    results = []
    with app.app_context():
        for action, revision in ((upgrade, NEW_REVISION), (downgrade, OLD_REVISION), (upgrade, NEW_REVISION)):
            print(json.dumps({"event": action.__name__, "target": revision}), flush=True)
            action(directory=str(WORKSPACE / "migrations"), revision=revision)
            db.session.remove()
            db.engine.dispose()
            current = snapshot(destination, list(baseline["counts"]), baseline["tracked_columns"])
            verify_preserved(baseline, current, revision)
            results.append({"action": action.__name__, "revision": revision, "quick_check": "ok",
                            "original_counts_preserved": True, "original_columns_preserved": True,
                            "new_foreign_key_violations": 0})
        check(directory=str(WORKSPACE / "migrations"))
        db.session.remove()
        db.engine.dispose()
    final_source_stat = source.stat()
    print(json.dumps({"event": "verified", "source": str(source), "destination": str(destination),
                      "copy_bytes": destination.stat().st_size, "steps": results,
                      "alembic_model_drift": False, "original_table_counts": baseline["counts"],
                      "baseline_foreign_key_violation_count": len(baseline["foreign_key_violations"]),
                      "source_size_unchanged": final_source_stat.st_size == source_stat.st_size,
                      "source_mtime_unchanged": final_source_stat.st_mtime_ns == source_stat.st_mtime_ns,
                      "copy_retained": True}, indent=2), flush=True)


if __name__ == "__main__":
    main()
