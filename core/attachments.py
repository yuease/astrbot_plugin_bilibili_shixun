"""Bounded attachment metadata from Bilibili cards; absent parsing is never 'none'."""

import re
from html import unescape
from urllib.parse import urlsplit, urlunsplit

from .covers import content_image_url


def readable_text(value, limit=20000):
    value = unescape(re.sub(r"<[^>]*>", "", value if isinstance(value, str) else ""))
    return re.sub(r"\[UPOWER_\d+_([^\]\n]{1,40})\]", r":\1:", value).strip()[:limit]


def attachment_url(value):
    if not isinstance(value, str):
        return None
    try:
        u = urlsplit("https:" + value if value.startswith("//") else value)
        host = u.hostname or ""
        if (
            u.scheme not in ("http", "https")
            or u.username
            or u.password
            or u.port not in (None, 80, 443)
            or not (host == "bilibili.com" or host.endswith(".bilibili.com") or host == "b23.tv")
        ):
            return None
        return urlunsplit(("https", host, u.path, u.query, ""))[:2048]
    except ValueError:
        return None


def poll_attachment(value):
    raw = value.get("options")
    values = list(raw.values()) if isinstance(raw, dict) else raw if isinstance(raw, list) else []
    options = []
    truncated = len(values) > 20
    for option in values[:20]:
        if isinstance(option, dict):
            full_text = readable_text(
                option.get("desc", option.get("text", option.get("title"))), 1000
            )
            text = full_text[:300]
            truncated = truncated or len(full_text) > 300
            if text:
                options.append({"text": text})
    identity = str(value.get("vote_id", ""))
    question = readable_text(value.get("title") or value.get("desc"), 1000)
    truncated = truncated or len(question) > 500
    return {
        "type": "poll",
        "id": identity if re.fullmatch(r"\d{1,30}", identity) else None,
        "question": question[:500],
        "options": options,
        "details_status": "complete"
        if question and options and len(options) == len(values) and not truncated
        else "partial",
    }


def dynamic_attachments(content, kind, images):
    major = content.get("major") or {}
    if not isinstance(major, dict):
        major = {"type": "unsupported"}
    attachments = []
    known_major = {
        "MAJOR_TYPE_NONE",
        "MAJOR_TYPE_DRAW",
        "MAJOR_TYPE_OPUS",
        "MAJOR_TYPE_ARCHIVE",
        "MAJOR_TYPE_ARTICLE",
        "MAJOR_TYPE_MUSIC",
        "MAJOR_TYPE_LIVE_RCMD",
    }
    recognized = (
        not major
        or major.get("type") in known_major
        or any(
            name in major for name in ("draw", "opus", "archive", "article", "music", "live_rcmd")
        )
    )
    picture_values = []
    image_known = kind in {
        "DYNAMIC_TYPE_WORD",
        "DYNAMIC_TYPE_AV",
        "DYNAMIC_TYPE_ARTICLE",
        "DYNAMIC_TYPE_MUSIC",
        "DYNAMIC_TYPE_LIVE_RCMD",
    }
    for name, field, url_key in (("opus", "pics", "url"), ("draw", "items", "src")):
        if isinstance(major.get(name), dict):
            if isinstance(major[name].get(field), list):
                image_known = True
                picture_values += [
                    p.get(url_key) if isinstance(p, dict) else None for p in major[name][field]
                ]
            else:
                image_known = False
    images_status = "known" if image_known else "unknown"
    if images and not image_known:
        images_status = "partial"
    if picture_values and (
        len({v for v in picture_values if isinstance(v, str)}) != len(images)
        or any(not content_image_url(v) for v in picture_values)
    ):
        images_status = "partial"
    if images:
        attachments.append({"type": "image", "count": len(images), "details_status": "complete"})
    for key, type_name in (
        ("archive", "video"),
        ("article", "article"),
        ("music", "audio"),
        ("live_rcmd", "live"),
    ):
        value = major.get(key)
        if isinstance(value, dict):
            if key == "live_rcmd" and isinstance(value.get("content"), dict):
                value = value["content"].get("live_play_info", value["content"])
            attachments.append(
                {
                    "type": type_name,
                    "title": readable_text(value.get("title"), 300),
                    "url": attachment_url(value.get("jump_url")),
                    "details_status": "complete",
                }
            )
    additional = content.get("additional")
    if (
        isinstance(additional, dict)
        and additional
        and additional.get("type") != "ADDITIONAL_TYPE_NONE"
    ):
        raw_type = additional.get("type") or (
            "ADDITIONAL_TYPE_VOTE" if isinstance(additional.get("vote"), dict) else "UNTYPED"
        )
        if raw_type == "ADDITIONAL_TYPE_VOTE" and isinstance(additional.get("vote"), dict):
            attachments.append(poll_attachment(additional["vote"]))
        else:
            mapping = {
                "ADDITIONAL_TYPE_RESERVE": ("reserve", "reservation"),
                "ADDITIONAL_TYPE_UGC": ("ugc", "video"),
                "ADDITIONAL_TYPE_COMMON": ("common", "link"),
            }
            key, type_name = mapping.get(raw_type, ("", "unknown"))
            value = additional.get(key) or {}
            if not isinstance(value, dict):
                value = {}
            attachments.append(
                {
                    "type": type_name,
                    "title": readable_text(value.get("title"), 300),
                    "url": attachment_url(value.get("jump_url")),
                    "source_type": readable_text(raw_type, 80),
                    "details_status": "partial" if type_name != "unknown" else "unknown",
                }
            )
    elif additional and not isinstance(additional, dict):
        attachments.append(
            {"type": "unknown", "source_type": "malformed_additional", "details_status": "unknown"}
        )
    if not recognized:
        images_status = "partial" if images else "unknown"
        attachments.append(
            {
                "type": "unknown",
                "source_type": readable_text(major.get("type"), 80),
                "details_status": "unknown",
            }
        )
    status = (
        "known"
        if recognized
        and images_status == "known"
        and all(a["details_status"] == "complete" for a in attachments)
        else "partial"
    )
    return attachments[:8], status, images_status


def attachment_summary(article):
    types = list(
        dict.fromkeys(
            [a.get("type", "unknown") for a in article.get("attachments", [])]
            + (["image"] if article.get("images") else [])
        )
    )
    status = article.get("attachments_status", "unknown")
    return {
        "image_count": len(article.get("images", [])),
        "images_status": article.get("images_status", "unknown"),
        "attachment_types": types,
        "attachments_status": status,
        "has_attachments": True if types else False if status == "known" else None,
    }
