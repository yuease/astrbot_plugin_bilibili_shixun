"""Only trusted video artwork, never arbitrary image URLs or a generic proxy."""

from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit


def cover_url(value):
    if not isinstance(value, str) or not value or any(c in value for c in "\r\n\\"):
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        u = urlsplit(value)
        host = u.hostname or ""
        if (
            u.scheme not in ("http", "https")
            or u.username
            or u.password
            or u.port not in (None, 80, 443)
            or not any(host.endswith("." + d) for d in ("hdslb.com", "biliimg.com"))
        ):
            return None
        return urlunsplit(("https", host, u.path, u.query, ""))
    except ValueError:
        return None


class Artwork(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            a = dict(attrs)
            u = cover_url(a.get("src") or a.get("data-src"))
            # Bilibili video poster namespaces. Excludes face, emoji, badges and unrelated images.
            if u and any(
                part in urlsplit(u).path.lower() for part in ("/bfs/archive/", "/bfs/ugc/")
            ):
                self.images.append(u)


def extract_cover(raw, item):
    for child in item:
        tag = child.tag.rsplit("}", 1)[-1].lower()
        if tag in ("thumbnail", "enclosure"):
            if tag == "enclosure" and not child.get("type", "").startswith("image/"):
                continue
            u = cover_url(child.get("url"))
            if u and not any(
                x in urlsplit(u).path.lower() for x in ("/face/", "/garb/", "/emote/", "/emoji/")
            ):
                return u
    parser = Artwork()
    parser.feed(raw)
    return parser.images[0] if parser.images else None


def content_image_url(value):
    u = cover_url(value)
    if not u or any(
        x in urlsplit(u).path.lower() for x in ("/face/", "/emote/", "/emoji/", "/garb/")
    ):
        return None
    return (
        u
        if any(
            x in urlsplit(u).path.lower()
            for x in ("/bfs/new_dyn/", "/bfs/album/", "/bfs/archive/", "/bfs/ugc/", "/bfs/article/")
        )
        else None
    )


class ContentImages(HTMLParser):
    def __init__(self):
        super().__init__()
        self.images = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            a = dict(attrs)
            u = content_image_url(a.get("src") or a.get("data-src"))
            if u and u not in self.images:
                self.images.append(u)


def extract_images(raw):
    parser = ContentImages()
    parser.feed(raw)
    return parser.images[:12]
