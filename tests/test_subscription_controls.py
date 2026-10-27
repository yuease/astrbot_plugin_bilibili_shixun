"""Fixed admin page binding and transactional subscription controls."""

from dataclasses import replace

import pytest
from astrbot_plugin_bilibili_shixun.services.errors import PluginError
from astrbot_plugin_bilibili_shixun.services.settings import Settings
from test_users import event, invoke
from test_users import plugin as shared_plugin

plugin = shared_plugin


async def configure_up(app):
    state = await app.io(app.runtime.state)
    plan = state["profile"]["source_plan"]
    plan["ups"] = [{"uid": "123", "enabled": False, "scope": "both", "content_type": "non_video"}]
    await app.io(
        app.runtime.save_profile,
        {
            "expected_version": state["profile_version"],
            "changes": {"source_plan": plan},
            "request_id": "seed-up",
        },
    )


async def toggle(plugin, who, **params):
    current = await invoke(plugin, who, "get_settings")
    return await invoke(
        plugin, who, "set_subscription_enabled", expected_version=current["version"], **params
    )


def test_new_dashboard_binding_accepts_plain_number_or_sid_only():
    assert Settings.from_config({"dashboard_bindings": " 10001 "}).dashboard_bindings == "10001"
    assert (
        Settings.from_config(
            {"dashboard_bindings": "official:FriendMessage:OPENID_A"}
        ).dashboard_bindings
        == "official:FriendMessage:OPENID_A"
    )
    for value in (["admin|a|b|c"], "admin|a|b|c", 10001, "some user"):
        with pytest.raises(PluginError):
            Settings.from_config({"dashboard_bindings": value})


@pytest.mark.asyncio
async def test_plain_number_binds_one_admin_for_every_dashboard_user(plugin):
    admin = await plugin.users.chat(event("10001"))
    ordinary = await plugin.users.chat(event())
    plugin.users.settings = replace(plugin.settings, dashboard_bindings="10001")
    assert await plugin.users.dashboard("admin") is admin
    assert await plugin.users.dashboard("editor") is admin
    assert ordinary is not admin and len(plugin.users.apps) == 2
    plugin.context._config["admins_id"] = []
    with pytest.raises(PluginError) as exc:
        await plugin.users.dashboard("admin")
    assert exc.value.code == "DASHBOARD_BINDING_NOT_FOUND"


@pytest.mark.asyncio
async def test_ambiguous_number_fails_until_exact_sid_is_configured(plugin):
    a = await plugin.users.chat(event("10001"))
    b = await plugin.users.chat(event("10001", platform="bot-2"))
    plugin.users.settings = replace(plugin.settings, dashboard_bindings="10001")
    with pytest.raises(PluginError) as exc:
        await plugin.users.dashboard("admin")
    assert exc.value.code == "DASHBOARD_BINDING_AMBIGUOUS"
    plugin.users.settings = replace(plugin.settings, dashboard_bindings="bot-2:FriendMessage:10001")
    assert await plugin.users.dashboard("editor") is b and a is not b


@pytest.mark.asyncio
async def test_official_sid_uses_existing_private_identity(plugin):
    from test_official import official_event

    who, _ = official_event()
    plugin.context._config["admins_id"].append("OPENID_USER_A")
    settings = await invoke(plugin, who, "get_settings")
    assert settings["user_identity"]["sid"] == who.unified_msg_origin
    plugin.users.settings = replace(
        plugin.settings, dashboard_bindings=settings["user_identity"]["sid"]
    )
    assert await plugin.users.dashboard("admin") is await plugin.users.chat(who)


@pytest.mark.asyncio
async def test_unknown_binding_does_not_create_empty_dashboard_profile(plugin):
    plugin.users.settings = replace(plugin.settings, dashboard_bindings="10001")
    with pytest.raises(PluginError) as exc:
        await plugin.users.dashboard("admin")
    assert exc.value.code == "DASHBOARD_BINDING_NOT_FOUND" and not plugin.users.apps


@pytest.mark.asyncio
async def test_keyword_toggle_requires_terms_then_preserves_other_settings(plugin):
    who = event()
    denied = await toggle(plugin, who, source="keyword_search", enabled=True)
    assert denied["error_code"] == "SUBSCRIPTION_KEYWORDS_REQUIRED"
    current = await invoke(plugin, who, "get_settings")
    await invoke(
        plugin,
        who,
        "update_settings",
        expected_version=current["version"],
        add_keywords=["Codex"],
        interests="教程",
    )
    result = await toggle(plugin, who, source="keyword_search", enabled=True)
    assert (
        result["settings_saved"] and result["needs_collection"] and not result["collection_started"]
    )
    current = await invoke(plugin, who, "get_settings")
    assert current["settings"]["source_plan"]["keyword_search"]["enabled"]
    assert (
        current["settings"]["keywords"] == ["Codex"] and current["settings"]["interests"] == "教程"
    )
    off = await toggle(plugin, who, source="keyword_search", enabled=False)
    assert off["settings_saved"] and not off["needs_collection"]
    assert (await invoke(plugin, who, "get_settings"))["settings"]["keywords"] == ["Codex"]


@pytest.mark.asyncio
async def test_following_requires_permission_and_login_then_uses_own_uid(plugin):
    denied = await toggle(plugin, event(), source="following", enabled=True)
    assert denied["error_code"] == "ACCOUNT_HOSTING_DENIED"
    who = event("10001")
    denied = await toggle(plugin, who, source="following", enabled=True)
    assert denied["error_code"] == "BILI_LOGIN_REQUIRED"
    app = await plugin.users.chat(who)
    app.login.state.update(status="valid", uid="1001", credential={"sessdata": "synthetic"})
    await app.install_account("1001")
    result = await toggle(plugin, who, source="following", enabled=True, following_scope="dynamics")
    assert result["settings_saved"] and not result["collection_started"]
    current = await invoke(plugin, who, "get_settings")
    following = current["settings"]["source_plan"]["following"]
    assert following["enabled"] and following["scope"] == "dynamics" and following["uid"] == "1001"
    assert not (await invoke(plugin, event(), "get_settings"))["settings"]["source_plan"][
        "following"
    ]["enabled"]
    plugin.context._config["admins_id"] = []
    off = await toggle(plugin, who, source="following", enabled=False)
    assert off["settings_saved"]


@pytest.mark.asyncio
async def test_up_toggle_preserves_scope_content_and_unrelated_sources(plugin):
    who = event("10001")
    app = await plugin.users.chat(who)
    await configure_up(app)
    app.login.state.update(status="valid", uid="1001", credential={"sessdata": "synthetic"})
    await app.install_account("1001")
    result = await toggle(plugin, who, source="up", up_uid="123", enabled=True)
    assert result["settings_saved"]
    state = await invoke(plugin, who, "get_settings")
    up = state["settings"]["source_plan"]["ups"][0]
    assert up == {"uid": "123", "enabled": True, "scope": "both", "content_type": "non_video"}
    unknown = await toggle(plugin, who, source="up", up_uid="999", enabled=True)
    assert unknown["error_code"] == "SUBSCRIPTION_NOT_FOUND"
    app.login.state.update(status="expired")
    off = await toggle(plugin, who, source="up", up_uid="123", enabled=False)
    assert off["settings_saved"]


@pytest.mark.asyncio
async def test_source_switch_replay_conflict_and_undo(plugin):
    who = event()
    initial = await invoke(plugin, who, "get_settings")
    await invoke(
        plugin, who, "update_settings", expected_version=initial["version"], add_keywords=["Codex"]
    )
    current = await invoke(plugin, who, "get_settings")
    params = dict(expected_version=current["version"], source="keyword_search", enabled=True)
    result = await invoke(plugin, who, "set_subscription_enabled", **params)
    replay = await invoke(plugin, who, "set_subscription_enabled", **params)
    assert replay["replayed"] and replay["change_id"] == result["change_id"]
    stale = await invoke(
        plugin,
        who,
        "set_subscription_enabled",
        expected_version=current["version"],
        source="keyword_search",
        enabled=False,
    )
    assert stale["error_code"] == "SETTINGS_CONFLICT"
    latest = await invoke(plugin, who, "get_settings")
    undo = await invoke(
        plugin,
        who,
        "undo_settings",
        change_id=result["change_id"],
        expected_version=latest["version"],
    )
    assert undo["settings_saved"]
    assert not (await invoke(plugin, who, "get_settings"))["settings"]["source_plan"][
        "keyword_search"
    ]["enabled"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {"source": "up", "enabled": False},
        {"source": "keyword_search", "enabled": False, "up_uid": "123"},
        {"source": "up", "enabled": False, "up_uid": "123", "following_scope": "videos"},
        {"source": "following", "enabled": False, "uid": "foreign"},
        {"source": "following", "enabled": False, "account_uid": "foreign"},
        {"source": "following", "enabled": False, "user_id": "foreign"},
    ],
)
async def test_switch_rejects_invalid_target_and_model_selected_identity(plugin, params):
    denied = await toggle(plugin, event(), **params)
    assert denied["error_code"] == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_source_switch_obeys_task_lock(plugin):
    who = event()
    app = await plugin.users.chat(who)
    app.active = "synthetic-running"
    denied = await toggle(plugin, who, source="following", enabled=False)
    assert denied["error_code"] == "TASK_BUSY"
    app.active = None
