# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

log = logging.getLogger("gajim.c.util.gif_hosts")

_TENOR_HOSTS = {"tenor.com"}
_KLIPY_HOSTS = {"klipy.com"}
_KLIPY_PATH_RX = re.compile(r"^/(gifs|gif|stickers|clips)/([^/]+)/?$")
_KLIPY_API_KIND = {
    "gif": "gifs",
    "gifs": "gifs",
    "stickers": "stickers",
    "clips": "clips",
}
# Tenor/Klipy serve the same animation as MP4; treat that as the GIF.
_MEDIA_FORMAT_ORDER = ("mp4", "webm", "gif", "webp")
_SIZE_ORDER = ("hd", "md", "sm", "xs")


def is_gif_host_page(uri: str) -> bool:
    """True for Tenor/Klipy *pages* that wrap a GIF, not already-direct files."""
    try:
        parts = urlparse(uri)
    except Exception:
        return False

    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False

    host = parts.hostname.lower().removeprefix("www.")
    if host in _TENOR_HOSTS:
        return True
    if host in _KLIPY_HOSTS:
        return _KLIPY_PATH_RX.fullmatch(parts.path) is not None
    return False


def klipy_api_url(uri: str) -> str | None:
    try:
        parts = urlparse(uri)
    except Exception:
        return None

    if not parts.hostname:
        return None

    host = parts.hostname.lower().removeprefix("www.")
    if host not in _KLIPY_HOSTS:
        return None

    match = _KLIPY_PATH_RX.fullmatch(parts.path)
    if match is None:
        return None

    kind = _KLIPY_API_KIND.get(match.group(1))
    slug = match.group(2)
    if kind is None:
        return None
    return f"https://api.klipy.com/api/v1/{kind}/{slug}"


class _TenorMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.video: str | None = None
        self.stream: str | None = None
        self.image: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return

        attributes = {key.lower(): value for key, value in attrs if value}
        prop = attributes.get("property") or attributes.get("name") or ""
        content = attributes.get("content")
        if not content:
            return

        if prop in ("og:video", "og:video:secure_url"):
            if self.video is None:
                self.video = content
        elif prop == "twitter:player:stream":
            self.stream = content
        elif prop in ("og:image", "og:image:url", "twitter:image"):
            if self.image is None:
                self.image = content

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            raise _StopParse


class _StopParse(Exception):
    pass


def tenor_media_url_from_html(html: str) -> str | None:
    parser = _TenorMetaParser()
    try:
        parser.feed(html)
    except _StopParse:
        pass

    for candidate in (parser.video, parser.stream, parser.image):
        if candidate and _looks_like_video_url(candidate):
            return candidate
    for candidate in (parser.image, parser.video, parser.stream):
        if candidate and _looks_like_media_url(candidate):
            return candidate
    return None


def klipy_media_url_from_payload(payload: object) -> str | None:
    if not isinstance(payload, dict) or not payload.get("result"):
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    files = data.get("file") or data.get("files")
    if not isinstance(files, dict):
        return None

    for size in _SIZE_ORDER:
        variants = files.get(size)
        if not isinstance(variants, dict):
            continue
        for fmt in _MEDIA_FORMAT_ORDER:
            entry = variants.get(fmt)
            if not isinstance(entry, dict):
                continue
            url = entry.get("url")
            if isinstance(url, str) and url:
                return url
    return None


def klipy_media_url_from_json(raw: str | bytes) -> str | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Invalid Klipy JSON")
        return None
    return klipy_media_url_from_payload(payload)


def _looks_like_video_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".mp4", ".webm"))


def _looks_like_media_url(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith((".mp4", ".webm", ".gif", ".webp", ".jpg", ".jpeg", ".png"))
