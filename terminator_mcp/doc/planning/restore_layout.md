# Restore layout

One action that gets a saved workspace tab back to work: build it if it isn't
open, then run each pane's command in the panes that are idle. Typical use is
after unpausing the VM, when every pane's ssh has dropped but the tab and its
panes are still there.

## User flow

1. Arrange a tab by hand: splits, pane titles (right-click a titlebar), and
   in **Preferences → Layouts** a **Custom command** per pane. Save it as a
   layout named after the tab, e.g. `pnp`.
2. Right-click any terminal → **Restore layout ▸ pnp**.
3. If the current window has no tab labeled `pnp`, a new tab is built from the
   layout and labeled `pnp`. Then each pane's command is typed into it.
4. Panes that weren't idle get a badge naming what's running. The badge clears
   when the pane is focused or on the next restore.

Restoring twice is harmless: panes still connected are busy, so they get a
badge instead of a second command.

## Layout source

Terminator's saved layouts (`[layouts]` in the config), not a new file. The
layout supplies the split tree, ratios, pane titles and pane commands.

- **Only single-tab layouts.** The layout's window must have exactly one child
  (a Terminal or a Paned tree). A layout saved from a window with several tabs
  (a Notebook child) is not offered in the menu.
- **The Custom command is typed, not spawned.** Restore types it into the
  pane's shell, followed by Enter, the way a person would. One shell line per
  pane, e.g. `ssh -t pnp.jlp "bash -ic 'devtach launch'"` (the `bash -i`
  sources `.bashrc`, which puts `devtach` on PATH; the outer quotes keep the
  inner ones intact through the remote shell's re-parse). When the session detaches or the
  connection drops, ssh exits and the pane is back at an idle local prompt,
  ready for the next restore.
- A pane with no command is created and titled but gets nothing typed.
- `terminator -l <layout>` still spawns the command in place of the shell
  (upstream behavior), so layouts meant for restore shouldn't be launched
  that way.

## Restore algorithm

Given layout `L` and the window `W` of the right-clicked terminal:

1. **Find the tab.** The first tab in `W` whose label is `L`.
2. **If none, build it.** Open a new tab, label it `L`, and recreate the split
   tree the way `Window.create_layout` does (`split_axis` per Paned, then
   `create_layout` on each child). The layout dict is copied with `command`
   and `uuid` removed from each terminal first: `command` would make
   `spawn_child` run it in place of the shell, and a saved `uuid` would
   collide if the layout is built twice.
3. **Match panes.** Each terminal in the layout is matched to the pane in that
   tab with the same custom title. Layout panes with no match are reported in
   a badge on the first matched pane (`missing: control`); restore never
   re-splits an existing tab.
4. **Type commands.** For each matched pane with a command: if it is idle,
   type the command and Enter; otherwise set its badge.

## Idle detection

A pane is idle when its shell is the pty's foreground process:
`os.tcgetpgrp(vte.get_pty().get_fd()) == terminal.pid`. Otherwise the
foreground process group's leader names what's running
(`/proc/<pgrp>/comm`), and that name goes in the badge: `busy: ssh`.

Restore doesn't treat any program specially. A hung ssh is handled in ssh's
own config instead, so it exits shortly after an unpause:

```
Host *.jlp
    ServerAliveInterval 5
    ServerAliveCountMax 2
```

## Badges

A small label at the right end of the pane's titlebar, next to the bell icon,
via a new `Titlebar.set_badge(text)` (empty text hides it). It is separate from
the custom title, which restore uses for matching. It clears when the pane
gains focus or when the next restore runs. There is no dialog.

## Where the code lives

- `terminatorlib/plugins/restore_layout.py`: a `MenuItem` plugin providing the
  submenu, layout lookup, tab building, matching, idle checks and typing. It
  must be enabled in **Preferences → Plugins**.
- `terminatorlib/titlebar.py`: `set_badge`, the only change to upstream code.

## Later: keybinding

A plugin keybinding (via `KeyBindUtil`) that restores the layout named by the
current tab's label. That's the one-key "reconnect this tab".

## Testing

- Unit tests (`tests/test_mcp_restore_layout.py`) for the pure parts: the
  layout filter (single-tab only), stripping `command`/`uuid`, matching panes
  by title with missing ones reported, and the idle/busy decision given a
  foreground pgrp and pid.
- Manual: restore into a fresh window (tab built, commands typed), restore
  again (badges on connected panes), and restore after killing one pane's
  connection (only that pane gets its command).
