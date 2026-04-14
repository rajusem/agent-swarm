# UI & TUI Improvements — 2026-04-14

## Plan

A set of UI polish and TUI terminal fixes across the dashboard.

### 1. Branding & navigation cleanup
- Rename "Swarmer" → "Agent Swarm" in the navbar brand
- Remove redundant "Workspaces" nav link (already in breadcrumbs)
- Remove the `<h1>Workspaces</h1>` page title from the list view

### 2. Rename icon on session page
- Replace the plain `✎` text character with a proper pencil-square SVG icon
- Make it grey and slightly larger (20×20)

### 3. Mode-specific session layouts
Three distinct layouts depending on session mode:

**Prompt** — unchanged: config + repos left, last output right.

**Server** — reorganised:
- Config left, Git Repos right
- Last Output full-width below
- When output is empty and session is running, show "Open Web Chat ↗" inside the Last Output area
- "Open Web Chat ↗" button (highlighted, `btn-info`) next to Launch/Stop; hidden when not running
- Remove the old separate Web Chat card

**TUI** — reorganised:
- Config left, Git Repos right
- Terminal full-width below (xterm.js)
- Last Output full-width below terminal, collapsed by default

### 4. Default model — Claude Sonnet 4.6
- Set `"model": "google-vertex-anthropic/claude-sonnet-4-6"` in the opencode ConfigMap
- Update `_DEFAULT_MODEL` constant for prompt-mode sessions

### 5. TUI terminal full-width fix
- xterm.js defaults to 80 columns regardless of container width
- The PTY was also initialised at `80×24`, causing opencode to redraw at 80 cols on startup
- Fix both the browser-side column count and the server-side PTY size

---

## Implementation

### Files changed

#### `swarmer/templates/base.html`
- Navbar brand: `⚡ Swarmer` → `⚡ Agent Swarm`
- Removed the "Workspaces" `<li>` nav item entirely (leaving only brand + Logout)

#### `swarmer/templates/workspaces/list.html`
- Removed `<h1 class="h3 mb-0">Workspaces</h1>` heading
- "New Workspace" button now right-aligned on its own

#### `swarmer/templates/sessions/detail.html`
- Replaced `✎` with inline Bootstrap `pencil-square` SVG (20×20, grey via `text-muted`)
- Restructured content into a single two-column row (config always left) plus mode-specific rows below:
  - Prompt: repos in left col, last output in right col (unchanged behaviour)
  - Server: repos in right col; full-width last output row; `btn-info` web chat button in action bar; old Web Chat card removed
  - TUI: repos in right col; full-width terminal row; full-width collapsed last output row
- xterm.js script: measures character cell dimensions from the `#terminal` div (already in DOM at script-run time), passes `cols`/`rows` to `new Terminal()` constructor, and appends `?cols=N&rows=N` to the WebSocket URL
- `fitTerm()` on `window.resize` sends `{"type":"resize","cols":N,"rows":N}` JSON to the server

#### `swarmer/templates/sessions/_last_output.html`
- Added server-mode branch: when `session.mode == 'server'` and output is empty, renders a `<div>` (not `<pre>`) with a web-chat link if running, or a plain placeholder if stopped
- HTMX polling replaces the element as normal; once output arrives the server returns the `<pre>` branch instead

#### `swarmer/k8s.py`
- `_OPENCODE_CONFIG` updated to include `"model": "google-vertex-anthropic/claude-sonnet-4-6"`
- (Subsequently refactored by user into `_build_opencode_config(secret)` to select model based on available credentials: ADC → Sonnet 4.6, Gemini key → Gemini 2.5 Flash)

#### `swarmer/k8s_session.py`
- `_DEFAULT_MODEL` constant updated to `"google-vertex-anthropic/claude-sonnet-4-6"`
- (Subsequently refactored by user: model selection now based on `has_adc`/`has_gemini` flags)

#### `swarmer/routers/tui_ws.py`
- Added imports: `json`, `struct`, `termios`
- Added `_set_winsize(fd, rows, cols)` — calls `TIOCSWINSZ` on the PTY master fd
- `session_tui` endpoint now accepts `cols: int = 80` and `rows: int = 24` query params
- Calls `_set_winsize(master_fd, rows, cols)` immediately after `pty.openpty()` so opencode sees the correct dimensions when it queries `TIOCGWINSZ` on startup
- `write_loop` updated to use `websocket.receive()` (instead of `receive_bytes`) and dispatch:
  - binary frames → write to PTY master (keystrokes)
  - text frames → parse JSON; if `{"type":"resize"}` call `_set_winsize` to keep PTY in sync with browser window resizes

### Root cause of TUI width bug
xterm.js rendered at the correct pixel width visually, but the PTY was created at the default `80×24`. When opencode started it called `TIOCGWINSZ`, received `80×24`, and forced a redraw to 80 columns — shrinking the visible content to roughly 1/3 of the screen width. Setting `TIOCSWINSZ` before exec fixed this permanently.
