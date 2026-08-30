# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

from typing import Any

import dataclasses
from enum import Enum

from gi.repository import Gdk
from gi.repository import GObject
from gi.repository import Gtk

from gajim.common import app
from gajim.common.i18n import _

from gajim.gtk.dropdown import GajimDropDown
from gajim.gtk.util.classes import SignalManager


class ChatTypeFilter(Enum):
    ALL = "all"
    CHAT = "chats"
    GROUPCHAT = "group_chats"


@dataclasses.dataclass
class ChatFilters:
    type: ChatTypeFilter = ChatTypeFilter.ALL
    group: str | None = None
    account: str | None = None


class ChatFilter(Gtk.Overlay, SignalManager):
    __gtype_name__ = "ChatFilter"

    __gsignals__ = {
        "filter-changed": (GObject.SignalFlags.RUN_LAST, None, ()),
    }

    def __init__(self) -> None:
        Gtk.Overlay.__init__(self, halign=Gtk.Align.CENTER, tooltip_text=_("Filters"))
        SignalManager.__init__(self)

        self._block_signal = False

        self._toggle_button = Gtk.ToggleButton()
        self._connect(self._toggle_button, "notify::active", self._on_toggle_button)
        icon = Gtk.Image.new_from_icon_name("lucide-list-filter-symbolic")
        self._toggle_button.set_child(icon)
        self.set_child(self._toggle_button)

        self._filter_active_dot = Gtk.Box(
            halign=Gtk.Align.END,
            valign=Gtk.Align.START,
            visible=False,
            width_request=12,
            height_request=12,
        )
        self._filter_active_dot.add_css_class("chat-filter-active")
        self.add_overlay(self._filter_active_dot)

        # Nested DropDowns inside a grabbing popover steal the seat, which
        # blocks window-manager shortcuts. No autohide grab; the toggle
        # (and Escape) close it.
        self._popover = Gtk.Popover(autohide=False, cascade_popdown=False)
        self._popover.set_parent(self._toggle_button)
        self._connect(self._popover, "closed", self._on_popover_closed)
        key = Gtk.EventControllerKey()
        self._connect(key, "key-pressed", self._on_popover_key_pressed)
        self._popover.add_controller(key)

        popover_content = Gtk.Grid(row_spacing=6, column_spacing=12)
        popover_content.add_css_class("p-6")
        self._popover.set_child(popover_content)

        popover_title = Gtk.Label(label=_("Filter Chats"), margin_bottom=6)
        popover_title.add_css_class("bold")
        popover_content.attach(popover_title, 0, 0, 2, 1)

        chat_type_label = Gtk.Label(label=_("Chat Type"), halign=Gtk.Align.END)
        chat_type_label.add_css_class("dimmed")
        popover_content.attach(chat_type_label, 0, 1, 1, 1)

        chat_type_data = {
            ChatTypeFilter.ALL: _("All"),
            ChatTypeFilter.CHAT: _("Chats"),
            ChatTypeFilter.GROUPCHAT: _("Group Chats"),
        }
        self._chat_type_drop_down: GajimDropDown[ChatTypeFilter] = GajimDropDown(
            fixed_width=20, data=chat_type_data
        )
        self._connect(
            self._chat_type_drop_down, "notify::selected", self._on_chat_type_selected
        )
        popover_content.attach(self._chat_type_drop_down, 1, 1, 1, 1)

        roster_groups_label = Gtk.Label(label=_("Group"), halign=Gtk.Align.END)
        roster_groups_label.add_css_class("dimmed")
        popover_content.attach(roster_groups_label, 0, 2, 1, 1)

        self._roster_groups_drop_down: GajimDropDown[str | None] = GajimDropDown(
            fixed_width=20
        )
        self._roster_groups_drop_down.set_enable_search(True)
        self._connect(
            self._roster_groups_drop_down,
            "notify::selected",
            self._on_roster_group_selected,
        )
        popover_content.attach(self._roster_groups_drop_down, 1, 2, 1, 1)

        self._account_label = Gtk.Label(label=_("Account"), halign=Gtk.Align.END)
        self._account_label.add_css_class("dimmed")
        popover_content.attach(self._account_label, 0, 3, 1, 1)

        self._account_drop_down: GajimDropDown[str | None] = GajimDropDown(
            fixed_width=20
        )
        self._connect(
            self._account_drop_down,
            "notify::selected",
            self._on_account_selected,
        )
        popover_content.attach(self._account_drop_down, 1, 3, 1, 1)

        split_label = Gtk.Label(label=_("Separate Group Chats"), halign=Gtk.Align.END)
        split_label.add_css_class("dimmed")
        popover_content.attach(split_label, 0, 4, 1, 1)

        self._split_switch = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        self._split_switch.set_active(app.settings.get("chat_list_split_groups"))
        self._connect(self._split_switch, "notify::active", self._on_split_changed)
        popover_content.attach(self._split_switch, 1, 4, 1, 1)

        reset_button = Gtk.Button(
            label=_("Reset Filters"), halign=Gtk.Align.CENTER, margin_top=6
        )
        self._connect(reset_button, "clicked", self._on_reset_button_clicked)
        popover_content.attach(reset_button, 0, 5, 2, 1)

        app.settings.connect_signal("chat_list_split_groups", self._on_split_setting)
        self._populate_dropdowns()

    def do_unroot(self) -> None:
        app.settings.disconnect_signals(self)
        self._disconnect_all()
        self._chat_type_drop_down.run_destroy()
        self._roster_groups_drop_down.run_destroy()
        self._account_drop_down.run_destroy()
        self._popover.unparent()
        Gtk.Overlay.do_unroot(self)
        app.check_finalize(self)

    def get_filters(self) -> ChatFilters:
        chat_type_item = self._chat_type_drop_down.get_selected_key()
        assert chat_type_item is not None
        roster_group = self._roster_groups_drop_down.get_selected_key()
        account = self._account_drop_down.get_selected_key()

        return ChatFilters(
            type=chat_type_item,
            group=roster_group,
            account=account,
        )

    def set_filters(self, filters: ChatFilters) -> None:
        self._block_signal = True
        self._chat_type_drop_down.select_key(filters.type)
        self._roster_groups_drop_down.select_key(filters.group)
        self._account_drop_down.select_key(filters.account)
        self._block_signal = False
        self.emit("filter-changed")

    def reset(self) -> None:
        self._chat_type_drop_down.set_selected(0)
        self._roster_groups_drop_down.set_selected(0)
        self._account_drop_down.set_selected(0)

    def _on_toggle_button(self, button: Gtk.ToggleButton, *args: Any) -> None:
        if button.get_active():
            if not self._popover.get_visible():
                self._populate_dropdowns()
                self._popover.popup()
                self._toggle_button.grab_focus()
            return

        if self._popover.get_visible():
            self._popover.popdown()

    def _on_popover_closed(self, _popover: Gtk.Popover) -> None:
        if self._toggle_button.get_active():
            self._toggle_button.set_active(False)

    def _on_popover_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        _state: Gdk.ModifierType,
    ) -> bool:
        if keyval == Gdk.KEY_Escape:
            self._toggle_button.set_active(False)
            return Gdk.EVENT_STOP
        return Gdk.EVENT_PROPAGATE

    def _populate_dropdowns(self) -> None:
        roster_group_key = self._roster_groups_drop_down.get_selected_key()
        account_key = self._account_drop_down.get_selected_key()

        roster_groups = self._get_roster_groups()
        accounts = self._get_accounts()

        show_account_filter = len(accounts) > 2
        self._account_drop_down.set_visible(show_account_filter)
        self._account_label.set_visible(show_account_filter)

        self._block_signal = True
        self._roster_groups_drop_down.set_data(roster_groups)
        self._roster_groups_drop_down.select_key(roster_group_key)

        self._account_drop_down.set_data(accounts)
        self._account_drop_down.select_key(account_key)
        self._block_signal = False

    def _update(self) -> None:
        filters = self.get_filters()
        active = (
            filters.type != ChatTypeFilter.ALL
            or filters.group is not None
            or filters.account is not None
        )
        if active:
            self.set_tooltip_text(_("Filters (active)"))
        else:
            self.set_tooltip_text(_("Filters"))

        self._filter_active_dot.set_visible(active)

    def _on_chat_type_selected(
        self, _dropdown: GajimDropDown[ChatTypeFilter], *args: Any
    ) -> None:
        self._update()
        if not self._block_signal:
            self.emit("filter-changed")

    def _on_roster_group_selected(
        self, _dropdown: GajimDropDown[str | None], *args: Any
    ) -> None:
        self._update()
        if not self._block_signal:
            self.emit("filter-changed")

    def _on_account_selected(
        self, _dropdown: GajimDropDown[str | None], *args: Any
    ) -> None:
        self._update()
        if not self._block_signal:
            self.emit("filter-changed")

    def _on_split_changed(self, switch: Gtk.Switch, *args: Any) -> None:
        app.settings.set("chat_list_split_groups", switch.get_active())

    def _on_split_setting(self, value: bool, *args: Any) -> None:
        if self._split_switch.get_active() != value:
            self._split_switch.set_active(value)

    def _on_reset_button_clicked(self, _button: Gtk.Button) -> None:
        self.reset()

    def _get_roster_groups(self) -> dict[str | None, str]:
        roster_groups: dict[str | None, str] = {None: _("All")}

        groups: set[str] = set()
        for account, _label in app.get_enabled_accounts_with_labels():
            client = app.get_client(account)
            groups.update(client.get_module("Roster").get_groups())

        for group in sorted(groups):
            roster_groups[group] = group

        return roster_groups

    def _get_accounts(self) -> dict[str | None, str]:
        accounts: dict[str | None, str] = {None: _("All")}
        for acc, label in app.get_enabled_accounts_with_labels():
            accounts[acc] = label
        return accounts
