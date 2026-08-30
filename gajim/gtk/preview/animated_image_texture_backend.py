# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import logging
from concurrent.futures import Future
from functools import partial
from pathlib import Path

from gi.repository import Gdk
from gi.repository import GdkPixbuf
from gi.repository import GLib
from gi.repository import GObject
from gi.repository.Gdk import Paintable

from gajim.common import app
from gajim.common.multiprocess.animated_image_frames import extract_rgba_frames

from gajim.gtk.preview.frame_paintable import FramePaintable
from gajim.gtk.util.classes import SignalManager

log = logging.getLogger("gajim.gtk.preview_animated_image_texture_backend")


class AnimatedImageTextureBackend(GObject.Object, SignalManager):
    """Animate GIFs by swapping Gdk.Textures. No GStreamer required."""

    __gtype_name__ = "AnimatedImageTextureBackend"
    __gsignals__ = {
        "pipeline-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "playback-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
    }

    def __init__(self, orig_path: Path, max_loops: int = 0) -> None:
        super().__init__()
        SignalManager.__init__(self)

        self._orig_path = orig_path
        self._max_loops = max_loops

        self._pipeline_is_setup = False
        self._pipeline_setup_failed = False
        self._creating_pipeline = False

        self._paintable = FramePaintable()
        self._timeout_id: int | None = None
        self._playing = False
        self._loop_counter = 0
        self._index = 0

        self._pixbuf_iter: GdkPixbuf.PixbufAnimationIter | None = None
        self._frames: list[tuple[Gdk.Texture, int]] = []

    @property
    def pipeline_is_setup(self) -> bool:
        return self._pipeline_is_setup

    @property
    def pipeline_setup_failed(self) -> bool:
        return self._pipeline_setup_failed

    @property
    def paintable(self) -> Paintable | None:
        return self._paintable

    def cleanup(self) -> None:
        self.pause()
        self._pixbuf_iter = None
        self._frames = []
        self._disconnect_all()
        app.check_finalize(self)

    def is_playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        if self._playing or not self._pipeline_is_setup:
            return
        self._playing = True
        self.emit("playback-changed", True)
        self._schedule_next()

    def pause(self) -> None:
        if self._timeout_id is not None:
            GLib.source_remove(self._timeout_id)
            self._timeout_id = None
        if not self._playing:
            return
        self._playing = False
        self._loop_counter = 0
        self._index = 0
        self._show_frame(0)
        self.emit("playback-changed", False)

    def setup_pipeline(self) -> None:
        if (
            self._creating_pipeline
            or self._pipeline_is_setup
            or self._pipeline_setup_failed
        ):
            return

        self._creating_pipeline = True
        if self._try_pixbuf_animation():
            return

        try:
            future = app.process_pool.submit(extract_rgba_frames, self._orig_path)
            future.add_done_callback(
                partial(GLib.idle_add, self._on_rgba_frames_finished)
            )
        except Exception:
            log.exception("Unable to decode animated image")
            self._pipeline_setup_failed = True
            self.emit("pipeline-changed", False)

    def _try_pixbuf_animation(self) -> bool:
        try:
            animation = GdkPixbuf.PixbufAnimation.new_from_file(str(self._orig_path))
        except GLib.Error as error:
            log.info("Pixbuf animation loader failed: %s", error)
            return False

        if animation.is_static_image():
            return False

        self._pixbuf_iter = animation.get_iter()
        self._show_pixbuf_frame()
        self._pipeline_is_setup = True
        self.emit("pipeline-changed", True)
        return True

    def _on_rgba_frames_finished(
        self, future: Future[tuple[int, int, list[tuple[bytes, int]]]]
    ) -> bool:
        try:
            width, height, raw_frames = future.result()
        except Exception:
            log.exception("Extracting frames failed for %s", self._orig_path)
            self._pipeline_setup_failed = True
            self.emit("pipeline-changed", False)
            return GLib.SOURCE_REMOVE

        if not raw_frames:
            self._pipeline_setup_failed = True
            self.emit("pipeline-changed", False)
            return GLib.SOURCE_REMOVE

        stride = width * 4
        for data, duration in raw_frames:
            texture = Gdk.MemoryTexture.new(
                width,
                height,
                Gdk.MemoryFormat.R8G8B8A8,
                GLib.Bytes.new(data),
                stride,
            )
            self._frames.append((texture, duration))

        self._show_frame(0)
        self._pipeline_is_setup = True
        self.emit("pipeline-changed", True)
        return GLib.SOURCE_REMOVE

    def _schedule_next(self) -> None:
        if not self._playing:
            return
        delay = self._current_delay()
        self._timeout_id = GLib.timeout_add(delay, self._advance)

    def _current_delay(self) -> int:
        if self._pixbuf_iter is not None:
            delay = self._pixbuf_iter.get_delay_time()
            return 100 if delay <= 0 else delay
        if self._frames:
            return self._frames[self._index][1]
        return 100

    def _advance(self) -> bool:
        self._timeout_id = None
        if not self._playing:
            return GLib.SOURCE_REMOVE

        if self._pixbuf_iter is not None:
            self._pixbuf_iter.advance()
            self._show_pixbuf_frame()
            self._schedule_next()
            return GLib.SOURCE_REMOVE

        if not self._frames:
            return GLib.SOURCE_REMOVE

        self._index = (self._index + 1) % len(self._frames)
        self._show_frame(self._index)
        if self._index == 0 and self._max_loops > 0:
            self._loop_counter += 1
            if self._loop_counter >= self._max_loops:
                self.pause()
                return GLib.SOURCE_REMOVE
        self._schedule_next()
        return GLib.SOURCE_REMOVE

    def _show_pixbuf_frame(self) -> None:
        assert self._pixbuf_iter is not None
        pixbuf = self._pixbuf_iter.get_pixbuf()
        if pixbuf is None:
            return
        self._paintable.set_texture(Gdk.Texture.new_for_pixbuf(pixbuf))

    def _show_frame(self, index: int) -> None:
        if not self._frames:
            return
        texture, _duration = self._frames[index]
        self._paintable.set_texture(texture)
