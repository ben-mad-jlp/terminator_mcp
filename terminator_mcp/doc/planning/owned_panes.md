# Owned panes: spawn, status and signal processes in managed panes

## Goal

Give clients a generic primitive for running a process in a visible pane, where only the client that
started it may stop it. This is the basis for policy-enforcing tools built outside this repo; the first
is jlp-claude-plugin's `run_sim`. terminator_mcp itself stays domain-agnostic: no ROS, no assumptions
about what runs.

## Why spawn instead of typing

`run_command` types into the pane's existing shell. That can't guarantee a process environment:
- typed prefixes don't reach `&&` chains, pipes or subshells;
- the text lands in whatever program is in the foreground;
- the user's shell rc has already run.

Spawning the pane's child directly, with explicit argv and env, can guarantee it.

## Bridge RPCs (no new MCP tools)

These are called over the bridge socket by other MCP servers, the same way jlp-ros-build already calls
`new_tab` and `write_display`.

### `spawn_in_pane(owner, name, argv, env, tab)`

- `owner` scopes the registry, e.g. `"jlp-sim"`. It's a label, not a security boundary.
- `name` must match `[a-z0-9_-]+`.
- `env` is the complete environment; the client decides what to inherit.

**Placement:**
- The tab titled `tab` is created if missing. Other tabs are never touched.
- If `(owner, name)` already owns a pane whose process has exited, respawn in that pane.
- If it's still running, refuse with `already_running`.
- Otherwise:
  - If the tab is missing, it is created around the new pane itself; no shell is ever spawned in it.
  - If the tab exists, split the owner's pane with the largest pixel area across its longer pixel
    dimension (side by side if wider than tall). Fall back to any pane in the tab if the owner has
    none there.
- Creating the tab shows it, as jlp-build's tab does. Later spawns leave every window on the tab
  the user was viewing, and focus returns to the user's pane if it is on that tab.

**Spawn:** VTE spawn with `argv` and `env`; nothing is typed. The pane title is set to `name`.

**Exit:** the bridge catches VTE's `child-exited`, records the exit status, writes
`[exited N]` to the display (display only, like `write_display`), and keeps the pane open, so the
scrollback stays readable until a respawn.

**Registry:** `{owner, name, pane uuid, pid, started_at, exit_status|null}` is kept in the bridge. It
survives MCP server restarts; a Terminator restart closes the panes anyway.

**Returns:** `{uuid, name, pid}`.

### `owned_status(owner, name)`

Returns `{state: running|exited|unknown, exit_status, uuid}`.

### `signal_owned(owner, name, signal="INT", wait_ms=10000)`

- Acts only on an `(owner, name)` entry that is `running`. Returns `not_owned` or `not_running` otherwise.
- Sends the signal to the pane's foreground process group (SIGINT = Ctrl-C), then waits up to
  `wait_ms` for exit.
- Returns `{state, exit_status}`.

Clients read output with the existing `get_text` / tail RPCs.

## Other changes in this work

- **Remove `command`** from the `new_tab` and `split` MCP tools; running something goes through
  `run_command`. The bridge-side `new_tab` RPC keeps its parameters, because jlp-ros-build uses it.
- **Delete the `open_workspace` MCP tool.** It's unused, and it runs commands with no checks.
- **Fix the 10 s cutoff:**
  - `_execute_capture` passes the requested `timeout_ms` (+ margin) to `socket_client.call`;
  - `socket.timeout` during recv becomes a `BridgeError`.
- Update `terminator_mcp/README.md`: drop the stale "P0 read-only" header and document the RPCs.

## Security note

The bridge socket is `0600` in a `0700` directory, so only the user's own processes (and root) can
connect. That includes any agent's shell, which could connect directly and bypass MCP-level
permission rules. The existing `feed` RPC already allows that, and `spawn_in_pane` adds no new
exposure. Enforcing a boundary against the user's own processes needs Claude Code's Bash sandbox,
which is out of scope here.

## Tests

terminator_mcp has no tests today. New pytest unit tests cover:
- placement choice (largest area, longer dimension; respawn vs. `already_running`)
- registry state transitions
- `signal_owned` refusals (`not_owned`, `not_running`)
- the 10 s timeout fix (a client call honors `timeout_ms`)

A manual check confirms that `spawn_in_pane` env actually reaches the child (`env` printed in the pane).

## Not in scope

The policy any client applies on top (see jlp-claude-plugin `doc/planning/run_sim.md`), and
dtach-backed panes.
