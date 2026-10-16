"""Unit tests for terminatorlib.rowmap and the bridge reads built on it.

FakeVte models VTE 0.76's buffer as its source describes it: the ring holds
absolute rows [delta, next), the scroll adjustment is absolute - delta
(lower 0, upper insert_delta + row_count - delta), and get_text_range reads a
row missing from the ring as an empty line.
"""

import pytest

from terminatorlib import rowmap
from terminatorlib.plugins.mcp_bridge import MCPBridge


class FakeAdj:
    def __init__(self, vte):
        self._vte = vte

    def get_lower(self):
        return 0.0

    def get_upper(self):
        v = self._vte
        return float(v.insert_delta + v.rows - v.delta)

    def get_value(self):
        return float(self._vte.scroll - self._vte.delta)

    def get_page_size(self):
        return float(self._vte.rows)

    def set_value(self, value):
        self._vte.scroll = int(value) + self._vte.delta


class FakeVte:
    def __init__(self, lines, rows=5, delta=0):
        """lines: text of absolute rows delta, delta+1, ...; the screen is
        the last `rows` of them, scrolled to the bottom, cursor on the last
        non-empty line."""
        self.rows = rows
        self.delta = delta
        self.text = {delta + i: line for i, line in enumerate(lines)}
        nxt = delta + len(lines)
        self.insert_delta = max(delta, nxt - rows)
        self.scroll = self.insert_delta
        filled = [r for r, t in self.text.items() if t] or [self.insert_delta]
        self.cursor = max(filled)
        self.cursor = min(max(self.cursor, self.insert_delta),
                          self.insert_delta + rows - 1)

    def _range(self, first, end_excl):
        return ''.join(self.text.get(r, '') + '\n'
                       for r in range(first, end_excl))

    def get_vadjustment(self):
        return FakeAdj(self)

    def get_row_count(self):
        return self.rows

    def get_cursor_position(self):
        return (0, self.cursor)

    def get_has_selection(self):
        return False

    def get_text_range_format(self, _fmt, r0, _c0, r1, _c1):
        text = self._range(r0, r1)
        return (text, len(text))

    def get_text_format(self, _fmt):
        return self._range(self.scroll, self.scroll + self.rows)


def cleared(prompt_row, rows=5, prompt='$ '):
    """A terminal right after `clear`: every earlier row dropped, the prompt
    alone on the top line of an otherwise blank screen."""
    v = FakeVte([prompt] + [''] * (rows - 1), rows=rows, delta=prompt_row)
    v.cursor = prompt_row
    return v


@pytest.fixture(autouse=True)
def fresh_cache():
    rowmap._cache.clear()


def test_no_rows_dropped_is_identity():
    v = FakeVte(['l%d' % i for i in range(12)])
    assert rowmap.offset(v) == 0
    assert rowmap.bounds(v) == (0, 12)
    assert rowmap.top_row(v) == 7


def test_offset_after_clear():
    v = cleared(305)
    assert rowmap.offset(v) == 305
    assert rowmap.bounds(v) == (305, 310)
    assert rowmap.top_row(v) == 305


def test_offset_tracks_dropped_rows_with_cursor_mid_screen():
    v = FakeVte(['l%d' % i for i in range(8)] + ['$ ', '', ''], rows=5,
                delta=40)
    assert rowmap.offset(v) == 40
    assert rowmap.top_row(v) == 46


def test_offset_when_scrolled_into_history():
    v = FakeVte(['l%d' % i for i in range(30)], rows=5, delta=100)
    v.scroll = 110
    assert rowmap.offset(v) == 100
    assert rowmap.top_row(v) == 110


def test_stale_cache_is_replaced():
    v = FakeVte(['l%d' % i for i in range(12)])
    assert rowmap.offset(v) == 0
    v2 = cleared(305)
    rowmap._cache[v2] = 0
    assert rowmap.offset(v2) == 305


def test_offset_can_decrease():
    # The alternate screen (vim, less) has its own ring; leaving it brings
    # back the normal screen's smaller delta.
    v = cleared(305)
    assert rowmap.offset(v) == 305
    rowmap._cache[v] = 900
    assert rowmap.offset(v) == 305


def test_blank_screen_keeps_cached_offset_in_range():
    v = cleared(305)
    v.text[305] = ''
    rowmap._cache[v] = 303
    assert rowmap.offset(v) == 303


def test_blank_screen_with_impossible_cache_puts_cursor_at_top():
    v = cleared(305)
    v.text[305] = ''
    assert rowmap.offset(v) == 305


def test_repeated_text_prefers_cached_offset():
    v = FakeVte(['x'] * 20, rows=5, delta=10)
    rowmap._cache[v] = 10
    assert rowmap.offset(v) == 10


def test_value_for_row_round_trips():
    v = cleared(305)
    assert rowmap.value_for_row(v, 307) == 2


# ---- bridge reads after rows were dropped --------------------------------

class FakeTerm:
    def __init__(self, vte):
        self._vte = vte

    def get_vte(self):
        return self._vte


@pytest.fixture
def bridge(monkeypatch):
    b = MCPBridge.__new__(MCPBridge)
    monkeypatch.setattr(b, '_resolve', lambda _uuid: b.term, raising=False)
    return b


def test_read_visible_after_clear(bridge):
    bridge.term = FakeTerm(cleared(305, prompt='jlp$ '))
    res = bridge._h_get_terminal_text({'mode': 'visible'})
    assert res['text'].startswith('jlp$ ')
    assert res['range'] == [305, 309]
    assert res['cursor_row'] == 305


def test_read_scrollback_after_clear(bridge):
    bridge.term = FakeTerm(cleared(305, prompt='jlp$ '))
    res = bridge._h_get_terminal_text({'mode': 'scrollback'})
    assert res['text'].startswith('jlp$ ')
    assert res['range'] == [305, 309]
    assert res['total_rows'] == 310
    assert res['next_offset'] is None


def test_probe_sees_prompt_after_clear(bridge):
    bridge.term = FakeTerm(cleared(305, prompt='Password: '))
    assert bridge._h_probe_terminal({})['looks_like_password_prompt']


def test_read_raw_and_search_after_clear(bridge):
    bridge.term = FakeTerm(cleared(305, prompt='jlp$ '))
    raw = bridge._h_read_raw({'start_row': 305, 'end_row': 305})
    assert raw['text'] == 'jlp$ \n'
    assert raw['range'] == [305, 305]
    found = bridge._h_search_terminal({'pattern': r'jlp\$'})
    assert [m['row'] for m in found['matches']] == [305]


def test_scroll_to_absolute_row_after_rows_dropped(bridge):
    v = FakeVte(['l%d' % i for i in range(30)], rows=5, delta=100)
    bridge.term = FakeTerm(v)
    res = bridge._h_scroll_to({'row': 110})
    assert v.scroll == 107          # 110 - page/2, truncated
    assert (res['value'], res['lower'], res['upper']) == (107, 100, 130)
