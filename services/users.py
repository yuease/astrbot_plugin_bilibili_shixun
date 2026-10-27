"""Trusted host identities select independent application runtimes."""

import asyncio
import hashlib
import json
import os
import re

from .access import chat_identity
from .application import Application
from .bilibili_login import write_private
from .bilibili_source import SourceGate
from .errors import PluginError, log_failure


def user_key(identity):
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class UserApplications:
    def __init__(self, root, context, settings, logger):
        self.root, self.context, self.settings, self.logger = root, context, settings, logger
        self.apps = {}
        self.lock = asyncio.Lock()
        self.closed = False
        self.source_gate = SourceGate()
        self.model_gate = asyncio.Lock()

    async def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.root.chmod(0o700)
        # Restore personal schedulers and reauthentication monitors after reload.
        for path in sorted(self.root.glob("*/identity.json")):
            if not re.fullmatch(r"[a-f0-9]{64}", path.parent.name):
                continue
            try:
                identity = json.loads(await asyncio.to_thread(path.read_text, encoding="utf-8"))
                if user_key(identity) != path.parent.name:
                    raise ValueError("identity mismatch")
                if not self.settings.allow_private_users and identity.get("kind") == "chat":
                    umo = self._umo(identity)
                    admins = {str(v) for v in self.context.get_config(umo).get("admins_id", [])}
                    if identity["sender"] not in admins:
                        continue
                if identity.get("kind") == "dashboard":
                    allowed = self.settings.dashboard_admins or (
                        self.context.get_config().get("dashboard", {}).get("username"),
                    )
                    if identity.get("username") not in allowed:
                        continue
                await self.get(identity)
            except Exception as exc:
                log_failure(self.logger, "restore_user", path.parent.name[:12], exc)

    @staticmethod
    def _umo(identity):
        if identity["kind"] != "chat":
            return None
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType

        return str(
            MessageSession(
                platform_name=identity["platform_id"],
                message_type=MessageType.FRIEND_MESSAGE,
                session_id=identity["sender"],
            )
        )

    async def chat(self, event):
        return await self.get(chat_identity(event))

    async def dashboard(self, username):
        for binding in self.settings.dashboard_bindings:
            name, adapter, platform, sender = [v.strip() for v in binding.split("|")]
            if name == username:
                return await self.get(
                    {"kind": "chat", "adapter": adapter, "platform_id": platform, "sender": sender}
                )
        return await self.get({"kind": "dashboard", "username": username})

    async def get(self, identity):
        key = user_key(identity)
        async with self.lock:
            if self.closed:
                raise PluginError("PLUGIN_CLOSED")
            if key in self.apps:
                return self.apps[key]
            if len(self.apps) >= self.settings.max_users:
                raise PluginError("USER_LIMIT", status=429)
            root = self.root / key
            if root.is_symlink() or root.resolve().parent != self.root.resolve():
                raise PluginError("ACCESS_DENIED", status=403)
            root.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                root.chmod(0o700)
            app = Application(
                root,
                self.context,
                self.settings,
                self.logger,
                source_gate=self.source_gate,
                model_gate=self.model_gate,
                umo=self._umo(identity),
                identity=dict(identity),
            )
            try:
                await app.start()
                await asyncio.to_thread(write_private, root / "identity.json", identity)
            except BaseException:
                await app.close()
                raise
            self.apps[key] = app
            return app

    async def close(self):
        async with self.lock:
            self.closed = True
            apps = tuple(self.apps.values())
        results = await asyncio.gather(*(app.close() for app in apps), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                log_failure(self.logger, "close_user", "shutdown", result)
