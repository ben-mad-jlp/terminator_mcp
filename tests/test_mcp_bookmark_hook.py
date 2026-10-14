"""Tests for the UserPromptSubmit bookmark hook's formatting."""

from terminator_mcp.bookmark_hook import _format, _row


def test_row_is_hex_like_the_gutter_with_decimal():
    assert _row(105) == '0x69 (105)'
    assert _row(0) == '0x0 (0)'
    assert _row(None) == '?'


def test_format_uses_click_time_snapshot():
    out = _format([{'name': 'talker', 'row': 104, 'context_start': 101,
                    'context_end': 107, 'line': 'Hello World: 13',
                    'context': 'a\nHello World: 13\nb\n'}])
    assert '--- terminal=talker row=0x68 (104) context=[0x65 (101)..0x6B (107)] ---' in out
    assert 'a\nHello World: 13\nb\n\n</system-reminder>' in out


def test_format_flags_missing_snapshot():
    out = _format([{'name': 'talker', 'row': 170, 'context_start': None,
                    'context_end': None, 'line': None, 'context': None}])
    assert 'row=0xAA (170)' in out
    assert '(text could not be captured at click time)' in out
