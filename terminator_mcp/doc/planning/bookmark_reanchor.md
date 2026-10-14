# Re-anchoring bookmarks across reflow (deferred)

## Status

Not planned. Agents receive the click-time snapshot (`Terminal._snapshot_bookmark`), so what they
see no longer depends on layout. This doc records how gutter marks *could* follow their content,
so a future attempt doesn't start from text matching.

## Problem

A bookmark is keyed by physical buffer row. Maximizing, splitting, resizing or changing font
re-wraps the buffer, so the gutter mark stays on the same row number while the text it marked moves.
One row number can hold only one bookmark, so you can't mark line X while maximized and a different
line that lands on the same row number while not.

## Approach if revisited

Don't search for the text: re-wrapped or repeated log lines make matching unreliable, and a miss
would put the mark on a wrong line.

Instead, anchor each bookmark to (logical line index, character offset). VTE knows which rows are
soft wraps: reading the buffer joins wrapped rows and puts newlines only at real line ends. So
counting newlines up to the clicked row gives the logical line, deterministically. After a reflow,
the new physical row is the row where that logical line starts, plus the offset divided by the
column count.

Costs and limits:
- It needs a debounced handler on resize or content reflow that recomputes every bookmark's row.
- Scrollback trimming at the top shifts logical indices and must be tracked.
- Wide characters and tabs make the offset-to-row step approximate. The mark lands on the right
  logical line, but possibly on another wrapped row of it.

It's worth doing only if gutter marks drifting becomes a real annoyance in practice.
