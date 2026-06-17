# Terminator line-number gutter
# GPL v2 only
"""linenumbers.py - a row-number gutter drawn to the left of the terminal.

Shows the absolute buffer row number for each visible terminal line, as a
5-digit zero-padded hex number (no 0x prefix, to stay narrow), right-aligned,
with a thin separator between the gutter and the terminal output. The numbers
match the rows used elsewhere (the MCP scroll_to / read_raw / find_in_scrollback
and the minimap), so a user can read a row off the gutter and refer to it.

Doubles as the bookmark gutter: clicking a row toggles a bookmark on it. The
bookmark set lives on the Terminal (Terminal.add_bookmark / remove_bookmark /
toggle_bookmark / get_bookmarks); this widget only renders + dispatches clicks,
so the minimap and the MCP bridge see the same set.

Per-terminal and session-only; hidden by default, toggled from the right-click
menu (Terminal.do_linenumbers_toggle).
"""

from gi.repository import Gtk, Gdk

PAD = 6                      # px padding either side of the numbers
DIGITS = 5                   # zero-padded hex width (~1M rows)
_FMT = '%0{}X'.format(DIGITS)

_BOOKMARK_RGBA = (1.0, 0.80, 0.20, 0.55)   # filled cell behind bookmarked rows
_BOOKMARK_FG   = (0.10, 0.07, 0.00, 1.0)   # dark text on the amber cell


class LineNumbers(Gtk.DrawingArea):
    """A row-number gutter aligned with the terminal's visible lines."""

    def __init__(self, terminal):
        Gtk.DrawingArea.__init__(self)
        self.terminal = terminal
        self.vte = terminal.vte
        self.set_no_show_all(True)          # hidden until toggled on
        self.set_size_request(56, -1)       # refined on first draw

        self._adj = self.vte.get_vadjustment()
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect('draw', self._on_draw)
        self.connect('button-press-event', self._on_button)
        self._cid_value = self._adj.connect('value-changed',
                                            lambda *a: self.queue_draw())
        self._cid_contents = self.vte.connect('contents-changed',
                                              lambda *a: self.queue_draw())

    def _y_to_row(self, y):
        """Map a click y-coordinate to the absolute buffer row under it."""
        char_h = self.vte.get_char_height() or 1
        top_row = int(self._adj.get_value())
        return top_row + int(y // char_h)

    def _on_button(self, _widget, event):
        if event.button != 1:
            return False
        row = self._y_to_row(event.y)
        self.terminal.toggle_bookmark(row)
        return True

    def _on_draw(self, _widget, cr):
        alloc = self.get_allocation()
        width, height = alloc.width, alloc.height
        cr.set_source_rgb(0.10, 0.10, 0.12)
        cr.paint()

        char_h = self.vte.get_char_height() or 1
        rows = self.vte.get_row_count()
        top_row = int(self._adj.get_value())

        cr.select_font_face('monospace', 0, 0)
        cr.set_font_size(max(8.0, char_h - 2.0))

        # Size the gutter to exactly fit DIGITS hex chars (fixed width).
        ext = cr.text_extents(_FMT % 0)
        need = int(ext.width) + PAD * 2 + 2
        if need != width:
            self.set_size_request(need, -1)

        bookmarks = self.terminal._bookmarks

        for r in range(rows):
            abs_row = top_row + r
            cell_y = r * char_h
            if cell_y > height:
                break
            bookmarked = abs_row in bookmarks
            # Bookmark cell background.
            if bookmarked:
                cr.set_source_rgba(*_BOOKMARK_RGBA)
                cr.rectangle(0, cell_y, width - 1, char_h)
                cr.fill()
            # Row number — high-contrast dark text on the amber cell, soft
            # gray otherwise.
            label = _FMT % abs_row
            le = cr.text_extents(label)
            x = (width - PAD - 2) - le.width
            y = cell_y + char_h - 2          # text baseline for the row
            if bookmarked:
                cr.set_source_rgba(*_BOOKMARK_FG)
            else:
                cr.set_source_rgba(0.50, 0.52, 0.58, 0.95)
            cr.move_to(x, y)
            cr.show_text(label)

        # separator between the gutter and the terminal output
        cr.set_source_rgba(1, 1, 1, 0.14)
        cr.set_line_width(1)
        cr.move_to(width - 0.5, 0)
        cr.line_to(width - 0.5, height)
        cr.stroke()
        return False

    def disconnect_hooks(self):
        for owner, cid in ((self._adj, self._cid_value),
                           (self.vte, self._cid_contents)):
            try:
                owner.disconnect(cid)
            except Exception:
                pass
