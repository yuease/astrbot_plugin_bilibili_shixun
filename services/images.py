"""Bounded delivery of retained Bilibili images; no arbitrary URL or filesystem input."""

import asyncio
import copy
import re
import time
from collections import OrderedDict
from io import BytesIO
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image as PillowImage
from PIL import ImageOps, UnidentifiedImageError

from ..core.covers import content_image_url
from .access import authorize_chat
from .errors import PluginError, log_failure
from .qq_platform import QQ_OFFICIAL, qq_message

MAX_DOWNLOAD = 12 * 1024 * 1024
MAX_PIXELS = 32_000_000
MAX_OUTPUT = 2 * 1024 * 1024
HEADERS = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}


def original_image_url(url):
    safe = content_image_url(url)
    if safe is None:
        raise PluginError("IMAGE_URL_DENIED")
    parsed = urlsplit(safe)
    path = re.sub(r"(\.(?:jpe?g|png|webp|gif))@[^/]*$", r"\1", parsed.path, flags=re.I)
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def original_image(data):
    try:
        with PillowImage.open(BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise PluginError("IMAGE_INVALID")
            if image.width * image.height > MAX_PIXELS:
                raise PluginError("IMAGE_TOO_LARGE")
            image.verify()
        return data
    except (UnidentifiedImageError, OSError, ValueError, PillowImage.DecompressionBombError):
        raise PluginError("IMAGE_INVALID") from None


def resize_image(data):
    try:
        with PillowImage.open(BytesIO(data)) as source:
            if source.format not in {"JPEG", "PNG", "WEBP", "GIF"}:
                raise PluginError("IMAGE_INVALID")
            if source.width * source.height > MAX_PIXELS:
                raise PluginError("IMAGE_TOO_LARGE")
            source.seek(0)
            source.thumbnail((1080, 1620))
            image = ImageOps.exif_transpose(source).convert("RGBA")
            image.thumbnail((1080, 1620))
            background = PillowImage.new("RGB", image.size, "white")
            background.paste(image, mask=image.getchannel("A"))
            for quality in (85, 65, 45):
                output = BytesIO()
                background.save(output, "JPEG", quality=quality)
                if output.tell() <= MAX_OUTPUT:
                    return output.getvalue()
            raise PluginError("IMAGE_TOO_LARGE")
    except (UnidentifiedImageError, OSError, ValueError, PillowImage.DecompressionBombError):
        raise PluginError("IMAGE_INVALID") from None


async def download_image(client, url, *, original=False):
    safe = original_image_url(url) if original else content_image_url(url)
    if safe is None:
        raise PluginError("IMAGE_URL_DENIED")
    async with client.stream("GET", safe) as response:
        response.raise_for_status()
        if (
            response.headers.get("content-length", "").isdigit()
            and int(response.headers["content-length"]) > MAX_DOWNLOAD
        ):
            raise PluginError("IMAGE_TOO_LARGE")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > MAX_DOWNLOAD:
                raise PluginError("IMAGE_TOO_LARGE")
            data.extend(chunk)
    return bytes(data)


class ImageDelivery:
    def __init__(self, context, io, logger, closing, settings=None):
        self.context, self.io, self.logger, self.closing = context, io, logger, closing
        self.receipts = OrderedDict()
        self.settings = settings

    async def send(self, event, item, indices, key, quality="original"):
        # The application holds its account/settings lock for the entire delivery.
        if quality not in {"original", "preview"}:
            raise PluginError("INVALID_ARGUMENT")
        if key in self.receipts:
            return dict(copy.deepcopy(self.receipts[key]), replayed=True)
        if not item["images"]:
            raise PluginError(
                "IMAGE_NOT_FOUND"
                if item.get("images_status") == "known"
                else "IMAGE_METADATA_UNKNOWN"
            )
        if any(i >= len(item["images"]) for i in indices):
            raise PluginError("INVALID_ARGUMENT")
        result = {
            "item_id": item["id"],
            "sent": 0,
            "sent_indices": [],
            "errors": [],
            "images_analyzed": False,
            "quality": quality,
            "sent_details": [],
        }
        self.receipts[key] = result
        while len(self.receipts) > 256:
            self.receipts.popitem(last=False)
        deadline = time.monotonic() + 40
        async with httpx.AsyncClient(
            headers=HEADERS, timeout=12, follow_redirects=False, trust_env=False
        ) as client:
            for index in indices:
                try:
                    if self.closing():
                        raise PluginError("PLUGIN_CLOSED")
                    async with asyncio.timeout(max(0, min(15, deadline - time.monotonic()))):
                        data = await download_image(
                            client, item["images"][index], original=quality == "original"
                        )
                        data = await self.io(
                            original_image if quality == "original" else resize_image, data
                        )
                    authorize_chat(event, self.context, self.settings)
                    if self.closing():
                        raise PluginError("PLUGIN_CLOSED")
                    adapter = event.get_platform_name()
                    caption = f"{item['title'][:120]} · {item['author']} · 图片 {index + 1}"
                    if adapter not in QQ_OFFICIAL:
                        caption += "\n" + item["url"]
                    else:
                        caption = re.sub(r"https?://\S+", "[链接见原文]", caption)
                    try:
                        await asyncio.wait_for(
                            event.send(qq_message(adapter, caption, data)),
                            15,
                        )
                    except Exception:
                        raise PluginError("IMAGE_DELIVERY_UNCERTAIN") from None
                    result["sent"] += 1
                    result["sent_indices"].append(index)
                    result["sent_details"].append(
                        {
                            "index": index,
                            "quality": quality,
                            "bytes": len(data),
                            "original_bytes_preserved": quality == "original",
                            "delivery_adapter": adapter,
                        }
                    )
                except Exception as exc:
                    log_failure(self.logger, "image_delivery", "image", exc)
                    error = (
                        exc
                        if isinstance(exc, PluginError)
                        else PluginError("IMAGE_DOWNLOAD_FAILED")
                    )
                    result["errors"].append(dict(index=index, **error.as_dict()))
                    if error.code in {"PLUGIN_CLOSED", "ACCESS_DENIED", "IMAGE_DELIVERY_UNCERTAIN"}:
                        break
        result["status"] = (
            "sent" if not result["errors"] else "partial" if result["sent"] else "failed"
        )
        return copy.deepcopy(result)
