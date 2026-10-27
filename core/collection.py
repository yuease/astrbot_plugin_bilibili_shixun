"""Bounded collection into SQLite; no model imports or selection."""

import os
import re
from collections import deque
from datetime import timedelta
from time import monotonic

from .errors import (
    classify,
    diagnostic,
    public_diagnostic,
    run_failure,
    safe_model_logs,
    safe_type,
    source_diagnostic,
)
from .filtering import marks
from .pool import namespace, upsert
from .schemas import Article, Profile
from .source_types import SourceBatch, SourceError
from .sources import prepare, subscriptions
from .storage import atomic_write, dump


def collect(profile, *, store, clock, run_id, fetcher, timer=monotonic):
    profile = Profile.model_validate(profile)
    now = clock()
    started = timer()
    phase = "persist"
    data = dict(
        profile=profile.model_dump(),
        created=now.isoformat(),
        run_id=run_id,
        process_id=os.getpid(),
        operation="collect",
        preview=True,
        articles=[],
        source_results=[],
        source_labels=[],
        failures=[],
        metrics=dict(
            logical_model_calls=0,
            http_attempts=None,
            network_retries=None,
            tokens=None,
            source_requests=0,
            source_retries=None,
        ),
    )
    committed = False
    raw = []
    try:
        store.run(run_id, profile.context, now, "started", data)
        refs = subscriptions(profile)
        queue = deque((ref, None, 0) for ref in refs)
        states = {
            (r["kind"], r["key"]): dict(
                {k: r[k] for k in ("kind", "key", "label")},
                status="pending",
                count=0,
                batches=0,
                stop_reason=None,
            )
            for r in refs
        }
        seen = {key: set() for key in states}
        groups = {key: [] for key in states}
        while queue:
            ref, cursor, batch = queue.popleft()
            key = (ref["kind"], ref["key"])
            result = states[key]
            remaining = profile.collection.max_seconds - (timer() - started)
            if (
                data["metrics"]["source_requests"] >= profile.collection.max_requests
                or remaining <= 0
            ):
                for pending in [key] + [(r["kind"], r["key"]) for r, _, _ in queue]:
                    states[pending]["stop_reason"] = (
                        "global_request_budget" if remaining > 0 else "global_time_budget"
                    )
                    if states[pending]["batches"] == 0:
                        states[pending].update(status="failed", **diagnostic("SOURCE_BUDGET"))
                break
            phase = "collect"
            data["metrics"]["source_requests"] += 1
            try:
                with safe_model_logs():
                    response = fetcher(
                        ref,
                        account_scope=profile.account_scope,
                        cursor=cursor,
                        timeout=min(profile.source_timeout_seconds, remaining),
                    )
                items = [Article.model_validate(a).model_dump() for a in response]
            except Exception as exc:
                error = source_diagnostic(exc)
                result.update(
                    status=exc.status if isinstance(exc, SourceError) else "failed",
                    stop_reason="request_failed",
                    **error,
                )
                store.record_source_check(
                    profile.account_scope,
                    ref,
                    result["status"],
                    now,
                    profile=profile,
                    count=result["count"],
                    error_code=result.get("error_code"),
                )
                continue
            phase = "persist"
            raw_ids = getattr(response, "raw_ids", None)
            if raw_ids is None:
                raw_ids = [a.get("dynamic_id") or a["id"] for a in items]
            old_seen = seen[key].copy()
            seen[key].update(i for i in raw_ids if i)
            result["batches"] += 1
            for field, combine in (("observed_oldest_at", min), ("observed_newest_at", max)):
                stamp = getattr(
                    response, "oldest" if field == "observed_oldest_at" else "newest", None
                )
                if stamp is not None:
                    result[field] = combine(result.get(field, stamp.isoformat()), stamp.isoformat())
            if ref["kind"] in ("up_dynamics", "following_videos", "following_dynamics"):
                result["has_more"] = bool(getattr(response, "has_more", False))
            # Persist each successful batch immediately, before applying any user filter.
            normalized = []
            for a in items:
                a["fetched_at"] = now.isoformat()
                if ref["kind"] in ("up_dynamics", "following_dynamics", "following_videos"):
                    a["is_dynamic"] = True
                a["filter_hits"] = marks(a)
                normalized.append(store.canonicalize(a))
            with store.db:
                for a in normalized:
                    upsert(store.db, profile, ref, a, now)
            groups[key].extend(normalized)
            result["count"] = len({a.get("dynamic_id") or a["id"] for a in groups[key]})
            result["status"] = "ok" if result["count"] else "empty"
            store.record_source_check(
                profile.account_scope,
                ref,
                result["status"],
                now,
                profile=profile,
                count=result["count"],
                error_code=result.get("error_code"),
            )
            # Dynamic sources use the opaque cursor returned by Bilibili.
            next_cursor = (
                getattr(response, "cursor", None)
                if isinstance(response, SourceBatch)
                else (raw_ids[-1] if raw_ids else None)
            )
            if next_cursor is not None and not re.fullmatch(
                r"[A-Za-z0-9_-]{1,200}", str(next_cursor)
            ):
                next_cursor = None
            oldest = getattr(response, "newest", None)
            if oldest is None:
                from .pool import parse_time

                times = [t for a in items if (t := parse_time(a.get("published_at")))]
                oldest = max(times) if times and len(times) == len(items) else None
            reason = "single_batch"
            if ref["kind"] in ("up_dynamics", "following_videos", "following_dynamics"):
                if not raw_ids:
                    reason = "empty_batch"
                elif not getattr(response, "has_more", False):
                    reason = "end_of_feed"
                elif not next_cursor:
                    reason = "missing_cursor"
                elif str(next_cursor) == str(cursor):
                    reason = "cursor_not_advanced"
                elif not (set(i for i in raw_ids if i) - old_seen):
                    reason = "no_new_items"
                elif oldest and oldest <= now - timedelta(
                    hours=profile.collection.dynamic_ttl_hours
                ):
                    reason = "time_boundary"
                elif batch + 1 >= profile.collection.dynamic_batches:
                    reason = "batch_limit"
                else:
                    queue.append((ref, str(next_cursor), batch + 1))
                    reason = None
            result["stop_reason"] = reason
            data["source_results"] = list(states.values())
            data["source_labels"] = [r["label"] for r in data["source_results"]]
            _, partial, _ = prepare(
                [
                    ({k: states[kind][k] for k in ("kind", "key", "label")}, items)
                    for kind, items in groups.items()
                ],
                profile,
                set(),
            )
            data["articles"] = partial
            data["metrics"].update(
                collected_count=len(partial),
                candidate_count=len(partial),
                elapsed_seconds=timer() - started,
            )
            store.run(run_id, profile.context, now, "collected", data, partial)
        source_results = list(states.values())
        # Persist why a scan stopped; successful HTTP alone does not prove complete coverage.
        with store.db:
            for result in source_results:
                scan = {
                    k: result[k]
                    for k in (
                        "status",
                        "batches",
                        "count",
                        "stop_reason",
                        "has_more",
                        "observed_oldest_at",
                        "observed_newest_at",
                    )
                    if k in result
                }
                scan.update(scan_started_at=now.isoformat(), scan_finished_at=clock().isoformat())
                scan["pagination_exhausted"] = result["stop_reason"] == "end_of_feed" or (
                    result["stop_reason"] == "empty_batch" and result.get("has_more") is False
                )
                store.db.execute(
                    "INSERT OR REPLACE INTO source_scans VALUES(?,?,?,?,?)",
                    (
                        namespace(profile),
                        result["kind"],
                        result["key"],
                        now.isoformat(),
                        dump(scan),
                    ),
                )
        # Record source status independently of whether cached pool content remains usable.
        data["source_results"] = source_results
        data["source_labels"] = [r["label"] for r in source_results]
        data["failures"] = [
            r["label"] + "：" + r["error_message"] for r in source_results if r.get("error_message")
        ]
        grouped = [
            ({k: states[key][k] for k in ("kind", "key", "label")}, items)
            for key, items in groups.items()
        ]
        _, raw, counts = prepare(grouped, profile, set())
        data["articles"] = raw
        data["metrics"].update(
            counts,
            collected_count=len(raw),
            candidate_count=len(raw),
            elapsed_seconds=timer() - started,
        )
        data["empty_reason"] = (
            "all_sources_failed"
            if all(r["status"] not in ("ok", "empty") for r in source_results)
            else "sources_empty"
            if not raw
            else None
        )
        if data["empty_reason"] == "all_sources_failed":
            phase = "all_failed"
            raise RuntimeError("all sources failed")
        with store.db:
            store.db.execute(
                "INSERT OR REPLACE INTO pool_collections VALUES(?,?,?)",
                (namespace(profile), now.isoformat(), run_id),
            )
        data["status"] = "collection"
        store.run(run_id, profile.context, now, "collection", data, raw)
        committed = True
        phase = "export"
        store.export(run_id)
        return data
    except Exception as exc:
        error = diagnostic("ALL_SOURCES_FAILED") if phase == "all_failed" else classify(exc, phase)
        data.update(
            error,
            failed_stage="collect" if phase in ("collect", "all_failed") else "persist",
            error_type=safe_type(exc),
            status="collection" if committed else "failed",
            briefing_saved=False,
            export_failed=phase == "export",
            data_saved=False,
        )
        if not data["source_results"] and "states" in locals():
            data["source_results"] = list(states.values())
            data["source_labels"] = [r["label"] for r in data["source_results"]]
            data["failures"] = [
                r["label"] + "：" + r["error_message"]
                for r in data["source_results"]
                if r.get("error_message")
            ]
        data["metrics"]["elapsed_seconds"] = timer() - started
        try:
            data["data_saved"] = True
            data.update(public_diagnostic(data))
            store.run(run_id, profile.context, now, data["status"], data, raw)
        except Exception:
            data["data_saved"] = False
        safe = {
            k: data[k]
            for k in (
                "run_id",
                "status",
                "error_code",
                "error_message",
                "failed_stage",
                "error_type",
                "data_saved",
                "briefing_saved",
                "export_failed",
                "metrics",
            )
        }
        if "error_details" in data:
            safe["error_details"] = data["error_details"]
        try:
            atomic_write(store.output / run_id / "error.json", dump(safe))
            if phase == "export" and data["data_saved"]:
                atomic_write(store.output / run_id / "run.json", dump(data))
            elif data["data_saved"]:
                store.export(run_id)
        except Exception:
            pass
        raise run_failure(exc, run_id, safe) from None
