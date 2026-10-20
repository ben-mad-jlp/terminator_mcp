#!/usr/bin/env python3
"""UserPromptSubmit hook: inject any bookmarked terminal lines into context.

Reads the bookmark event queue from the running Terminator MCP bridge, formats
the events as a <system-reminder> block on stdout, and exits 0. The hook is
silent (no output) when there are no events or when Terminator is not running,
so it is safe to leave wired up unconditionally.

Wire it up in Claude Code settings.json (~/.claude/settings.json):
    {
      "hooks": {
        "UserPromptSubmit": [
          {"hooks": [{"type": "command",
                      "command": "python3 -m terminator_mcp.bookmark_hook"}]}
        ]
      }
    }
"""

import json
import sys

from . import socket_client


def _row(value):
    """Hex like the line-number gutter, with decimal for read_raw."""
    if value is None:
        return '?'
    return '0x%X (%d)' % (value, value)


def _format(events):
    lines = [
        '<system-reminder>',
        'Terminal bookmarks the user clicked since the last message. Text was '
        'captured at click time. Rows are absolute buffer rows as of the '
        'click; a resize since then may have moved them, so use the terminator '
        'MCP `read_raw` for more context only if the pane has not been resized:',
        '',
    ]
    for ev in events:
        name = ev.get('name') or ev.get('uuid')
        lines.append('--- terminal=%s row=%s context=[%s..%s] ---'
                     % (name, _row(ev.get('row')),
                        _row(ev.get('context_start')),
                        _row(ev.get('context_end'))))
        context = ev.get('context')
        if context is None:
            lines.append('(text could not be captured at click time)')
        else:
            lines.append(context.rstrip('\n'))
        lines.append('')
    lines.append('</system-reminder>')
    return '\n'.join(lines)


def _from_agent(stdin_text):
    """True when the prompt is a subagent or peer-session message, not typed.

    Claude Code fires UserPromptSubmit for those too, and the hook input has
    no origin field; the prompt text starting with <agent-message is the only
    marker. Unreadable input counts as typed, so bookmarks still get sent.
    """
    try:
        prompt = json.loads(stdin_text).get('prompt') or ''
    except (ValueError, AttributeError):
        return False
    return prompt.lstrip().startswith('<agent-message ')


def main():
    # Leave the queue for the next prompt the user types, in whatever session.
    if _from_agent(sys.stdin.read()):
        return 0
    try:
        result = socket_client.call('drain_bookmark_events', {})
    except socket_client.BridgeError:
        return 0
    events = (result or {}).get('events') or []
    if not events:
        return 0
    sys.stdout.write(_format(events) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
