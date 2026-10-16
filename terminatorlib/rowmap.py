"""rowmap.py - absolute buffer row numbers for a VTE terminal.

VTE numbers rows two ways. get_text_range and get_cursor_position use absolute
rows, counted from the first line the terminal ever printed. The scroll
adjustment counts from the oldest row still in the buffer (VTE 0.76
widget.cc: lower = 0, value = scroll_position - delta). The two agree only
until rows are dropped from the top: by `clear` (which erases scrollback),
`reset`, or a scrollback limit. Everything here hands out absolute rows, so a
row number keeps naming the same line after a clear.

VTE doesn't expose delta, so offset() recovers it. get_text_format() returns
exactly the displayed rows; the right delta is the one at which
get_text_range over those rows returns the same text. The cursor is always on
the bottom screen, which bounds the candidates to one screenful.
"""

import weakref

_cache = weakref.WeakKeyDictionary()    # vte -> last offset found


def _text_format():
    from gi.repository import Vte
    return Vte.Format.TEXT


def _as_text(res):
    if isinstance(res, tuple):
        res = res[0] if res else ''
    return res or ''


def offset(vte):
    """delta such that absolute row = adjustment row + delta."""
    fmt = _text_format()
    adj = vte.get_vadjustment()
    value = int(adj.get_value())
    upper = int(adj.get_upper())
    rows = vte.get_row_count()
    _col, cursor = vte.get_cursor_position()
    # upper = insert_delta + rows - delta, and the cursor lies in
    # [insert_delta, insert_delta + rows), so delta lies in [lo, hi]. It is hi
    # when the cursor is on the screen's top line, as right after a clear.
    hi = cursor + rows - upper
    lo = hi - rows + 1
    cached = _cache.get(vte, 0)
    fallback = cached if lo <= cached <= hi else hi

    shown = _as_text(vte.get_text_format(fmt))
    if not shown.strip():
        found = fallback    # nothing on screen to match against
    else:
        def matches(d):
            top = value + d
            # The view can show one partial row past row_count.
            return any(_as_text(vte.get_text_range_format(
                fmt, top, 0, top + n, 0)) == shown for n in (rows, rows + 1))

        candidates = [cached] + [d for d in range(hi, lo - 1, -1)
                                 if d != cached]
        found = next((d for d in candidates if lo <= d <= hi and matches(d)),
                     fallback)
    found = max(0, found)
    _cache[vte] = found
    return found


def bounds(vte):
    """(first, end): the buffer holds absolute rows first .. end - 1."""
    d = offset(vte)
    return d, d + int(vte.get_vadjustment().get_upper())


def top_row(vte):
    """Absolute row at the top of the view."""
    return int(vte.get_vadjustment().get_value()) + offset(vte)


def value_for_row(vte, row):
    """Adjustment value that puts absolute `row` at the top of the view."""
    return row - offset(vte)
