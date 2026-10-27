"""Idempotent attribution migration. Original run JSON and feedback are immutable."""

import json

from .schemas import Profile


def migrate(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS context_migrations(
            run TEXT PRIMARY KEY REFERENCES runs(id), original_context TEXT, stable_context TEXT,
            reason TEXT NOT NULL)""")
        db.execute("""CREATE TABLE IF NOT EXISTS source_checks(
            account_scope TEXT, kind TEXT, key TEXT, status TEXT, checked TEXT, PRIMARY KEY(account_scope,kind,key))""")
        # Run-at-a-time attribution; unknown hashes are left isolated, not silently mixed.
        for row in db.execute(
            "SELECT id,context,data FROM runs WHERE id NOT IN (SELECT run FROM context_migrations)"
        ).fetchall():
            target = None
            reason = "unattributed: missing or incompatible profile snapshot"
            try:
                p = Profile.model_validate(json.loads(row["data"])["profile"])
                if row["context"] in (p.legacy_context, p.context):
                    target = p.context
                    reason = "stable-v1: exact explicit interest and feature policy"
            except (ValueError, KeyError, TypeError):
                pass
            db.execute(
                "INSERT INTO context_migrations VALUES (?,?,?,?)",
                (row["id"], row["context"], target, reason),
            )
            if target and target != row["context"]:
                db.execute("UPDATE runs SET context=? WHERE id=?", (target, row["id"]))
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            db.execute("PRAGMA user_version=2")
        db.commit()
    except Exception:
        db.rollback()
        raise
