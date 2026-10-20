"""Tests for the UserPromptSubmit bookmark hook."""

import io
import json

from terminator_mcp import bookmark_hook
from terminator_mcp.bookmark_hook import _format, _row

AGENT_PROMPT = ('<agent-message from="afd2e87d26f7282a9">\n'
                '[Subagent hand-back] The report follows:\n  done\n'
                '</agent-message>')


def _run_main(monkeypatch, stdin_text):
    """Run main() with stdin_text; return (drain calls made, stdout)."""
    calls = []

    def fake_call(method, args):
        calls.append(method)
        return {'events': [{'name': 'talker', 'row': 1, 'context_start': 0,
                            'context_end': 2, 'line': 'x', 'context': 'x\n'}]}

    monkeypatch.setattr(bookmark_hook.socket_client, 'call', fake_call)
    monkeypatch.setattr('sys.stdin', io.StringIO(stdin_text))
    out = io.StringIO()
    monkeypatch.setattr('sys.stdout', out)
    assert bookmark_hook.main() == 0
    return calls, out.getvalue()


def test_typed_prompt_drains_bookmarks(monkeypatch):
    calls, out = _run_main(monkeypatch, json.dumps({'prompt': 'look at this'}))
    assert calls == ['drain_bookmark_events']
    assert 'terminal=talker' in out


def test_agent_message_leaves_bookmarks_queued(monkeypatch):
    calls, out = _run_main(monkeypatch, json.dumps({'prompt': AGENT_PROMPT}))
    assert calls == []
    assert out == ''


def test_unreadable_stdin_still_drains(monkeypatch):
    calls, _ = _run_main(monkeypatch, 'not json')
    assert calls == ['drain_bookmark_events']


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
