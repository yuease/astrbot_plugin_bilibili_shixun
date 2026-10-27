"""Synchronous domain boundary. All methods run away from AstrBot's event loop."""

import json
import threading
from contextlib import closing
from pathlib import Path

from ..core.agent_models import UndoPreferenceChangeRequest, UpdatePreferencesRequest
from ..core.briefing import local_now, run_briefing
from ..core.collection import collect
from ..core.core_errors import CoreError
from ..core.filtering import catalog
from ..core.library import Library, RequestError
from ..core.query import QueryService
from ..core.retrieval import hash_data
from ..core.schemas import Profile
from ..core.settings_service import SettingsService, goal_id, scope, version
from ..core.storage import Store, atomic_write, dump
from ..core.temporary import record_failure, save_temporary
from .errors import PluginError
from .settings import PROFILE_ID


class Runtime:
    def __init__(self, root, settings):
        self.root, self.settings = Path(root), settings
        self.path = self.root / "briefing_profile.json"
        self.library = Library(root)
        self.query = QueryService(root, self.draft)
        self.lock = threading.RLock()
        self.account_scope = ""

    def initialize(self):
        self.root.mkdir(parents=True, exist_ok=True)
        receipt_path = self.root / "initialization.json"
        if not self.path.exists() and receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            profile = Profile.model_validate(receipt["profile"])
            atomic_write(self.path, dump(profile.model_dump()))

    def draft(self):
        profile = Profile.model_construct(
            account_scope=self.account_scope,
            profile_id=PROFILE_ID,
            interests="",
            keywords=[],
            source_plan=None,
        ).model_dump()
        profile["source_plan"] = {
            "keyword_search": {"enabled": False},
            "ups": [],
            "following": {
                "uid": self.account_scope,
                "enabled": False,
                "scope": "videos",
                "content_type": "all",
            },
        }
        profile["sources"] = {
            "keyword_search": {"enabled": False},
            "up_uploads": {"enabled": False, "uids": []},
            "following_videos": {"enabled": False, "uid": self.account_scope},
        }
        return Profile.model_validate(profile).model_dump()

    def bind_account(self, uid):
        with self.lock:
            self.account_scope = uid
            if self.path.exists():
                profile = self.library.profile()
                data = profile.model_dump()
                data["account_scope"] = uid
                if uid:
                    data["source_plan"]["following"]["uid"] = uid
                atomic_write(self.path, dump(Profile.model_validate(data).model_dump()))

    def require_profile(self):
        if not self.path.exists():
            raise PluginError("NOT_CONFIGURED")
        return self.library.profile()

    def current_profile(self):
        return (
            self.library.profile() if self.path.exists() else Profile.model_validate(self.draft())
        )

    def settings_service(self):
        return SettingsService(self.root, default_profile=self.draft)

    def get_settings(self):
        with self.lock:
            service = self.settings_service()
            with closing(Store(self.library.db_path, rebuild_preferences=False)) as store:
                profile = service.load()
                namespace = scope(self.path, profile.profile_id, profile.account_scope)
                changes = [
                    {
                        "change_id": r["id"],
                        "changed_fields": json.loads(r["fields"]),
                        "created": r["created"],
                        "undo_of": r["undo_of"],
                    }
                    for r in store.db.execute(
                        "SELECT * FROM setting_changes WHERE namespace=? AND status='committed' ORDER BY rowid DESC LIMIT 5",
                        (namespace,),
                    )
                ]
            settings = {
                k: profile.model_dump()[k]
                for k in (
                    "keywords",
                    "interests",
                    "source_plan",
                    "filters",
                    "collection",
                    "max_picks",
                    "max_duration_seconds",
                    "exploration_ratio",
                    "history_cooldown_days",
                    "snooze_days",
                )
            }
            settings["learning_enabled"] = profile.learning.enabled
            return {
                "version": version(profile),
                "configured": self.path.exists(),
                "settings": settings,
                "goal_id": goal_id(profile),
                "recent_changes": changes,
                "source_status": self.query.call("get_source_status", {"include_disabled": True})[
                    "sources"
                ],
                "guidance": "这里是与GUI共用的当前设置；保存不等于已采集，修改前使用此version，采集需单独调用刷新。",
            }

    def update_settings(self, params, request_id):
        with self.lock:
            value = dict(
                params, profile_id=self.current_profile().profile_id, request_id=request_id
            )
            if "interests" in value:
                value["interest_mode"] = "same_goal"
            change = self.settings_service().update(UpdatePreferencesRequest.model_validate(value))
            return self.setting_receipt(change)

    def undo_settings(self, params, request_id):
        with self.lock:
            change = self.settings_service().undo(
                UndoPreferenceChangeRequest.model_validate(
                    dict(
                        params, request_id=request_id, profile_id=self.current_profile().profile_id
                    )
                )
            )
            return self.setting_receipt(change)

    def setting_receipt(self, change):
        return {
            "settings_saved": True,
            "change_id": change.change_id,
            "replayed": change.replayed,
            "version": change.after.version,
            "changed_fields": change.changed_fields,
            "keywords": change.after.keywords,
            "interests": change.after.interests,
            "keyword_search_enabled": change.after.keyword_search_enabled,
            "learning_continues": change.before.learning_context == change.after.learning_context,
            "collection_started": False,
            "needs_collection": change.needs_collection,
            "cached_items_promoted": change.promoted_items,
            "data_ready_for_added_keywords": change.data_ready
            if set(change.after.keywords) - set(change.before.keywords)
            else None,
            "next_action": change.next_action,
            "guidance": "设置变更已保存，未自动采集。请刷新设置查看当前版本和来源状态；需要新内容时单独刷新来源。",
        }

    def state(self):
        with self.lock:
            if self.path.exists():
                data = self.library.state()
                message = "已配置，尚未采集。" if data["pool"]["last_collected_at"] is None else ""
            else:
                data = {
                    "profile": self.draft(),
                    "runs": [],
                    "preferences": {"version": None, "weights": {}},
                    "stats": {
                        "recommendations": 0,
                        "feedback_count": 0,
                        "evaluated": 0,
                        "coverage": None,
                        "useful_rate": None,
                    },
                    "job": None,
                    "collection_job": None,
                    "source_status": [],
                    "pool": {"eligible_count": 0, "last_collected_at": None, "filter_counts": {}},
                    "filter_rule_catalog": catalog(),
                    "filter_warnings": [],
                    "capabilities": dict.fromkeys(
                        (
                            "sources_v1",
                            "stable_preferences_v1",
                            "sources_v2",
                            "filter_rules_v1",
                            "candidate_pool_v1",
                            "cover_images_v1",
                        ),
                        True,
                    ),
                }
                message = "尚未配置来源；保存来源后刷新采集。聊天查询不额外调用筛选模型。"
            data.pop("token", None)
            data["profile_version"] = hash_data(data["profile"])
            data["initialization"] = {"configured": self.path.exists(), "message": message}
            data["capabilities"]["retrieval_only_chat"] = True
            return data

    def save_profile(self, body):
        allowed = {"expected_version", "changes", "request_id", "interest_mode"}
        if (
            not isinstance(body, dict)
            or set(body) - allowed
            or not isinstance(body.get("changes"), dict)
        ):
            raise PluginError("INVALID_ARGUMENT")
        with self.lock:
            receipt_path = self.root / "initialization.json"
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt["request_id"] == body["request_id"]:
                    if receipt["fingerprint"] != hash_data(body):
                        raise PluginError("REQUEST_CONFLICT", status=409)
                    if not self.path.exists():
                        atomic_write(self.path, dump(receipt["profile"]))
                    return {"changed_context": False, "initialized": True, "replayed": True}
            try:
                change = self.settings_service().web_update(body)
            except CoreError as exc:
                raise RequestError(
                    exc.message,
                    409
                    if exc.code in ("SETTINGS_CONFLICT", "SETTINGS_BUSY", "IDEMPOTENCY_CONFLICT")
                    else 400,
                ) from None
            return dict(
                self.setting_receipt(change),
                changed_context=not (
                    change.before.learning_context == change.after.learning_context
                ),
            )

    def tool(self, operation, params):
        return self.query.call(operation, params)

    def temporary_result(self, job, response):
        from datetime import datetime

        from ..core.agent_models import TimeRange
        from ..core.retrieval import resolve_range

        profile = self.current_profile()
        with closing(Store(self.library.db_path, rebuild_preferences=False)) as store:
            count = save_temporary(store, profile, job, response, local_now())
        params = job["params"]
        absolute = resolve_range(
            TimeRange.model_validate(params["time_range"]),
            "",
            datetime.fromisoformat(job["started"]),
        )
        result = self.query.call(
            "search_items",
            {
                "sources": [{"kind": "temporary_keyword", "key": params["keyword"]}],
                "temporary_query_id": job["id"],
                "time_range": absolute,
                "limit": params["limit"],
            },
        )
        result.update(
            network_search_succeeded=True,
            subscription_saved=False,
            keyword=params["keyword"],
            remote_page=params["remote_page"],
            fetched_count=count,
            remote_has_more=response.get("remote_has_more"),
            reported_remote_total=response.get("reported_total"),
            result_scope="this_search_page",
            retained_ttl_hours=24,
        )
        result["metrics"].update(source_requests=1, actual_http_requests=None)
        result["guidance"] = (
            "本次匿名联网搜索成功，未写入长期订阅；只读取指定远端页，关键词命中不代表已确认主题相关。has_more仅表示本页保留匹配尚未展示完，可用search_items查询temporary_keyword的保留结果；查更多远端页需显式再次search_bilibili。"
        )
        while (
            len(json.dumps(result, ensure_ascii=False)) > profile.retrieval.max_output_chars
            and result["items"]
        ):
            result["items"].pop()
            result.update(has_more=True, next_page=2)
        if not result["items"] and result["total_matches"]:
            raise CoreError("OUTPUT_BUDGET")
        return result

    def temporary_failure(self, job, error_code):
        with closing(Store(self.library.db_path, rebuild_preferences=False)) as store:
            record_failure(store, self.current_profile(), job, error_code, local_now())

    def feedback(self, body, undo=False):
        self.require_profile()
        return self.library.feedback(body, undo=undo)

    def detail(self, run_id, download=False):
        self.require_profile()
        data = self.library.detail(run_id)
        if not download:
            return data
        if data["status"] not in ("completed", "empty") or not data["report"]:
            raise PluginError("NOT_FOUND", status=404)
        return {"report": data["report"], "filename": f"briefing-{run_id}.md"}

    def run_job(self, operation, run_id, source, model):
        profile = self.require_profile()
        with closing(Store(self.library.db_path)) as store:
            if operation == "collect":
                return collect(
                    profile.model_dump(),
                    store=store,
                    clock=local_now,
                    run_id=run_id,
                    fetcher=source.fetch,
                )
            return run_briefing(
                profile.model_dump(),
                store=store,
                clock=local_now,
                run_id=run_id,
                model=model,
            )
