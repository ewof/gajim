# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from gi.repository import Gdk
from gi.repository import GObject
from gi.repository import Graphene


class FramePaintable(GObject.Object, Gdk.Paintable):
    """A Gdk.Paintable that can be updated with successive Gdk.Textures."""

    __gtype_name__ = "FramePaintable"

    def __init__(self) -> None:
        GObject.Object.__init__(self)
        self._texture: Gdk.Texture | None = None
        self._width = 1
        self._height = 1

    def set_texture(self, texture: Gdk.Texture) -> None:
        size_changed = (
            self._texture is None
            or texture.get_width() != self._width
            or texture.get_height() != self._height
        )
        self._texture = texture
        self._width = texture.get_width()
        self._height = texture.get_height()
        self.invalidate_contents()
        if size_changed:
            self.invalidate_size()

    def do_snapshot(
        self, snapshot: Gdk.Snapshot, width: float, height: float
    ) -> None:
        if self._texture is None:
            return
        snapshot.append_texture(self._texture, Graphene.Rect().init(0, 0, width, height))

    def do_get_intrinsic_width(self) -> int:
        return self._width

    def do_get_intrinsic_height(self) -> int:
        return self._height
