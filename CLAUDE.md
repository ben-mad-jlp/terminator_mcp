# terminator_mcp

A fork of Terminator plus an MCP server that lets Claude read and drive its panes.
Architecture and install are in `terminator_mcp/README.md`; owned-pane design in
`terminator_mcp/doc/planning/owned_panes.md`. Fork-owned: `terminator_mcp/`,
`terminatorlib/plugins/mcp_bridge.py`, `terminatorlib/plugins/restore_layout.py`,
`terminatorlib/rowmap.py` and `tests/test_mcp_*.py`; the rest is upstream Terminator
with small fork changes.

## Two processes, two restarts

- **The bridge** (`terminatorlib/plugins/mcp_bridge.py`) runs *inside* Terminator.
  `~/.local/bin/terminator-mcp` launches this checkout, so a bridge change takes
  effect only when Terminator restarts, which closes every pane. Say so when a
  change needs one; never assume the running bridge has your edit.
- **The MCP server** (`terminator_mcp/`) runs from
  `~/.local/share/terminator-mcp/venv` with `PYTHONPATH` set to this checkout, so a
  server change takes effect on the next Claude Code restart.
- A change to the socket protocol touches both. Restart both.

## Tests

```
python3 -m pytest -q tests/test_mcp_*.py
```

## Constraints

- **`mcp<2`**: 2.x renamed `FastMCP`. The venv must be pinned too (README).
