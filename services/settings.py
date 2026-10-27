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

    @classmethod
    def from_config(cls, config):
        provider = config.get("briefing_provider_id", "")
        timeout = config.get("model_timeout_seconds", 90)
        admins = config.get("dashboard_admins", [])
        automatic = config.get("bili_auto_relogin", True)
        interval = config.get("bili_check_interval_minutes", 30)
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
        ):
            raise PluginError("INVALID_ARGUMENT")
        return cls(
            provider.strip(), timeout, tuple(item.strip() for item in admins), automatic, interval
        )
