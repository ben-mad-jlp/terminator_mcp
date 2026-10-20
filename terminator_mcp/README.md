# terminator-mcp

An MCP server that lets an LLM (e.g. Claude) observe and drive a running
[Terminator](https://github.com/gnome-terminator/terminator) terminal emulator:
read buffers, run and send commands behind a safety model (see [Safety](#safety)),
and manage tabs, panes and bookmarks.

## Architecture

```
Claude  --stdio/JSON-RPC-->  terminator-mcp  (this package, separate process)
                                  |  length-prefixed JSON frames
                                  v  over a per-user Unix-domain socket
   Terminator GTK process  ->  MCPBridge plugin (Gio.SocketService on the
                                  GLib main loop; direct, thread-safe VTE access)
```

The bridge runs **inside** Terminator as a plugin, on the existing GLib main
loop — so it touches the live `Vte.Terminal` objects safely with no extra
threads. The MCP server is a **separate process**: it never imports GTK/VTE and
cannot crash or freeze your terminals. Works on Linux and macOS (no D-Bus
dependency).

## Install & run

There are **two pieces**:

- **(A) the bridge plugin** — runs *inside* Terminator, so it needs the same
  Python that runs Terminator (GTK/VTE). This is the part that differs by OS.
- **(B) the MCP server** (this package) — a standalone process that only needs
  the `mcp` package. **Identical on Linux and macOS**; it never imports GTK/VTE.

They talk over a per-user Unix socket
(`$XDG_RUNTIME_DIR/terminator-mcp-$UID/bridge.sock` on Linux,
`$TMPDIR/terminator-mcp-$UID/bridge.sock` on macOS). Tools return
`{"error":"terminator_not_running"}` until Terminator is up with the plugin.

Replace `$REPO` below with your checkout path (e.g. `~/Projects/code/terminator_mcp`).

### Ubuntu / Linux

This fork is rebranded so it coexists with the system `/usr/bin/terminator`
without sharing config, plugins, or D-Bus bus name. See
[Fork notes](#fork-notes) at the bottom for the details.

**A. System deps** (sufficient even if the system `terminator` package isn't
installed — we just need the GTK/VTE bindings):
```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-vte-2.91 \
                 gir1.2-keybinder-3.0 python3-psutil python3-configobj \
                 python3-venv dbus-x11 gettext
```

**B. Plugin + per-fork config dir**
```bash
mkdir -p ~/.config/terminator-mcp/plugins
cp $REPO/terminatorlib/plugins/mcp_bridge.py ~/.config/terminator-mcp/plugins/
```
Enable it by writing `~/.config/terminator-mcp/config`:
```ini
[global_config]
  enabled_plugins = MCPBridge
```

**C. Launcher** (`~/.local/bin/terminator-mcp` — put `~/.local/bin` on `PATH`):
```bash
mkdir -p ~/.local/bin
cat > ~/.local/bin/terminator-mcp <<EOF
#!/usr/bin/env bash
exec python3 $REPO/terminator "\$@"
EOF
chmod +x ~/.local/bin/terminator-mcp
```

**D. Sidebar icon + .desktop** (Wayland-friendly: GNOME matches the running
window's app_id, set via `GLib.set_prgname('terminator-mcp')`, against the
`.desktop` filename):
```bash
mkdir -p ~/.local/share/icons/hicolor/scalable/apps
cp $REPO/data/icons/hicolor/scalable/apps/terminator-mcp.svg \
   ~/.local/share/icons/hicolor/scalable/apps/

mkdir -p ~/.local/share/applications
cat > ~/.local/share/applications/terminator-mcp.desktop <<EOF
[Desktop Entry]
Type=Application
Name=Terminator MCP
GenericName=Terminal
Comment=Terminator fork with MCP bridge for Claude
TryExec=terminator-mcp
Exec=terminator-mcp
Icon=terminator-mcp
Terminal=false
Categories=GTK;Utility;TerminalEmulator;
StartupNotify=true
StartupWMClass=terminator-mcp
EOF
update-desktop-database ~/.local/share/applications || true
```

**E. MCP server**
```bash
python3 -m venv ~/.local/share/terminator-mcp/venv
~/.local/share/terminator-mcp/venv/bin/pip install 'mcp<2'   # 2.x renamed FastMCP
claude mcp add terminator -s user \
  --env PYTHONPATH=$REPO \
  -- ~/.local/share/terminator-mcp/venv/bin/python -m terminator_mcp
```

Now launch `terminator-mcp`. The MCPBridge plugin starts the socket at
`/run/user/$UID/terminator-mcp-$UID/bridge.sock`. In a *new* Claude session,
`list_terminals` should return your terminal.

### macOS (Homebrew GTK)

Terminator isn't a native Mac app; run it under Homebrew's GTK Python.

**A. Plugin**
```bash
brew install pygobject3 gtk+3 vte3
# a venv that can see Homebrew's gi, plus Terminator's pure-python deps
/opt/homebrew/bin/python3 -m venv --system-site-packages ~/.local/share/terminator-gtk
~/.local/share/terminator-gtk/bin/pip install psutil configobj
mkdir -p ~/.config/terminator-mcp/plugins
cp $REPO/terminatorlib/plugins/mcp_bridge.py ~/.config/terminator-mcp/plugins/
```
Enable `MCPBridge` in `~/.config/terminator-mcp/config` (same `[global_config]`
stanza). Launch Terminator with that Python from the checkout:
```bash
cd $REPO
( ulimit -n 1024; ~/.local/share/terminator-gtk/bin/python ./terminator )
```
> **macOS gotcha:** keep `ulimit -n` finite (e.g. `1024`). If it is `unlimited`,
> glib's `fdwalk` fails and new shells won't spawn inside Terminator.

**B. MCP server** — same as Linux (a venv with `mcp` + `claude mcp add … PYTHONPATH=…`).
It is a pure socket client and does **not** need GTK.

### Sanity check (both platforms)

1. Start Terminator — the plugin loads when the first terminal is created.
2. In Claude, call `list_terminals`. You should see your terminal. A
   `terminator_not_running` error means the plugin isn't loaded (check it is in
   `enabled_plugins` and on the plugin path).

The minimap, line-number gutter, and bookmark menus are pure GUI and work
regardless of the MCP server.

## Tools

Read-only:

| Tool | Args | Returns |
|------|------|---------|
| `list_terminals` | — | `{terminals: [{uuid, name, title, window_title, is_focused, cwd, rows, cols}]}` (`name` auto-assigned `term-N`, or the rename) |
| `get_focused_terminal` | — | `{uuid}` |
| `tail` | `terminal?, lines=40` | `{text, total_rows, cursor_row, range, next_offset}` |
| `read_terminal` | `terminal?, mode='scrollback', max_lines=2000, offset=0, from_end=true` | `{text, total_rows, cursor_row, range, next_offset}` |
| `get_selection` | `terminal?` | `{has_selection, text}` |
| `find_in_scrollback` | `pattern, terminal?, regex=true, case_sensitive=false` | `{count, matches: [{row, line}], truncated}` |
| `read_raw` | `terminal?, start_row, end_row` | `{text, range, total_rows}` |

Write / command (gated — see Safety):

| Tool | Args | Returns |
|------|------|---------|
| `run_command` ⚠ | `command, terminal?, timeout_ms=15000, confirm_token?` | `{ok, output, exit_code, timed_out, command}` / `{requires_confirmation, …}` / `{error:"refused_password_prompt", …}` |
| `send_keys` ⚠ | `text, terminal?, enter=false, raw=false, confirm_token?` | `{ok, sent_bytes, visible_text}` / `{requires_confirmation, …}` |
| `confirm` | `confirm_token` | result of the deferred run/send |
| `focus_terminal` | `terminal` | `{ok, uuid}` |
| `rename` | `terminal, title` | rename one terminal's titlebar (per-pane) |
| `rename_tab` | `terminal, title` | rename the tab containing a terminal |
| `scroll_to` | `terminal?, row, position?` | scroll to a row (or 'top'/'bottom'); row<0 = bottom |
| `add_bookmark` | `terminal?, row \| pattern, label?, regex?, case_sensitive?` | bookmark a row, by number or text search |
| `list_bookmarks` | `terminal?` | minimap bookmarks `[{row, label}]` |
| `new_tab` | `terminal?, name?` | `{uuid, name}`. Layout only; run things with `run_command` |
| `split` | `terminal?, axis='h'` | `{uuid}`. Layout only; run things with `run_command` |

`terminal` accepts a UUID or a friendly title (case-insensitive); empty selects
the focused terminal. If Terminator isn't running / the plugin isn't enabled,
tools return `{"error": "terminator_not_running"}`.

## Safety

- **Reads are free.** list / tail / read_terminal / get_selection /
  find_in_scrollback / read_raw never modify a terminal.
- **Hard password gate (non-overridable).** `run_command` / `send_keys` refuse
  outright if the terminal's last line looks like a password / passphrase /
  sudo / yes-no prompt. No `confirm_token` bypasses this.
- **Destructive denylist → confirmation.** `rm -rf`, `sudo`, `dd of=`, `mkfs`,
  fork bombs, `> /dev/sd*`, `shutdown/reboot`, `git push --force`, `chmod -R`,
  … are not run on first call; they return a single-use `confirm_token`
  (~60 s TTL, bound to the exact command + terminal). Re-call with it, or use
  `confirm`.
- **`send_keys`** does not press Enter by default and rejects control/escape
  bytes unless `raw=true`. With `raw=true` it decodes backslash escapes
  (`\x03` = Ctrl+C, `\e[A` = Up, `\\` = literal backslash), since JSON tool
  arguments can't carry `\x` escapes themselves. It always returns the
  post-send screen.
- Command-output capture (`run_command`) is best-effort; interactive TUIs can
  defeat it — fall back to `send_keys` + `read_terminal`.

## Bookmarks

Click a row in the line-number gutter to bookmark it. The
`terminator_mcp.bookmark_hook` UserPromptSubmit hook sends your new bookmarks
to Claude with your next prompt.

- **Text is captured when you click.** The clicked row and the 3 rows on each
  side are captured then, so the agent gets exactly what you saw, even if you
  maximize, split or resize before sending. To send more per bookmark,
  maximize the pane before clicking: wider rows hold more of each log line.
- **Gutter marks are tied to row numbers, not text.** Any reflow (maximize,
  split, resize, font change) re-wraps lines, so a mark can end up beside
  different text. That only affects the display; what gets sent was captured
  at click time.
- **Rows are hex, like the gutter.** The hook reports `row=0x69 (105)`. The
  decimal is for `read_raw`.
- **Row numbers keep counting after `clear` or `reset`.** They count from the
  first line the pane ever printed, so a row number keeps naming the same line
  even when older lines are dropped.
- **Each bookmark is sent once, to whichever Claude session you next submit a
  prompt in.** There is one queue for all of Terminator. Subagent reports and
  messages between sessions also fire the hook, but they leave the queue
  alone; only a prompt you type takes it. To send the same line
  again (to the same or another session), click it off and on again. Clicking
  a line that is still marked only removes the mark. Removing a bookmark
  before sending cancels it.
- **One bookmark per row number.** You can't mark two different lines that
  land on the same row number in different layouts.

## Owned panes (bridge RPCs for other MCP servers)

`spawn_in_pane`, `owned_status` and `signal_owned` let another MCP server run a
process with an exact argv and environment in a managed tab, and stop it later.
They have no MCP tools here: they exist so a client can wrap them in its own
policy, such as jlp-claude-plugin's `run_sim`. Design and semantics:
[doc/planning/owned_panes.md](doc/planning/owned_panes.md).

## Restore layout

Right-click → **Restore layout ▸ <name>** finds the tab labeled `<name>` in the
window, or builds it as a new tab from the saved layout, then types each pane's
**Custom command** into the panes sitting at an idle prompt. Busy panes get a
titlebar badge (`busy: ssh`) that clears when you focus them. Panes are matched
by title, so title every pane in the layout.

- Enable **RestoreLayout** in Preferences → Plugins.
- Save a single-tab layout in Preferences → Layouts, with one shell line per
  pane as its Custom command, e.g. `ssh -t pnp.jlp "bash -ic 'devtach launch'"`.
  The outer quotes matter: ssh hands the remote shell one string to re-parse.
- Don't launch these layouts with `terminator -l`: that spawns the commands in
  place of the shell.
- So a dropped ssh exits soon after an unpause instead of hanging, set
  `ServerAliveInterval 5` and `ServerAliveCountMax 2` for those hosts in
  `~/.ssh/config`.

Design: [doc/planning/restore_layout.md](doc/planning/restore_layout.md).

## Tips

### Using with tmux (or screen) on a remote host

tmux uses the terminal's **alternate screen**, which VTE does not record into
its scrollback. The MCP server reads from VTE's scrollback, so by default
`read_raw`, `tail`, `find_in_scrollback`, the line-number gutter, and bookmarks
all see *nothing* while you are attached to tmux — even though text is visible
on screen.

If you want remote-session persistence (so a roslaunch keeps running across VM
suspends or SSH disconnects) **and** working MCP/bookmarks, disable tmux's
alt-screen on the remote. In `~/.tmux.conf` on the remote host:

```tmux
set -ga terminal-overrides ',*:smcup@:rmcup@'
```

Note `-ga` (global + append), not `-g` — plain `-g` replaces the whole
`terminal-overrides` string and the change won't take effect. After
`tmux kill-server` and reattach, tmux output flows into VTE's normal scrollback
and all MCP read tools / bookmarks work through tmux normally.

Trade-off: detaching no longer "restores" the pre-tmux screen — the tmux
contents stay visible in scrollback. For the read-tools-through-tmux use case
this is usually what you want anyway.

## Fork notes

This fork is set up to run **alongside** an unmodified system Terminator,
without sharing state. Specifically:

- **`APP_NAME = 'terminator-mcp'`** (`terminatorlib/version.py`) — drives the
  Wayland `app_id`, the gettext domain, and the icon-theme lookup name.
- **Config dir is `~/.config/terminator-mcp/`** (`terminatorlib/util.py`) —
  fully separate plugins, layouts, and profiles from the system Terminator's
  `~/.config/terminator/`.
- **D-Bus bus name is `net.tenshu.TerminatorMCP`** (`terminatorlib/ipc.py`) —
  `--new-tab`, `--toggle-visibility`, `remotinator`, and the single-instance
  handoff continue to work *between this fork's windows*, but never collide
  with the system Terminator's bus. Caveat: the system `/usr/bin/remotinator`
  won't control this fork (use `$REPO/remotinator` instead).
- **Icon fallbacks**: split-menu and group-label icons in `window.py` /
  `titlebar.py` / `terminal_popup_menu.py` use the literal name `terminator`
  so the existing system icon theme covers them while the app's main icon
  is `terminator-mcp`.
