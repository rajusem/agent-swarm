# Plan: ANSI Color Codes in Session Output
**Date:** 2026-04-23
**Jira:** [ACM-33206](https://redhat.atlassian.net/browse/ACM-33206)
**Branch:** main

## Context
Session output (prompt and server modes) is displayed in a `<pre>` tag via `_last_output.html`. Pod logs contain ANSI SGR escape sequences (e.g. `\x1b[33m\x1b[1m` for yellow-bold, `\x1b[31m` for red, `\x1b[0m` for reset) produced by tools like `grype`. Jinja2 autoescape passes the ESC byte through but renders the bracket sequences as literal text, so users see garbage like `[33m[1m+===+[0m` instead of colored output.

TUI mode already handles this correctly (xterm.js receives raw bytes). The fix targets prompt/server mode only.

## Approach
Add a `ansi_to_html` Jinja2 filter that converts ANSI SGR sequences to `<span>` elements **safely**:

1. **HTML-escape the raw text first** using `markupsafe.escape()` — this neutralizes any `<`, `>`, `&` in the pod output before we insert real HTML tags.
2. **Apply regex** to find `\x1b\[...m` sequences in the escaped string and replace them with `<span style="...">` / `</span>` tags.
3. **Return `markupsafe.Markup`** so Jinja2 treats the result as pre-escaped and won't double-encode it.

The ANSI sequences themselves only contain ASCII digits, semicolons, and letters — no HTML-special characters — so step 1 cannot corrupt them.

SGR codes handled:
- `0` / `` (empty) → close all open spans (reset)
- `1` → `font-weight:bold`
- `30`–`37`, `90`–`97` → foreground colors (mapped to readable hex values)
- Compound codes like `\x1b[1;33m` (bold+yellow) split on `;` and process each

Unclosed spans at end-of-string are closed automatically.

## Files Changed
- `swarmer/ansi.py` — new module: `ansi_to_html(text: str) -> Markup`
- `swarmer/routers/sessions.py` — import and register filter on the `templates` instance
- `swarmer/templates/sessions/_last_output.html` — `{{ _raw | ansi_to_html }}` replaces `{{ _raw }}`

## Verification
1. Seed a session's `last_output` with the example ANSI string from the user and confirm colors render in the browser.
2. XSS check: seed with `\x1b[33m<script>alert(1)</script>\x1b[0m` — script tag must appear as literal text.
3. Confirm HTMX 5-second poll updates also render colors correctly.
4. TUI mode (xterm.js) is unchanged — no regression risk.

---
## Implementation Summary
**Completed:** 2026-04-23

### What Changed
- `swarmer/ansi.py` — new module with `ansi_to_html(text: str) -> Markup`. HTML-escapes all input via `markupsafe.escape()` first, then uses regex `\x1b\[([\d;]*)m` to convert SGR sequences to `<span style="...">` elements. Returns `Markup` to prevent Jinja2 double-encoding. Supports codes 30–37, 90–97 (foreground colors), `1` (bold), `0` (reset); compound codes like `1;33` handled via `;` split.
- `swarmer/routers/sessions.py` — added `from swarmer.ansi import ansi_to_html` import and `templates.env.filters['ansi_to_html'] = ansi_to_html` registration immediately after templates instantiation.
- `swarmer/templates/sessions/_last_output.html` — changed `{{ _raw }}` to `{{ _raw | ansi_to_html }}` on line 27; affects both initial page render and HTMX 5-second polling updates.

### Tests
- No automated tests added. Manual verification steps in Verification section above.

### Known Gaps / Follow-up
- Background colors (codes 40–47, 100–107) not mapped — uncommon in practice, silently ignored
- No automated unit tests for `ansi_to_html` — could add with pytest covering: color codes, bold, reset, compound codes, XSS input, empty string, text with no ANSI
- Other routers with their own `Jinja2Templates` instances (auth, workspaces, secrets, chat_proxy) do not have the filter registered — only needed in sessions for now
