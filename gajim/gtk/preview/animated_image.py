# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import Any

import logging
from pathlib import Path

from gi.repository import Gdk
from gi.repository import GObject
from gi.repository import Gtk

from gajim.common import app

from gajim.gtk.util.classes import SignalManager

log = logging.getLogger("gajim.gtk.animated_image")


class AnimatedImage(Gtk.Box, SignalManager):
    __gtype_name__ = "AnimatedImage"
    __gsignals__ = {
        "error": (GObject.SignalFlags.RUN_LAST, None, ()),
        "clicked": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(
        self,
        thumbnail_path: Path,
        orig_path: Path,
        backends: list[type],
        max_loops: int = 0,
        enlarge_on_click: bool = True,
    ) -> None:
        Gtk.Box.__init__(self)
        SignalManager.__init__(self)

        self._orig_path = orig_path
        self._backend_classes = list(backends)
        self._max_loops = max_loops
        self._enlarge_on_click = enlarge_on_click

        self._picture = Gtk.Picture(content_fit=Gtk.ContentFit.CONTAIN)
        self._picture.set_filename(str(thumbnail_path))
        self._picture.add_css_class("preview-image")

        self._static_paintable = self._picture.get_paintable()
        self._animated_paintable = None

        self._icon = Gtk.Image.new_from_icon_name("inter-play-gif")
        self._icon.set_pixel_size(40 * app.window.get_scale_factor())
        self._icon.set_halign(Gtk.Align.CENTER)
        self._icon.set_valign(Gtk.Align.CENTER)
        self._icon.set_can_target(False)
        self._icon.set_visible(False)

        self._overlay = Gtk.Overlay(hexpand=True, vexpand=True)
        self._overlay.set_child(self._picture)
        self._overlay.add_overlay(self._icon)
        self.append(self._overlay)

        self._controller = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        self._connect(self._controller, "pressed", self._on_click)
        self.add_controller(self._controller)

        first = self._backend_classes.pop(0)
        self._backend = first(self._orig_path, max_loops=max_loops)
        self._connect(self._backend, "pipeline-changed", self._on_pipeline_changed)
        self._connect(self._backend, "playback-changed", self._on_playback_changed)
        self._backend.setup_pipeline()

    def do_unroot(self) -> None:
        Gtk.Box.do_unroot(self)

        self._backend.cleanup()
        self._static_paintable = None
        self._animated_paintable = None

        self._disconnect_all()
        app.check_finalize(self)

    def _on_pipeline_changed(self, _backend: Any, success: bool) -> None:
        if not success:
            if self._try_fallback():
                return
            self.emit("error")
            return

        paintable = self._backend.paintable
        if paintable is None:
            log.warning("We got no paintable")
            if self._try_fallback():
                return
            self.emit("error")
            return

        self._animated_paintable = paintable
        log.debug("Start playback...")
        self._backend.play()

    def _on_playback_changed(self, _backend: Any, is_playing: bool) -> None:
        if is_playing and self._animated_paintable is not None:
            self._picture.set_paintable(self._animated_paintable)

        self._icon.set_visible(not is_playing)

    def _try_fallback(self) -> bool:
        if not self._backend_classes:
            return False

        next_backend = self._backend_classes.pop(0)
        self._disconnect_object(self._backend)
        self._backend.cleanup()
        self._backend = next_backend(self._orig_path, max_loops=self._max_loops)
        self._connect(self._backend, "pipeline-changed", self._on_pipeline_changed)
        self._connect(self._backend, "playback-changed", self._on_playback_changed)
        log.info("Retrying animated image with %s", next_backend.__name__)
        self._backend.setup_pipeline()
        return True

    def _on_click(
        self, gesture_click: Gtk.GestureClick, _n_press: int, _x: float, _y: float
    ) -> None:
        gesture_click.set_state(Gtk.EventSequenceState.CLAIMED)

        if self._backend.pipeline_setup_failed:
            if self._try_fallback():
                return
            self.emit("error")
            return

        if self._enlarge_on_click:
            self.emit("clicked")
            return

        if not self._backend.pipeline_is_setup:
            self._backend.setup_pipeline()
            return

        self.toggle_playback()

    def toggle_playback(self) -> None:
        if self._backend.is_playing():
            self._backend.pause()
        else:
            self._backend.play()
