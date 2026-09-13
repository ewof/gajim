# This file is part of Gajim.
# SPDX-License-Identifier: GPL-3.0-only

import time

from gi.repository import GLib
from gi.repository import Gtk

from gajim.common import app
from gajim.common.calls.security import trust_recent_keys
from gajim.common.calls.security import TRUST_REQUIRED
from gajim.common.i18n import _

from gajim.gtk.window import GajimAppWindow


class SecureCallWindow(GajimAppWindow):
    def __init__(self, module, peer) -> None:
        contact = module._client.get_module("Contacts").get_contact(peer.new_as_bare())
        super().__init__(
            name="SecureCallWindow",
            title=_("Audio Call"),
            default_width=400,
            default_height=280,
            transient_for=app.window,
            header_bar=True,
            add_window_padding=True,
        )
        self._module = module
        self.add_css_class("secure-call-window")
        self._peer = peer.new_as_bare()
        self._connected_at: float | None = None
        self._ended_at: float | None = None
        self._timer_id = 0
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        title = Gtk.Label(label=contact.name, wrap=True)
        title.add_css_class("title-2")
        box.append(title)
        self._status = Gtk.Label(wrap=True, max_width_chars=45, vexpand=True)
        box.append(self._status)
        self._duration = Gtk.Label(visible=False)
        self._duration.add_css_class("title-2")
        self._duration.add_css_class("monospace")
        box.append(self._duration)
        self._trust = Gtk.Button(label=_("Trust keys used in the last 24 hours"))
        self._trust.set_tooltip_text(
            _(
                "Approve this contact's recently used keys without comparing fingerprints."
            )
        )
        self._connect(self._trust, "clicked", self._trust_clicked)
        box.append(self._trust)
        self._direct = Gtk.CheckButton(label=_("Do not relay calls"))
        self._direct.set_active(app.settings.get("calls_do_not_relay"))
        self._direct.set_tooltip_text(
            _(
                "Allow direct connections, exposing your IP address to the other caller. Applies to future calls."
            )
        )
        self._connect(self._direct, "toggled", self._direct_toggled)
        box.append(self._direct)
        privacy = self._privacy = Gtk.Label(
            label=_(
                "Direct calls share your IP address with the other person. OMEMO verification is still required."
            ),
            wrap=True,
            max_width_chars=45,
        )
        privacy.add_css_class("dim-label")
        privacy.set_visible(self._direct.get_active())
        box.append(privacy)
        self._retry = Gtk.Button(label=_("Retry call"))
        self._connect(self._retry, "clicked", lambda _button: module.start(self._peer))
        box.append(self._retry)
        buttons = Gtk.Box(spacing=12, halign=Gtk.Align.CENTER)
        self._answer = Gtk.Button(label=_("Answer"))
        self._answer.add_css_class("suggested-action")
        self._connect(self._answer, "clicked", lambda _button: module.accept())
        buttons.append(self._answer)
        self._mute = Gtk.ToggleButton(label=_("Mute"))
        self._connect(
            self._mute, "toggled", lambda button: module.mute(button.get_active())
        )
        buttons.append(self._mute)
        self._end = Gtk.Button(label=_("End Call"))
        self._end.add_css_class("destructive-action")
        self._connect(self._end, "clicked", self._end_clicked)
        buttons.append(self._end)
        box.append(buttons)
        self.set_child(box)

    @property
    def peer(self):
        return self._peer

    def update(self, text: str, *, incoming: bool = False, active: bool = True) -> None:
        self._status.set_text(text)
        self._direct.set_sensitive(not active)
        self._retry.set_visible(not active and text != TRUST_REQUIRED)
        self._trust.set_visible(not active and text == TRUST_REQUIRED)
        self._answer.set_visible(incoming and active)
        connected = active and self._module.call.state == "connected"
        if connected and self._connected_at is None:
            self._connected_at = time.monotonic()
            self._duration.set_visible(True)
            self._update_duration()
            self._timer_id = GLib.timeout_add(250, self._update_duration)
        elif not active and self._connected_at is not None and self._ended_at is None:
            self._ended_at = time.monotonic()
            self._stop_timer()
            self._update_duration()
        self._mute.set_visible(connected)
        if not active:
            self._mute.set_active(False)
        self._end.set_label(
            _("Decline") if incoming else (_("End Call") if active else _("Close"))
        )

    def _direct_toggled(self, button) -> None:
        app.settings.set("calls_do_not_relay", button.get_active())
        self._privacy.set_visible(button.get_active())

    def _update_duration(self) -> bool:
        assert self._connected_at is not None
        end = self._ended_at if self._ended_at is not None else time.monotonic()
        seconds = max(0, int(end - self._connected_at))
        hours, seconds = divmod(seconds, 3600)
        minutes, seconds = divmod(seconds, 60)
        duration = f"{minutes:02d}:{seconds:02d}"
        if hours:
            duration = f"{hours}:{duration}"
        self._duration.set_text(
            _("Call duration: %s") % duration
            if self._ended_at is not None
            else duration
        )
        return GLib.SOURCE_CONTINUE

    def _stop_timer(self) -> None:
        if self._timer_id:
            GLib.source_remove(self._timer_id)
            self._timer_id = 0

    def _end_clicked(self, _button) -> None:
        if self._module.call is None:
            self.close()
        else:
            self._module.end()

    def _trust_clicked(self, _button) -> None:
        backend = self._module._client.get_module("OMEMO").backend
        count = trust_recent_keys(backend, self._peer.bare)
        if count:
            self._module.start(self._peer)
        else:
            self._status.set_text(
                _(
                    "No unverified keys were used in the last 24 hours. Exchange an encrypted message with this contact, then try again."
                )
            )

    def _cleanup(self) -> None:
        self._stop_timer()
        self._module.end()
        self._module.window = None
