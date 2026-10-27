"""Normalize Bilibili JSON without inferring video/image contents."""

import hashlib
import re
from datetime import datetime, timezone

from .attachments import dynamic_attachments, readable_text
from .schemas import Article
from .source_types import SourceBatch


def text(value, limit=20000):
    return readable_text(value, limit)


def published(value):
    try:
        return datetime.fromtimestamp(int(value), timezone.utc) if int(value) > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def duration(value):
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, str) and re.fullmatch(r"\d+:\d{2}(?::\d{2})?", value):
        pieces = [int(part) for part in value.split(":")]
        if pieces[-1] < 60 and (len(pieces) == 2 or pieces[-2] < 60):
            return sum(part * 60**index for index, part in enumerate(reversed(pieces))) or None
    return None


def video_item(value):
    bvid = value.get("bvid")
    aid = str(value.get("aid", value.get("id", "")))
    if isinstance(bvid, str) and re.fullmatch(r"BV[A-Za-z0-9]+", bvid):
        identity = bvid
    elif re.fullmatch(r"\d+", aid):
        identity = "av" + aid
    else:
        return None
    stamp = published(value.get("pubdate", value.get("created")))
    author = value.get("owner") if isinstance(value.get("owner"), dict) else {}
    body = text(value.get("description", value.get("desc", "")))
    tags = value.get("tag", "")
    return Article(
        id=hashlib.sha256(("bilibili:" + identity).encode()).hexdigest()[:20],
        title=text(value.get("title"), 1000),
        url="https://www.bilibili.com/video/" + identity,
        author=text(value.get("author", author.get("name")), 100) or "未知",
        author_uid=str(value.get("mid", author.get("mid", ""))) or None,
        published=stamp.isoformat() if stamp else "未知",
        published_at=stamp.isoformat() if stamp else None,
        body_text=body,
        excerpt=body[:1200],
        duration_seconds=duration(value.get("duration", value.get("length"))),
        tags=[text(tag, 100) for tag in tags.split(",") if tag.strip()]
        if isinstance(tags, str)
        else [],
        source_ids=["bilibili:" + identity] + (["bilibili:av" + aid] if aid.isdigit() else []),
        cover_url=value.get("pic", value.get("cover")),
        content_type="video",
        images_status="known",
        attachments_status="known",
        attachments=[
            {
                "type": "video",
                "title": text(value.get("title"), 300),
                "url": "https://www.bilibili.com/video/" + identity,
                "details_status": "complete",
            }
        ],
    )


def video_batch(items):
    result = [article for item in items if isinstance(item, dict) and (article := video_item(item))]
    return SourceBatch(result, raw_ids=[item.id for item in result])


def dynamic_batch(data, *, videos_only=False):
    result, raw_ids, stamps = [], [], []
    for card in data.get("items", []):
        if not isinstance(card, dict):
            continue
        did = str(card.get("id_str", ""))
        if not re.fullmatch(r"\d{1,30}", did):
            continue
        raw_ids.append(did)
        modules = card.get("modules", {})
        author = modules.get("module_author", {})
        stamp = published(author.get("pub_ts"))
        if stamp:
            stamps.append(stamp)
        forwarded = isinstance(card.get("orig"), dict)
        original = card["orig"] if forwarded else card
        content = original.get("modules", {}).get("module_dynamic", {})
        original_author = original.get("modules", {}).get("module_author", {})
        major = content.get("major") or {}
        archive = major.get("archive")
        outer_text = text((modules.get("module_dynamic", {}).get("desc") or {}).get("text"))
        original_text = text((content.get("desc") or {}).get("text"))
        kind = original.get("type")
        article = None
        if isinstance(archive, dict):
            article = video_item(
                dict(
                    archive,
                    pic=archive.get("cover"),
                    description=archive.get("desc", ""),
                    duration=archive.get("duration_text"),
                    owner=original_author,
                )
            )
            if article is not None:
                article = article.model_copy(
                    update={
                        "author": text(original_author.get("name"), 100) or "未知",
                        "author_uid": str(original_author.get("mid", "")) or None,
                    }
                )
        elif videos_only:
            continue
        else:
            opus = major.get("opus") or {}
            draw = major.get("draw") or {}
            body = original_text or text((opus.get("summary") or {}).get("text"))
            pictures = [p.get("url") for p in opus.get("pics", []) if isinstance(p, dict)]
            pictures += [p.get("src") for p in draw.get("items", []) if isinstance(p, dict)]
            article_card = major.get("article") or {}
            types = {
                "DYNAMIC_TYPE_WORD": "text",
                "DYNAMIC_TYPE_DRAW": "image",
                "DYNAMIC_TYPE_ARTICLE": "article",
                "DYNAMIC_TYPE_MUSIC": "audio",
                "DYNAMIC_TYPE_LIVE_RCMD": "live",
            }
            article = Article(
                id=hashlib.sha256(("bilibili:dynamic:" + did).encode()).hexdigest()[:20],
                title=text(opus.get("title") or article_card.get("title") or body[:80], 1000)
                or "动态（文字缺失）",
                url="https://t.bilibili.com/" + did,
                author=text(original_author.get("name"), 100) or "未知",
                author_uid=str(original_author.get("mid", "")) or None,
                body_text=body,
                excerpt=body[:1200],
                images=pictures,
                content_type=types.get(kind, "unknown"),
                text_available=bool(body),
            )
        if article is None:
            continue
        attachments, attachment_status, images_status = dynamic_attachments(
            content, kind, article.images
        )
        contexts = []
        outer = dict(
            id=did,
            url="https://t.bilibili.com/" + did,
            author=text(author.get("name"), 100),
            author_uid=str(author.get("mid", "")) or None,
            text=outer_text,
            published_at=stamp.isoformat() if stamp else None,
        )
        if outer_text:
            contexts.append(outer)
        if forwarded:
            original_id = str(original.get("id_str", ""))
            original_stamp = published(original_author.get("pub_ts"))
            if original_text:
                contexts.append(
                    dict(
                        id=original_id,
                        url="https://t.bilibili.com/" + original_id,
                        author=text(original_author.get("name"), 100),
                        author_uid=str(original_author.get("mid", "")) or None,
                        text=original_text,
                        published_at=original_stamp.isoformat() if original_stamp else None,
                    )
                )
        # Publication in the followed feed belongs to this contribution. The
        # original author remains separate from the person forwarding it.
        update = {
            "is_dynamic": True,
            "dynamic_id": did,
            "dynamic_contexts": contexts,
            "repost_text": outer_text if forwarded else "",
            "published": stamp.isoformat() if stamp else "未知",
            "published_at": stamp.isoformat() if stamp else None,
            "original_author_uid": str(original_author.get("mid", "")) or None,
            "activity_type": "repost" if forwarded else "original",
            "activity_url": "https://t.bilibili.com/" + did,
            "forwarded_by": text(author.get("name"), 100) if forwarded else None,
            "original_published_at": original_stamp.isoformat()
            if forwarded and original_stamp
            else None,
            "attachments": attachments,
            "attachments_status": attachment_status,
            "images_status": images_status,
        }
        if forwarded and article.content_type != "video" and re.fullmatch(r"\d{1,30}", original_id):
            update["url"] = "https://t.bilibili.com/" + original_id
        # The forwarder's attribution belongs to its own context; do not turn
        # original text or a video description into the forwarder's statement.
        result.append(Article.model_validate(dict(article.model_dump(), **update)))
    return SourceBatch(
        result,
        raw_ids=raw_ids,
        cursor=data.get("offset"),
        has_more=data.get("has_more", False),
        times=stamps,
    )
