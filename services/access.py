"""Authentication against framework identities, before any data access."""

import hashlib
import json
from collections import OrderedDict

from .errors import PluginError
from .settings import PLUGIN_NAME


def chat_request_id(event, operation):
    """Stable task receipt across repeated tool calls for one platform message."""
    identity = [
        event.unified_msg_origin,
        str(event.get_sender_id()),
        str(event.message_obj.message_id),
        operation,
    ]
    return "chat-" + hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def authorize_chat(event, context):
    try:
        config = context.get_config(event.unified_msg_origin)
        admins = {str(value) for value in config.get("admins_id", [])}
        if (
            not event.is_private_chat()
            or event.get_group_id()
            or not event.is_admin()
            or str(event.get_sender_id()) not in admins
        ):
            raise PluginError("ACCESS_DENIED", status=403)
    except PluginError:
        raise
    except Exception:
        raise PluginError("ACCESS_DENIED", status=403) from None


def authorize_web(request, context, settings):
    from astrbot.core.workspace import API_KEY_USERNAME_PREFIX

    try:
        username = request.username
        users = settings.dashboard_admins or (
            context.get_config().get("dashboard", {}).get("username"),
        )
        if (
            request.plugin_name != PLUGIN_NAME
            or not isinstance(username, str)
            or username not in users
            or username.startswith(API_KEY_USERNAME_PREFIX)
        ):
            raise PluginError("DASHBOARD_DENIED", status=403)
    except PluginError:
        raise
    except Exception:
        raise PluginError("DASHBOARD_DENIED", status=403) from None


class ReadBudget:
    """Bound chat fan-out in memory; reads never reserve paid model requests."""

    def __init__(self):
        self.messages = OrderedDict()

    def reserve(self, event, operation, params):
        identity = [
            event.unified_msg_origin,
            str(event.get_sender_id()),
            str(event.message_obj.message_id),
        ]
        message = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        digest = hashlib.sha256(
            json.dumps([operation, params], sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        entries = self.messages.setdefault(message, set())
        if digest not in entries and len(entries) >= 8:
            raise PluginError("MESSAGE_READ_LIMIT")
        entries.add(digest)
        self.messages.move_to_end(message)
        while len(self.messages) > 1024:
            self.messages.popitem(last=False)
