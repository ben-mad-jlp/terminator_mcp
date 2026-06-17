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

import sys

from . import socket_client


def _format(events):
    lines = [
        '<system-reminder>',
        'Terminal bookmarks the user clicked since the last message '
        '(rows are absolute buffer rows; use the terminator MCP '
        '`read_raw` to fetch more context if needed):',
        '',
    ]
    for ev in events:
        name = ev.get('name') or ev.get('uuid')
        row = ev.get('row')
        context = (ev.get('context') or '').rstrip('\n')
        start = ev.get('context_start')
        end = ev.get('context_end')
        lines.append('--- terminal=%s row=%s context=[%s..%s] ---'
                     % (name, row, start, end))
        lines.append(context)
        lines.append('')
    lines.append('</system-reminder>')
    return '\n'.join(lines)


def main():
    try:
        result = socket_client.call('drain_bookmark_events', {'context': 3})
    except socket_client.BridgeError:
        return 0
    events = (result or {}).get('events') or []
    if not events:
        return 0
    sys.stdout.write(_format(events) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
