# Fix: Last Output panel grows unboundedly (ACM-33033)

**Date:** 2026-04-19

## Problem

The "Last Output" panel on the session detail page grew without bound during a long-running agent session. Every 10 s the `/status` endpoint called `k8s.get_pod_logs()`, which fetched the entire pod log and overwrote `session.last_output` in the database. The template rendered the full string in a `<pre>` element, growing indefinitely. For completed sessions, the panel expanded to 6× the height of the left column.

## Root Cause

Bootstrap's row flex-stretch makes the taller column set the row height. A long completed log made the right column the tallest, so both columns grew to match. `overflow-auto` on the card-body had no effect because the card-body was not height-constrained — `offsetHeight` measurements taken after layout returned the already-stretched height, not the left column's natural height.

## Implementation

### `swarmer/templates/sessions/detail.html`
- Added `id="left-col"` to the left column and `id="last-output-card"` to the Last Output card.
- Added a JS snippet in `{% block scripts %}` that measures the left column's **natural** height before Bootstrap's row-stretch inflates it: temporarily sets `align-self: flex-start` on the left column (opting it out of stretch), reads `offsetHeight`, resets, then applies that value as `max-height` on the Last Output card. This makes the panel align exactly with the bottom of the Git Repositories card regardless of how many repos are present. Re-runs on window resize.
- Card-body retains `flex-grow-1 overflow-auto min-height:0` so it fills the bounded card and scrolls when log content overflows.

### `swarmer/templates/sessions/_last_output.html`
- Changed `hx-trigger="every 10s"` → `hx-trigger="every 5s"` on both polling elements for more responsive output updates.
- Added instruction prompt block before log output: when `session.instruction_prompt` is non-empty, renders a `<span class="text-info">` block with `┌─ Instruction ─…` / `└──…` box-drawing borders, then a blank line, then the log output.

## Verification

1. Open a completed prompt-mode session with verbose output — Last Output panel aligns with the bottom of the Git Repositories card and scrolls rather than growing the page.
2. Open a session with 0 and 2+ repos — confirm the panel height adjusts accordingly.
3. Launch an active session — confirm output updates every ~5 s.
4. Session with an instruction prompt — confirm it appears at the top in blue with box-drawing borders.
