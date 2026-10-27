"""Bilibili web QR login and bounded QQ reauthentication notifications."""

import asyncio
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import PluginError, log_failure

PASSPORT = "https://passport.bilibili.com"
API = "https://api.bilibili.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com/",
}
QQ_ADAPTERS = {"aiocqhttp", "qq_official", "qq_official_webhook"}
QR_LOCATIONS = {
    ("passport.bilibili.com", "/h5-app/passport/login/scan"),
    ("account.bilibili.com", "/h5/account-h5/auth/scan-web"),
}


def qr_payload(value):
    """Accept Bilibili's current and legacy QR destinations without logging tokens."""
    if not isinstance(value, dict):
        raise PluginError("BILI_QR_FAILED", reason="invalid_response")
    if value.get("code") != 0:
        raise PluginError("BILI_QR_FAILED", reason="api_rejected", api_code=value.get("code"))
    data = value.get("data")
    if not isinstance(data, dict):
        raise PluginError("BILI_QR_FAILED", reason="invalid_data")
    url, key = data.get("url"), data.get("qrcode_key")
    if not isinstance(url, str) or len(url) > 4096:
        raise PluginError("BILI_QR_FAILED", reason="invalid_url")
    try:
        parsed = urlsplit(url)
        supported = (
            parsed.scheme == "https"
            and (parsed.hostname, parsed.path) in QR_LOCATIONS
            and parsed.port in (None, 443)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        supported = False
    if not supported:
        raise PluginError("BILI_QR_FAILED", reason="unsupported_url")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", key):
        raise PluginError("BILI_QR_FAILED", reason="invalid_key")
    return url, key


def now():
    return datetime.now(timezone.utc).isoformat()


def write_private(path, value):
    """Atomic credential write, with owner-only POSIX permissions from creation."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.parent.chmod(0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


class BilibiliLogin:
    def __init__(self, root, context, settings, logger, install_account):
        self.path = Path(root) / "account" / "session.json"
        self.context, self.settings, self.logger = context, settings, logger
        self.install_account = install_account
        self.state = {"status": "not_logged_in", "credential": {}, "owner": None, "revision": ""}
        self.client = httpx.AsyncClient(
            headers=HEADERS, timeout=15, follow_redirects=False, trust_env=False
        )
        self.lock = asyncio.Lock()
        self.poll_task = self.monitor_task = None
        self.pending = None
        self.stop = asyncio.Event()
        self.closing = False
        self.next_notice_attempt = 0.0

    async def start(self):
        if self.path.exists():
            raw = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("credential"), dict):
                raise PluginError("BILI_SESSION_INVALID")
            self.state.update(value)
        self.monitor_task = asyncio.create_task(self._monitor())

    async def persist(self):
        # All callers hold self.lock; writes are never run in an abandoned thread.
        task = asyncio.create_task(asyncio.to_thread(write_private, self.path, dict(self.state)))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    @property
    def uid(self):
        return str(self.state.get("uid", "")) if self.state.get("status") == "valid" else ""

    @property
    def revision(self):
        return self.state.get("revision", "")

    @property
    def authenticating(self):
        return self.pending is not None

    def credentials(self):
        from bilibili_api import Credential

        if not self.uid or not self.state["credential"].get("sessdata"):
            raise PluginError("BILI_LOGIN_REQUIRED")
        return Credential(**self.state["credential"])

    def public_state(self):
        return {
            "status": self.state.get("status", "not_logged_in"),
            "uid": self.state.get("uid"),
            "name": self.state.get("name"),
            "last_checked_at": self.state.get("last_checked_at"),
            "qr_pending": self.pending is not None,
            "auto_relogin": self.settings.bili_auto_relogin,
            "notification_bound": bool(self.state.get("owner")),
            "proactive_supported": (self.state.get("owner") or {}).get("adapter") == "aiocqhttp",
        }

    def _owner(self, event):
        if event.get_platform_name() not in QQ_ADAPTERS:
            raise PluginError("BILI_QQ_PRIVATE_REQUIRED")
        return {
            "umo": event.unified_msg_origin,
            "sender": str(event.get_sender_id()),
            "platform_id": event.get_platform_id(),
            "adapter": event.get_platform_name(),
        }

    def _authorized(self, owner):
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType

        try:
            session = MessageSession.from_str(owner["umo"])
            admins = {
                str(value) for value in self.context.get_config(owner["umo"]).get("admins_id", [])
            }
            return (
                session.message_type == MessageType.FRIEND_MESSAGE
                and session.platform_id == owner["platform_id"]
                and session.session_id == owner["sender"]
                and (self.settings.allow_private_users or owner["sender"] in admins)
                and owner["adapter"] in QQ_ADAPTERS
            )
        except Exception:
            return False

    async def _send(self, owner, message, png=None, event=None):
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import Image, Plain

        if not self._authorized(owner):
            raise PluginError("ACCESS_DENIED", status=403)
        chain = MessageChain(chain=[Plain(message)] + ([Image.fromBytes(png)] if png else []))
        if event is not None:
            await event.send(chain)
        else:
            if owner["adapter"] != "aiocqhttp":
                raise PluginError("BILI_PROACTIVE_UNAVAILABLE")
            sent = await self.context.send_message(owner["umo"], chain)
            if sent is False:
                raise PluginError("BILI_DELIVERY_FAILED")

    async def request_login(self, event):
        return await self._begin(self._owner(event), event=event)

    async def _begin(self, owner, *, event=None, automatic=False):
        async with self.lock:
            if self.closing:
                raise PluginError("PLUGIN_CLOSED")
            bound = self.state.get("owner")
            if bound and (bound["sender"], bound["platform_id"]) != (
                owner["sender"],
                owner["platform_id"],
            ):
                raise PluginError("BILI_OTHER_OWNER", status=403)
            if self.pending is not None:
                if self.pending["owner"] != owner:
                    raise PluginError("BILI_OTHER_OWNER", status=403)
                await self._send(
                    owner,
                    "请扫描这张尚未过期的二维码，并在 B 站 App 中确认。",
                    self.pending["png"],
                    event,
                )
                return "二维码已发送，等待 B 站 App 确认。"
            async with httpx.AsyncClient(
                headers=HEADERS, timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                response = await client.get(
                    PASSPORT + "/x/passport-login/web/qrcode/generate",
                    params={"source": "main-fe-header"},
                )
                response.raise_for_status()
                try:
                    value = response.json()
                except ValueError:
                    raise PluginError("BILI_QR_FAILED", reason="invalid_json") from None
            url, key = qr_payload(value)

            def render():
                import qrcode

                output = BytesIO()
                qrcode.make(url).save(output, format="PNG")
                return output.getvalue()

            png = await asyncio.to_thread(render)
            if self.closing:
                raise PluginError("PLUGIN_CLOSED")
            pending = {
                "owner": owner,
                "key": key,
                "png": png,
                "deadline": time.monotonic() + 180,
                "event": event,
                "nonce": uuid.uuid4().hex,
            }
            # Credentials remain unchanged until the scan succeeds and the
            # application has finished any collection already in progress.
            self.pending = pending
            prefix = (
                "B 站登录已失效，请重新扫码。" if automatic else "请用 B 站 App 扫码并确认登录。"
            )
            try:
                await self._send(
                    owner, prefix + "二维码仅在本次登录中有效；插件最多等待三分钟。", png, event
                )
            except BaseException:
                self.pending = None
                raise
            if automatic:
                self.state["notified_revision"] = self.revision
                await self.persist()
            self.poll_task = asyncio.create_task(self._poll(pending))
            return "二维码已发送，确认后会在本私聊通知登录结果。"

    async def _poll(self, pending):
        owner, event = pending["owner"], pending["event"]
        try:
            async with httpx.AsyncClient(
                headers=HEADERS, timeout=15, follow_redirects=False, trust_env=False
            ) as client:
                while not self.closing and time.monotonic() < pending["deadline"]:
                    await asyncio.sleep(3)
                    try:
                        response = await client.get(
                            PASSPORT + "/x/passport-login/web/qrcode/poll",
                            params={"qrcode_key": pending["key"], "source": "main-fe-header"},
                        )
                        response.raise_for_status()
                        body = response.json()
                    except (httpx.HTTPError, ValueError):
                        continue  # Transient failure is neither successful nor expired login.
                    if body.get("code") != 0 or not isinstance(body.get("data"), dict):
                        continue
                    code = body["data"].get("code")
                    if code in (86101, 86090):
                        continue
                    if code == 86038:
                        break
                    if code != 0:
                        raise PluginError("BILI_QR_FAILED")
                    cookies = {
                        cookie.name: cookie.value
                        for cookie in client.cookies.jar
                        if cookie.domain.lstrip(".").endswith("bilibili.com")
                    }
                    if not cookies.get("SESSDATA") or not cookies.get("bili_jct"):
                        raise PluginError("BILI_QR_FAILED")
                    nav = await client.get(API + "/x/web-interface/nav")
                    nav.raise_for_status()
                    account = nav.json()
                    data = account.get("data", {})
                    uid = str(data.get("mid", ""))
                    if (
                        account.get("code") != 0
                        or data.get("isLogin") is not True
                        or not uid.isdigit()
                    ):
                        raise PluginError("BILI_QR_FAILED")
                    credential = {
                        "sessdata": cookies["SESSDATA"],
                        "bili_jct": cookies["bili_jct"],
                        "dedeuserid": uid,
                    }
                    credential.update(
                        {name: cookies[name] for name in ("buvid3", "buvid4") if cookies.get(name)}
                    )
                    if not self._authorized(owner):
                        raise PluginError("ACCESS_DENIED", status=403)
                    await self.install_account(uid)
                    async with self.lock:
                        if (
                            self.closing
                            or self.pending is not pending
                            or not self._authorized(owner)
                        ):
                            return
                        self.state.update(
                            status="valid",
                            credential=credential,
                            uid=uid,
                            name=str(data.get("uname", ""))[:100],
                            owner=owner,
                            revision=uuid.uuid4().hex,
                            last_checked_at=now(),
                            notified_revision=None,
                        )
                        await self.persist()
                    await self._send(
                        owner, f"B 站登录成功，UID：{uid}。已绑定此私聊，可开始采集。", event=event
                    )
                    return
            if not self.closing:
                await self._send(
                    owner,
                    "二维码已过期或等待结束。需要登录时发送 /B站登录 重新获取，不会连续自动刷码。",
                    event=event,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_failure(self.logger, "bilibili_login", "qr", exc)
            try:
                await self._send(owner, "本次登录未完成，请稍后发送 /B站登录 重试。", event=event)
            except Exception:
                pass
        finally:
            async with self.lock:
                if self.pending is pending:
                    self.pending = None

    async def mark_expired(self, revision):
        async with self.lock:
            if revision != self.revision or not self.state.get("credential"):
                return
            self.state["status"] = "expired"
            self.state["last_checked_at"] = now()
            await self.persist()

    async def check(self):
        async with self.lock:
            cookie = dict(self.state.get("credential", {}))
            revision = self.revision
        if not cookie.get("sessdata"):
            return
        self.client.cookies.clear()
        response = await self.client.get(
            API + "/x/web-interface/nav", cookies={"SESSDATA": cookie["sessdata"]}
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") == -101 or (
            body.get("code") == 0 and body.get("data", {}).get("isLogin") is False
        ):
            await self.mark_expired(revision)
        elif body.get("code") == 0 and body.get("data", {}).get("isLogin") is True:
            async with self.lock:
                if revision == self.revision:
                    self.state.update(status="valid", last_checked_at=now())
                    await self.persist()

    async def _monitor(self):
        next_check = time.monotonic() + 10
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + self.settings.bili_check_interval_minutes * 60
                    await self.check()
                owner = self.state.get("owner")
                if (
                    self.settings.bili_auto_relogin
                    and self.state.get("status") == "expired"
                    and owner
                    and owner.get("adapter") == "aiocqhttp"
                    and self.pending is None
                    and self.state.get("notified_revision") != self.revision
                    and self._authorized(owner)
                    and time.monotonic() >= self.next_notice_attempt
                ):
                    self.next_notice_attempt = time.monotonic() + 600
                    await self._begin(owner, automatic=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_failure(self.logger, "bilibili_health", "monitor", exc)
            try:
                await asyncio.wait_for(self.stop.wait(), 30)
            except TimeoutError:
                pass

    async def logout(self, event):
        owner = self._owner(event)
        async with self.lock:
            bound = self.state.get("owner") or (self.pending or {}).get("owner")
            if bound and (bound["sender"], bound["platform_id"]) != (
                owner["sender"],
                owner["platform_id"],
            ):
                raise PluginError("BILI_OTHER_OWNER", status=403)
        if self.poll_task and not self.poll_task.done():
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        await self.install_account("")
        async with self.lock:
            self.state = {
                "status": "not_logged_in",
                "credential": {},
                "owner": None,
                "revision": "",
            }
            await self.persist()

    async def close(self):
        self.closing = True
        self.stop.set()
        tasks = [task for task in (self.monitor_task, self.poll_task) if task and not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
