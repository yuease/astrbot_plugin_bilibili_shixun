"""Short-lived public search evidence, isolated from permanent subscriptions."""

import json
from datetime import timedelta, timezone

from .agent_migrations import observation
from .pool import namespace
from .schemas import Article

TTL_HOURS = 24


def record_failure(store, profile, job, error_code, now):
    space, keyword = namespace(profile), job["params"]["keyword"]
    ref = dict(kind="temporary_keyword", key=keyword, label="临时搜索 · " + keyword)
    with store.db:
        observation(store.db, space, ref, "failed", now, error_code=error_code)
        store.db.execute(
            "INSERT OR REPLACE INTO source_scans VALUES(?,?,?,?,?)",
            (
                space,
                "temporary_keyword",
                keyword,
                now.isoformat(),
                json.dumps(
                    dict(
                        status="failed",
                        batches=0,
                        count=0,
                        stop_reason="source_failed",
                        remote_page=job["params"]["remote_page"],
                        has_more=None,
                        pagination_exhausted=False,
                        scan_started_at=job["started"],
                        scan_finished_at=now.isoformat(),
                    )
                ),
            ),
        )
    prune_temporary(store, profile, now)


def prune_temporary(store, profile, now):
    space = namespace(profile)
    cutoff = (now - timedelta(hours=TTL_HOURS)).isoformat()
    with store.db:
        store.db.execute(
            "DELETE FROM temporary_items WHERE namespace=? AND julianday(last_seen)<julianday(?)",
            (space, cutoff),
        )
        store.db.execute(
            "DELETE FROM temporary_origins WHERE namespace=? AND julianday(fetched)<julianday(?)",
            (space, cutoff),
        )
        store.db.execute(
            "DELETE FROM temporary_items WHERE namespace=? AND rowid NOT IN (SELECT rowid FROM temporary_items WHERE namespace=? ORDER BY julianday(last_seen) DESC,rowid DESC LIMIT 200)",
            (space, space),
        )
        store.db.execute(
            "DELETE FROM source_observations WHERE namespace=? AND kind='temporary_keyword' AND julianday(checked)<julianday(?)",
            (space, cutoff),
        )
        store.db.execute(
            "DELETE FROM source_scans WHERE namespace=? AND kind='temporary_keyword' AND julianday(checked)<julianday(?)",
            (space, cutoff),
        )


def save_temporary(store, profile, job, response, now):
    space, keyword = namespace(profile), job["params"]["keyword"]
    stamp = now.astimezone(timezone.utc).isoformat()
    articles = []
    for raw in response["items"][:20]:
        article = Article.model_validate(raw).model_dump()
        article["fetched_at"] = stamp
        articles.append(store.canonicalize(article))
    with store.db:
        for article in articles:
            store.db.execute(
                """INSERT INTO temporary_items VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(namespace,keyword,entry_key) DO UPDATE SET id=excluded.id,last_seen=excluded.last_seen,query_id=excluded.query_id,data=excluded.data""",
                (
                    space,
                    keyword,
                    article["id"],
                    article["id"],
                    stamp,
                    stamp,
                    job["id"],
                    json.dumps(article, ensure_ascii=False),
                ),
            )
            store.db.execute(
                "INSERT OR REPLACE INTO temporary_origins VALUES(?,?,?,?,?)",
                (space, job["id"], keyword, article["id"], stamp),
            )
        ref = dict(kind="temporary_keyword", key=keyword, label="临时搜索 · " + keyword)
        observation(store.db, space, ref, "ok" if articles else "empty", now, len(articles))
        scan = {
            "status": "ok" if articles else "empty",
            "batches": 1,
            "count": len(articles),
            "stop_reason": "single_batch",
            "remote_page": job["params"]["remote_page"],
            "has_more": response.get("remote_has_more"),
            "pagination_exhausted": False,
            "scan_started_at": job["started"],
            "scan_finished_at": stamp,
        }
        store.db.execute(
            "INSERT OR REPLACE INTO source_scans VALUES(?,?,?,?,?)",
            (space, "temporary_keyword", keyword, stamp, json.dumps(scan)),
        )
    prune_temporary(store, profile, now)
    return len(articles)
