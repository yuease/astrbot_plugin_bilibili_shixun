"""Isolation is checked through trusted tool and web entry points, offline."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from astrbot.core.agent.tool import ToolSet
from astrbot.core.star.context import Context
from astrbot_plugin_bilibili_shixun import main
from astrbot_plugin_bilibili_shixun.core.bilibili_items import video_batch
from astrbot_plugin_bilibili_shixun.core.core_errors import CoreError
from astrbot_plugin_bilibili_shixun.core.library import RequestError
from astrbot_plugin_bilibili_shixun.services.access import authorize_chat, chat_identity
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from astrbot_plugin_bilibili_shixun.services.users import UserApplications, user_key
from test_smoke import LOGGER, Model, seed, video


def event(sender="20001", platform="bot-1", adapter="aiocqhttp"):
    return SimpleNamespace(
        unified_msg_origin=f"{platform}:FriendMessage:{sender}",
        get_sender_id=lambda: sender,
        get_platform_id=lambda: platform,
        get_platform_name=lambda: adapter,
        is_private_chat=lambda: True,
        get_group_id=lambda: "",
        is_admin=lambda: sender == "10001",
        message_obj=SimpleNamespace(message_id="same-message-id"),
        send=AsyncMock(),
    )


@pytest_asyncio.fixture
async def plugin(tmp_path, monkeypatch):
    context = object.__new__(Context)
    context._config = {"admins_id": ["10001"], "dashboard": {"username": "admin"}}
    context.astrbot_config_mgr = SimpleNamespace(get_conf=lambda umo: context._config)
    context.provider_manager = SimpleNamespace(llm_tools=ToolSet())
    context.registered_web_apis = []
    context.llm_generate = AsyncMock(side_effect=AssertionError("No paid model calls"))
    monkeypatch.setattr(main, "get_astrbot_plugin_data_path", lambda: str(tmp_path))
    instance = main.BilibiliShixunPlugin(context, {"dashboard_admins": ["admin", "editor"]})
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.terminate()


async def invoke(plugin, who, operation, **params):
    return json.loads(await plugin.call_tool(who, operation, params))


@pytest.mark.asyncio
async def test_regular_users_have_independent_keywords_interest_and_change_receipts(plugin):
    a, b = event(), event("20002")
    initial_a = await invoke(plugin, a, "get_settings")
    initial_b = await invoke(plugin, b, "get_settings")
    changed = await invoke(
        plugin,
        a,
        "update_settings",
        expected_version=initial_a["version"],
        add_keywords=["Codex"],
        interests="实操",
    )
    assert changed["settings_saved"]
    replay = await invoke(
        plugin,
        a,
        "update_settings",
        expected_version=initial_a["version"],
        add_keywords=["Codex"],
        interests="实操",
    )
    assert replay["replayed"] and replay["change_id"] == changed["change_id"]
    still_b = await invoke(plugin, b, "get_settings")
    assert still_b["settings"] == initial_b["settings"]
    assert not still_b["recent_changes"]
    rejected = await invoke(
        plugin,
        b,
        "undo_settings",
        change_id=changed["change_id"],
        expected_version=still_b["version"],
    )
    assert "error_code" in rejected and not rejected.get("settings_saved")
    assert (await invoke(plugin, a, "get_settings"))["settings"]["interests"] == "实操"
    assert initial_a["user_identity"]["sender"] == "20001"
    assert initial_b["user_identity"]["sender"] == "20002"


@pytest.mark.asyncio
async def test_platform_instances_and_adapter_types_are_part_of_identity(plugin):
    first = await plugin.users.chat(event())
    assert await plugin.users.chat(event()) is first
    second = await plugin.users.chat(event(platform="bot-2"))
    third = await plugin.users.chat(event(adapter="telegram"))
    assert len({first.root, second.root, third.root}) == 3
    assert first.source.gate is second.source.gate
    assert first.model.gate is third.model.gate
    assert first.login is not second.login


@pytest.mark.asyncio
async def test_same_bilibili_uid_does_not_merge_pool_history_feedback_or_files(plugin):
    a, b = await plugin.users.chat(event()), await plugin.users.chat(event("20002"))
    for app in (a, b):
        await app.io(seed, app.runtime)
    source = SimpleNamespace(fetch=lambda *args, **kwargs: video_batch([video()]))
    model = Model()
    await a.io(a.runtime.run_job, "collect", "collect-a", source, model)
    generated = await a.io(a.runtime.run_job, "generate", "briefing-a", source, model)
    detail = await a.io(a.runtime.detail, "briefing-a")
    recommendation = detail["picks"][0]["recommendation_id"]
    await a.io(
        a.runtime.feedback,
        {"recommendation": recommendation, "value": "useful", "key": "feedback-a"},
    )
    assert (await a.io(a.runtime.state))["stats"]["feedback_count"] == 1
    state_b = await b.io(b.runtime.state)
    assert state_b["stats"]["feedback_count"] == 0 and not state_b["runs"]
    assert not (await b.tool("search_items", {}))["items"]
    item = (await a.tool("search_items", {}))["items"][0]
    with pytest.raises(CoreError):
        await b.tool("get_item", {"item_id": item["id"]})
    with pytest.raises(RequestError):
        await b.io(b.runtime.detail, "briefing-a", True)
    with pytest.raises(RequestError):
        await b.io(
            b.runtime.feedback,
            {"recommendation": recommendation, "value": "useful", "key": "bad-feedback"},
        )
    assert (await a.io(a.runtime.detail, "briefing-a", True))["report"] == generated["report"]
    assert a.runtime.library.db_path != b.runtime.library.db_path


@pytest.mark.asyncio
async def test_temporary_results_and_task_ids_cannot_be_read_by_other_user(plugin):
    a, b = await plugin.users.chat(event()), await plugin.users.chat(event("20002"))
    a.source.search_public = AsyncMock(
        return_value={"items": video_batch([video()]), "remote_has_more": False}
    )
    result = await invoke(plugin, event(), "search_bilibili", keyword="Codex")
    assert result["status"] == "completed"
    assert (
        await invoke(
            plugin, event("20002"), "search_bilibili", action="status", search_id=result["id"]
        )
    )["error_code"]
    assert not (await invoke(plugin, event("20002"), "search_items"))["items"]
    missing = await invoke(
        plugin, event("20002"), "get_item", item_id=result["result"]["items"][0]["id"]
    )
    assert missing["error_code"] == "ITEM_NOT_AVAILABLE"
    a.login.state.update(status="valid", uid="1001", credential={"sessdata": "synthetic-a"})
    await a.login.persist()
    assert b.login.public_state()["status"] == "not_logged_in"
    assert not b.login.path.exists() and a.login.path.exists()
    assert a.login._authorized(
        {
            "umo": event().unified_msg_origin,
            "sender": "20001",
            "platform_id": "bot-1",
            "adapter": "aiocqhttp",
        }
    )


@pytest.mark.asyncio
async def test_admin_and_group_cannot_select_another_user_via_tool_arguments(plugin):
    for who, params in [
        (event("10001"), {"user_id": "20001"}),
        (event(), {"profile_id": "foreign"}),
    ]:
        result = await invoke(plugin, who, "get_settings", **params)
        assert result["error_code"] == "INVALID_ARGUMENT"
    assert not plugin.users.apps
    group = event()
    group.is_private_chat = lambda: False
    result = await invoke(plugin, group, "get_settings")
    assert result["error_code"] == "ACCESS_DENIED" and not plugin.users.apps


@pytest.mark.asyncio
async def test_dashboard_identity_is_separate_and_binding_is_explicit(plugin, monkeypatch):
    a = await plugin.users.chat(event())
    gui = await plugin.users.dashboard("admin")
    editor = await plugin.users.dashboard("editor")
    assert len({a.root, gui.root, editor.root}) == 3
    monkeypatch.setattr(
        main, "request", SimpleNamespace(username="admin", plugin_name=main.PLUGIN_NAME, query={})
    )
    monkeypatch.setattr(main, "json_response", lambda value, **kwargs: value)
    monkeypatch.setattr(main, "error_response", lambda message, **kwargs: kwargs)
    state = await plugin._web("state")
    assert state["user_scope"]["isolated"]
    bound = replace(plugin.settings, dashboard_bindings=("admin|aiocqhttp|bot-1|20001",))
    plugin.users.settings = bound
    assert await plugin.users.dashboard("admin") is a
    assert await plugin.users.dashboard("editor") is editor
    main.request.query = {"user_id": "20001"}
    assert (await plugin._web("state"))["status_code"] == 400
    main.request.query = {}
    main.request.username = "stranger"
    assert (await plugin._web("state"))["status_code"] == 403


@pytest.mark.asyncio
async def test_registry_restores_personal_settings_logins_and_background_tasks(plugin):
    a = await plugin.users.chat(event())
    current = await a.tool("get_settings", {})
    await a.change_settings(
        "update_settings",
        {"expected_version": current["version"], "add_keywords": ["Codex"]},
        "save-a",
    )
    a.login.state.update(status="valid", uid="1001", credential={"sessdata": "synthetic-a"})
    await a.login.persist()
    await plugin.users.close()
    restored = UserApplications(plugin.users.root, plugin.context, plugin.settings, LOGGER)
    try:
        await restored.start()
        same = await restored.chat(event())
        assert (await same.tool("get_settings", {}))["settings"]["keywords"] == ["Codex"]
        assert same.login.uid == "1001" and not same.scheduler.done()
        other = await restored.chat(event("20002"))
        assert not other.login.uid
        assert not (await other.tool("get_settings", {}))["settings"]["keywords"]
    finally:
        await restored.close()
    assert all(
        not app.tasks and not app.threads and not app.started for app in restored.apps.values()
    )


@pytest.mark.asyncio
async def test_concurrent_same_identity_creates_one_runtime_and_cap_preserves_users(plugin):
    plugin.users.settings = replace(plugin.settings, max_users=1)
    a, again = await asyncio.gather(plugin.users.chat(event()), plugin.users.chat(event()))
    assert a is again
    with pytest.raises(PluginError) as limit:
        await plugin.users.chat(event("20002"))
    assert limit.value.code == "USER_LIMIT"
    assert len(plugin.users.apps) == 1 and await plugin.users.chat(event()) is a


def test_private_access_can_be_restricted_to_admins():
    context = SimpleNamespace(get_config=lambda *args: {"admins_id": ["10001"]})
    authorize_chat(event(), context, Settings())
    with pytest.raises(PluginError):
        authorize_chat(event(), context, Settings(allow_private_users=False))
    authorize_chat(event("10001"), context, Settings(allow_private_users=False))
    assert user_key(chat_identity(event())) != user_key(chat_identity(event("20002")))


@pytest.mark.parametrize(
    "value",
    [
        {"max_users": 0},
        {"max_users": True},
        {"max_users": 129},
        {"allow_private_users": "true"},
        {"dashboard_bindings": ["admin|bot|user"]},
        {"dashboard_bindings": ["admin|a|p|u", "admin|b|q|v"]},
    ],
)
def test_multi_user_config_rejects_invalid_access_and_binding(value):
    with pytest.raises(PluginError):
        Settings.from_config(value)


@pytest.mark.asyncio
async def test_sdk_cookies_and_client_routing_are_task_local_and_public_is_anonymous(
    plugin, monkeypatch
):
    from bilibili_api import Api
    from bilibili_api.utils import network

    a, b = await plugin.users.chat(event()), await plugin.users.chat(event("20002"))
    a.source.transport.session.cookies.set("SESSDATA", "synthetic-a", domain=".bilibili.com")
    b.source.transport.session.cookies.set("SESSDATA", "synthetic-b", domain=".bilibili.com")
    first, second = asyncio.Event(), asyncio.Event()

    async def routed(source, expected, ready, wait):
        slot, token = source.transport.activate()
        try:
            ready.set()
            await wait.wait()
            assert network.get_client() is source.transport.client
            assert network.get_client().get_wrapped_session().cookies.get("SESSDATA") == expected
        finally:
            slot.reset(token)

    await asyncio.gather(
        routed(a.source, "synthetic-a", first, second),
        routed(b.source, "synthetic-b", second, first),
    )
    dispatcher = network.get_client
    assert dispatcher._shixun_client_slot.get() is None

    async def request(api, **kwargs):
        assert network.get_client() is a.source.public_transport.client
        assert "SESSDATA" not in network.get_client().get_wrapped_session().cookies
        return {"result": []}

    monkeypatch.setattr(Api, "request", request)
    result = await a.source.search_public("public")
    assert result["items"] == []
    assert dispatcher._shixun_client_slot.get() is None
    assert network.get_client is dispatcher  # No per-request resolver replacements.
    assert b.source.transport.session.cookies.get("SESSDATA") == "synthetic-b"


@pytest.mark.asyncio
async def test_sdk_response_cookies_do_not_cross_users_or_anonymous_queries(plugin):
    import httpx
    from bilibili_api.utils import network

    a, b = await plugin.users.chat(event()), await plugin.users.chat(event("20002"))
    requests = []

    def response(request):
        requests.append(request.headers.get("cookie", ""))
        headers = (
            {"set-cookie": "SESSDATA=synthetic-a; Domain=.bilibili.com; Path=/"}
            if len(requests) == 1
            else {}
        )
        return httpx.Response(200, json={"code": 0}, headers=headers)

    clients = [a.source.transport, b.source.transport, a.source.public_transport]
    for client in clients:
        await client.session.aclose()
        client.session = httpx.AsyncClient(transport=httpx.MockTransport(response))
        from bilibili_api.clients.HTTPXClient import HTTPXClient

        client.client = HTTPXClient(session=client.session)
        slot, token = client.activate()
        try:
            await network.get_client().request(
                method="GET", url="https://api.bilibili.com/isolated-test"
            )
        finally:
            slot.reset(token)
    assert "SESSDATA" not in requests[1] and "SESSDATA" not in requests[2]
    assert a.source.transport.session.cookies.get("SESSDATA") == "synthetic-a"


@pytest.mark.asyncio
async def test_personal_model_provider_routing_and_shared_generation_gate(plugin, monkeypatch):
    from astrbot_plugin_bilibili_shixun.core.schemas import Selection

    a, b = await plugin.users.chat(event()), await plugin.users.chat(event("20002"))
    active = 0
    peak = 0

    async def provider(umo):
        return SimpleNamespace(meta=lambda: SimpleNamespace(id=umo))

    async def generate(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return SimpleNamespace(completion_text='{"picks":[]}', usage=None)

    monkeypatch.setattr(plugin.context, "get_using_provider_async", AsyncMock(side_effect=provider))
    plugin.context.llm_generate = AsyncMock(side_effect=generate)
    await asyncio.gather(a.model._select("a", Selection), b.model._select("b", Selection))
    assert peak == 1
    assert {
        call.kwargs["chat_provider_id"] for call in plugin.context.llm_generate.call_args_list
    } == {event().unified_msg_origin, event("20002").unified_msg_origin}


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["not json", '{"picks":{},"unexpected":1}'])
async def test_briefing_model_parse_failure_does_not_repeat_paid_call(plugin, response):
    from astrbot_plugin_bilibili_shixun.core.errors import DiagnosedError
    from astrbot_plugin_bilibili_shixun.core.schemas import Selection

    app = await plugin.users.chat(event())
    app.model.settings = replace(plugin.settings, briefing_provider_id="offline")
    plugin.context.llm_generate = AsyncMock(
        return_value=SimpleNamespace(completion_text=response, usage=None)
    )
    with pytest.raises(DiagnosedError):
        await app.model._select("prompt", Selection)
    assert plugin.context.llm_generate.await_count == 1
