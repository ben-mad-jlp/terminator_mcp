"""restore_layout.py - get a saved workspace tab back to work.

Right-click -> Restore layout -> <name> finds the tab labeled <name> in this
window, or builds it from the saved layout as a new tab, then types each
pane's Custom command into the panes whose shell is idle. Busy panes get a
titlebar badge naming what's running instead. Panes are matched by title.
Design: terminator_mcp/doc/planning/restore_layout.md.
"""

import copy
import os

from gi.repository import GLib, Gtk

import terminatorlib.plugin as plugin
from terminatorlib.config import Config
from terminatorlib.factory import Factory
from terminatorlib.util import dbg, enumerate_descendants, err

AVAILABLE = ['RestoreLayout']

PANED_TYPES = ('HPaned', 'VPaned')
READY_POLL_MS = 100         # new pane: how often to look for its prompt
READY_TIMEOUT_MS = 3000     # new pane: type anyway after this long


# ---- pure helpers (unit-tested) ----------------------------------------

def _order(node):
    try:
        return int(node.get('order', 0))
    except (TypeError, ValueError):
        return 0


def restorable_root(flat):
    """The node under a single-tab layout's window, nested via 'children',
    or None if the layout has several windows or several tabs."""
    nodes = {key: dict(value, children={}) for key, value in flat.items()}
    windows = [key for key, node in nodes.items()
               if node.get('type') == 'Window']
    for key, node in nodes.items():
        parent = node.get('parent')
        if parent in nodes:
            nodes[parent]['children'][key] = node
    if len(windows) != 1:
        return None
    children = list(nodes[windows[0]]['children'].values())
    if len(children) != 1:
        return None
    root = children[0]
    if root.get('type') not in PANED_TYPES + ('Terminal',):
        return None
    return root


def _terminals(node):
    """Terminal nodes under node, in layout order."""
    if node.get('type') == 'Terminal':
        return [node]
    out = []
    for child in sorted(node.get('children', {}).values(), key=_order):
        out.extend(_terminals(child))
    return out


def layout_panes(root):
    """[(title, command)] for each terminal in the layout, in order."""
    return [(t.get('title') or None, t.get('command') or None)
            for t in _terminals(root)]


def strip_layout(root):
    """Copy of the tree without terminals' command and uuid: a command would
    be spawned in place of the shell, and a saved uuid would collide if the
    layout is built twice."""
    root = copy.deepcopy(root)
    for term in _terminals(root):
        term.pop('command', None)
        term.pop('uuid', None)
    return root


def match_panes(panes, existing):
    """Match layout panes [(title, command)] to existing [(title, term)].
    Returns ([(term, title, command)], [missing titles])."""
    by_title = {}
    for title, term in existing:
        by_title.setdefault(title, term)
    matched, missing = [], []
    for title, command in panes:
        if title is None:
            if command:
                missing.append('(untitled)')
            continue
        term = by_title.get(title)
        if term is None:
            missing.append(title)
        else:
            matched.append((term, title, command))
    return matched, missing


def busy_label(fg_pgrp, shell_pid, comm):
    """None if the shell is the pty's foreground process, else a badge."""
    if fg_pgrp == shell_pid:
        return None
    return 'busy: %s' % comm if comm else 'busy'


# ---- plugin ------------------------------------------------------------

class RestoreLayout(plugin.MenuItem):
    """Right-click menu: Restore layout -> <saved layout>."""
    capabilities = ['terminal_menu']

    def callback(self, menuitems, menu, terminal):
        config = Config()
        names = [name for name in sorted(config.list_layouts())
                 if restorable_root(
                     copy.deepcopy(config.layout_get_config(name) or {}))]
        if not names:
            return
        item = Gtk.MenuItem.new_with_mnemonic('_Restore layout')
        submenu = Gtk.Menu()
        item.set_submenu(submenu)
        for name in names:
            entry = Gtk.MenuItem(label=name)
            entry.connect('activate', lambda _w, n=name: self.restore(terminal, n))
            submenu.append(entry)
        menuitems.append(item)

    def restore(self, terminal, name):
        flat = copy.deepcopy(Config().layout_get_config(name) or {})
        root = restorable_root(flat)
        if root is None:
            err('restore_layout: %s is not a single-tab layout' % name)
            return
        window = terminal.get_toplevel()
        # Zoomed, the notebook is detached and its tabs unreachable.
        window.unzoom()
        page = self._find_tab(window, name)
        built = page is None
        if built:
            page = self._build_tab(window, name, root)
        terms = self._tab_terminals(page)
        for term in terms:
            self._set_badge(term, '')
        existing = [(t.titlebar.get_custom_string(), t) for t in terms]
        matched, missing = match_panes(layout_panes(root), existing)
        for term, title, command in matched:
            if not command:
                continue
            if built:
                self._type_when_ready(term, command)
                continue
            label = self._busy(term)
            if label:
                self._set_badge(term, label)
            else:
                term.feed(command + '\n')
        if missing:
            target = matched[0][0] if matched else (terms[0] if terms else None)
            if target is not None:
                self._set_badge(target, 'missing: %s' % ', '.join(missing))

    # ---- tabs ------------------------------------------------------------

    def _find_tab(self, window, name):
        """Root widget of the first tab in window labeled name, or None."""
        if not window.is_child_notebook():
            return None
        notebook = window.get_child()
        for child in notebook.get_children():
            label = notebook.get_tab_label(child)
            if label is not None and label.get_custom_label() == name:
                return child
        return None

    def _build_tab(self, window, name, root):
        """New tab labeled name with root's split tree; returns its root."""
        maker = Factory()
        if not window.is_child_notebook():
            maker.make('Notebook', window=window)
        notebook = window.get_child()
        term = maker.make('Terminal')
        term.spawn_child()
        notebook.newtab(widget=term)
        notebook.get_tab_label(term).set_custom_label(name, force=True)
        layout = strip_layout(root)
        if layout['type'] == 'Terminal':
            term.create_layout(layout)
            return term
        # As Window.create_layout does: split, then let the Paned recurse.
        notebook.split_axis(term, layout['type'] == 'VPaned')
        paned = term.get_parent()
        paned.create_layout(layout)
        return paned

    def _tab_terminals(self, page):
        maker = Factory()
        if maker.isinstance(page, 'Terminal'):
            return [page]
        return [t for t in enumerate_descendants(page)[1]
                if maker.isinstance(t, 'Terminal')]

    # ---- panes -----------------------------------------------------------

    def _busy(self, term):
        """Badge text if something other than the shell has the pty."""
        try:
            fg = os.tcgetpgrp(term.get_vte().get_pty().get_fd())
        except (AttributeError, OSError) as ex:
            dbg('restore_layout: no foreground for %s: %s' % (term, ex))
            return 'busy'
        comm = None
        try:
            with open('/proc/%d/comm' % fg) as f:
                comm = f.read().strip()
        except OSError:
            pass
        return busy_label(fg, term.pid, comm)

    def _type_when_ready(self, term, command):
        """Type into a new pane once its shell has drawn a prompt, so the
        command doesn't race shell startup."""
        from gi.repository import Vte
        state = {'waited': 0}

        def poll():
            state['waited'] += READY_POLL_MS
            text = term.get_vte().get_text_format(Vte.Format.TEXT) or ''
            if text.strip() or state['waited'] >= READY_TIMEOUT_MS:
                label = self._busy(term)
                if label:
                    self._set_badge(term, label)
                else:
                    term.feed(command + '\n')
                return False
            return True

        GLib.timeout_add(READY_POLL_MS, poll)

    def _set_badge(self, term, text):
        """Show text in term's titlebar until the pane is focused."""
        term.titlebar.set_badge(text)
        handler = getattr(term, '_restore_badge_handler', None)
        if handler is not None:
            term.get_vte().disconnect(handler)
            term._restore_badge_handler = None
        if not text:
            return

        def on_focus(_widget, _event):
            self._set_badge(term, '')
            return False

        term._restore_badge_handler = term.get_vte().connect(
            'focus-in-event', on_focus)
