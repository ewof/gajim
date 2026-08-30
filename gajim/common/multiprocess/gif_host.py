# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import logging
import threading
from urllib.parse import urlparse

from nbxmpp.util import utf8_decode

from gajim.common.multiprocess.http import http_request
from gajim.common.util.gif_hosts import is_gif_host_page
from gajim.common.util.gif_hosts import klipy_api_url
from gajim.common.util.gif_hosts import klipy_media_url_from_json
from gajim.common.util.gif_hosts import tenor_media_url_from_html

log = logging.getLogger("gajim.c.multiprocess.gif_host")

_HTML_DOWNLOAD_LIMIT = 256 * 1024
_JSON_DOWNLOAD_LIMIT = 64 * 1024


def resolve_gif_host_media_url(
    url: str,
    proxy: str | None,
    http2: bool,
) -> str | None:
    if not is_gif_host_page(url):
        return None

    host = urlparse(url).hostname
    if host is None:
        return None
    host = host.lower().removeprefix("www.")

    event = threading.Event()
    if host == "tenor.com":
        return _resolve_tenor(url, event, proxy, http2)
    if host == "klipy.com":
        return _resolve_klipy(url, event, proxy, http2)
    return None


def _resolve_tenor(
    url: str, event: threading.Event, proxy: str | None, http2: bool
) -> str | None:
    result = http_request(
        event=event,
        ft_id="gif-host",
        method="GET",
        url=url,
        timeout=10,
        max_download_size=_HTML_DOWNLOAD_LIMIT,
        proxy=proxy,
        http2=http2,
    )
    html, _ = utf8_decode(result.content)
    media_url = tenor_media_url_from_html(html)
    if media_url is None:
        log.info("No media URL in Tenor page: %s", url)
    return media_url


def _resolve_klipy(
    url: str, event: threading.Event, proxy: str | None, http2: bool
) -> str | None:
    api_url = klipy_api_url(url)
    if api_url is None:
        return None

    result = http_request(
        event=event,
        ft_id="gif-host",
        method="GET",
        url=api_url,
        timeout=10,
        max_download_size=_JSON_DOWNLOAD_LIMIT,
        proxy=proxy,
        http2=http2,
    )
    media_url = klipy_media_url_from_json(result.content)
    if media_url is None:
        log.info("No media URL in Klipy API for: %s", url)
    return media_url
