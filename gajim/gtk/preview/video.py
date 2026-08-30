# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import typing

import logging
from concurrent.futures import Future
from functools import partial
from pathlib import Path

from gi.repository import Adw
from gi.repository import Gdk
from gi.repository import GLib
from gi.repository import GObject
from gi.repository import Gtk

from gajim.common import app
from gajim.common.i18n import _
from gajim.common.multiprocess.video_thumbnail import (
    extract_video_thumbnail_and_properties,
)
from gajim.common.util.filesystem import load_file_async
from gajim.common.util.text import format_duration

from gajim.gtk.gstreamer import create_video_elements
from gajim.gtk.preview.file_control_buttons import FileControlButtons
from gajim.gtk.preview.frame_paintable import FramePaintable
from gajim.gtk.preview.image import ImagePreviewLayout
from gajim.gtk.preview.misc import LoadingBox  # noqa: F401 # type: ignore
from gajim.gtk.util.classes import SignalManager
from gajim.gtk.util.misc import get_ui_string

try:
    from gi.repository import Gst
except Exception:
    if typing.TYPE_CHECKING:
        from gi.repository import Gst

log = logging.getLogger("gajim.gtk.preview.video")

_playing_widget: VideoPreviewWidget | None = None


@Gtk.Template.from_string(string=get_ui_string("preview/video.ui"))
class VideoPreviewWidget(Gtk.Box, SignalManager):
    __gtype_name__ = "VideoPreviewWidget"

    __gsignals__ = {
        "display-error": (
            GObject.SignalFlags.RUN_LAST | GObject.SignalFlags.ACTION,
            None,
            (),
        ),
        "playback-updated": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    _stack: Gtk.Stack = Gtk.Template.Child()
    _content_clamp: Adw.Clamp = Gtk.Template.Child()
    _content_overlay: Gtk.Overlay = Gtk.Template.Child()
    _picture: Gtk.Picture = Gtk.Template.Child()
    _play_image: Gtk.Image = Gtk.Template.Child()
    _file_control_buttons: FileControlButtons = Gtk.Template.Child()
    _controls_box: Gtk.Box = Gtk.Template.Child()
    _play_pause_button: Gtk.Button = Gtk.Template.Child()
    _play_icon: Gtk.Image = Gtk.Template.Child()
    _seek_bar: Gtk.Scale = Gtk.Template.Child()
    _seek_adj: Gtk.Adjustment = Gtk.Template.Child()
    _progress_label: Gtk.Label = Gtk.Template.Child()
    _mute_button: Gtk.Button = Gtk.Template.Child()
    _volume_icon: Gtk.Image = Gtk.Template.Child()
    _volume_bar: Gtk.Scale = Gtk.Template.Child()
    _volume_adj: Gtk.Adjustment = Gtk.Template.Child()
    _fullscreen_button: Gtk.Button = Gtk.Template.Child()

    def __init__(
        self,
        filename: str,
        file_size: int,
        mime_type: str,
        orig_path: Path,
        thumb_path: Path,
        loop_as_gif: bool = False,
        uri: str | None = None,
    ) -> None:
        Gtk.Box.__init__(self)
        SignalManager.__init__(self)

        self._orig_path = orig_path
        self._thumb_path = thumb_path
        self._filename = filename
        self._mime_type = mime_type
        self._loop_as_gif = loop_as_gif
        self._uri = uri

        self._layout = ImagePreviewLayout()
        self.set_layout_manager(self._layout)

        self._thumbnail: bytes | None = None
        self._thumb_paintable: Gdk.Paintable | None = None
        self._playbin: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._video_paintable: Gdk.Paintable | None = None
        self._duration = 0.0
        self._position = 0.0
        self._is_playing = False
        self._seeking = False
        self._volume = 1.0
        self._muted = False
        self._volume_before_mute = 1.0
        self._updating_volume_ui = False
        self._progress_id: int | None = None
        self._pipeline_failed = False
        self._destroyed = False

        content_hover_controller = Gtk.EventControllerMotion()
        self._connect(content_hover_controller, "enter", self._on_content_cursor_enter)
        self._connect(content_hover_controller, "leave", self._on_content_cursor_leave)
        self.add_controller(content_hover_controller)

        self._file_control_buttons.set_file_size(file_size)
        self._file_control_buttons.set_file_name(filename)
        self._file_control_buttons.set_path(orig_path)

        pointer_cursor = Gdk.Cursor.new_from_name("pointer")
        self._picture.set_cursor(pointer_cursor)
        self._play_pause_button.set_cursor(pointer_cursor)

        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        self._connect(click, "pressed", self._on_surface_clicked)
        self._picture.add_controller(click)

        self._connect(self._play_pause_button, "clicked", self._on_play_pause_clicked)
        self._connect(self._seek_bar, "change-value", self._on_seek)
        self._connect(self._mute_button, "clicked", self._on_mute_clicked)
        self._connect(self._volume_bar, "value-changed", self._on_volume_changed)
        self._connect(self._fullscreen_button, "clicked", self._on_fullscreen_clicked)
        self._mute_button.set_cursor(pointer_cursor)
        self._fullscreen_button.set_cursor(pointer_cursor)

        if loop_as_gif:
            self._play_image.set_visible(False)
            self._controls_box.set_visible(False)
            self._fullscreen_button.set_visible(False)
            preview_size = app.settings.get("preview_size")
            self._apply_preview_dimension(preview_size, preview_size)
            self._picture.set_tooltip_text(self._filename)
            self._picture.add_css_class("preview-video-overlay")
            self._stack.set_visible_child_name("preview")
            self.ensure_playing()
            return

        if not self._thumb_path.exists():
            self._create_thumbnail()
        else:
            load_file_async(self._thumb_path, self._on_thumb_load_finished)

    def run_destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        from gajim.gtk.preview.media_lightbox import close_lightbox_for

        close_lightbox_for(self)
        self._stop_progress()
        self._release_playback()
        self._cleanup_pipeline()
        self._disconnect_all()
        app.check_finalize(self)

    def do_unroot(self) -> None:
        self._layout.reset()
        self.run_destroy()
        Gtk.Box.do_unroot(self)

    def pause(self) -> None:
        if self._playbin is None or not self._is_playing:
            return
        self._playbin.set_state(Gst.State.PAUSED)
        self._is_playing = False
        self._stop_progress()
        self._update_play_ui()
        self._release_playback()

    def _claim_playback(self) -> None:
        if self._loop_as_gif:
            return
        global _playing_widget
        if _playing_widget is not None and _playing_widget is not self:
            _playing_widget.pause()
        _playing_widget = self

    def _release_playback(self) -> None:
        global _playing_widget
        if _playing_widget is self:
            _playing_widget = None

    def _on_thumb_load_finished(
        self, data: bytes | None, error: GLib.Error | None, user_data: typing.Any
    ) -> None:
        if data is None:
            log.error("Loading thumbnail failed, %s: %s", self._thumb_path.name, error)
            self.emit("display-error")
            return

        self._thumbnail = data
        self._display_thumbnail()

    def _create_thumbnail(self) -> None:
        try:
            future = app.process_pool.submit(
                extract_video_thumbnail_and_properties,
                self._orig_path,
                self._thumb_path,
                app.settings.get("preview_size"),
            )
            future.add_done_callback(
                partial(GLib.idle_add, self._create_thumbnail_finished)
            )
        except Exception as error:
            log.warning("Creating thumbnail failed for: %s %s", self._orig_path, error)
            self.emit("display-error")

    def _create_thumbnail_finished(
        self, future: Future[tuple[bytes, dict[str, typing.Any]]]
    ) -> bool:
        try:
            thumbnail_bytes, _metadata = future.result()
        except Exception as error:
            log.exception(
                "Creating thumbnail failed for: %s %s", self._orig_path, error
            )
            self.emit("display-error")
        else:
            self._thumbnail = thumbnail_bytes
            self._display_thumbnail()
        return GLib.SOURCE_REMOVE

    def _image_preview_dimension(
        self, image_width: int, image_height: int
    ) -> tuple[int, int]:
        max_preview_size = app.settings.get("preview_size")
        if image_width > max_preview_size or image_height > max_preview_size:
            if image_width > image_height:
                width = max_preview_size
                height = int(max_preview_size / image_width * image_height)
            else:
                width = int(max_preview_size / image_height * image_width)
                height = max_preview_size
        else:
            width = image_width
            height = image_height
        return width, height

    def _apply_preview_dimension(self, width: int, height: int) -> None:
        width = max(width, 1)
        height = max(height, 1)
        self._content_clamp.set_maximum_size(width)
        self._content_clamp.set_tightening_threshold(width)
        self._layout.set_preview_dimension(width, height)

    def _on_video_size(self, paintable: Gdk.Paintable, *_args: object) -> None:
        if self._destroyed:
            return
        width = paintable.get_intrinsic_width()
        height = paintable.get_intrinsic_height()
        if width <= 1 or height <= 1:
            return
        disp_w, disp_h = self._image_preview_dimension(width, height)
        self._apply_preview_dimension(disp_w, disp_h)

    def _display_thumbnail(self) -> None:
        assert self._thumbnail is not None
        try:
            texture = Gdk.Texture.new_from_bytes(GLib.Bytes.new(self._thumbnail))
        except GLib.Error:
            log.exception("Could not load video thumbnail %s", self._filename)
            self.emit("display-error")
            return

        self._thumb_paintable = texture
        width, height = self._image_preview_dimension(
            texture.get_width(), texture.get_height()
        )
        self._apply_preview_dimension(width, height)

        self._picture.set_paintable(texture)
        self._picture.set_tooltip_text(self._filename)
        self._picture.add_css_class("preview-video-overlay")
        self._play_image.set_pixel_size(min(width, height) // 3)
        if self._loop_as_gif:
            self._play_image.set_visible(False)
            self._controls_box.set_visible(False)
            self._fullscreen_button.set_visible(False)
        else:
            self._play_image.set_visible(True)
        self._stack.set_visible_child_name("preview")
        if self._loop_as_gif:
            self.ensure_playing()

    def _on_surface_clicked(
        self,
        gesture_click: Gtk.GestureClick,
        _n_press: int,
        _x: float,
        _y: float,
    ) -> None:
        gesture_click.set_state(Gtk.EventSequenceState.CLAIMED)
        if self._loop_as_gif:
            from gajim.gtk.preview.media_lightbox import show_image_lightbox

            show_image_lightbox(
                self._orig_path,
                self._mime_type,
                uri=self._uri,
                from_link=True,
            )
            return
        self.toggle_playback()

    def _on_play_pause_clicked(self, _button: Gtk.Button) -> None:
        self.toggle_playback()

    def _on_fullscreen_clicked(self, _button: Gtk.Button) -> None:
        from gajim.gtk.preview.media_lightbox import show_video_fullscreen

        show_video_fullscreen(self)

    def _on_mute_clicked(self, _button: Gtk.Button) -> None:
        self.toggle_mute()

    def _on_volume_changed(self, _scale: Gtk.Scale) -> None:
        if self._updating_volume_ui:
            return
        self.set_volume(self._volume_adj.get_value())

    def _apply_volume(self) -> None:
        if self._playbin is None or self._loop_as_gif:
            return
        self._playbin.set_property("volume", self._volume)
        self._playbin.set_property("mute", self._muted)

    def _update_volume_ui(self) -> None:
        displayed = 0.0 if self._muted else self._volume
        self._updating_volume_ui = True
        self._volume_adj.set_value(displayed)
        self._updating_volume_ui = False
        if self._muted or displayed == 0:
            self._volume_icon.set_from_icon_name("lucide-volume-off-symbolic")
            self._mute_button.set_tooltip_text(_("Unmute"))
        elif displayed < 0.34:
            self._volume_icon.set_from_icon_name("audio-volume-low-symbolic")
            self._mute_button.set_tooltip_text(_("Mute"))
        elif displayed < 0.67:
            self._volume_icon.set_from_icon_name("audio-volume-medium-symbolic")
            self._mute_button.set_tooltip_text(_("Mute"))
        else:
            self._volume_icon.set_from_icon_name("audio-volume-high-symbolic")
            self._mute_button.set_tooltip_text(_("Mute"))

    def toggle_playback(self) -> None:
        if self._pipeline_failed:
            return

        if self._playbin is None:
            if not self._setup_pipeline():
                return
            self._play()
            return

        if self._is_playing:
            self.pause()
        else:
            self._play()

    def ensure_playing(self) -> None:
        if not self._is_playing:
            self.toggle_playback()

    def get_display_paintable(self) -> Gdk.Paintable | None:
        return self._picture.get_paintable()

    def get_playback_state(self) -> tuple[bool, float, float]:
        return self._is_playing, self._position, self._duration

    def get_volume_state(self) -> tuple[float, bool]:
        return self._volume, self._muted

    def set_volume(self, volume: float) -> None:
        volume = max(0.0, min(volume, 1.0))
        self._volume = volume
        if volume > 0:
            self._muted = False
            self._volume_before_mute = volume
        else:
            self._muted = True
        self._apply_volume()
        self._update_volume_ui()
        self.emit("playback-updated")

    def toggle_mute(self) -> None:
        if self._muted or self._volume == 0:
            self._muted = False
            if self._volume == 0:
                self._volume = self._volume_before_mute or 1.0
        else:
            if self._volume > 0:
                self._volume_before_mute = self._volume
            self._muted = True
        self._apply_volume()
        self._update_volume_ui()
        self.emit("playback-updated")

    def seek_to(self, position: float) -> None:
        if self._playbin is None or self._duration <= 0:
            return
        position = max(0.0, min(position, self._duration))
        self._playbin.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(position)
        )
        self._position = position
        self._update_progress_ui()

    def _setup_pipeline(self) -> bool:
        video_elements = create_video_elements()
        if video_elements is None:
            log.error("No GTK video sink available")
            self._pipeline_failed = True
            return False

        sink, paintable, _name = video_elements
        playbin = Gst.ElementFactory.make("playbin", "video-preview")
        if playbin is None:
            log.error("Could not create playbin")
            self._pipeline_failed = True
            return False

        playbin.set_property("video-sink", sink)
        playbin.set_property("uri", self._orig_path.as_uri())
        if self._loop_as_gif:
            playbin.set_property("mute", True)
        else:
            playbin.set_property("volume", self._volume)
            playbin.set_property("mute", self._muted)

        bus = playbin.get_bus()
        if bus is None:
            log.error("Could not get playbin bus")
            self._pipeline_failed = True
            return False

        bus.add_signal_watch()
        self._connect(bus, "message", self._on_bus_message)

        self._playbin = playbin
        self._bus = bus
        self._video_paintable = paintable
        if self._loop_as_gif:
            self._connect(paintable, "invalidate-size", self._on_video_size)
        return True

    def _play(self) -> None:
        assert self._playbin is not None
        self._claim_playback()
        if self._video_paintable is not None:
            if (
                isinstance(self._video_paintable, FramePaintable)
                and isinstance(self._thumb_paintable, Gdk.Texture)
            ):
                self._video_paintable.set_texture(self._thumb_paintable)
            self._picture.set_paintable(self._video_paintable)
            if self._loop_as_gif:
                self._on_video_size(self._video_paintable)
        self._playbin.set_state(Gst.State.PLAYING)
        self._is_playing = True
        self._start_progress()
        self._update_play_ui()

    def _cleanup_pipeline(self) -> None:
        if self._playbin is not None:
            self._playbin.set_state(Gst.State.NULL)
        if self._bus is not None:
            self._bus.remove_signal_watch()
        self._playbin = None
        self._bus = None
        self._video_paintable = None
        self._is_playing = False

    def _start_progress(self) -> None:
        if self._progress_id is not None:
            return
        self._progress_id = GLib.timeout_add(100, self._on_progress_timeout)

    def _stop_progress(self) -> None:
        if self._progress_id is not None:
            GLib.source_remove(self._progress_id)
            self._progress_id = None

    def _on_progress_timeout(self) -> bool:
        if self._playbin is None or self._seeking:
            return GLib.SOURCE_CONTINUE
        success, position = self._playbin.query_position(Gst.Format.TIME)
        if success:
            self._position = float(position)
            self._update_progress_ui()
        if self._duration <= 0:
            success, duration = self._playbin.query_duration(Gst.Format.TIME)
            if success and duration > 0:
                self._duration = float(duration)
                self._seek_adj.set_upper(self._duration)
                self._update_progress_ui()
        return GLib.SOURCE_CONTINUE

    def _on_seek(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        if self._playbin is None or self._duration <= 0:
            return False
        position = max(0.0, min(value, self._duration))
        self._seeking = True
        self._playbin.seek_simple(
            Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT, int(position)
        )
        self._position = position
        self._update_progress_ui()
        GLib.timeout_add(200, self._end_seek)
        return False

    def _end_seek(self) -> bool:
        self._seeking = False
        return GLib.SOURCE_REMOVE

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if message.type == Gst.MessageType.EOS:
            self._on_eos()
        elif message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            log.warning("Video playback error: %s %s", err, debug)
            self._pipeline_failed = True
            self._cleanup_pipeline()
            if self._thumb_paintable is not None:
                self._picture.set_paintable(self._thumb_paintable)
            self._update_play_ui()
        elif message.type == Gst.MessageType.DURATION_CHANGED:
            if self._playbin is None:
                return
            success, duration = self._playbin.query_duration(Gst.Format.TIME)
            if success and duration > 0:
                self._duration = float(duration)
                self._seek_adj.set_upper(self._duration)
                self._update_progress_ui()
        elif message.type == Gst.MessageType.STATE_CHANGED:
            if message.src != self._playbin:
                return
            _old, new, _pending = message.parse_state_changed()
            self._is_playing = new == Gst.State.PLAYING
            self._update_play_ui()

    def _on_eos(self) -> None:
        if self._playbin is None:
            return
        self._playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)
        if self._loop_as_gif:
            self._playbin.set_state(Gst.State.PLAYING)
            self._is_playing = True
            self._position = 0.0
            return
        self._playbin.set_state(Gst.State.PAUSED)
        self._is_playing = False
        self._position = 0.0
        self._stop_progress()
        self._update_progress_ui()
        self._update_play_ui()
        self._release_playback()
        if self._thumb_paintable is not None:
            self._picture.set_paintable(self._thumb_paintable)

    def _update_play_ui(self) -> None:
        if self._loop_as_gif:
            self._play_image.set_visible(False)
            self._controls_box.set_visible(False)
            self.emit("playback-updated")
            return
        self._play_image.set_visible(not self._is_playing)
        if self._is_playing:
            self._play_icon.set_from_icon_name("lucide-pause-symbolic")
            self._play_pause_button.set_tooltip_text(_("Pause"))
            self._controls_box.set_visible(True)
        else:
            self._play_icon.set_from_icon_name("lucide-play-symbolic")
            self._play_pause_button.set_tooltip_text(_("Play"))
        self.emit("playback-updated")

    def _update_progress_ui(self) -> None:
        if self._duration > 0:
            self._seek_adj.set_value(self._position)
        self._progress_label.set_text(
            f"{format_duration(self._position, self._duration or 1)}/"
            f"{format_duration(self._duration, self._duration or 1)}"
        )
        self.emit("playback-updated")

    def _on_content_cursor_enter(
        self,
        _controller: Gtk.EventControllerMotion,
        _x: int,
        _y: int,
    ) -> None:
        self._file_control_buttons.set_visible(True)
        if not self._loop_as_gif:
            self._controls_box.set_visible(True)

    def _on_content_cursor_leave(
        self,
        _controller: Gtk.EventControllerMotion,
    ) -> None:
        self._file_control_buttons.set_visible(False)
        if not self._is_playing:
            self._controls_box.set_visible(False)
