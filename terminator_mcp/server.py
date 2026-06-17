"""Terminator MCP server (P0 — read-only).

A standalone stdio MCP process that lets an LLM observe a running Terminator:
list terminals, read the visible buffer + scrollback, and read the selection.
It talks to the in-process `MCPBridge` plugin over a local Unix-domain socket
(see terminator_mcp.socket_client). It never imports GTK/VTE and never launches
Terminator, so it cannot crash or block the user's terminals.

P0 exposes NO write/send/command tools — it is incapable of changing a
terminal. Those arrive in later phases behind the safety model.

Run:  python -m terminator_mcp
Register with Claude Code:  claude mcp add terminator -- python -m terminator_mcp
"""

from mcp.server.fastmcp import FastMCP

from . import socket_client, naming, safety

mcp = FastMCP('terminator')


def _err(exc):
    """Render a BridgeError / ValueError as a structured tool result.

    BridgeError carries a `.code` (e.g. terminator_not_running,
    terminal_not_found). naming.resolve raises ValueError('<code>: detail') for
    terminal_not_found / ambiguous_name — surface that code, not a generic one.
    """
    code = getattr(exc, 'code', None)
    if code is None:
        msg = str(exc)
        code = msg.split(':', 1)[0].strip() if ':' in msg else 'error'
    return {'error': code, 'message': str(exc)}


def _resolve(terminal):
    """Resolve a friendly name/uuid to a uuid string (or '' for focused)."""
    return naming.resolve(terminal)


@mcp.tool()
def list_terminals() -> dict:
    """List all open Terminator terminals with uuid, title, focus, cwd, size."""
    try:
        return {'terminals': socket_client.call('list_terminals_info', {})}
    except socket_client.BridgeError as ex:
        return _err(ex)


@mcp.tool()
def get_focused_terminal() -> dict:
    """Return the uuid of the currently focused terminal."""
    try:
        return {'uuid': naming.focused_uuid()}
    except socket_client.BridgeError as ex:
        return _err(ex)


@mcp.tool()
def tail(terminal: str = '', lines: int = 40) -> dict:
    """Read the last `lines` rows of a terminal (visible + recent scrollback).

    terminal: uuid or friendly title; empty = focused terminal.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('get_terminal_text', {
            'uuid': uuid, 'mode': 'scrollback',
            'max_lines': max(1, lines), 'from_end': True,
        })
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def read_terminal(terminal: str = '', mode: str = 'scrollback',
                  max_lines: int = 2000, offset: int = 0,
                  from_end: bool = True) -> dict:
    """Read a terminal's buffer.

    mode: 'visible' (just the on-screen rows) or 'scrollback' (a bounded
    window of the full buffer). For large scrollback, page with `offset`
    using the `next_offset` returned by the previous call.
    terminal: uuid or friendly title; empty = focused terminal.
    """
    try:
        uuid = _resolve(terminal)
        if offset:
            return socket_client.call('get_terminal_text_page', {
                'uuid': uuid, 'offset': offset, 'limit': max_lines,
            })
        return socket_client.call('get_terminal_text', {
            'uuid': uuid, 'mode': mode,
            'max_lines': max_lines, 'from_end': from_end,
        })
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def get_selection(terminal: str = '') -> dict:
    """Return the current text selection in a terminal (if any).

    terminal: uuid or friendly title; empty = focused terminal.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('get_selection', {'uuid': uuid})
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


def _execute_capture(uuid, command, timeout_ms):
    """Final step: actually run the command and capture output via the bridge.

    Always preceded by the password gate. Returns the bridge's
    {ok, output, exit_code, timed_out, command}.
    """
    return socket_client.call('run_command_capture', {
        'uuid': uuid, 'command': command, 'timeout_ms': timeout_ms,
    })


@mcp.tool()
def run_command(command: str, terminal: str = '', timeout_ms: int = 15000,
                confirm_token: str = '') -> dict:
    """Run a shell command in a terminal and capture its output. DESTRUCTIVE.

    Safety: refuses outright if the terminal's last line looks like a password
    or confirmation prompt (no token overrides this). Destructive commands
    (rm -rf, sudo, dd of=, git push --force, ...) are NOT run on first call —
    they return {requires_confirmation, confirm_token}; call again with that
    token (or use the `confirm` tool) to execute.

    terminal: uuid or friendly title; empty = focused terminal.
    Returns {ok, output, exit_code, timed_out, command} on execution, or
    {requires_confirmation, reason, matched_pattern, confirm_token, command},
    or {error: "refused_password_prompt", last_line}.
    """
    try:
        uuid = _resolve(terminal)
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)

    # 1. HARD password/confirm-prompt gate (non-overridable).
    try:
        probe = socket_client.call('probe_terminal', {'uuid': uuid})
    except socket_client.BridgeError as ex:
        return _err(ex)
    if probe.get('looks_like_password_prompt') or \
            safety.looks_like_password_prompt(probe.get('last_line')):
        return {'error': 'refused_password_prompt',
                'last_line': probe.get('last_line')}

    # 2. Denylist -> dry-run -> single-use confirm token.
    matched = safety.match_denylist(command)
    if matched:
        if not confirm_token:
            token = safety.STORE.issue(command, uuid, timeout_ms)
            return {
                'requires_confirmation': True,
                'reason': 'destructive_command',
                'matched_pattern': matched,
                'confirm_token': token,
                'command': command,
            }
        record = safety.STORE.consume(confirm_token, command, uuid)
        if record is None:
            return {'error': 'invalid_or_expired_token', 'command': command}
        timeout_ms = record.get('timeout_ms', timeout_ms)

    # 3. Execute.
    try:
        return _execute_capture(uuid, command, timeout_ms)
    except socket_client.BridgeError as ex:
        return _err(ex)


def _password_gate(uuid):
    """Return a refusal dict if the terminal's last line looks like a prompt
    we must not type into, else None. Raises BridgeError on transport failure.
    """
    probe = socket_client.call('probe_terminal', {'uuid': uuid})
    if probe.get('looks_like_password_prompt') or \
            safety.looks_like_password_prompt(probe.get('last_line')):
        return {'error': 'refused_password_prompt',
                'last_line': probe.get('last_line')}
    return None


def _do_send(uuid, text, enter, raw):
    return socket_client.call('send_text', {
        'uuid': uuid, 'text': text, 'append_enter': enter, 'raw': raw,
    })


@mcp.tool()
def confirm(confirm_token: str) -> dict:
    """Execute an action previously deferred by a confirmation gate.

    Consumes the single-use token issued by run_command or send_keys for a
    destructive action and performs it. Re-checks the password gate first.
    """
    record = safety.STORE.consume(confirm_token)
    if record is None:
        return {'error': 'invalid_or_expired_token'}
    uuid = record['uuid']
    try:
        refusal = _password_gate(uuid)
    except socket_client.BridgeError as ex:
        return _err(ex)
    if refusal:
        return refusal
    try:
        if record.get('kind') == 'send':
            extra = record.get('extra') or {}
            return _do_send(uuid, record['command'],
                            extra.get('enter', False), extra.get('raw', False))
        return _execute_capture(uuid, record['command'],
                                record.get('timeout_ms', 15000))
    except socket_client.BridgeError as ex:
        return _err(ex)


@mcp.tool()
def send_keys(text: str, terminal: str = '', enter: bool = False,
              raw: bool = False, confirm_token: str = '') -> dict:
    """Send literal keystrokes/text to a terminal. DESTRUCTIVE (escape hatch).

    Unlike run_command this does NOT capture output — it stages text exactly as
    typed. By default it does NOT press Enter (enter=false): use it to fill in
    a prompt, then call again with enter=true (or use run_command for a full
    command). Control/escape bytes are rejected unless raw=true. The same
    password gate + destructive denylist + confirmation flow as run_command
    applies. Always returns the post-send visible screen.

    terminal: uuid or friendly title; empty = focused terminal.
    """
    try:
        uuid = _resolve(terminal)
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)

    if not raw and safety.has_blocked_control_bytes(text):
        return {'error': 'control_bytes_blocked',
                'hint': 'set raw=true to send control/escape sequences'}

    try:
        refusal = _password_gate(uuid)
    except socket_client.BridgeError as ex:
        return _err(ex)
    if refusal:
        return refusal

    matched = safety.match_denylist(text)
    if matched:
        if not confirm_token:
            token = safety.STORE.issue(text, uuid, 0, kind='send',
                                       extra={'enter': enter, 'raw': raw})
            return {'requires_confirmation': True,
                    'reason': 'destructive_text', 'matched_pattern': matched,
                    'confirm_token': token, 'text': text}
        if safety.STORE.consume(confirm_token, text, uuid) is None:
            return {'error': 'invalid_or_expired_token', 'text': text}

    try:
        return _do_send(uuid, text, enter, raw)
    except socket_client.BridgeError as ex:
        return _err(ex)


@mcp.tool()
def find_in_scrollback(pattern: str, terminal: str = '', regex: bool = True,
                       case_sensitive: bool = False) -> dict:
    """Search a terminal's buffer (read-only) for a pattern.

    Returns {count, matches: [{row, line}], truncated} with absolute row
    numbers. If regex=false the pattern is matched literally. Does not move the
    user's view. terminal: uuid or friendly title; empty = focused.
    """
    try:
        uuid = _resolve(terminal)
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)
    import re as _re
    pat = pattern if regex else _re.escape(pattern)
    try:
        return socket_client.call('search_terminal', {
            'uuid': uuid, 'pattern': pat,
            'case_sensitive': case_sensitive, 'max_matches': 200,
        })
    except socket_client.BridgeError as ex:
        return _err(ex)


@mcp.tool()
def focus_terminal(terminal: str) -> dict:
    """Make a terminal visible (switch to its tab) and give it focus.

    terminal: uuid or friendly title.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('focus_terminal', {'uuid': uuid})
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def rename(terminal: str, title: str) -> dict:
    """Rename a terminal — set its titlebar label (persistent, per-terminal).

    Unlike a tab label (shared by split panes), this names the individual
    terminal and survives the running program changing its own title.
    terminal: uuid or current friendly title.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('rename_terminal',
                                  {'uuid': uuid, 'title': title})
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def scroll_to(terminal: str = '', row: int = -1, position: str = '') -> dict:
    """Scroll a terminal to an absolute buffer row (or 'top'/'bottom').

    Use a `row` from find_in_scrollback / list_bookmarks to jump there
    (centered). row < 0 with no position scrolls to the bottom (live output).
    terminal: uuid or friendly title; empty = focused.
    """
    try:
        uuid = _resolve(terminal)
        args = {'uuid': uuid, 'row': row}
        if position:
            args['position'] = position
        return socket_client.call('scroll_to', args)
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def list_bookmarks(terminal: str = '') -> dict:
    """List the bookmarks for a terminal as {row, label}.

    Bookmarks are absolute buffer rows the user marked on the line-number
    gutter (or that an agent added via add_bookmark). Pair with
    scroll_to(row=...) to jump to one. terminal: uuid or friendly title;
    empty = focused.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('list_bookmarks', {'uuid': uuid})
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def add_bookmark(terminal: str = '', row: int = -1, pattern: str = '',
                 label: str = '', regex: bool = False,
                 case_sensitive: bool = False) -> dict:
    """Add a bookmark — by absolute row OR by text/pattern search.

    Provide `pattern` to bookmark the first matching line (the matched line
    becomes the label unless you pass one); `regex=false` matches literally.
    Otherwise provide `row` (an absolute buffer row, e.g. from
    find_in_scrollback). terminal: uuid or friendly title; empty = focused.
    """
    try:
        uuid = _resolve(terminal)
        args = {'uuid': uuid, 'label': label}
        if pattern:
            args.update({'pattern': pattern, 'regex': regex,
                         'case_sensitive': case_sensitive})
        else:
            args['row'] = row
        return socket_client.call('add_bookmark', args)
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def remove_bookmark(terminal: str = '', row: int = -1) -> dict:
    """Remove the bookmark at `row` from `terminal`.

    terminal: uuid or friendly title; empty = focused.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('remove_bookmark',
                                  {'uuid': uuid, 'row': row})
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def rename_tab(terminal: str, title: str) -> dict:
    """Rename the TAB containing a terminal (the notebook tab label).

    The label is shared by all split panes in that tab. To rename a single
    pane instead, use `rename`. terminal: uuid or current friendly title.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('set_tab_title',
                                  {'uuid': uuid, 'title': title})
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def read_raw(terminal: str = '', start_row: int = 0, end_row: int = 0) -> dict:
    """Read an explicit absolute row range from the buffer (escape hatch).

    Use with the row numbers returned by find_in_scrollback. terminal: uuid or
    friendly title; empty = focused.
    """
    try:
        uuid = _resolve(terminal)
        return socket_client.call('read_raw', {
            'uuid': uuid, 'start_row': start_row, 'end_row': end_row,
        })
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


def _start_command(uuid, command):
    """Start a (possibly long-running) command in a terminal by typing it +
    Enter. Not captured — use run_command when you need the output."""
    if command:
        socket_client.call('send_text', {
            'uuid': uuid, 'text': command, 'append_enter': True, 'raw': False,
        })


@mcp.tool()
def new_tab(terminal: str = '', command: str = '', name: str = '') -> dict:
    """Open a new tab in the window of `terminal` (empty = focused terminal).

    Optionally name the tab and start a command in it. Returns the new
    terminal's uuid (address the tab by it).
    """
    try:
        uuid = _resolve(terminal)
        result = socket_client.call('new_tab', {
            'uuid': uuid, 'title': name or None})
        _start_command(result['uuid'], command)
        return {'uuid': result['uuid'], 'name': name or None}
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def split(terminal: str = '', axis: str = 'h', command: str = '') -> dict:
    """Split `terminal` (empty = focused) horizontally ('h') or vertically
    ('v'). Optionally start a command in the new pane. Returns its uuid."""
    try:
        uuid = _resolve(terminal)
        result = socket_client.call('split', {
            'uuid': uuid, 'axis': 'v' if axis == 'v' else 'h'})
        _start_command(result['uuid'], command)
        return {'uuid': result['uuid']}
    except (socket_client.BridgeError, ValueError) as ex:
        return _err(ex)


@mcp.tool()
def open_workspace(layout: dict, name: str = '') -> dict:
    """Create a named multi-tab / split workspace in a new window in one call.

    layout shape:
      {"window": {"tabs": [
        {"name": "server", "command": "npm run dev"},
        {"name": "tests", "command": "...", "splits": [
            {"axis": "v", "command": "npm test -- --watch", "name": "watch"}]}
      ]}}
    Each tab/split may carry an optional `command` (started with Enter, not
    captured) and `name`. Returns {window_uuid, terminals: [{role, uuid, kind}]}
    so you can immediately target a pane by uuid (and by name via run_command).
    """
    window = layout.get('window', layout) if isinstance(layout, dict) else None
    tabs = (window or {}).get('tabs') if isinstance(window, dict) else None
    if not tabs:
        return {'error': 'empty_layout',
                'hint': 'expected {window:{tabs:[...]}} with >=1 tab'}
    try:
        window_uuid = socket_client.call('new_window', {})['uuid']
    except socket_client.BridgeError as ex:
        return _err(ex)

    created = []
    try:
        for i, tab in enumerate(tabs):
            tname = tab.get('name')
            if i == 0:
                # First tab reuses the new window's initial terminal.
                tab_uuid = window_uuid
                if tname:
                    socket_client.call('set_tab_title',
                                       {'uuid': tab_uuid, 'title': tname})
            else:
                tab_uuid = socket_client.call('new_tab', {
                    'uuid': window_uuid, 'title': tname})['uuid']
            _start_command(tab_uuid, tab.get('command', ''))
            created.append({'role': tname, 'uuid': tab_uuid, 'kind': 'tab'})

            for sp in tab.get('splits') or []:
                su = socket_client.call('split', {
                    'uuid': tab_uuid,
                    'axis': 'v' if sp.get('axis') == 'v' else 'h'})['uuid']
                _start_command(su, sp.get('command', ''))
                created.append({'role': sp.get('name'), 'uuid': su,
                                'kind': 'split'})
    except socket_client.BridgeError as ex:
        # Partial workspace — report what was made plus the failure.
        return {'error': ex.code, 'window_uuid': window_uuid,
                'terminals': created, 'partial': True}

    return {'window_uuid': window_uuid, 'terminals': created}


def main():
    mcp.run()


if __name__ == '__main__':
    main()
