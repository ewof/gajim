# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from pathlib import Path

from gi.repository import Gdk
from gi.repository import GLib
from gi.repository import Gtk

from gajim.common import app
from gajim.common.const import IMAGE_MIME_TYPES
from gajim.common.i18n import _
from gajim.common.util.image import get_texture_from_file
from gajim.common.util.image import image_size
from gajim.common.util.image import is_image_animated
from gajim.common.util.preview import is_video
from gajim.common.util.text import format_duration

from gajim.gtk.gstreamer import create_video_elements
from gajim.gtk.menus import get_preview_menu
from gajim.gtk.preview.animated_image import AnimatedImage
from gajim.gtk.preview.animated_image_texture_backend import AnimatedImageTextureBackend
from gajim.gtk.util.classes import SignalManager
from gajim.gtk.util.misc import container_remove_all
from gajim.gtk.widgets import GajimPopover

try:
    from gi.repository import Gst
except Exception:
    Gst = None  # type: ignore[misc, assignment]

_open_overlay: ImageLightbox | None = None
_open_fullscreen: VideoFullscreen | None = None


def show_image_lightbox(
    path: Path,
    mime_type: str,
    uri: str | None = None,
    from_link: bool = False,
) -> None:
    _close_overlay()
    host = app.window.get_media_lightbox_host()
    container_remove_all(host)
    overlay = ImageLightbox(path, mime_type, uri=uri, from_link=from_link)
    host.append(overlay)
    host.set_visible(True)
    overlay.grab_focus()


def show_video_fullscreen(video_widget: Gtk.Widget) -> None:
    global _open_fullscreen
    if _open_fullscreen is not None:
        _open_fullscreen.close()
    _open_fullscreen = VideoFullscreen(video_widget)
    _open_fullscreen.fullscreen()
    _open_fullscreen.present()


def close_lightbox_for(video_widget: Gtk.Widget) -> None:
    if _open_fullscreen is not None and _open_fullscreen.video_widget is video_widget:
        _open_fullscreen.close()


def close_open_overlay() -> bool:
    if _open_overlay is None:
        return False
    _open_overlay.close()
    return True


def _close_overlay() -> None:
    if _open_overlay is not None:
        _open_overlay.close()


def _fit_size(
    nat_w: int, nat_h: int, max_w: int, max_h: int
) -> tuple[int, int]:
    if nat_w <= 0 or nat_h <= 0:
        return max(max_w, 1), max(max_h, 1)
    if nat_w <= max_w and nat_h <= max_h:
        return nat_w, nat_h
    scale = min(max_w / nat_w, max_h / nat_h)
    return max(int(nat_w * scale), 1), max(int(nat_h * scale), 1)


class ImageLightbox(Gtk.Overlay, SignalManager):
    def __init__(
        self,
        path: Path,
        mime_type: str,
        uri: str | None = None,
        from_link: bool = False,
    ) -> None:
        global _open_overlay
        Gtk.Overlay.__init__(self, hexpand=True, vexpand=True, can_focus=True)
        SignalManager.__init__(self)

        self._path = path
        self._mime_type = mime_type
        self._uri = uri
        self._from_link = from_link
        self._nat_w, self._nat_h = 0, 0

        backdrop = Gtk.Box(hexpand=True, vexpand=True)
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        self._connect(click, "pressed", self._on_background_clicked)
        backdrop.add_controller(click)
        self.set_child(backdrop)

        self._media_bin = Gtk.Box(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        media: Gtk.Widget
        if is_video(mime_type):
            media = _LoopingVideoPicture(path)
        elif mime_type in IMAGE_MIME_TYPES and is_image_animated(path):
            self._nat_w, self._nat_h = image_size(path)
            media = AnimatedImage(
                path, path, [AnimatedImageTextureBackend], enlarge_on_click=False
            )
        else:
            texture = get_texture_from_file(path)
            picture = Gtk.Picture(
                content_fit=Gtk.ContentFit.CONTAIN,
                can_target=True,
            )
            if texture is not None:
                self._nat_w = texture.get_width()
                self._nat_h = texture.get_height()
                picture.set_paintable(texture)
            else:
                self._nat_w, self._nat_h = image_size(path)
                # filename is a method, not a GObject construct property
                picture.set_filename(str(path))
            media = picture
        self._media = media
        if isinstance(media, Gtk.Picture):
            paintable = media.get_paintable()
            if paintable is not None:
                self._connect(paintable, "invalidate-size", self._on_paintable_size)
        self._media_bin.append(media)
        self.add_overlay(self._media_bin)

        close_button = Gtk.Button(
            icon_name="lucide-x-symbolic",
            tooltip_text=_("Close"),
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
        )
        close_button.add_css_class("circular")
        close_button.add_css_class("media-lightbox-close")
        self._connect(close_button, "clicked", lambda *_: self.close())
        self.add_overlay(close_button)

        self._menu_popover = GajimPopover(None)
        self._menu_popover.set_parent(self)
        right_click = Gtk.GestureClick(button=Gdk.BUTTON_SECONDARY)
        self._connect(right_click, "pressed", self._on_context_clicked)
        self.add_controller(right_click)

        key = Gtk.EventControllerKey()
        key.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        self._connect(key, "key-pressed", self._on_key_pressed)
        self.add_controller(key)

        self._connect(self, "notify::width", self._on_allocate)
        self._connect(self, "notify::height", self._on_allocate)

        _open_overlay = self

    def close(self) -> None:
        global _open_overlay
        if isinstance(self._media, _LoopingVideoPicture):
            self._media.cleanup()
        self._menu_popover.unparent()
        host = app.window.get_media_lightbox_host()
        host.set_visible(False)
        container_remove_all(host)
        self._disconnect_all()
        if _open_overlay is self:
            _open_overlay = None

    def _on_paintable_size(self, *_args: object) -> None:
        self._update_nat_from_paintable()
        self._on_allocate()

    def _update_nat_from_paintable(self) -> None:
        if not isinstance(self._media, Gtk.Picture):
            return
        paintable = self._media.get_paintable()
        if paintable is None:
            return
        width = paintable.get_intrinsic_width()
        height = paintable.get_intrinsic_height()
        if width > 0 and height > 0:
            self._nat_w, self._nat_h = width, height

    def _on_allocate(self, *_args: object) -> None:
        self._update_nat_from_paintable()
        max_w = max(self.get_width() - 80, 1)
        max_h = max(self.get_height() - 80, 1)
        width, height = _fit_size(self._nat_w, self._nat_h, max_w, max_h)
        self._media.set_size_request(width, height)

    def _on_background_clicked(self, *_args: object) -> None:
        self.close()

    def _on_context_clicked(
        self,
        gesture_click: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
    ) -> None:
        gesture_click.set_state(Gtk.EventSequenceState.CLAIMED)
        orig_path = None if is_video(self._mime_type) else self._path
        if orig_path is None and not self._uri:
            return
        menu = get_preview_menu(
            self._uri or "",
            orig_path=orig_path,
            from_link=self._from_link and bool(self._uri),
        )
        if menu.get_n_items() == 0:
            return
        self._menu_popover.set_menu_model(menu)
        self._menu_popover.set_pointing_to_coord(x, y)
        self._menu_popover.popup()

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self.close()
            return Gdk.EVENT_STOP
        return Gdk.EVENT_PROPAGATE


class VideoFullscreen(Gtk.Window, SignalManager):
    def __init__(self, video_widget: Gtk.Widget) -> None:
        Gtk.Window.__init__(
            self,
            application=app.app,
            decorated=False,
            title=_("Video"),
        )
        SignalManager.__init__(self)
        self.add_css_class("media-lightbox")

        self._video_widget = video_widget
        self._seeking = False
        self._updating_volume_ui = False
        self._progress_id: int | None = None

        overlay = Gtk.Overlay()
        self.set_child(overlay)

        self._picture = Gtk.Picture(
            hexpand=True,
            vexpand=True,
            content_fit=Gtk.ContentFit.CONTAIN,
        )
        overlay.set_child(self._picture)

        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        self._connect(click, "pressed", self._on_picture_clicked)
        self._picture.add_controller(click)

        close_button = Gtk.Button(
            icon_name="lucide-x-symbolic",
            tooltip_text=_("Close"),
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
        )
        close_button.add_css_class("circular")
        close_button.add_css_class("media-lightbox-close")
        self._connect(close_button, "clicked", lambda *_: self.close())
        overlay.add_overlay(close_button)

        self._controls = Gtk.Box(
            spacing=6, valign=Gtk.Align.END, hexpand=True
        )
        self._controls.add_css_class("preview-video-controls")
        self._controls.add_css_class("media-lightbox-controls")

        self._play_pause_button = Gtk.Button(tooltip_text=_("Play"))
        self._play_pause_button.add_css_class("circular")
        self._play_pause_button.add_css_class("flat")
        self._play_icon = Gtk.Image.new_from_icon_name("lucide-play-symbolic")
        self._play_pause_button.set_child(self._play_icon)
        self._connect(self._play_pause_button, "clicked", self._on_play_pause)

        self._seek_adj = Gtk.Adjustment(lower=0, upper=1, step_increment=0.01)
        self._seek_bar = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=self._seek_adj,
            hexpand=True,
            draw_value=False,
        )
        self._connect(self._seek_bar, "change-value", self._on_seek)

        self._progress_label = Gtk.Label(label="0:00/0:00")
        self._progress_label.add_css_class("numeric")

        self._mute_button = Gtk.Button(tooltip_text=_("Mute"))
        self._mute_button.add_css_class("circular")
        self._mute_button.add_css_class("flat")
        self._volume_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        self._mute_button.set_child(self._volume_icon)
        self._connect(self._mute_button, "clicked", self._on_mute)

        self._volume_adj = Gtk.Adjustment(
            lower=0, upper=1, value=1, step_increment=0.05, page_increment=0.1
        )
        self._volume_bar = Gtk.Scale(
            orientation=Gtk.Orientation.HORIZONTAL,
            adjustment=self._volume_adj,
            width_request=80,
            hexpand=False,
            valign=Gtk.Align.CENTER,
            draw_value=False,
            tooltip_text=_("Volume"),
        )
        self._volume_bar.add_css_class("preview-video-volume")
        self._connect(self._volume_bar, "value-changed", self._on_volume_changed)

        self._fullscreen_button = Gtk.Button(tooltip_text=_("Exit Fullscreen"))
        self._fullscreen_button.add_css_class("circular")
        self._fullscreen_button.add_css_class("flat")
        self._fullscreen_button.set_child(
            Gtk.Image.new_from_icon_name("lucide-maximize-symbolic")
        )
        self._connect(self._fullscreen_button, "clicked", lambda *_: self.close())

        self._controls.append(self._play_pause_button)
        self._controls.append(self._seek_bar)
        self._controls.append(self._progress_label)
        self._controls.append(self._mute_button)
        self._controls.append(self._volume_bar)
        self._controls.append(self._fullscreen_button)
        overlay.add_overlay(self._controls)

        key = Gtk.EventControllerKey()
        self._connect(key, "key-pressed", self._on_key_pressed)
        self.add_controller(key)
        self._connect(self, "close-request", self._on_close_request)

        from gajim.gtk.preview.video import VideoPreviewWidget

        assert isinstance(video_widget, VideoPreviewWidget)
        self._picture.set_paintable(video_widget.get_display_paintable())
        video_widget.ensure_playing()
        self._sync_controls()
        self._progress_id = GLib.timeout_add(100, self._on_tick)
        self._connect(video_widget, "playback-updated", self._on_video_updated)

    @property
    def video_widget(self) -> Gtk.Widget:
        return self._video_widget

    def _on_tick(self) -> bool:
        self._sync_controls()
        return GLib.SOURCE_CONTINUE

    def _on_video_updated(self, *_args: object) -> None:
        from gajim.gtk.preview.video import VideoPreviewWidget

        if isinstance(self._video_widget, VideoPreviewWidget):
            self._picture.set_paintable(self._video_widget.get_display_paintable())
        self._sync_controls()

    def _sync_controls(self) -> None:
        from gajim.gtk.preview.video import VideoPreviewWidget

        if not isinstance(self._video_widget, VideoPreviewWidget):
            return
        playing, position, duration = self._video_widget.get_playback_state()
        if playing:
            self._play_icon.set_from_icon_name("lucide-pause-symbolic")
            self._play_pause_button.set_tooltip_text(_("Pause"))
        else:
            self._play_icon.set_from_icon_name("lucide-play-symbolic")
            self._play_pause_button.set_tooltip_text(_("Play"))
        if duration > 0 and not self._seeking:
            self._seek_adj.set_upper(duration)
            self._seek_adj.set_value(position)
        self._progress_label.set_text(
            f"{format_duration(position, duration or 1)}/"
            f"{format_duration(duration, duration or 1)}"
        )
        volume, muted = self._video_widget.get_volume_state()
        displayed = 0.0 if muted else volume
        self._updating_volume_ui = True
        self._volume_adj.set_value(displayed)
        self._updating_volume_ui = False
        if muted or displayed == 0:
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

    def _on_picture_clicked(self, gesture_click: Gtk.GestureClick, *_args: object) -> None:
        gesture_click.set_state(Gtk.EventSequenceState.CLAIMED)
        self._on_play_pause()

    def _on_play_pause(self, *_args: object) -> None:
        from gajim.gtk.preview.video import VideoPreviewWidget

        if isinstance(self._video_widget, VideoPreviewWidget):
            self._video_widget.toggle_playback()

    def _on_mute(self, *_args: object) -> None:
        from gajim.gtk.preview.video import VideoPreviewWidget

        if isinstance(self._video_widget, VideoPreviewWidget):
            self._video_widget.toggle_mute()

    def _on_volume_changed(self, _scale: Gtk.Scale) -> None:
        from gajim.gtk.preview.video import VideoPreviewWidget

        if self._updating_volume_ui:
            return
        if isinstance(self._video_widget, VideoPreviewWidget):
            self._video_widget.set_volume(self._volume_adj.get_value())

    def _on_seek(self, _scale: Gtk.Scale, _scroll: Gtk.ScrollType, value: float) -> bool:
        from gajim.gtk.preview.video import VideoPreviewWidget

        if not isinstance(self._video_widget, VideoPreviewWidget):
            return False
        self._seeking = True
        self._video_widget.seek_to(value)
        GLib.timeout_add(200, self._end_seek)
        return False

    def _end_seek(self) -> bool:
        self._seeking = False
        return GLib.SOURCE_REMOVE

    def _on_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval in (Gdk.KEY_Escape, Gdk.KEY_F11):
            self.close()
            return Gdk.EVENT_STOP
        if keyval == Gdk.KEY_space:
            self._on_play_pause()
            return Gdk.EVENT_STOP
        return Gdk.EVENT_PROPAGATE

    def _on_close_request(self, *_args: object) -> bool:
        global _open_fullscreen
        if self._progress_id is not None:
            GLib.source_remove(self._progress_id)
            self._progress_id = None
        self._disconnect_all()
        if _open_fullscreen is self:
            _open_fullscreen = None
        return Gdk.EVENT_PROPAGATE


class _LoopingVideoPicture(Gtk.Picture, SignalManager):
    """Inline MP4/WebM played as a looping GIF inside the overlay lightbox."""

    def __init__(self, path: Path) -> None:
        Gtk.Picture.__init__(
            self, content_fit=Gtk.ContentFit.CONTAIN, can_target=True
        )
        SignalManager.__init__(self)
        self._playbin: Gst.Element | None = None
        self._bus: Gst.Bus | None = None
        self._playing = False
        click = Gtk.GestureClick(button=Gdk.BUTTON_PRIMARY)
        self._connect(click, "pressed", self._on_clicked)
        self.add_controller(click)
        self._setup(path)

    def _setup(self, path: Path) -> None:
        if Gst is None:
            return

        video_elements = create_video_elements()
        if video_elements is None:
            return

        sink, paintable, _name = video_elements
        playbin = Gst.ElementFactory.make("playbin", "lightbox-loop")
        if playbin is None:
            return

        playbin.set_property("video-sink", sink)
        playbin.set_property("uri", path.as_uri())
        playbin.set_property("mute", True)

        bus = playbin.get_bus()
        if bus is None:
            return

        bus.add_signal_watch()
        self._connect(bus, "message", self._on_bus_message)
        self._playbin = playbin
        self._bus = bus
        self.set_paintable(paintable)
        playbin.set_state(Gst.State.PLAYING)
        self._playing = True

    def _on_clicked(self, gesture_click: Gtk.GestureClick, *_args: object) -> None:
        gesture_click.set_state(Gtk.EventSequenceState.CLAIMED)
        self.toggle_playback()

    def toggle_playback(self) -> None:
        if self._playbin is None:
            return
        if self._playing:
            self._playbin.set_state(Gst.State.PAUSED)
            self._playing = False
        else:
            self._playbin.set_state(Gst.State.PLAYING)
            self._playing = True

    def _on_bus_message(self, _bus: Gst.Bus, message: Gst.Message) -> None:
        if self._playbin is None:
            return
        if message.type == Gst.MessageType.EOS and self._playing:
            self._playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, 0)

    def cleanup(self) -> None:
        if self._playbin is not None:
            self._playbin.set_state(Gst.State.NULL)
        if self._bus is not None:
            self._bus.remove_signal_watch()
        self._playbin = None
        self._bus = None
        self._disconnect_all()
