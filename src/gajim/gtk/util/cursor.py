# This file is part of Gajim.
#
# SPDX-License-Identifier: GPL-3.0-only

from gi.repository import Adw
from gi.repository import GObject
from gi.repository import Gtk


def _is_clickable(widget: Gtk.Widget) -> bool:
    if isinstance(widget, (Gtk.Editable, Gtk.TextView)):
        return False
    # Popover menu items use GTK's private GtkModelButton type, which does not
    # inherit from Gtk.Button and is not exposed through introspection.
    if widget.__gtype__.name == "GtkModelButton":
        return True
    if isinstance(
        widget,
        (
            Gtk.Button,
            Gtk.MenuButton,
            Gtk.CheckButton,
            Gtk.Switch,
            Gtk.DropDown,
            Gtk.ComboBox,
            Gtk.Scale,
        ),
    ):
        return True
    if isinstance(widget, Adw.ActionRow):
        return widget.get_activatable()
    if isinstance(widget, Gtk.ListBoxRow):
        parent = widget.get_parent()
        return (
            widget.get_selectable()
            and isinstance(parent, Gtk.ListBox)
            and parent.get_selection_mode() != Gtk.SelectionMode.NONE
        ) or widget.has_css_class("clickable")
    if isinstance(widget, Gtk.FlowBoxChild):
        parent = widget.get_parent()
        return (
            isinstance(parent, Gtk.FlowBox)
            and parent.get_selection_mode() != Gtk.SelectionMode.NONE
        ) or widget.has_css_class("clickable")

    # Containers also use click gestures for focus and event propagation. Giving
    # those a pointer makes their blank space and descendants inherit it too.
    # Custom action targets can opt in explicitly instead.
    return widget.has_css_class("clickable")


def _update_cursor(widget: Gtk.Widget, *_args: object) -> None:
    widget.set_cursor_from_name(
        "pointer" if widget.is_sensitive() and _is_clickable(widget) else "default"
    )


def _on_widget_map(widget: Gtk.Widget) -> bool:
    # Mapping covers template widgets, dynamically added controls, and popovers.
    # An explicit cursor belongs to the widget (text, links, resizing, etc.).
    if widget.get_cursor() is None and _is_clickable(widget):
        _update_cursor(widget)
        widget.connect("state-flags-changed", _update_cursor)
        if isinstance(widget, Gtk.ListBoxRow):
            widget.connect("notify::selectable", _update_cursor)
            widget.connect("notify::activatable", _update_cursor)
    return True


def install_pointer_cursors() -> None:
    # Initialize the class so its signals are registered before adding the hook.
    GObject.type_class_ref(Gtk.Widget)
    GObject.add_emission_hook(Gtk.Widget, "map", _on_widget_map)
