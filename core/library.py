"""Library read models, transactional settings and feedback."""

from __future__ import annotations

import json
import threading
from contextlib import closing
from pathlib import Path

from .briefing import local_now
from .covers import cover_url
from .errors import public_diagnostic, source_public
from .filtering import catalog
from .pool import snapshot
from .preferences import refresh_safe
from .schemas import Article, Profile
from .storage import Store


class RequestError(Exception):
    def __init__(self, message, status=400):
        self.status = status
        super().__init__(message)


class Library:
    def __init__(self, root):
        self.root = Path(root)
        self.profile_path = self.root / "briefing_profile.json"
        self.db_path = self.root / "workspace" / "bilibili_shixun.sqlite3"
        self.clock = local_now
        self.token = ""
        self.lock = threading.RLock()
        self.job = None

    def profile(self):
        with closing(self.store()):
            pass  # complete any durable setting intent before reading the atomic file
        return Profile.model_validate(json.loads(self.profile_path.read_text(encoding="utf-8-sig")))

    def store(self):
        # Each request/worker owns its SQLite connection.
        return Store(self.db_path)

    def state(self):
        with self.lock:
            p = self.profile()
            job = dict(self.job) if self.job else None
        with closing(self.store()) as s:
            if s.preferences(p.context)[0] is None:
                refresh_safe(s, p, self.clock())
            runs = []
            for r in s.db.execute("SELECT * FROM runs ORDER BY created DESC,rowid DESC LIMIT 100"):
                d = json.loads(r["data"])
                if d.get("profile", {}).get("account_scope") != p.account_scope:
                    continue
                runs.append(
                    dict(
                        id=r["id"],
                        created=r["created"],
                        status=r["status"],
                        current_context=r["context"] == p.context,
                        count=s.db.execute(
                            "SELECT count(*) FROM recommendations WHERE run=?", (r["id"],)
                        ).fetchone()[0],
                        candidates=d.get("metrics", {}).get(
                            "candidate_count", len(d.get("articles", []))
                        ),
                        operation=d.get("operation", "legacy"),
                        preview=d.get("preview", False),
                        keywords=d.get("profile", {}).get("keywords", []),
                        source_labels=d.get("source_labels", []),
                        **public_diagnostic(d),
                    )
                )
            version, weights = s.preferences(p.context)
            stats = s.feedback_stats(p.context)
            source_status = s.source_status(p)
            pool = snapshot(s, p, self.clock())
            collection_job = None
        return dict(
            profile=p.model_dump(),
            runs=runs,
            preferences=dict(version=version, weights=weights),
            stats=stats,
            job=job,
            token=self.token,
            source_status=source_status,
            collection_job=collection_job,
            pool={k: pool[k] for k in ("eligible_count", "last_collected_at", "filter_counts")},
            filter_rule_catalog=catalog(),
            filter_warnings=[
                dict(id=r.id, reason="rule_or_version_unavailable")
                for r in p.filters.rules
                if r.enabled
                and not any(c["id"] == r.id and c["version"] == r.version for c in catalog())
            ],
            capabilities=dict(
                sources_v1=True,
                cover_images_v1=True,
                stable_preferences_v1=True,
                sources_v2=True,
                filter_rules_v1=True,
                candidate_pool_v1=True,
            ),
        )

    def detail(self, run_id):
        p = self.profile()
        with closing(self.store()) as s:
            r = s.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not r:
                raise RequestError("找不到这份简报。", 404)
            d = json.loads(r["data"])
            if d.get("profile", {}).get("account_scope") != p.account_scope:
                raise RequestError("找不到当前账号的这份简报。", 404)
            feedback = {
                f["recommendation"]: dict(id=f["id"], value=f["value"], reason=f["reason"])
                for f in s.effective_feedback(r["context"])
            }
            picks = []
            for row in s.db.execute(
                "SELECT * FROM recommendations WHERE run=? ORDER BY position", (run_id,)
            ):
                item = json.loads(row["data"])
                item["article"] = Article.model_validate(item["article"]).model_dump()
                item.update(
                    recommendation_id=row["id"],
                    position=row["position"],
                    feedback=feedback.get(row["id"]),
                )
                picks.append(item)
            candidates = d.get("articles", [])
            if "articles" not in d:
                candidates = [
                    json.loads(x[0])
                    for x in s.db.execute(
                        "SELECT data FROM candidates WHERE run=? ORDER BY rowid", (run_id,)
                    )
                ]
            candidates = [
                dict(a, cover_url=cover_url(a.get("cover_url")), sources=a.get("sources", []))
                for a in candidates
            ]
            return dict(
                id=run_id,
                created=r["created"],
                status=r["status"],
                picks=picks,
                candidates=candidates,
                current_context=r["context"] == p.context,
                metrics=d.get("metrics", {}),
                failures=d.get("failures", []),
                report=d.get("report", ""),
                failed_stage=d.get("failed_stage"),
                error_type=d.get("error_type"),
                preference_update_error=d.get("preference_update_error"),
                source_results=[source_public(x) for x in d.get("source_results", [])],
                empty_reason=d.get("empty_reason"),
                operation=d.get("operation", "legacy"),
                keywords=d.get("profile", {}).get("keywords", []),
                briefing_saved=d.get("briefing_saved", r["status"] in ("completed", "empty")),
                export_failed=d.get("export_failed", False),
                **public_diagnostic(d),
            )

    def save_profile(self, body):
        from .core_errors import CoreError
        from .settings_service import SettingsService

        with self.lock:
            if self.job and self.job["status"] == "running":
                raise RequestError("正在生成简报，请完成后再保存设置。", 409)
            try:
                change = SettingsService(self.root, self.profile_path, self.clock).web_update(body)
                return dict(
                    changed_context=change.before.learning_context != change.after.learning_context,
                    change_id=change.change_id,
                    version=change.after.version,
                    goal_id=change.after.goal_id,
                )
            except CoreError as exc:
                message = (
                    "设置已在其他窗口修改，请刷新后重试。"
                    if exc.code == "SETTINGS_CONFLICT"
                    else exc.message
                )
                raise RequestError(
                    message,
                    409
                    if exc.code
                    in ("SETTINGS_CONFLICT", "SETTINGS_BUSY", "PREFERENCE_UNDO_CONFLICT")
                    else 400,
                ) from None

    def feedback(self, body, undo=False):
        with self.lock:
            p = self.profile()
            with closing(self.store()) as s:
                key = body.get("key")
                if not isinstance(key, str) or not 1 <= len(key) <= 100:
                    raise RequestError("缺少有效的请求编号。")
                if undo:
                    event = body.get("event")
                    if not isinstance(event, str) or not event:
                        raise RequestError("缺少有效的反馈事件编号。")
                    row = s.db.execute(
                        """SELECT f.id FROM feedback_events f
                        JOIN recommendations r ON r.id=f.recommendation JOIN runs u ON u.id=r.run
                        WHERE f.id=? AND f.value!='undo' AND u.context=?""",
                        (event, p.context),
                    ).fetchone()
                    if not row:
                        raise RequestError("反馈不属于当前兴趣设置或已不可用。", 404)
                    result = s.feedback(None, "undo", "", self.clock(), key, event)
                else:
                    rid, value, reason = (
                        body.get("recommendation"),
                        body.get("value"),
                        body.get("reason", ""),
                    )
                    if not isinstance(rid, str) or not rid:
                        raise RequestError("缺少有效的推荐编号。")
                    if (
                        value not in ("useful", "not_useful", "later")
                        or not isinstance(reason, str)
                        or len(reason) > 300
                    ):
                        raise RequestError("反馈格式无效，原因最多 300 字。")
                    row = s.db.execute(
                        """SELECT r.id FROM recommendations r JOIN runs u ON u.id=r.run
                        WHERE r.id=? AND u.context=? AND u.status='completed' """,
                        (rid, p.context),
                    ).fetchone()
                    if not row:
                        raise RequestError("该条目不属于当前兴趣设置的正式推荐。", 404)
                    result = s.feedback(rid, value, reason.strip(), self.clock(), key)
                _, _, error = refresh_safe(s, p, self.clock())
        return dict(
            event=result, warning="反馈已保存；偏好将在下次运行时重试更新。" if error else None
        )
