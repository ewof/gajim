# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-or-later

import tempfile
import unittest
from pathlib import Path

from gajim.common import app
from gajim.common.util.gif_hosts import is_gif_host_page
from gajim.common.util.gif_hosts import klipy_api_url
from gajim.common.util.gif_hosts import klipy_media_url_from_json
from gajim.common.util.gif_hosts import tenor_media_url_from_html
from gajim.common.util.preview import get_preview_data
from gajim.common.util.preview import get_size_and_mime_type
from gajim.common.util.preview import is_video
from gajim.common.util.preview import iter_direct_media_urls
from gajim.common.util.preview import UrlPreview

TENOR_HTML = """
<html><head>
<meta class="dynamic" property="og:image" content="https://media1.tenor.com/m/i9U5IplOa2kAAAAC/genthru-gensuru.gif">
<meta class="dynamic" property="og:video" content="https://media.tenor.com/i9U5IplOa2kAAAPo/genthru-gensuru.mp4">
<meta class="dynamic" property="og:video:type" content="video/mp4">
</head><body></body></html>
"""

KLIPY_JSON = """
{
  "result": true,
  "data": {
    "slug": "gakumas-hiro",
    "file": {
      "hd": {
        "gif": {"url": "https://static.klipy.com/hd.gif", "size": 4000000},
        "mp4": {"url": "https://static.klipy.com/hd.mp4", "size": 400000}
      }
    }
  }
}
"""


class TestPreview(unittest.TestCase):
    def test_media_url_requires_setting(self) -> None:
        url = "https://example.com/clip.mp4"
        app.settings.set("preview_allow_all_images", False)
        self.assertIsNone(get_preview_data(url, []))

        app.settings.set("preview_allow_all_images", True)
        preview = get_preview_data(url, [])
        self.assertIsInstance(preview, UrlPreview)
        assert isinstance(preview, UrlPreview)
        self.assertEqual(preview.uri, url)
        self.assertTrue(preview.mime_type.startswith("video/"))

    def test_mkv_is_video(self) -> None:
        self.assertTrue(is_video("video/x-matroska"))
        self.assertTrue(is_video("video/matroska"))
        app.settings.set("preview_allow_all_images", True)
        url = "https://example.com/clip.mkv"
        preview = get_preview_data(url, [])
        assert isinstance(preview, UrlPreview)
        self.assertTrue(is_video(preview.mime_type))
        self.assertIn(url, iter_direct_media_urls(f"see {url}"))

    def test_gif_url_preview(self) -> None:
        url = "https://example.com/funny.gif"
        app.settings.set("preview_allow_all_images", True)
        preview = get_preview_data(url, [])
        self.assertIsInstance(preview, UrlPreview)
        assert isinstance(preview, UrlPreview)
        self.assertTrue(preview.mime_type.startswith("image/"))

    def test_iter_direct_media_urls_in_text(self) -> None:
        text = (
            "watch this https://example.com/clip.mp4 and "
            "https://example.com/page and https://example.com/pic.gif"
        )
        urls = iter_direct_media_urls(text)
        self.assertEqual(
            urls,
            [
                "https://example.com/clip.mp4",
                "https://example.com/pic.gif",
            ],
        )

    def test_webpage_url_not_media(self) -> None:
        app.settings.set("preview_allow_all_images", True)
        self.assertIsNone(get_preview_data("https://example.com/article", []))

    def test_gif_host_page_detection(self) -> None:
        self.assertTrue(
            is_gif_host_page(
                "https://tenor.com/view/genthru-gensuru-greed-island-hxh1999-hxh-gif-23304089"
            )
        )
        self.assertTrue(
            is_gif_host_page("https://www.tenor.com/view/something-gif-1")
        )
        self.assertTrue(is_gif_host_page("https://klipy.com/gifs/gakumas-hiro"))
        self.assertTrue(is_gif_host_page("https://www.klipy.com/gifs/gakumas-hiro"))
        self.assertFalse(is_gif_host_page("https://media.tenor.com/abc/file.mp4"))
        self.assertFalse(is_gif_host_page("https://example.com/view/gif-1"))
        self.assertFalse(is_gif_host_page("https://klipy.com/about"))

    def test_tenor_media_url_from_html(self) -> None:
        self.assertEqual(
            tenor_media_url_from_html(TENOR_HTML),
            "https://media.tenor.com/i9U5IplOa2kAAAPo/genthru-gensuru.mp4",
        )

    def test_klipy_media_url_from_json(self) -> None:
        self.assertEqual(
            klipy_api_url("https://klipy.com/gifs/gakumas-hiro"),
            "https://api.klipy.com/api/v1/gifs/gakumas-hiro",
        )
        self.assertEqual(
            klipy_media_url_from_json(KLIPY_JSON),
            "https://static.klipy.com/hd.mp4",
        )

    def test_sniff_mp4_without_extension(self) -> None:
        # ISO BMFF ftyp box so Gio can sniff video/mp4 with no filename suffix.
        payload = (
            b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 256
        )
        with tempfile.NamedTemporaryFile(suffix="", delete=False) as handle:
            handle.write(payload)
            path = Path(handle.name)
        try:
            mime_type, size = get_size_and_mime_type(path)
        finally:
            path.unlink()
        self.assertEqual(size, len(payload))
        self.assertEqual(mime_type, "video/mp4")

    def test_http_media_is_from_link(self) -> None:
        app.settings.set("preview_allow_all_images", True)
        preview = get_preview_data("https://example.com/pic.gif", [])
        assert isinstance(preview, UrlPreview)
        self.assertTrue(preview.from_link)

    def test_tenor_page_preview_requires_setting(self) -> None:
        url = (
            "https://tenor.com/view/genthru-gensuru-greed-island-"
            "hxh1999-hxh-gif-23304089"
        )
        app.settings.set("preview_allow_all_images", False)
        self.assertIsNone(get_preview_data(url, []))

        app.settings.set("preview_allow_all_images", True)
        preview = get_preview_data(url, [])
        self.assertIsInstance(preview, UrlPreview)
        assert isinstance(preview, UrlPreview)
        self.assertEqual(preview.uri, url)

    def test_iter_includes_gif_host_pages(self) -> None:
        text = (
            "lol https://tenor.com/view/genthru-gif-23304089 and "
            "https://klipy.com/gifs/gakumas-hiro thanks"
        )
        self.assertEqual(
            iter_direct_media_urls(text),
            [
                "https://tenor.com/view/genthru-gif-23304089",
                "https://klipy.com/gifs/gakumas-hiro",
            ],
        )


if __name__ == "__main__":
    unittest.main()
