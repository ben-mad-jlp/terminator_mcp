# Terminator MCP Bridge plugin
# GPL v2 only
"""mcp_bridge.py - Hosts a local Unix-domain control socket inside the
Terminator GTK process so an external MCP server (the `terminator_mcp`
package) can let an LLM observe and drive the running terminals.

P0 scope: READ-ONLY. The socket exposes only liveness + read methods
(list terminals, read buffer/scrollback, read selection). No write / send /
command-execution methods exist yet — those arrive in later phases behind the
safety model. There is therefore nothing here that can mutate a terminal.

Design: the socket lives on the EXISTING GLib main loop via Gio.SocketService,
so its `incoming` callbacks run ON the GTK main thread. That gives direct,
thread-safe access to the live Vte.Terminal objects with no GLib.idle_add
marshaling and no extra threads. Handlers are kept fast and every read is
bounded/paginated so a single call can never dump a multi-MB buffer and freeze
the UI.

Wire protocol: length-prefixed JSON frames. Each frame is a 4-byte big-endian
unsigned length followed by that many bytes of UTF-8 JSON. One request frame
per connection, one response frame back, then the connection closes.
  request : {"method": "<name>", "args": {...}}
  response: {"ok": true, "result": <json>} | {"ok": false, "error": "<msg>"}

To enable: add 'MCPBridge' to the `enabled_plugins` list in Terminator's
config, e.g. ~/.config/terminator/config:
    [plugins]
    enabled_plugins = MCPBridge
"""

import os
import re
import json
import struct

from gi.repository import Gio, GLib

import terminatorlib.plugin as plugin
from terminatorlib.terminator import Terminator
from terminatorlib.util import dbg, err

# Every plugin Terminator loads must be named in AVAILABLE.
AVAILABLE = ['MCPBridge']

# Protocol / sizing constants. Reads are clamped so a handler stays fast.
HEADER = struct.Struct('>I')        # 4-byte big-endian length prefix
MAX_FRAME = 16 * 1024 * 1024        # 16 MiB hard ceiling on a single frame
DEFAULT_MAX_LINES = 2000            # default scrollback rows returned
MAX_LINES_CAP = 20000               # absolute clamp per call (UI-freeze guard)
PAGE_LIMIT_CAP = 5000               # absolute clamp on an explicit page
CAPTURE_TIMEOUT_MS = 15000          # default run_command_capture timeout
CAPTURE_TIMEOUT_CAP = 120000        # absolute clamp on a capture timeout
CAPTURE_POLL_MS = 60                # marker poll interval (yields to the loop)
SEARCH_SCAN_CAP = 5000              # max recent rows scanned by search

# Heuristic for a prompt we must never blind-type into. Mirrors the hard,
# non-overridable gate in terminator_mcp.safety; the plugin reports the signal,
# the MCP server enforces the refusal.
PASSWORD_RE = re.compile(
    r'(?i)(password.*:|passphrase|sudo.*password|verification code'
    r'|\(yes/no\)|are you sure|permission denied, please try again)')


def socket_dir():
    """Per-user runtime directory for the bridge socket.

    Linux: $XDG_RUNTIME_DIR (already 0700 per-user). macOS: $TMPDIR (per-user
    0700). Fallback: /tmp. The directory itself is created 0700. This exact
    convention is mirrored in terminator_mcp.socket_client so the external
    server finds the socket without configuration.
    """
    base = os.environ.get('XDG_RUNTIME_DIR') or os.environ.get('TMPDIR') \
        or '/tmp'
    return os.path.join(base, 'terminator-mcp-%d' % os.getuid())


def socket_path():
    """Absolute path to the bridge's Unix-domain socket."""
    return os.path.join(socket_dir(), 'bridge.sock')


def _read_rows(vte, start_row, end_row):
    """Return the text of buffer rows [start_row, end_row] INCLUSIVE.

    Verified against Vte 2.91 / vte3 0.84: the end row is EXCLUSIVE in
    get_text_range[_format], so to include end_row we pass (end_row + 1, col 0)
    — passing (end_row, -1) silently drops the last row. The text splits into
    exactly (end_row - start_row + 1) rows, so a line's index maps to its
    absolute row (start_row + index). The modern, non-deprecated
    get_text_range_format is preferred; we fall back to the deprecated
    get_text_range (returns a (text, attrs) tuple) for older builds.
    """
    if end_row < start_row:
        return ''
    from gi.repository import Vte
    end_excl = end_row + 1
    # Preferred: non-deprecated *_format API (vte 0.46+).
    if hasattr(vte, 'get_text_range_format'):
        try:
            res = vte.get_text_range_format(Vte.Format.TEXT,
                                            start_row, 0, end_excl, 0)
            if isinstance(res, tuple):
                res = res[0] if res else ''
            return res or ''
        except Exception as ex:
            err('mcp_bridge: get_text_range_format failed: %s' % ex)
    # Fallback: deprecated get_text_range (callback optional in 2.91 binding).
    try:
        res = vte.get_text_range(start_row, 0, end_excl, 0)
    except TypeError:
        try:
            res = vte.get_text_range(start_row, 0, end_excl, 0,
                                     lambda *a: True, None)
        except Exception as ex:
            err('mcp_bridge: get_text_range failed: %s' % ex)
            return ''
    except Exception as ex:
        err('mcp_bridge: get_text_range failed: %s' % ex)
        return ''
    if isinstance(res, tuple):
        res = res[0] if res else ''
    return res or ''


# Module-level singleton for the running socket service. Terminal.__init__
# reloads plugins with force=True on EVERY new terminal, which unloads and
# re-instantiates this plugin; without this guard the socket would flap (drop +
# rebind) on every new terminal/tab/split. We start the service exactly once
# and keep it for the life of the process.
_RUNNING = {'service': None}


class MCPBridge(plugin.Plugin):
    """Background plugin: opens the control socket on the GLib main loop.

    It has no UI capability; it exists purely to host the socket. Its __init__
    runs when the plugin registry first loads enabled plugins — which happens at
    startup via Terminal.__init__ → load_plugins, so the socket is up as soon as
    the first terminal exists.
    """
    capabilities = ['mcp_bridge']

    def __init__(self):
        plugin.Plugin.__init__(self)
        self.terminator = Terminator()
        self.path = socket_path()
        # Dispatch table — P0 is read-only. New phases register more here.
        self.handlers = {
            'ping': self._h_ping,
            'list_terminals_info': self._h_list_terminals_info,
            'get_terminal_text': self._h_get_terminal_text,
            'get_terminal_text_page': self._h_get_terminal_text_page,
            'get_selection': self._h_get_selection,
            # P1 — write/capture (guarded in the MCP server layer)
            'probe_terminal': self._h_probe_terminal,
            'run_command_capture': self._h_run_command_capture,
            # P2 — send / search / focus / raw read
            'send_text': self._h_send_text,
            'write_display': self._h_write_display,
            'redraw_prompt': self._h_redraw_prompt,
            'search_terminal': self._h_search_terminal,
            'focus_terminal': self._h_focus_terminal,
            'read_raw': self._h_read_raw,
            # P3 — layout / naming
            'new_window': self._h_new_window,
            'new_tab': self._h_new_tab,
            'split': self._h_split,
            'set_tab_title': self._h_set_tab_title,
            'rename_terminal': self._h_rename_terminal,
            # navigation + bookmarks
            'scroll_to': self._h_scroll_to,
            'list_bookmarks': self._h_list_bookmarks,
            'add_bookmark': self._h_add_bookmark,
            'remove_bookmark': self._h_remove_bookmark,
            'drain_bookmark_events': self._h_drain_bookmark_events,
        }
        # Start the socket exactly once for the whole process.
        if _RUNNING['service'] is None:
            try:
                _RUNNING['service'] = self._start_socket()
            except Exception as ex:
                err('mcp_bridge: failed to start socket: %s' % ex)
        self.service = _RUNNING['service']

    # ---- lifecycle -------------------------------------------------------

    def _start_socket(self):
        d = socket_dir()
        os.makedirs(d, mode=0o700, exist_ok=True)
        # Clear a stale socket from a previous run / crash.
        try:
            if os.path.exists(self.path):
                os.unlink(self.path)
        except OSError as ex:
            err('mcp_bridge: could not remove stale socket: %s' % ex)

        service = Gio.SocketService.new()
        address = Gio.UnixSocketAddress.new(self.path)
        service.add_address(address, Gio.SocketType.STREAM,
                            Gio.SocketProtocol.DEFAULT, None)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        service.connect('incoming', self._on_incoming)
        service.start()
        dbg('mcp_bridge: listening on %s' % self.path)
        return service

    def unload(self):
        """Called by the registry when the plugin is reloaded/disabled.

        Terminal.__init__ force-reloads plugins on every new terminal, so we do
        NOT tear the socket down here (that would flap it). The service is a
        module-level singleton kept for the process lifetime; the OS reclaims
        the socket file on exit. A genuine disable takes effect on next restart.
        """
        pass

    # ---- framing ---------------------------------------------------------

    def _read_exact(self, istream, n):
        """Read exactly n bytes from a Gio input stream (blocking, main-thread).

        Frames are tiny (a request is a few hundred bytes), so this completes
        in microseconds and does not meaningfully block the loop.
        """
        chunks = []
        remaining = n
        while remaining > 0:
            data = istream.read_bytes(remaining, None).get_data()
            if not data:
                return None
            chunks.append(data)
            remaining -= len(data)
        return b''.join(chunks)

    def _on_incoming(self, _service, connection, _source):
        """Handle one request/response on the GTK main thread, then close."""
        try:
            istream = connection.get_input_stream()
            ostream = connection.get_output_stream()

            header = self._read_exact(istream, HEADER.size)
            if header is None:
                return False
            (length,) = HEADER.unpack(header)
            if length <= 0 or length > MAX_FRAME:
                self._write_frame(ostream,
                                  {'ok': False, 'error': 'bad_frame_length'})
                return False
            payload = self._read_exact(istream, length)
            if payload is None:
                return False

            response = self._dispatch(payload)
            self._write_frame(ostream, response)
        except Exception as ex:
            err('mcp_bridge: request handling failed: %s' % ex)
            try:
                self._write_frame(connection.get_output_stream(),
                                  {'ok': False, 'error': 'internal_error'})
            except Exception:
                pass
        finally:
            try:
                connection.close(None)
            except Exception:
                pass
        return False

    def _write_frame(self, ostream, obj):
        body = json.dumps(obj).encode('utf-8')
        ostream.write_bytes(GLib.Bytes(HEADER.pack(len(body))), None)
        ostream.write_bytes(GLib.Bytes(body), None)
        ostream.flush(None)

    def _dispatch(self, payload):
        try:
            request = json.loads(payload.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return {'ok': False, 'error': 'invalid_json'}
        method = request.get('method')
        args = request.get('args') or {}
        if not isinstance(args, dict):
            return {'ok': False, 'error': 'args_must_be_object'}
        handler = self.handlers.get(method)
        if handler is None:
            return {'ok': False, 'error': 'unknown_method: %s' % method}
        try:
            return {'ok': True, 'result': handler(args)}
        except _BridgeError as ex:
            return {'ok': False, 'error': str(ex)}
        except Exception as ex:
            err('mcp_bridge: handler %s failed: %s' % (method, ex))
            return {'ok': False, 'error': 'handler_error'}

    # ---- terminal resolution --------------------------------------------

    def _resolve(self, uuid):
        """uuid (urn string) -> Terminal. Empty/None -> focused terminal."""
        if not uuid:
            term = self.terminator.last_focused_term
            if term is None:
                raise _BridgeError('no_focused_terminal')
            return term
        term = self.terminator.find_terminal_by_uuid(uuid)
        if term is None:
            raise _BridgeError('terminal_not_found')
        return term

    # ---- handlers (P0: read-only) ---------------------------------------

    def _h_ping(self, _args):
        return {'pong': True, 'terminals': len(self.terminator.terminals)}

    def _h_list_terminals_info(self, _args):
        focused = self.terminator.last_focused_term
        out = []
        for term in self.terminator.terminals:
            vte = term.get_vte()
            try:
                cwd = term.get_cwd()
            except Exception:
                cwd = None
            # Per-terminal title = the OSC title the running program set.
            try:
                title = vte.get_window_title() if vte else None
            except Exception:
                title = None
            # Window title = the toplevel GTK window title.
            try:
                window_title = term.get_toplevel().get_title()
            except Exception:
                window_title = None
            # Custom titlebar label set via `rename` (None unless customized).
            try:
                custom_title = term.titlebar.get_custom_string()
            except Exception:
                custom_title = None
            out.append({
                'uuid': term.uuid.urn,
                'name': getattr(term, 'assigned_name', None),
                'title': title,
                'window_title': window_title,
                'custom_title': custom_title,
                'is_focused': term is focused,
                'cwd': cwd,
                'rows': vte.get_row_count() if vte else None,
                'cols': vte.get_column_count() if vte else None,
            })
        return out

    def _text_slice(self, term, start_row, end_row, total_rows):
        vte = term.get_vte()
        try:
            _col, cursor_row = vte.get_cursor_position()
        except Exception:
            cursor_row = None
        text = _read_rows(vte, start_row, end_row)
        return {
            'text': text,
            'total_rows': total_rows,
            'cursor_row': cursor_row,
            'range': [start_row, end_row],
        }

    def _h_get_terminal_text(self, args):
        term = self._resolve(args.get('uuid', ''))
        mode = args.get('mode', 'visible')
        max_lines = _clamp(int(args.get('max_lines', DEFAULT_MAX_LINES)),
                           1, MAX_LINES_CAP)
        from_end = bool(args.get('from_end', True))
        vte = term.get_vte()
        vadj = vte.get_vadjustment()
        total_rows = int(vadj.get_upper())
        row_count = vte.get_row_count()

        if mode == 'visible':
            value = int(vadj.get_value())
            start = value
            end = value + row_count - 1
            result = self._text_slice(term, start, end, total_rows)
            result['next_offset'] = None
            return result

        # scrollback mode: bounded window of the full buffer
        if from_end:
            start = max(0, total_rows - max_lines)
            end = max(start, total_rows - 1)
            next_offset = start - 1 if start > 0 else None
        else:
            start = 0
            end = min(total_rows - 1, max_lines - 1)
            next_offset = end + 1 if end + 1 < total_rows else None
        result = self._text_slice(term, start, end, total_rows)
        result['next_offset'] = next_offset
        return result

    def _h_get_terminal_text_page(self, args):
        """Explicit forward pager for huge scrollback: rows [offset, offset+limit)."""
        term = self._resolve(args.get('uuid', ''))
        offset = max(0, int(args.get('offset', 0)))
        limit = _clamp(int(args.get('limit', 500)), 1, PAGE_LIMIT_CAP)
        vte = term.get_vte()
        total_rows = int(vte.get_vadjustment().get_upper())
        start = min(offset, max(0, total_rows - 1))
        end = min(start + limit - 1, total_rows - 1)
        result = self._text_slice(term, start, end, total_rows)
        nxt = end + 1
        result['next_offset'] = nxt if nxt < total_rows else None
        return result

    def _h_get_selection(self, args):
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        if not vte.get_has_selection():
            return {'has_selection': False, 'text': ''}
        try:
            from gi.repository import Vte
            text = vte.get_text_selected(Vte.Format.TEXT)
        except Exception:
            # Older bindings: get_text_selected() with no format arg.
            try:
                text = vte.get_text_selected()
            except Exception:
                text = ''
        if isinstance(text, tuple):
            text = text[0] if text else ''
        return {'has_selection': True, 'text': text or ''}

    # ---- handlers (P1: write / capture — guarded in the MCP server) ------

    def _last_nonempty_line(self, vte):
        """Cheap read of the last non-empty visible line (for the safety probe)."""
        vadj = vte.get_vadjustment()
        total = int(vadj.get_upper())
        row_count = vte.get_row_count()
        start = max(0, total - row_count)
        text = _read_rows(vte, start, max(start, total - 1))
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines[-1] if lines else ''

    def _wait_ready(self, vte, max_ms=2000):
        """Block (pumping the loop) until the terminal shows a prompt.

        A freshly-spawned shell has a blank screen until it draws its first
        prompt; feeding before then races shell startup. We wait until the last
        visible line is non-empty, capped at max_ms so we never hang.
        """
        if self._last_nonempty_line(vte):
            return
        loop = GLib.MainLoop()
        live = {'poll', 'timeout'}

        def poll():
            if self._last_nonempty_line(vte):
                live.discard('poll')
                loop.quit()
                return False
            return True

        def on_timeout():
            live.discard('timeout')
            loop.quit()
            return False

        poll_id = GLib.timeout_add(60, poll)
        timeout_id = GLib.timeout_add(max_ms, on_timeout)
        loop.run()
        if 'poll' in live:
            GLib.source_remove(poll_id)
        if 'timeout' in live:
            GLib.source_remove(timeout_id)

    def _h_probe_terminal(self, args):
        """Cheap preflight the MCP server calls before any write."""
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        last = self._last_nonempty_line(vte)
        return {
            'last_line': last,
            'looks_like_password_prompt': bool(PASSWORD_RE.search(last or '')),
            'has_selection': vte.get_has_selection(),
        }

    def _h_run_command_capture(self, args):
        """Run a command and capture its output, keeping the command line clean.

        The command is fed ALONE (so its echo looks exactly like what a user
        typed), then a single exit-code marker is fed on its own line:
            <command>
            printf "<END> $?\\n"
        We poll the bottom of the buffer (nested GLib loop, never a blocking
        sleep) until the END marker appears, then take everything before it,
        drop the command-echo line and the trailing marker/prompt echo, and
        return the output. `$?` is read on the line after the command; on most
        interactive shells (incl. zsh) this is the command's status, but a shell
        whose prompt hook clobbers $? may report 0 — exit_code is best-effort.

        Best-effort overall: interactive TUIs / line-rewriting shells defeat the
        marker (documented; fall back to send_keys + read_terminal).
        """
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        command = args.get('command', '')
        timeout_ms = _clamp(int(args.get('timeout_ms', CAPTURE_TIMEOUT_MS)),
                            100, CAPTURE_TIMEOUT_CAP)

        # Wait for the shell prompt before feeding (avoids racing shell startup).
        self._wait_ready(vte)

        nonce = os.urandom(8).hex()
        end_marker = '__MCP_E_%s__' % nonce
        end_re = re.compile(r'(?m)^%s (-?\d+)\s*$' % re.escape(end_marker))

        # Feed the command on its own line (clean echo), then the exit-code
        # marker on a separate line.
        term.feed(command + '\n')
        term.feed('printf "%s %%s\\n" "$?"\n' % end_marker)

        readback = _clamp(int(vte.get_row_count()) + 1000, 1, MAX_LINES_CAP)

        def read_tail():
            total = int(vte.get_vadjustment().get_upper())
            start = max(0, total - readback)
            return _read_rows(vte, start, max(start, total - 1))

        state = {'found': False, 'exit': None, 'text': '', 'live': set()}
        loop = GLib.MainLoop()

        def poll():
            text = read_tail()
            end = end_re.search(text)
            if end:
                state['found'] = True
                state['exit'] = int(end.group(1))
                state['text'] = text[:end.start()]
                state['live'].discard('poll')
                loop.quit()
                return False
            return True

        def on_timeout():
            state['text'] = read_tail()
            state['live'].discard('timeout')
            loop.quit()
            return False

        poll_id = GLib.timeout_add(CAPTURE_POLL_MS, poll)
        timeout_id = GLib.timeout_add(timeout_ms, on_timeout)
        state['live'].update({'poll', 'timeout'})
        loop.run()
        if 'poll' in state['live']:
            GLib.source_remove(poll_id)
        if 'timeout' in state['live']:
            GLib.source_remove(timeout_id)

        output = self._extract_output(state['text'], command, end_marker)
        return {
            'ok': state['found'],
            'output': output,
            'exit_code': state['exit'],
            'timed_out': not state['found'],
            'command': command,
        }

    @staticmethod
    def _extract_output(text, command, end_marker):
        """Pull the command's output from the captured tail.

        The tail (before the END marker) looks like:
            <prompt> <command>          <- command echo
            <output...>
            <prompt> printf "<END> ..."  <- the marker-feed echo
        Drop the leading command-echo line and trailing marker/prompt-echo and
        blank lines, leaving the output.
        """
        lines = text.split('\n')
        # Drop the command-echo line: the first line that ends with the command.
        cmd_tail = command.strip().splitlines()[0] if command.strip() else ''
        for i, line in enumerate(lines):
            if cmd_tail and line.rstrip().endswith(cmd_tail):
                lines = lines[i + 1:]
                break
        else:
            if lines:
                lines = lines[1:]
        # Drop trailing marker-echo / prompt / blank lines.
        while lines and (end_marker in lines[-1] or not lines[-1].strip()):
            lines.pop()
        return '\n'.join(lines).strip('\n')

    # ---- handlers (P2: send / search / focus / raw read) ----------------

    def _settle(self, ms):
        """Pump the main loop briefly so freshly-sent input can echo/render."""
        loop = GLib.MainLoop()
        GLib.timeout_add(ms, lambda: (loop.quit(), False)[1])
        loop.run()

    def _visible_text(self, vte):
        vadj = vte.get_vadjustment()
        total = int(vadj.get_upper())
        row_count = vte.get_row_count()
        value = int(vadj.get_value())
        start = value
        end = min(value + row_count - 1, max(0, total - 1))
        return _read_rows(vte, start, end)

    def _h_send_text(self, args):
        """Send raw text to the terminal's child. Always echoes the screen."""
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        text = args.get('text', '')
        if args.get('append_enter'):
            text = text + '\n'
        data = text.encode() if isinstance(text, str) else text
        term.feed(data)
        self._settle(120)
        return {
            'ok': True,
            'sent_bytes': len(data),
            'visible_text': self._visible_text(vte),
        }

    def _h_write_display(self, args):
        """Write raw bytes to the terminal display only — never to the child's
        stdin. Lets an external process stream output into a pane while the
        shell stays at its prompt. ANSI escapes and color codes are rendered."""
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        text = args.get('text', '')
        data = text.encode() if isinstance(text, str) else text
        vte.feed(data)
        # No _settle: vte.feed is synchronous to the display, and we may be
        # called once per output line at high frequency.
        return {'ok': True, 'wrote_bytes': len(data)}

    def _h_redraw_prompt(self, args):
        """Force the shell to redraw its prompt + input line by flickering the
        pty winsize. A bare SIGWINCH is a no-op when the size hasn't actually
        changed; setting winsize to (cols-1, rows) then back to (cols, rows)
        triggers two real SIGWINCHes and bash's readline picks up the second
        one as a redraw. Used after streaming external output into the pane."""
        import fcntl
        import struct
        import termios
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        try:
            pty_obj = vte.get_pty()
            master_fd = pty_obj.get_fd() if pty_obj else None
        except Exception:
            master_fd = None
        if master_fd is None or master_fd < 0:
            return {'ok': False, 'error': 'no_pty_fd'}
        rows = max(int(vte.get_row_count() or 24), 2)
        cols = max(int(vte.get_column_count() or 80), 2)
        try:
            small = struct.pack('HHHH', rows, cols - 1, 0, 0)
            real = struct.pack('HHHH', rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, small)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, real)
        except OSError as e:
            return {'ok': False, 'error': 'ioctl_failed: %s' % e}
        return {'ok': True, 'rows': rows, 'cols': cols}

    def _h_search_terminal(self, args):
        """Read-only regex scan of the buffer → matches with absolute row nums.

        Uses a Python regex over the text (not vte.search_set_regex, which
        would move the user's view) so the search has no side effects and can
        report real row numbers.
        """
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        pattern = args.get('pattern', '')
        flags = 0 if args.get('case_sensitive') else re.IGNORECASE
        max_matches = _clamp(int(args.get('max_matches', 200)), 1, 2000)
        matches, truncated = self._search_rows(
            vte, pattern, bool(args.get('case_sensitive')),
            regex=True, max_matches=max_matches)
        return {'count': len(matches), 'matches': matches,
                'truncated': truncated}

    def _search_rows(self, vte, pattern, case_sensitive=False, regex=True,
                     max_matches=200):
        """Scan PHYSICAL rows one at a time so each match's row is exactly the
        row read_raw(row) reproduces (a bulk read + splitlines drifts because
        get_text_range joins wrapped rows). Bounded to the most recent
        SEARCH_SCAN_CAP rows. Returns (matches, truncated)."""
        flags = 0 if case_sensitive else re.IGNORECASE
        pat = pattern if regex else re.escape(pattern)
        try:
            rx = re.compile(pat, flags)
        except re.error as ex:
            raise _BridgeError('bad_regex: %s' % ex)
        vadj = vte.get_vadjustment()
        lower = int(vadj.get_lower())
        total = int(vadj.get_upper())
        start = max(lower, total - SEARCH_SCAN_CAP)
        matches = []
        truncated = False
        for row in range(start, total):
            line = _read_rows(vte, row, row).rstrip('\n')
            if not line or '__MCP_S_' in line or '__MCP_E_' in line:
                # skip our own injected run_command capture sentinels
                continue
            if rx.search(line):
                matches.append({'row': row, 'line': line})
                if len(matches) >= max_matches:
                    truncated = True
                    break
        return matches, truncated

    def _h_focus_terminal(self, args):
        term = self._resolve(args.get('uuid', ''))
        term.ensure_visible_and_focussed()
        return {'ok': True, 'uuid': term.uuid.urn}

    def _h_read_raw(self, args):
        """Read an explicit absolute row range [start_row, end_row]."""
        term = self._resolve(args.get('uuid', ''))
        vte = term.get_vte()
        vadj = vte.get_vadjustment()
        lower = int(vadj.get_lower())
        total = int(vadj.get_upper())
        last = max(lower, total - 1)
        a = _clamp(int(args.get('start_row', 0)), lower, last)
        b = _clamp(int(args.get('end_row', a)), lower, last)
        if b < a:
            a, b = b, a
        if b - a + 1 > MAX_LINES_CAP:
            b = a + MAX_LINES_CAP - 1
        return {'text': _read_rows(vte, a, b), 'range': [a, b],
                'total_rows': total}

    # ---- handlers (P3: layout / naming) ---------------------------------

    def _all_uuids(self):
        return set(t.uuid.urn for t in self.terminator.terminals)

    def _detect_new(self, before):
        """Return the single newly-created terminal uuid, mirroring ipc.py."""
        added = list(self._all_uuids() - before)
        if len(added) != 1:
            raise _BridgeError('uuid_detect_failed')
        return added[0]

    def _set_tab_title(self, term, title):
        """Set the label of the tab CONTAINING `term` (not the current tab).

        ipc.py's set_tab_title renames whatever tab is active; to rename a tab
        by one of its terminals we locate the notebook page whose descendants
        include `term` (the approach ipc.py's get_tab_title uses).
        """
        from terminatorlib.factory import Factory
        from terminatorlib.util import enumerate_descendants
        window = term.get_toplevel()
        if not window.is_child_notebook():
            return False
        notebook = window.get_children()[0]
        maker = Factory()
        for tab_child in notebook.get_children():
            terms = [tab_child]
            if not maker.isinstance(tab_child, 'Terminal'):
                terms = enumerate_descendants(tab_child)[1]
            if term in terms:
                notebook.get_tab_label(tab_child).set_custom_label(
                    title, force=True)
                return True
        return False

    def _h_new_window(self, _args):
        before = self._all_uuids()
        self.terminator.new_window()
        return {'uuid': self._detect_new(before)}

    def _h_new_tab(self, args):
        term = self._resolve(args.get('uuid', ''))
        before = self._all_uuids()
        term.key_new_tab()
        uuid = self._detect_new(before)
        title = args.get('title')
        if title:
            new_term = self.terminator.find_terminal_by_uuid(uuid)
            if new_term is not None:
                self._set_tab_title(new_term, title)
        return {'uuid': uuid, 'title': title}

    def _h_split(self, args):
        term = self._resolve(args.get('uuid', ''))
        axis = args.get('axis', 'h')
        before = self._all_uuids()
        if axis == 'v':
            term.key_split_vert()
        else:
            term.key_split_horiz()
        return {'uuid': self._detect_new(before)}

    def _h_set_tab_title(self, args):
        term = self._resolve(args.get('uuid', ''))
        return {'ok': bool(self._set_tab_title(term, args.get('title', ''))),
                'uuid': term.uuid.urn, 'title': args.get('title', '')}

    def _h_rename_terminal(self, args):
        """Set a single terminal's titlebar label (persistent, per-terminal).

        Uses titlebar.set_custom_string (same call ipc.py uses), which marks the
        label custom so the running program's title updates won't override it.
        """
        term = self._resolve(args.get('uuid', ''))
        title = args.get('title', '')
        term.titlebar.set_custom_string(title)
        # Also make the new name the terminal's addressable name.
        term.assigned_name = title
        return {'ok': True, 'uuid': term.uuid.urn, 'title': title}

    # ---- handlers: navigation + minimap bookmarks -----------------------

    def _h_scroll_to(self, args):
        """Scroll a terminal to an absolute row or a named position.

        args: row (int; <0 means bottom) and/or position ('top'|'bottom').
        Centers the requested row in the viewport.
        """
        term = self._resolve(args.get('uuid', ''))
        adj = term.get_vte().get_vadjustment()
        lower = adj.get_lower()
        upper = adj.get_upper()
        page = adj.get_page_size()
        bottom = max(lower, upper - page)
        position = args.get('position')
        row = args.get('row', None)
        if position == 'top':
            value = lower
        elif position == 'bottom':
            value = bottom
        elif row is not None:
            r = int(row)
            value = bottom if r < 0 else max(lower, min(bottom, r - page / 2.0))
        else:
            value = adj.get_value()
        adj.set_value(value)
        return {'ok': True, 'value': int(adj.get_value()),
                'lower': int(lower), 'upper': int(upper), 'page': int(page)}

    def _h_list_bookmarks(self, args):
        term = self._resolve(args.get('uuid', ''))
        return {'bookmarks': term.get_bookmarks()}

    def _h_add_bookmark(self, args):
        """Bookmark a row, found either by absolute row or by text/pattern.

        args: row (int) OR pattern (str, with regex/case_sensitive). When a
        pattern is given the first matching row is bookmarked and the matched
        line becomes the default label.
        """
        term = self._resolve(args.get('uuid', ''))
        pattern = args.get('pattern')
        if pattern:
            found, _trunc = self._search_rows(
                term.get_vte(), pattern, bool(args.get('case_sensitive')),
                bool(args.get('regex', False)), max_matches=1)
            if not found:
                return {'ok': False, 'error': 'pattern_not_found',
                        'pattern': pattern}
            row = found[0]['row']
            label = args.get('label') or found[0]['line'].strip()
        else:
            row = int(args.get('row', 0))
            label = args.get('label', '')
        term.add_bookmark(row, label)
        return {'ok': True, 'bookmark': {'row': row, 'label': label}}

    def _h_remove_bookmark(self, args):
        term = self._resolve(args.get('uuid', ''))
        removed = term.remove_bookmark(int(args.get('row', -1)))
        return {'ok': bool(removed)}

    def _h_drain_bookmark_events(self, args):
        """Drain unseen bookmark clicks for the UserPromptSubmit hook.

        Returns events across ALL terminals (the hook runs once per submit and
        needs everything since the last drain). Each event includes the row,
        the line text, ±`context` rows of surrounding context, and identifying
        info for the terminal. Resets the per-terminal unseen queue.
        """
        context = _clamp(int(args.get('context', 3)), 0, 20)
        out = []
        for term in self.terminator.terminals:
            rows = term.drain_unseen_bookmarks()
            if not rows:
                continue
            vte = term.get_vte()
            total = int(vte.get_vadjustment().get_upper())
            for row in rows:
                start = max(0, row - context)
                end = min(total - 1, row + context)
                text = _read_rows(vte, start, end) if total > 0 else ''
                out.append({
                    'uuid': term.uuid.urn,
                    'name': getattr(term, 'assigned_name', None),
                    'row': row,
                    'context_start': start,
                    'context_end': end,
                    'line': _read_rows(vte, row, row).rstrip('\n'),
                    'context': text,
                })
        return {'events': out}


class _BridgeError(Exception):
    """Handler-level error surfaced to the client as {ok:false, error:...}."""


def _clamp(value, low, high):
    return max(low, min(high, value))
