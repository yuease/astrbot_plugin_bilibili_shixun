"""Bounded deterministic retrieval. This module never calls a model."""

import json
import time
from contextlib import closing
from pathlib import Path

from . import agent_models as models
from .briefing import local_now
from .chat_output import attribution, compact_coverage, compact_item, localize
from .core_errors import CoreError
from .retrieval import (
    access_stamp,
    coverage,
    hash_data,
    item_view,
    resolve_range,
    retrieve,
    source_statuses,
    terms_for,
)
from .schemas import Profile
from .source_diagnostics import diagnose, query_outcome
from .storage import Store
from .temporary import prune_temporary
from .text_evidence import detail_body


class QueryService:
    def __init__(self, root, default_profile=None):
        self.root = Path(root)
        self.path = self.root / "briefing_profile.json"
        self.db_path = self.root / "workspace/bilibili_shixun.sqlite3"
        self.default_profile = default_profile

    def profile(self):
        if not self.path.exists() and self.default_profile:
            return Profile.model_validate(self.default_profile())
        return Profile.model_validate_json(self.path.read_text(encoding="utf-8"))

    def call(self, operation, params):
        profile = self.profile()
        request = dict(params, profile_id=profile.profile_id)
        if operation == "get_source_status":
            value = models.SourceStatusRequest.model_validate(request)
            with closing(Store(self.db_path, rebuild_preferences=False)) as store:
                prune_temporary(store, profile, local_now())
                statuses = source_statuses(store, profile, value.include_disabled)
            return localize(
                {
                    "sources": [
                        s.model_dump(exclude_none=True, exclude={"temporary"})
                        for s in statuses[:50]
                    ],
                    "sources_truncated": len(statuses) > 50,
                    "timezone": value.timezone,
                    "note": "ready仅表示接口采集成功；scan说明扫描范围，不能据此认定覆盖全部更新。",
                },
                value.timezone,
            )
        if operation == "get_item":
            return self.item(profile, models.GetItemRequest.model_validate(request))
        question = request.pop("question", "")
        request["content_types"] = [
            {"text_dynamic": "text", "image_dynamic": "image"}.get(kind, kind)
            for kind in request.get("content_types", [])
        ]
        request.setdefault("limit", 5)
        value = models.SearchItemsRequest.model_validate(request)
        started = time.monotonic()
        terms, groups = terms_for(question, value.terms, profile.retrieval.aliases)
        absolute = resolve_range(value.time_range, question, local_now())
        with closing(Store(self.db_path, rebuild_preferences=False)) as store:
            prune_temporary(store, profile, local_now())
            entries, missing, content = retrieve(store, profile, value, absolute, groups)
            scope = coverage(store, profile, value, absolute, local_now(), missing)
            diagnostics, scope_status, states = diagnose(
                source_statuses(store, profile, True), value.sources, absolute
            )
        snapshot = hash_data([access_stamp(profile), content, scope.model_dump(exclude={"as_of"})])
        if value.snapshot_version and value.snapshot_version != snapshot:
            raise CoreError("PAGINATION_CHANGED")
        data = dict(
            outcome=query_outcome(entries, states),
            source_scope_status=scope_status,
            requested_sources=diagnostics,
            total_matches=len(entries),
            match_scope="retained_allowed_data",
            page=value.page,
            retrieval_terms=terms,
            coverage=compact_coverage(scope),
            snapshot_version=snapshot,
            retrieval_only=True,
            relevance_review_required=True,
            guidance="只查询当前保留数据。先说明requested_sources中未满足的来源；无匹配不等于没有发生。本地has_more只表示匹配结果还有下一页，未读完可概括但不能声称逐条列全；读完也不等于远端采集完整。",
        )
        if question:
            data["question"] = question
        data = localize(data, absolute["timezone"])
        budget = (
            profile.retrieval.max_output_chars - len(json.dumps(data, ensure_ascii=False)) - 600
        )
        offset = 0
        for page in range(1, value.page + 1):
            items, used = [], 0
            while offset < len(entries) and len(items) < value.limit:
                item = localize(
                    compact_item(
                        item_view(entries[offset], groups, profile.retrieval.max_snippet_chars),
                        entries[offset],
                    ),
                    absolute["timezone"],
                )
                size = len(json.dumps(item, ensure_ascii=False)) + 1
                if used + size > budget:
                    if not items:
                        raise CoreError("OUTPUT_BUDGET")
                    break
                items.append(item)
                used += size
                offset += 1
            if offset == len(entries) and page < value.page:
                items = []
                break
        has_more = offset < len(entries)
        data.update(
            items=items,
            has_more=has_more,
            next_page=value.page + 1 if has_more else None,
            metrics={
                "logical_model_calls": 0,
                "source_requests": 0,
                "elapsed_seconds": round(time.monotonic() - started, 6),
            },
        )
        self.check_profile(profile)
        if len(json.dumps(data, ensure_ascii=False)) > profile.retrieval.max_output_chars:
            raise CoreError("OUTPUT_BUDGET")
        return data

    def check_profile(self, profile):
        if access_stamp(self.profile()) != access_stamp(profile):
            raise CoreError("QUERY_CONTEXT_CHANGED")

    def item(self, profile, value):
        query = models.SearchItemsRequest(profile_id=profile.profile_id, sources=value.sources)
        absolute = resolve_range(query.time_range, "", local_now())
        with closing(Store(self.db_path, rebuild_preferences=False)) as store:
            prune_temporary(store, profile, local_now())
            entries, _, _ = retrieve(store, profile, query, absolute, [])
        found = next((entry for entry in entries if entry["article"]["id"] == value.item_id), None)
        if found is None:
            raise CoreError("ITEM_NOT_AVAILABLE")
        limit = min(value.max_body_chars, profile.retrieval.max_body_chars)
        body, spans, truncated = detail_body(found, limit)
        item = item_view(
            found, [], min(profile.retrieval.max_snippet_chars, limit), context_chars=0
        )
        result = compact_item(item, found, detail=True)
        result.update(
            body=body[:limit],
            text_available=bool(body),
            body_truncated=truncated,
            images=found["article"].get("images", []),
            attachments=found["article"].get("attachments", []),
            images_analyzed=False,
            attributions=[
                dict(
                    body_start=span["body_start"],
                    body_end=span["body_end"],
                    **attribution(found, span),
                )
                for span in spans
            ],
            timezone=value.timezone,
        )
        result["image_count"] = len(result["images"])
        result = localize(result, value.timezone)
        self.check_profile(profile)
        if len(json.dumps(result, ensure_ascii=False)) > profile.retrieval.max_output_chars:
            raise CoreError("OUTPUT_BUDGET")
        return result
