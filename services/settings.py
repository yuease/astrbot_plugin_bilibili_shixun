"""Trusted runtime configuration; no source server or cookie text settings."""

from dataclasses import dataclass

from .errors import PluginError

PLUGIN_NAME = "astrbot_plugin_bilibili_shixun"
PROFILE_ID = "astrbot-bilibili-shixun"
MAX_BODY_BYTES = 128 * 1024


@dataclass(frozen=True)
class Settings:
    briefing_provider_id: str = ""
    model_timeout_seconds: int = 90
    dashboard_admins: tuple[str, ...] = ()
    bili_auto_relogin: bool = True
    bili_check_interval_minutes: int = 30
    allow_private_users: bool = True
    max_users: int = 32
    dashboard_bindings: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config):
        provider = config.get("briefing_provider_id", "")
        timeout = config.get("model_timeout_seconds", 90)
        admins = config.get("dashboard_admins", [])
        automatic = config.get("bili_auto_relogin", True)
        interval = config.get("bili_check_interval_minutes", 30)
        private_users = config.get("allow_private_users", True)
        max_users = config.get("max_users", 32)
        bindings = config.get("dashboard_bindings", [])
        if (
            not isinstance(provider, str)
            or len(provider) > 200
            or type(timeout) is not int
            or not 10 <= timeout <= 300
            or not isinstance(admins, list)
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 100 for item in admins
            )
            or type(automatic) is not bool
            or type(interval) is not int
            or not 5 <= interval <= 1440
            or type(private_users) is not bool
            or type(max_users) is not int
            or not 1 <= max_users <= 128
            or not isinstance(bindings, list)
            or any(
                not isinstance(item, str)
                or len(item) > 1024
                or len(item.split("|")) != 4
                or any(not part.strip() for part in item.split("|"))
                for item in bindings
            )
            or len({item.split("|")[0].strip() for item in bindings}) != len(bindings)
        ):
            raise PluginError("INVALID_ARGUMENT")
        return cls(
            provider.strip(),
            timeout,
            tuple(item.strip() for item in admins),
            automatic,
            interval,
            private_users,
            max_users,
            tuple(bindings),
        )
