"""Automatic context follows settled collections, trusted identity and current policy."""

import asyncio
import json
from contextlib import closing
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from astrbot.api.provider import ProviderRequest
from astrbot_plugin_bilibili_shixun.core.bilibili_items import video_batch
from astrbot_plugin_bilibili_shixun.core.source_types import SourceError
from astrbot_plugin_bilibili_shixun.core.storage import Store
from astrbot_plugin_bilibili_shixun.services.collection_context import END, START
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from astrbot_plugin_bilibili_shixun.services.users import UserApplications
from test_smoke import LOGGER, seed, video
from test_users import event
from test_users import plugin as shared_plugin

plugin = shared_plugin


def payload(req):
    block = req.system_prompt.split(START + "\n", 1)[1].split("\n" + END, 1)[0]
    return json.loads(block.split("\n", 1)[1])


async def collected(plugin, items=None):
    app = await plugin.users.chat(event("10001"))
    await app.io(seed, app.runtime)
    app.login.state.update(status="valid", uid="1001")
    app.source.fetch = lambda *args, **kwargs: video_batch(items or [video()])
    job = await app.refresh_sources("context-first", wait_seconds=10)
    assert job["status"] == "completed"
    return app


@pytest.mark.asyncio
async def test_collected_context_is_bounded_idempotent_and_preserves_request(plugin):
    app = await collected(plugin)
    req = ProviderRequest(system_prompt="人格设定", prompt="聊聊最近采集的内容", contexts=[])
    await plugin.inject_collection_context(event("10001"), req)
    data = payload(req)
    assert data["available_count"] == data["shown_count"] == 1
    assert data["items"][0]["title"] == "Codex 原文"
    assert data["items"][0]["snippet"] == "作者介绍 Codex 的使用方法"
    assert data["items"][0]["url"].startswith("https://www.bilibili.com/")
    assert data["warnings"] and data["data_completeness"] == "unknown"
    assert req.system_prompt.startswith("人格设定") and len(req.system_prompt) < 6010
    assert req.contexts == [] and req.prompt == "聊聊最近采集的内容"
    before = req.system_prompt
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == before
    another = ProviderRequest(system_prompt="人格设定")
    await plugin.inject_collection_context(event("10001"), another)
    assert another.system_prompt == "人格设定"
    assert app.context.llm_generate.await_count == 0


@pytest.mark.asyncio
async def test_only_latest_batch_is_injected_and_current_source_switch_applies(plugin):
    app = await collected(plugin)
    app.source.fetch = lambda *args, **kwargs: video_batch([video(), video(2)])
    done = await app.refresh_sources("context-second", wait_seconds=10)
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert payload(req)["run_id"] == done["run_id"]
    assert len(payload(req)["items"]) == 1
    assert payload(req)["items"][0]["url"].endswith("BV1Smoke0002")
    settings = await app.tool("get_settings", {})
    await app.change_settings(
        "set_subscription_enabled",
        {"source": "keyword_search", "enabled": False, "expected_version": settings["version"]},
        "disable-context-source",
    )
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == ""


@pytest.mark.asyncio
async def test_other_users_groups_and_unknown_chats_cannot_receive_context(plugin):
    await collected(plugin)
    await plugin.users.chat(event("20002"))
    count = len(plugin.users.apps)
    group = event("10001")
    group.is_private_chat = lambda: False
    for who in (event("20002"), event("30003"), event("10001", platform="other"), group):
        req = ProviderRequest(system_prompt="原设定")
        await plugin.inject_collection_context(who, req)
        assert req.system_prompt == "原设定"
    assert len(plugin.users.apps) == count


@pytest.mark.asyncio
async def test_failed_refresh_and_account_change_do_not_reuse_success_context(plugin):
    app = await collected(plugin)
    req = ProviderRequest(system_prompt="原设定")
    await plugin.inject_collection_context(event("10001"), req)
    assert START in req.system_prompt

    def fail(*args, **kwargs):
        raise SourceError("SOURCE_RATE_LIMIT")

    app.source.fetch = fail
    assert (await app.refresh_sources("context-failed", wait_seconds=10))["status"] == "failed"
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == "原设定"
    await app.io(app.runtime.bind_account, "2002")
    assert await app.collection_context() is None


@pytest.mark.asyncio
async def test_limits_disable_and_running_collection(plugin):
    items = [dict(video(i), description="长正文" * 300) for i in range(1, 21)]
    app = await collected(plugin, items)
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert 0 < payload(req)["shown_count"] <= 8 and payload(req)["truncated"]
    first_ids = {item["id"] for item in payload(req)["items"]}
    app.settings = replace(app.settings, collection_context_max_chars=1000)
    app.runtime.settings = app.settings
    small = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), small)
    assert len(small.system_prompt) <= 1000
    small_ids = {item["id"] for item in payload(small)["items"]} if small.system_prompt else set()
    assert first_ids.isdisjoint(small_ids)
    app.settings = replace(app.settings, collection_context_max_chars=6000)
    app.runtime.settings = app.settings
    remaining = set()
    for _ in range(20):
        next_req = ProviderRequest()
        await plugin.inject_collection_context(event("10001"), next_req)
        if not next_req.system_prompt:
            break
        ids = {item["id"] for item in payload(next_req)["items"]}
        assert ids.isdisjoint(first_ids | small_ids | remaining)
        remaining.update(ids)
    assert len(first_ids | small_ids | remaining) == 20
    app.active = "in-flight"
    try:
        await plugin.inject_collection_context(event("10001"), req)
        assert req.system_prompt == ""
    finally:
        app.active = None
    plugin.settings = replace(plugin.settings, collection_context_enabled=False)
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("injected_before_reload", [False, True])
async def test_restored_collection_keeps_consumption_receipts(plugin, injected_before_reload):
    app = await collected(plugin)
    if injected_before_reload:
        await plugin.inject_collection_context(event("10001"), ProviderRequest())
    # Match a normal logged-in reload, without contacting Bilibili.
    app.login.state.update(credential={"sessdata": "synthetic"})
    await app.login.persist()
    await plugin.users.close()
    plugin.users = UserApplications(plugin.users.root, plugin.context, plugin.settings, LOGGER)
    await plugin.users.start()
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    if injected_before_reload:
        assert req.system_prompt == ""
    else:
        assert payload(req)["shown_count"] == 1
    assert plugin.context.llm_generate.await_count == 0


@pytest.mark.asyncio
async def test_repeated_collection_without_new_items_injects_nothing(plugin):
    app = await collected(plugin)
    # Even without an earlier injection, a re-fetch does not turn an old item into a new item.
    await app.refresh_sources("same-content-again", wait_seconds=10)
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == ""


@pytest.mark.asyncio
async def test_concurrent_requests_claim_each_new_item_only_once(plugin):
    await collected(plugin, [video(i) for i in range(1, 16)])
    requests = [ProviderRequest() for _ in range(4)]
    await asyncio.gather(
        *(plugin.inject_collection_context(event("10001"), req) for req in requests)
    )
    seen = set()
    for req in requests:
        if req.system_prompt:
            ids = {item["id"] for item in payload(req)["items"]}
            assert seen.isdisjoint(ids)
            seen.update(ids)
    assert len(seen) == 15


@pytest.mark.asyncio
async def test_adding_another_source_does_not_make_old_content_new(plugin):
    app = await collected(plugin)
    state = await app.io(app.runtime.state)
    plan = state["profile"]["source_plan"]
    plan["ups"] = [{"uid": "2002", "enabled": True, "scope": "uploads", "content_type": "all"}]
    await app.io(
        app.runtime.save_profile,
        {
            "request_id": "add-context-up",
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan},
        },
    )
    await app.refresh_sources("same-content-new-source", wait_seconds=10)
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == ""


@pytest.mark.asyncio
async def test_empty_budget_and_filtered_items_are_not_consumed(plugin):
    app = await collected(plugin)
    app.runtime.settings = replace(app.settings, collection_context_max_chars=1)
    req = ProviderRequest()
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == ""
    app.runtime.settings = app.settings
    state = await app.io(app.runtime.state)
    plan = state["profile"]["source_plan"]
    plan["keyword_search"]["enabled"] = False
    await app.io(
        app.runtime.save_profile,
        {
            "request_id": "hide-context-source",
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan},
        },
    )
    await plugin.inject_collection_context(event("10001"), ProviderRequest())
    with closing(Store(app.runtime.library.db_path, rebuild_preferences=False)) as store:
        assert (
            store.db.execute("SELECT COUNT(*) FROM collection_context_consumed").fetchone()[0] == 0
        )
    state = await app.io(app.runtime.state)
    plan["keyword_search"]["enabled"] = True
    await app.io(
        app.runtime.save_profile,
        {
            "request_id": "show-context-source",
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan},
        },
    )
    await plugin.inject_collection_context(event("10001"), req)
    assert payload(req)["shown_count"] == 1


@pytest.mark.asyncio
async def test_context_error_does_not_break_chat(plugin, monkeypatch):
    app = await collected(plugin)
    monkeypatch.setattr(app, "collection_context", AsyncMock(side_effect=RuntimeError("offline")))
    req = ProviderRequest(system_prompt="原设定")
    await plugin.inject_collection_context(event("10001"), req)
    assert req.system_prompt == "原设定"


@pytest.mark.parametrize(
    "config",
    [
        {"collection_context_enabled": "true"},
        {"collection_context_max_chars": True},
        {"collection_context_max_chars": 999},
        {"collection_context_max_chars": 20001},
    ],
)
def test_invalid_context_config(config):
    with pytest.raises(PluginError):
        Settings.from_config(config)
