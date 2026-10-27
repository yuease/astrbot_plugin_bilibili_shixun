"""Shared settings, anonymous search and requested-source diagnostics."""

import asyncio
import json
from contextlib import closing
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot_plugin_bilibili_shixun.core.core_errors import CoreError
from astrbot_plugin_bilibili_shixun.core.library import RequestError
from astrbot_plugin_bilibili_shixun.core.storage import Store
from astrbot_plugin_bilibili_shixun.services.application import Application
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.runtime import Runtime
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from test_smoke import LOGGER, Model, owner_event, seed, video


@pytest.fixture
def runtime(tmp_path):
    value = Runtime(tmp_path, Settings())
    value.initialize()
    return value


@pytest_asyncio.fixture
async def app(tmp_path):
    context = SimpleNamespace(
        get_config=lambda *args: {"admins_id": ["10001"]}, llm_generate=AsyncMock()
    )
    value = Application(
        tmp_path,
        context,
        Settings(),
        LOGGER,
        identity={
            "kind": "chat",
            "adapter": "aiocqhttp",
            "platform_id": "test-bot",
            "sender": "10001",
        },
    )
    await value.start()
    try:
        yield value
    finally:
        await value.close()


def test_bot_initial_settings_share_gui_version_and_do_not_start_collection(runtime):
    current = runtime.get_settings()
    assert current["version"] == runtime.state()["profile_version"]
    change = runtime.update_settings(
        {
            "expected_version": current["version"],
            "add_keywords": ["AI"],
            "interests": "关注技术进展",
            "keyword_search_enabled": True,
        },
        "first",
    )
    assert change["settings_saved"] and not change["collection_started"]
    assert change["needs_collection"] and change["learning_continues"]
    assert runtime.state()["profile"]["keywords"] == ["AI"]
    assert runtime.state()["profile_version"] == change["version"]
    assert not runtime.state()["runs"]
    replay = runtime.update_settings(
        {
            "expected_version": current["version"],
            "add_keywords": ["AI"],
            "interests": "关注技术进展",
            "keyword_search_enabled": True,
        },
        "first",
    )
    assert replay["replayed"] and replay["change_id"] == change["change_id"]


def test_incremental_keywords_interest_and_undo_preserve_gui_subscriptions(runtime):
    seed(runtime)
    runtime.run_job(
        "collect",
        "learning-collect",
        SimpleNamespace(fetch=lambda *args, **kwargs: [video_as_article()]),
        Model(),
    )
    runtime.run_job("generate", "learning-briefing", None, Model())
    detail = runtime.detail("learning-briefing")
    runtime.feedback(
        {
            "recommendation": detail["picks"][0]["recommendation_id"],
            "value": "useful",
            "key": "learning-feedback",
        }
    )
    before = runtime.get_settings()
    change = runtime.update_settings(
        {
            "expected_version": before["version"],
            "add_keywords": ["AI", "CODEX"],
            "interests": "新的同目标表述",
        },
        "bot-change",
    )
    assert change["keywords"] == ["codex", "AI"] and change["learning_continues"]
    assert runtime.get_settings()["goal_id"] == before["goal_id"]
    assert runtime.state()["stats"]["feedback_count"] == 1
    state = runtime.state()
    plan = state["profile"]["source_plan"]
    plan["ups"] = [{"uid": "777", "enabled": True, "scope": "dynamics", "content_type": "all"}]
    runtime.save_profile(
        {
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan},
            "request_id": "gui-ups",
        }
    )
    current = runtime.get_settings()
    undo_payload = {"change_id": change["change_id"], "expected_version": current["version"]}
    undone = runtime.undo_settings(undo_payload, "undo-once")
    assert (
        undone["keywords"] == ["codex"] and undone["interests"] == before["settings"]["interests"]
    )
    assert runtime.state()["profile"]["source_plan"]["ups"][0]["uid"] == "777"
    assert runtime.undo_settings(undo_payload, "undo-once")["replayed"]
    with pytest.raises(CoreError) as conflict:
        runtime.undo_settings(dict(undo_payload, expected_version="different"), "undo-once")
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


def test_bot_gui_conflicts_and_later_keyword_change_block_undo(runtime):
    seed(runtime)
    old = runtime.get_settings()
    runtime.save_profile(
        {"expected_version": old["version"], "changes": {"max_picks": 7}, "request_id": "gui-first"}
    )
    with pytest.raises(CoreError) as conflict:
        runtime.update_settings(
            {"expected_version": old["version"], "add_keywords": ["AI"]}, "stale-bot"
        )
    assert conflict.value.code == "SETTINGS_CONFLICT"
    current = runtime.get_settings()
    change = runtime.update_settings(
        {"expected_version": current["version"], "add_keywords": ["AI"]}, "bot-fresh"
    )
    with pytest.raises(RequestError) as conflict:
        runtime.save_profile(
            {
                "expected_version": current["version"],
                "changes": {"max_picks": 8},
                "request_id": "stale-gui",
            }
        )
    assert conflict.value.status == 409
    current = runtime.get_settings()
    runtime.save_profile(
        {
            "expected_version": current["version"],
            "changes": {"keywords": ["codex", "AI", "安全"]},
            "request_id": "gui-keywords",
        }
    )
    with pytest.raises(CoreError) as conflict:
        runtime.undo_settings(
            {
                "change_id": change["change_id"],
                "expected_version": runtime.get_settings()["version"],
            },
            "undo-conflict",
        )
    assert conflict.value.code == "PREFERENCE_UNDO_CONFLICT"
    assert runtime.state()["profile"]["max_picks"] == 7


def test_removing_last_keyword_only_disables_keyword_source(runtime):
    seed(runtime)
    old = runtime.get_settings()
    changed = runtime.update_settings(
        {"expected_version": old["version"], "remove_keywords": ["CODEX"]}, "remove-last"
    )
    assert changed["keywords"] == [] and not changed["keyword_search_enabled"]
    assert runtime.path.exists()
    assert runtime.get_settings()["settings"]["source_plan"]["ups"] == []


@pytest.mark.asyncio
async def test_anonymous_search_without_profile_uses_sdk_and_preserves_settings(app, monkeypatch):
    from bilibili_api import Api

    calls = []

    async def request(api, **kwargs):
        calls.append(dict(api.params))
        assert not api.credential.get_cookies().get("SESSDATA")
        assert not api.credential.get_cookies().get("bili_jct")
        return {"result": [video(i) for i in range(1, 21)], "numPages": 8, "numResults": 160}

    monkeypatch.setattr(Api, "request", request)
    app.login.credentials = lambda: (_ for _ in ()).throw(
        AssertionError("Anonymous search must not read account credentials")
    )
    original = await app.io(app.runtime.get_settings)
    result = await app.search_bilibili({"keyword": "Codex", "wait_seconds": 5}, "message")
    assert result["status"] == "completed" and result["results_available"]
    assert not result["subscription_saved"] and result["result"]["network_search_succeeded"]
    assert result["result"]["total_matches"] == 20 and len(result["result"]["items"]) == 5
    assert (
        result["result"]["metrics"]["source_requests"] == 1
        and result["result"]["metrics"]["logical_model_calls"] == 0
    )
    assert calls[0]["order"] == "pubdate" and calls[0]["page_size"] == 20
    assert (await app.io(app.runtime.get_settings))["settings"] == original["settings"]
    assert not app.runtime.path.exists() and not app.login.uid
    local = await app.tool(
        "search_items", {"sources": [{"kind": "temporary_keyword", "key": "Codex"}]}
    )
    assert local["total_matches"] == 20 and local["metrics"]["source_requests"] == 0
    assert len(calls) == 1
    replay = await app.search_bilibili({"keyword": "Codex", "wait_seconds": 5}, "message")
    assert replay["id"] == result["id"] and len(calls) == 1
    detail = await app.tool("get_item", {"item_id": result["result"]["items"][0]["id"]})
    assert detail["body"]


@pytest.mark.asyncio
async def test_anonymous_failure_and_waiting_are_not_reported_as_no_match(app):
    gate = asyncio.Event()

    async def fetch(*args, **kwargs):
        await gate.wait()
        from astrbot_plugin_bilibili_shixun.core.source_types import SourceError

        raise SourceError("SOURCE_RATE_LIMIT")

    app.source.search_public = fetch
    first = await app.search_bilibili({"keyword": "test", "wait_seconds": 0}, "pending")
    assert first["status"] == "running" and not first["results_available"]
    gate.set()
    failed = await app.search_bilibili(
        {"action": "status", "search_id": first["id"], "wait_seconds": 2}, "status"
    )
    assert failed["status"] == "failed" and failed["error_code"] == "SOURCE_RATE_LIMIT"
    assert "result" not in failed
    local = await app.tool(
        "search_items", {"sources": [{"kind": "temporary_keyword", "key": "test"}]}
    )
    assert local["outcome"] == "collection_failed"
    assert local["requested_sources"][0]["state"] == "collection_failed"
    settings = await app.tool("get_settings", {})
    assert settings["data_status"] == "no_enabled_sources"
    assert settings["temporary_source_status"][0]["availability"] == "error"
    assert "不要再用status" in failed["guidance"]
    app.source.search_public = AsyncMock(
        return_value={"items": [video_as_article()], "remote_has_more": False}
    )
    await app.search_bilibili({"keyword": "test"}, "succeeded")
    app.source.search_public = fetch
    await app.search_bilibili({"keyword": "test"}, "failed-after-success")
    retained = await app.tool(
        "search_items", {"sources": [{"kind": "temporary_keyword", "key": "test"}]}
    )
    assert retained["items"]
    assert retained["requested_sources"][0]["state"] == "failed_with_retained"
    settings = await app.tool("get_settings", {})
    assert settings["data_status"] == "no_enabled_sources"
    assert not settings["has_retained_results"]
    assert settings["temporary_source_status"][0]["retained_count"] > 0


@pytest.mark.asyncio
async def test_temporary_budget_expiry_and_no_keyword_side_effect(app):
    async def fetch(*args, **kwargs):
        return {"items": [video_as_article()], "remote_has_more": True, "reported_total": 100}

    app.source.search_public = AsyncMock(side_effect=fetch)
    for page in range(1, 4):
        first = await app.search_bilibili({"keyword": "topic", "remote_page": page}, "same-message")
        assert first["status"] == "completed"
    with pytest.raises(PluginError) as limited:
        await app.search_bilibili({"keyword": "different"}, "same-message")
    assert limited.value.code == "TEMP_SEARCH_LIMIT"
    replay = await app.search_bilibili({"keyword": "topic", "remote_page": 1}, "same-message")
    assert replay["status"] == "completed" and app.source.search_public.await_count == 3
    job = await app.io(app.jobs.get, "temporary_search", first["id"], "")
    from astrbot_plugin_bilibili_shixun.core.briefing import local_now

    job["started"] = (local_now() - timedelta(hours=25)).isoformat()
    await app.io(app.jobs.finish, job)
    with pytest.raises(PluginError) as expired:
        await app.search_bilibili({"action": "status", "search_id": first["id"]}, "status")
    assert expired.value.code == "TEMP_SEARCH_EXPIRED"
    assert (await app.io(app.runtime.get_settings))["settings"]["keywords"] == []


def test_requested_source_diagnostics_never_silently_drop_missing_selector(runtime):
    seed(runtime)
    missing = runtime.tool(
        "search_items", {"sources": [{"kind": "temporary_keyword", "key": "希罗"}]}
    )
    assert missing["outcome"] == "source_unavailable"
    assert missing["requested_sources"][0]["state"] == "temporary_search_required"
    known = runtime.tool("search_items", {"sources": [{"kind": "keyword_search", "key": "codex"}]})
    assert known["outcome"] == "not_collected"
    runtime.run_job(
        "collect",
        "collect",
        SimpleNamespace(fetch=lambda *args, **kwargs: [video_as_article()]),
        Model(),
    )
    result = runtime.tool(
        "search_items",
        {
            "sources": [
                {"kind": "keyword_search", "key": "codex"},
                {"kind": "keyword_search", "key": "missing"},
            ]
        },
    )
    assert result["outcome"] == "found" and result["source_scope_status"] == "partial"
    assert (
        len(result["requested_sources"]) == 2
        and result["requested_sources"][1]["state"] == "not_configured"
    )
    assert runtime.tool("search_items", {"terms": ["不会命中"]})["outcome"] == "no_match"
    partial_empty = runtime.tool(
        "search_items",
        {
            "terms": ["不会命中"],
            "sources": [
                {"kind": "keyword_search", "key": "codex"},
                {"kind": "temporary_keyword", "key": "not-searched"},
            ],
        },
    )
    assert partial_empty["outcome"] == "partial_scope_no_match"
    wrong_key = runtime.tool(
        "search_items", {"sources": [{"kind": "keyword_search", "key": "other"}]}
    )
    assert wrong_key["outcome"] == "source_not_configured" and not wrong_key["items"]


def video_as_article(index=1):
    from astrbot_plugin_bilibili_shixun.core.bilibili_items import video_batch

    return video_batch([video(index)])[0]


def test_disabled_empty_failed_old_snapshot_and_local_pagination_are_distinct(runtime):
    seed(runtime)
    state = runtime.state()
    plan = state["profile"]["source_plan"]
    plan["ups"] = [{"uid": "777", "enabled": False, "scope": "uploads", "content_type": "all"}]
    runtime.save_profile(
        {
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan},
            "request_id": "disabled-up",
        }
    )
    disabled = runtime.tool("search_items", {"sources": [{"kind": "up_uploads", "key": "777"}]})
    assert disabled["outcome"] == "source_disabled"
    runtime.run_job("collect", "empty", SimpleNamespace(fetch=lambda *args, **kwargs: []), Model())
    empty = runtime.tool("search_items", {"sources": [{"kind": "keyword_search", "key": "codex"}]})
    assert (
        empty["outcome"] == "no_match"
        and empty["requested_sources"][0]["state"] == "collected_empty"
    )
    runtime.run_job(
        "collect",
        "full",
        SimpleNamespace(fetch=lambda *args, **kwargs: [video_as_article(i) for i in range(1, 23)]),
        Model(),
    )
    first = runtime.tool("search_items", {"limit": 10})
    second = runtime.tool(
        "search_items", {"limit": 10, "page": 2, "snapshot_version": first["snapshot_version"]}
    )
    third = runtime.tool(
        "search_items", {"limit": 10, "page": 3, "snapshot_version": first["snapshot_version"]}
    )
    assert [len(r["items"]) for r in (first, second, third)] == [10, 10, 2]
    assert second["next_page"] == 3 and not third["has_more"]
    from astrbot_plugin_bilibili_shixun.core.briefing import local_now

    with closing(Store(runtime.library.db_path)) as store:
        with store.db:
            store.db.execute(
                "UPDATE source_observations SET status='failed',error_code='SOURCE_HTTP' WHERE kind='keyword_search'"
            )
    failed = runtime.tool("search_items", {"terms": ["不存在"]})
    assert failed["outcome"] == "collection_failed"
    assert failed["requested_sources"][0]["matched_sources"][0]["state"] == "failed_with_retained"
    with closing(Store(runtime.library.db_path)) as store:
        with store.db:
            store.db.execute(
                "UPDATE source_observations SET status='ok',error_code=NULL,last_success=?",
                ((local_now() - timedelta(days=2)).isoformat(),),
            )
    stale = runtime.tool("search_items", {"time_range": {"period": "today"}})
    assert stale["source_scope_status"] == "unavailable"


@pytest.mark.asyncio
async def test_native_tools_authorize_before_settings_or_anonymous_search(tmp_path, monkeypatch):
    from astrbot.core.agent.tool import ToolSet
    from astrbot.core.star.context import Context
    from astrbot_plugin_bilibili_shixun import main

    context = object.__new__(Context)
    context._config = {"admins_id": ["10001"]}
    context.astrbot_config_mgr = SimpleNamespace(get_conf=lambda umo: context._config)
    context.provider_manager = SimpleNamespace(llm_tools=ToolSet())
    context.registered_web_apis = []
    monkeypatch.setattr(main, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    plugin = main.BilibiliShixunPlugin(context, {})
    event = owner_event()
    event.is_private_chat = lambda: True
    event.get_group_id = lambda: ""
    event.is_admin = lambda: True
    event.message_obj = SimpleNamespace(message_id="settings-message")
    try:
        await plugin.initialize()
        get = json.loads(await plugin.call_tool(event, "get_settings", {}))
        response = json.loads(
            await plugin.call_tool(
                event,
                "update_settings",
                {
                    "expected_version": get["version"],
                    "add_keywords": ["AI"],
                    "keyword_search_enabled": True,
                },
            )
        )
        assert response["settings_saved"] and not response["collection_started"]
        changed_interest = json.loads(
            await plugin.call_tool(
                event,
                "update_settings",
                {
                    "expected_version": response["version"],
                    "interests": "同一消息继续完善兴趣",
                },
            )
        )
        assert changed_interest["settings_saved"] and changed_interest["learning_continues"]
        event.is_private_chat = lambda: False
        application = await plugin.users.chat(event)
        application.source.search_public = AsyncMock()
        denied = json.loads(await plugin.call_tool(event, "search_bilibili", {"keyword": "test"}))
        assert denied["error_code"] == "ACCESS_DENIED"
        assert application.source.search_public.await_count == 0
        event.is_private_chat = lambda: True
        unsupported = json.loads(
            await plugin.call_tool(event, "search_items", {"sources": [{"kind": "rss"}]})
        )
        assert unsupported["error_code"] == "SOURCE_TYPE_UNSUPPORTED"
    finally:
        await plugin.terminate()


def test_bot_can_undo_gui_change_without_overwriting_later_unrelated_interest(runtime):
    seed(runtime)
    initial = runtime.state()
    changed = runtime.save_profile(
        {
            "expected_version": initial["profile_version"],
            "changes": {"max_picks": 8},
            "request_id": "gui-change",
        }
    )
    runtime.update_settings(
        {"expected_version": changed["version"], "interests": "保留这次兴趣修改"}, "bot-interest"
    )
    result = runtime.undo_settings(
        {"change_id": changed["change_id"], "expected_version": runtime.get_settings()["version"]},
        "undo-gui",
    )
    assert (
        result["settings_saved"]
        and runtime.state()["profile"]["max_picks"] == initial["profile"]["max_picks"]
    )
    assert runtime.get_settings()["settings"]["interests"] == "保留这次兴趣修改"


def test_undo_up_source_change_preserves_later_keyword_toggle(runtime):
    seed(runtime)
    initial = runtime.state()
    plan = initial["profile"]["source_plan"]
    plan["ups"] = [{"uid": "777", "enabled": True, "scope": "uploads", "content_type": "all"}]
    changed = runtime.save_profile(
        {
            "expected_version": initial["profile_version"],
            "changes": {"source_plan": plan},
            "request_id": "gui-up-plan",
        }
    )
    later = runtime.update_settings(
        {"expected_version": changed["version"], "keyword_search_enabled": False},
        "disable-keyword",
    )
    runtime.undo_settings(
        {"change_id": changed["change_id"], "expected_version": later["version"]}, "undo-ups"
    )
    current = runtime.get_settings()["settings"]["source_plan"]
    assert current["ups"] == [] and not current["keyword_search"]["enabled"]


def test_legacy_prepared_settings_intent_recovers_with_new_version_format(runtime, monkeypatch):
    from astrbot_plugin_bilibili_shixun.core import settings_service as settings_module

    seed(runtime)
    original_write = settings_module.atomic_write

    def failed_write(*args, **kwargs):
        raise OSError("simulated interruption")

    monkeypatch.setattr(settings_module, "atomic_write", failed_write)
    with pytest.raises(CoreError):
        runtime.update_settings(
            {"expected_version": runtime.get_settings()["version"], "interests": "待恢复的兴趣"},
            "prepared",
        )
    import sqlite3

    with sqlite3.connect(runtime.library.db_path) as db:
        row = db.execute(
            "SELECT id,before_text,after_text FROM setting_changes WHERE status='prepared'"
        ).fetchone()
        db.execute(
            "UPDATE setting_changes SET before_version=?,after_version=? WHERE id=?",
            (settings_module.digest(row[1]), settings_module.digest(row[2]), row[0]),
        )
    monkeypatch.setattr(settings_module, "atomic_write", original_write)
    current = runtime.get_settings()
    assert current["settings"]["interests"] == "待恢复的兴趣"
    assert current["version"] == runtime.state()["profile_version"]


@pytest.mark.asyncio
async def test_temporary_search_wait_cancellation_and_account_isolation(app):
    gate, entered = asyncio.Event(), asyncio.Event()

    async def search(*args, **kwargs):
        entered.set()
        await gate.wait()
        return {"items": [video_as_article()], "remote_has_more": None, "reported_total": None}

    app.source.search_public = search
    task = asyncio.create_task(
        app.search_bilibili({"keyword": "topic", "wait_seconds": 20}, "cancelled-client")
    )
    await asyncio.wait_for(entered.wait(), 2)
    identity = app.active
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    gate.set()
    done = await app.search_bilibili(
        {"action": "status", "search_id": identity, "wait_seconds": 2}, "status"
    )
    assert done["status"] == "completed"
    await app.install_account("1002")
    with pytest.raises(PluginError) as denied:
        await app.search_bilibili({"action": "status", "search_id": identity}, "new-account")
    assert denied.value.code == "REQUEST_CONFLICT"
    assert not (
        await app.tool("search_items", {"sources": [{"kind": "temporary_keyword", "key": "topic"}]})
    )["items"]


@pytest.mark.asyncio
async def test_expired_temporary_evidence_and_payload_are_pruned(app):
    import sqlite3

    from astrbot_plugin_bilibili_shixun.core.briefing import local_now

    app.source.search_public = AsyncMock(
        return_value={
            "items": [video_as_article()],
            "remote_has_more": None,
            "reported_total": None,
        }
    )
    result = await app.search_bilibili({"keyword": "expire"}, "expiry-message")
    old = local_now() - timedelta(hours=25)
    with sqlite3.connect(app.jobs.path) as db:
        db.execute("UPDATE jobs SET created=? WHERE id=?", (old.timestamp(), result["id"]))
    pruned = await app.io(app.jobs.get, "temporary_search", result["id"], "")
    assert pruned["results_expired"] and "result" not in pruned and "params" not in pruned
    with closing(Store(app.runtime.library.db_path)) as store:
        with store.db:
            store.db.execute("UPDATE temporary_items SET last_seen=?", (old.isoformat(),))
            store.db.execute(
                "UPDATE source_observations SET checked=? WHERE kind='temporary_keyword'",
                (old.isoformat(),),
            )
    local = await app.tool(
        "search_items", {"sources": [{"kind": "temporary_keyword", "key": "expire"}]}
    )
    assert not local["items"] and local["outcome"] == "source_unavailable"
    assert app.source.search_public.await_count == 1


@pytest.mark.asyncio
async def test_only_explicit_permanent_keyword_addition_promotes_temporary_evidence(app):
    app.source.search_public = AsyncMock(
        return_value={
            "items": [video_as_article()],
            "remote_has_more": None,
            "reported_total": None,
        }
    )
    await app.search_bilibili({"keyword": "topic"}, "temporary-only")
    settings = await app.tool("get_settings", {})
    assert settings["settings"]["keywords"] == []
    saved = await app.change_settings(
        "update_settings",
        {
            "expected_version": settings["version"],
            "add_keywords": ["topic"],
            "keyword_search_enabled": True,
        },
        "explicit-add",
    )
    assert saved["cached_items_promoted"] == 1 and saved["data_ready_for_added_keywords"]
    assert not saved["collection_started"]
    local = await app.tool(
        "search_items", {"sources": [{"kind": "keyword_search", "key": "topic"}]}
    )
    assert local["items"] and app.source.search_public.await_count == 1


@pytest.mark.asyncio
async def test_expired_temporary_evidence_is_not_promoted_by_settings_change(app):
    from astrbot_plugin_bilibili_shixun.core.briefing import local_now

    app.source.search_public = AsyncMock(
        return_value={"items": [video_as_article()], "remote_has_more": False}
    )
    await app.search_bilibili({"keyword": "expired-topic"}, "old-search")
    with closing(Store(app.runtime.library.db_path)) as store:
        with store.db:
            store.db.execute(
                "UPDATE temporary_items SET last_seen=?",
                ((local_now() - timedelta(hours=25)).isoformat(),),
            )
    # Do not read settings/query first: promotion must enforce expiry on its own.
    saved = await app.change_settings(
        "update_settings",
        {
            "expected_version": (await app.io(app.runtime.state))["profile_version"],
            "add_keywords": ["expired-topic"],
            "keyword_search_enabled": True,
        },
        "explicit-add-expired",
    )
    assert saved["cached_items_promoted"] == 0 and saved["needs_collection"]


@pytest.mark.asyncio
async def test_permanent_keyword_only_refresh_is_anonymous_but_private_sources_are_not(
    app, monkeypatch
):
    from bilibili_api import Api

    async def request(api, **kwargs):
        assert not api.credential.get_cookies().get("SESSDATA")
        return {"result": [video()]}

    monkeypatch.setattr(Api, "request", request)
    current = await app.tool("get_settings", {})
    await app.change_settings(
        "update_settings",
        {
            "expected_version": current["version"],
            "add_keywords": ["topic"],
            "keyword_search_enabled": True,
        },
        "public-keyword",
    )
    done = await app.refresh_sources("public-refresh", wait_seconds=3)
    assert done["status"] == "completed" and not app.login.uid
    state = await app.io(app.runtime.state)
    plan = state["profile"]["source_plan"]
    plan["following"].update(enabled=True, uid="1001")
    await app.web(
        "profile",
        {
            "body": {
                "expected_version": state["profile_version"],
                "changes": {"source_plan": plan},
                "request_id": "private-following",
            }
        },
    )
    with pytest.raises(PluginError) as missing_login:
        await app.refresh_sources("must-login", wait_seconds=0)
    assert missing_login.value.code == "BILI_LOGIN_REQUIRED"


@pytest.mark.asyncio
@pytest.mark.parametrize("permanent", [False, True])
async def test_observed_zero_hit_envelope_is_successful_empty_collection(
    app, monkeypatch, permanent
):
    from bilibili_api import Api

    async def request(api, **kwargs):
        return {"page": 1, "pagesize": 20, "numResults": 0, "numPages": 0, "in_black_key": 0}

    monkeypatch.setattr(Api, "request", request)
    if permanent:
        current = await app.tool("get_settings", {})
        await app.change_settings(
            "update_settings",
            {
                "expected_version": current["version"],
                "add_keywords": ["missing"],
                "keyword_search_enabled": True,
            },
            "empty-subscription",
        )
        result = await app.refresh_sources("empty-refresh", wait_seconds=3)
        assert result["status"] == "completed"
        assert (await app.tool("get_settings", {}))["data_status"] == "empty"
    else:
        result = await app.search_bilibili({"keyword": "missing"}, "empty-search")
        assert result["status"] == "completed"
        assert result["result"]["outcome"] == "no_match"
        assert result["result"]["total_matches"] == 0
        assert not result["subscription_saved"]
        assert result["result"]["network_search_succeeded"]


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"numResults": 0},
        {"numResults": False, "numPages": 0, "in_black_key": 0},
        {"numResults": 1, "numPages": 1},
        {"numResults": 0, "numPages": 0, "in_black_key": 1},
        {"numResults": 0, "numPages": 0, "in_black_key": 0, "result": None},
        {"numResults": 0, "numPages": 0, "in_black_key": 0, "code": -412},
    ],
)
def test_unknown_or_blocked_search_envelopes_are_not_silenced(data):
    from astrbot_plugin_bilibili_shixun.core.source_types import SourceError
    from astrbot_plugin_bilibili_shixun.services.bilibili_source import search_results

    with pytest.raises(SourceError):
        search_results(data)


def test_last_three_days_is_exact_72_hours_including_dst():
    from datetime import datetime, timezone

    from astrbot_plugin_bilibili_shixun.core.agent_models import TimeRange
    from astrbot_plugin_bilibili_shixun.core.retrieval import resolve_range

    for now in [
        datetime(2026, 10, 27, 8, 5, tzinfo=timezone.utc),
        datetime(2026, 11, 2, 8, tzinfo=timezone.utc),
    ]:
        for zone in ["Asia/Shanghai", "America/New_York"]:
            bounds = resolve_range(TimeRange(period="last_3_days", timezone=zone), "", now)
            end = datetime.fromisoformat(bounds["end"])
            start = datetime.fromisoformat(bounds["start"])
            assert end == now
            assert end - start == timedelta(hours=72)
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TimeRange(period="last_3_days", end="2026-10-27")


@pytest.mark.asyncio
async def test_temporary_failure_does_not_poison_long_term_status(app):
    from astrbot_plugin_bilibili_shixun.core.source_types import SourceError

    current = await app.tool("get_settings", {})
    await app.change_settings(
        "update_settings",
        {
            "expected_version": current["version"],
            "add_keywords": ["permanent"],
            "keyword_search_enabled": True,
        },
        "long-term-source",
    )
    app.source.fetch = lambda *args, **kwargs: [video_as_article()]
    assert (await app.refresh_sources("long-term-refresh", wait_seconds=3))["status"] == "completed"
    before = await app.tool("get_settings", {})
    app.source.search_public = AsyncMock(side_effect=SourceError("SOURCE_PARSE"))
    failed = await app.search_bilibili({"keyword": "bad"}, "bad-search")
    after = await app.tool("get_settings", {})
    assert failed["status"] == "failed"
    assert after["data_status"] == before["data_status"] == "available"
    assert after["collection_status"] == before["collection_status"]
    assert after["temporary_source_status"][0]["availability"] == "error"
    assert after["data_status_scope"] == "enabled_long_term_sources"


def test_public_tools_have_distinct_names_and_guidance_preserves_content():
    import re

    from astrbot_plugin_bilibili_shixun.services.contracts import (
        TOOL_NAMES,
        public_description,
        public_guidance,
        tool_specs,
    )

    assert len(set(TOOL_NAMES.values())) == len(tool_specs()) == 9
    for operation, _, description in tool_specs():
        assert re.fullmatch(r"[a-z_]{1,64}", TOOL_NAMES[operation])
        exposed = public_description(description)
        assert not re.search(
            r"(?<![a-z_])(?:search_items|get_item|get_settings|search_bilibili|get_source_status)(?![a-z_])",
            exposed,
        )
    raw = {
        "title": "get_item",
        "guidance": "先get_item，再search_items",
        "nested": {"guidance": "search_bilibili", "body": "search_bilibili"},
    }
    result = public_guidance(raw)
    assert result["title"] == "get_item"
    assert result["guidance"] == "先bilibili_read_content_detail，再bilibili_search_saved_content"
    assert result["nested"]["body"] == "search_bilibili"
    assert result["nested"]["guidance"] == "bilibili_search_videos_online"
