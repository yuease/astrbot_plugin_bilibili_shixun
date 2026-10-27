"""Hosting permissions are independent of public use and trusted per owner."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from astrbot_plugin_bilibili_shixun.services import bilibili_login as login_module
from astrbot_plugin_bilibili_shixun.services.access import account_hosting_allowed, chat_identity
from astrbot_plugin_bilibili_shixun.services.bilibili_login import (
    HOSTING_NOTICE,
    BilibiliLogin,
    write_private,
)
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from test_smoke import LOGGER, offline_client
from test_users import event, invoke
from test_users import plugin as shared_plugin

plugin = shared_plugin


@pytest.mark.parametrize(
    "admin,restricted,expected",
    [
        (False, True, False),
        (True, True, True),
        (True, False, True),
        (False, False, True),
    ],
)
def test_hosting_policy(admin, restricted, expected):
    identity = chat_identity(event())
    settings = Settings(
        account_hosting_admin_only=restricted,
    )
    context = SimpleNamespace(get_config=lambda *a: {"admins_id": ["20001"] if admin else []})
    assert account_hosting_allowed(identity, context, settings) is expected


@pytest.mark.parametrize(
    "config",
    [
        {"account_hosting_admin_only": 1},
        {"account_hosting_admin_only": "true"},
        {"account_hosting_admin_only": None},
        {"account_hosting_admin_only": []},
    ],
)
def test_invalid_hosting_config(config):
    with pytest.raises(PluginError):
        Settings.from_config(config)


def test_defaults_and_config_normalization():
    assert Settings.from_config({}).account_hosting_admin_only
    assert not hasattr(Settings(), "account_hosting_users")
    assert not account_hosting_allowed(
        {"kind": "dashboard", "username": "admin"}, None, Settings(account_hosting_admin_only=False)
    )


@pytest.mark.asyncio
async def test_default_denial_happens_before_notice_or_network(plugin):
    who = event()
    app = await plugin.users.chat(who)
    with pytest.raises(PluginError) as exc:
        await app.account_action("login", who)
    assert exc.value.code == "ACCOUNT_HOSTING_DENIED"
    who.send.assert_not_awaited()
    assert not app.login.pending and not app.login.path.exists()
    current = await invoke(plugin, who, "get_settings")
    assert current["account_hosting"] == {"allowed": False, "admin_only": True}
    saved = await invoke(
        plugin,
        who,
        "update_settings",
        expected_version=current["version"],
        add_keywords=["Codex"],
        interests="技术",
    )
    assert saved["settings_saved"]
    assert not (await invoke(plugin, who, "search_items")).get("error_code")


@pytest.mark.asyncio
async def test_dashboard_binding_rejects_non_admin_even_if_hosting_allowed(plugin):
    plugin.users.settings = replace(
        plugin.settings,
        dashboard_bindings="20001",
        account_hosting_admin_only=False,
    )
    app = await plugin.users.chat(event())
    assert app.login.hosting_allowed()
    with pytest.raises(PluginError) as denied:
        await plugin.users.dashboard("admin")
    assert denied.value.code == "DASHBOARD_BINDING_NOT_FOUND"


@pytest.mark.asyncio
async def test_notice_once_before_qr_and_survives_logout_restart(tmp_path, monkeypatch):
    requests = []
    who = event()

    def response(request):
        requests.append(request.url.path)
        assert who.send.call_args_list[0].args[0].chain[0].text == HOSTING_NOTICE
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web",
                    "qrcode_key": "offline",
                },
            },
        )

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(response))
    context = SimpleNamespace(get_config=lambda *a: {}, send_message=AsyncMock())
    settings = Settings(account_hosting_admin_only=False)
    for _ in range(2):
        login = BilibiliLogin(
            tmp_path, context, settings, LOGGER, AsyncMock(), identity=chat_identity(who)
        )
        try:
            await login.request_login(who)
            await login.logout(who)
        finally:
            await login.close()
    texts = [c.args[0].chain[0].text for c in who.send.call_args_list]
    assert texts.count(HOSTING_NOTICE) == 1
    assert len(requests) == 2
    assert json.loads(login.path.with_name("privacy_notice.json").read_text()) == {"version": 1}


@pytest.mark.asyncio
async def test_notice_delivery_failure_stops_qr(tmp_path):
    who = event("10001")
    who.send.side_effect = RuntimeError("offline delivery failed")
    context = SimpleNamespace(get_config=lambda *a: {"admins_id": ["10001"]})
    login = BilibiliLogin(
        tmp_path, context, Settings(), LOGGER, AsyncMock(), identity=chat_identity(who)
    )
    try:
        with pytest.raises(RuntimeError):
            await login.request_login(who)
        assert not login.pending and not login.path.with_name("privacy_notice.json").exists()
    finally:
        await login.close()


@pytest.mark.asyncio
async def test_revoke_clears_credentials_pending_and_scope(plugin):
    who = event("10001")
    app = await plugin.users.chat(who)
    await app.install_account("1001")
    app.login.state.update(
        status="valid",
        uid="1001",
        owner=app.login._owner(who),
        credential={"sessdata": "synthetic"},
    )
    await app.login.persist()
    app.login.pending = {"owner": app.login._owner(who)}
    plugin.context._config["admins_id"] = []
    with pytest.raises(PluginError) as exc:
        app.login.credentials()
    assert exc.value.code == "ACCOUNT_HOSTING_DENIED"
    await app.start()
    assert not app.login.uid and not app.login.pending
    assert app.runtime.account_scope != "1001"
    assert json.loads(app.login.path.read_text())["credential"] == {}
    await app.account_action("logout", who)


@pytest.mark.asyncio
async def test_startup_revocation_discards_old_cookie(tmp_path):
    who = event()
    path = tmp_path / "account/session.json"
    write_private(
        path,
        {
            "status": "valid",
            "uid": "1001",
            "owner": {"sender": "20001"},
            "credential": {"sessdata": "synthetic"},
        },
    )
    login = BilibiliLogin(
        tmp_path,
        SimpleNamespace(get_config=lambda *a: {}),
        Settings(),
        LOGGER,
        AsyncMock(),
        identity=chat_identity(who),
    )
    try:
        await login.start()
        assert not login.uid and json.loads(path.read_text())["credential"] == {}
    finally:
        await login.close()


@pytest.mark.asyncio
async def test_private_collect_denied_before_job_reservation(plugin):
    app = await plugin.users.chat(event())
    profile = await app.io(app.runtime.state)
    plan = profile["profile"]["source_plan"]
    plan["following"].update(enabled=True, uid="1001")
    await app.io(
        app.runtime.save_profile,
        {
            "changes": {"source_plan": plan},
            "expected_version": profile["profile_version"],
            "request_id": "private-plan",
        },
    )
    with pytest.raises(PluginError) as exc:
        await app.start_job("collect", "denied")
    assert exc.value.code == "ACCOUNT_HOSTING_DENIED"
    assert await app.io(app.jobs.latest, "collect", app.runtime.account_scope) is None


@pytest.mark.asyncio
async def test_pending_scan_cannot_commit_after_revocation(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    admins = ["10001"]
    who = event("10001")

    def response(request):
        if request.url.path.endswith("generate"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "url": "https://account.bilibili.com/h5/account-h5/auth/scan-web",
                        "qrcode_key": "offline",
                    },
                },
            )
        pytest.fail("No poll request after revocation")

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(response))

    async def revoke(_):
        admins.clear()
        await real_sleep(0)

    monkeypatch.setattr(login_module.asyncio, "sleep", revoke)
    install = AsyncMock()
    login = BilibiliLogin(
        tmp_path,
        SimpleNamespace(get_config=lambda *a: {"admins_id": admins}),
        Settings(),
        LOGGER,
        install,
        identity=chat_identity(who),
    )
    try:
        await login.request_login(who)
        await asyncio.wait_for(login.poll_task, 2)
        assert not login.uid and not login.path.exists()
        install.assert_not_awaited()
    finally:
        await login.close()


def test_general_access_switch_overrides_open_hosting():
    settings = Settings(allow_private_users=False, account_hosting_admin_only=False)
    assert not account_hosting_allowed(
        chat_identity(event()), SimpleNamespace(get_config=lambda *a: {}), settings
    )


@pytest.mark.asyncio
async def test_saved_settings_cannot_change_hosting_policy(plugin):
    who = event()
    current = await invoke(plugin, who, "get_settings")
    denied = await invoke(
        plugin,
        who,
        "update_settings",
        expected_version=current["version"],
        account_hosting_admin_only=False,
    )
    assert denied.get("error_code")
    app = await plugin.users.chat(who)
    assert not app.login.hosting_allowed()


@pytest.mark.asyncio
async def test_source_rechecks_permission_after_rate_limit_delay(monkeypatch):
    from astrbot_plugin_bilibili_shixun.core.source_types import SourceError
    from astrbot_plugin_bilibili_shixun.services.bilibili_source import BilibiliSource
    from bilibili_api import Api, Credential

    denied = False

    def guard():
        if denied:
            raise PluginError("ACCOUNT_HOSTING_DENIED", status=403)

    async def revoke(_):
        nonlocal denied
        denied = True

    monkeypatch.setattr(login_module.asyncio, "sleep", revoke)
    request = AsyncMock(side_effect=AssertionError("Private request must not start"))
    monkeypatch.setattr(Api, "request", request)
    login = SimpleNamespace(
        uid="1001",
        revision="one",
        credentials=lambda: Credential(sessdata="synthetic"),
        require_hosting=guard,
    )
    source = BilibiliSource(asyncio.get_running_loop(), login, LOGGER)
    with pytest.raises(SourceError) as exc:
        await source._fetch({"kind": "up_uploads", "key": "123"}, "1001", None, 10)
    assert exc.value.diagnosis["error_code"] == "SOURCE_AUTH_REQUIRED"
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_commit_revocation_rolls_back_account_scope(tmp_path, monkeypatch):
    real_sleep = asyncio.sleep
    admins = ["10001"]
    who = event("10001")
    installed = []

    async def install(uid):
        installed.append(uid)
        if uid:
            admins.clear()

    async def fast_sleep(_):
        await real_sleep(0)

    def response(request):
        if request.url.path.endswith("/poll"):
            return httpx.Response(
                200,
                json={"code": 0, "data": {"code": 0}},
                headers=[
                    ("Set-Cookie", "SESSDATA=synthetic; Domain=.bilibili.com; Path=/"),
                    ("Set-Cookie", "bili_jct=synthetic; Domain=.bilibili.com; Path=/"),
                ],
            )
        return httpx.Response(200, json={"code": 0, "data": {"isLogin": True, "mid": 1001}})

    monkeypatch.setattr(login_module.httpx, "AsyncClient", offline_client(response))
    monkeypatch.setattr(login_module.asyncio, "sleep", fast_sleep)
    login = BilibiliLogin(
        tmp_path,
        SimpleNamespace(get_config=lambda *a: {"admins_id": admins}),
        Settings(),
        LOGGER,
        install,
        identity=chat_identity(who),
    )
    pending = {
        "owner": login._owner(who),
        "event": who,
        "deadline": login_module.time.monotonic() + 30,
        "key": "offline",
    }
    login.pending = pending
    try:
        await login._poll(pending)
        assert installed == ["1001", ""]
        assert not login.uid and not login.pending and not login.path.exists()
    finally:
        await login.close()
