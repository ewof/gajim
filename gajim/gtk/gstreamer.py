# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import typing

import logging

from gi.repository import Gdk
from gi.repository import GLib

from gajim.gtk.preview.frame_paintable import FramePaintable

try:
    from gi.repository import Gst
except Exception:
    if typing.TYPE_CHECKING:
        from gi.repository import Gst

log = logging.getLogger("gajim.gtk.gstreamer")


def has_gtk4_paintable_sink() -> bool:
    try:
        if not Gst.is_initialized():
            Gst.init(None)
        return Gst.ElementFactory.find("gtk4paintablesink") is not None
    except Exception:
        return False


def create_video_elements() -> tuple[Gst.Element, Gdk.Paintable, str] | None:
    gtk4 = _create_gtk4_paintable_elements()
    if gtk4 is not None:
        return gtk4
    return _create_appsink_elements()


def _create_gtk4_paintable_elements() -> (
    tuple[Gst.Element, Gdk.Paintable, str] | None
):
    gtksink = Gst.ElementFactory.make("gtk4paintablesink", None)
    if gtksink is None:
        return None

    paintable = gtksink.get_property("paintable")
    if paintable.props.gl_context is not None:
        sink = Gst.ElementFactory.make("glsinkbin", None)
        if sink is None:
            return None

        log.info("Using GL gtk4paintablesink")
        sink.set_property("sink", gtksink)
        name = "gtkglsink"

    else:
        sink = Gst.Bin.new()
        convert = Gst.ElementFactory.make("videoconvert", None)
        if convert is None:
            return None

        sink.add(convert)
        sink.add(gtksink)
        convert.link(gtksink)

        pad = convert.get_static_pad("sink")
        if pad is None:
            return None

        log.info("Using software gtk4paintablesink")
        sink.add_pad(Gst.GhostPad.new("sink", pad))

        name = "gtksink"

    return sink, paintable, name


class _AppsinkOutput:
    """Keeps the appsink → paintable bridge alive for the Gst.Bin lifetime."""

    def __init__(self) -> None:
        self.paintable = FramePaintable()
        self._pending: tuple[bytes, int, int, int] | None = None
        self._idle_id: int | None = None

    def on_new_sample(self, appsink: Gst.Element) -> Gst.FlowReturn:
        sample = appsink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK

        buf = sample.get_buffer()
        caps = sample.get_caps()
        if buf is None or caps is None:
            return Gst.FlowReturn.OK

        structure = caps.get_structure(0)
        ok_w, width = structure.get_int("width")
        ok_h, height = structure.get_int("height")
        if not ok_w or not ok_h:
            return Gst.FlowReturn.OK

        success, mapped = buf.map(Gst.MapFlags.READ)
        if not success:
            return Gst.FlowReturn.OK

        data = bytes(mapped.data)
        buf.unmap(mapped)
        stride = width * 4
        self._pending = (data, width, height, stride)
        if self._idle_id is None:
            self._idle_id = GLib.idle_add(self._flush_frame)
        return Gst.FlowReturn.OK

    def _flush_frame(self) -> bool:
        self._idle_id = None
        pending = self._pending
        if pending is None:
            return GLib.SOURCE_REMOVE
        self._pending = None
        data, width, height, stride = pending
        texture = Gdk.MemoryTexture.new(
            width,
            height,
            Gdk.MemoryFormat.B8G8R8A8,
            GLib.Bytes.new(data),
            stride,
        )
        self.paintable.set_texture(texture)
        return GLib.SOURCE_REMOVE


def _create_appsink_elements() -> tuple[Gst.Element, Gdk.Paintable, str] | None:
    convert = Gst.ElementFactory.make("videoconvert", None)
    capsfilter = Gst.ElementFactory.make("capsfilter", None)
    appsink = Gst.ElementFactory.make("appsink", "sink")
    if convert is None or capsfilter is None or appsink is None:
        log.error("appsink video fallback is missing GStreamer elements")
        return None

    caps = Gst.Caps.from_string("video/x-raw,format=BGRA")
    capsfilter.set_property("caps", caps)
    appsink.set_property("caps", caps)
    appsink.set_property("emit-signals", True)
    appsink.set_property("sync", True)
    appsink.set_property("max-buffers", 2)
    appsink.set_property("drop", True)

    helper = _AppsinkOutput()
    appsink.connect("new-sample", helper.on_new_sample)

    sink = Gst.Bin.new("gajim-appsink-bin")
    sink.add(convert)
    sink.add(capsfilter)
    sink.add(appsink)
    convert.link(capsfilter)
    capsfilter.link(appsink)

    pad = convert.get_static_pad("sink")
    if pad is None:
        return None
    sink.add_pad(Gst.GhostPad.new("sink", pad))

    # Keep the Python helper alive as long as the bin is
    sink._gajim_appsink_helper = helper  # type: ignore[attr-defined]
    log.info("Using appsink video fallback (gtk4paintablesink not installed)")
    return sink, helper.paintable, "appsink"
