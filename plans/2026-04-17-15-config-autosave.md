# 2026-04-17-15 — Configuration Auto-save

**Date:** 2026-04-17
**PR:** [#7 Acm 33014 (includes config autosave)](https://github.com/stolostron/agent-swarm/pull/7)

## Problem

The Session detail page Configuration card had a Save button with a dirty-state indicator (amber colour when changed). This UX required an extra deliberate step after every field edit. It also showed a misleading "Stop the session to edit settings" message that could be interpreted as the only available action.

Goals:

- When **running**: all fields remain read-only (no change).
- When **stopped or after cleanup**: all fields are editable and changes are persisted automatically — no Save button required.
- **Selects** save on selection change.
- **Checkboxes** save on toggle.
- **Instruction Prompt textarea** saves on blur (fires automatically when focus moves to another element, e.g. the Launch button).
- If the user edits the textarea and immediately clicks Launch without blurring first, the pending save must complete before the launch form submits.

## Design decisions

### Auto-save via the existing `/edit` endpoint

The existing `POST /workspaces/{ws_id}/sessions/{sid}/edit` endpoint already accepts all configuration fields and returns a 302 redirect. Calling it via `fetch` with `redirect: 'manual'` means the browser does not follow the redirect; the response type is `opaqueredirect` (status 0), which is treated as success.

This avoids adding new per-field endpoints and keeps the backend unchanged.

### Mode select triggers a page reload

The template conditionally renders the Instruction Prompt textarea and its hidden-input fallback based on `session.mode`. If the mode changes without a page reload the DOM would be stale (e.g. the textarea would appear/disappear incorrectly). The fix: after saving a mode change, call `window.location.reload()`.

### Textarea flush before launch

`blur` fires synchronously when focus leaves an element, but `autoSave` is async (it performs a `fetch`). If a user clicks a Launch button immediately after editing the textarea, the blur fires first but the fetch may not complete before the launch form's native POST navigates away.

Solution: track whether the textarea has unsaved input (`pendingTextareaChange` flag). Each launch form listens for `submit`; if `pendingTextareaChange` is true, the submit is `preventDefault`'d, `autoSave` is awaited, then the form is submitted programmatically.

### `syncResume()` interop

The Persist checkbox carries `onchange="syncResume()"` which shows/hides the Resume row and unchecks Resume when Persist is unchecked. Inline `onchange` fires before `addEventListener` listeners in attachment order, so `syncResume()` always updates the DOM state before `autoSave()` reads the form — no coordination required.

## Implementation

**File changed:** `swarmer/templates/sessions/detail.html`

### 1. Removed dirty-state CSS

The `.btn-save-dirty` style block (amber Save button) was deleted from `{% block head %}`.

### 2. Replaced Save button with indicator div

```html
{# Before #}
{% if not session.is_active %}
<button type="submit" class="btn btn-sm btn-outline-secondary" id="cfg-save-btn">Save</button>
{% else %}
<p class="text-muted small mb-0">Stop the session to edit settings.</p>
{% endif %}

{# After #}
{% if session.is_active %}
<p class="text-muted small mb-0">Stop the session to edit settings.</p>
{% else %}
<div id="cfg-save-indicator" class="text-muted small" style="min-height:1.25em"></div>
{% endif %}
```

The indicator div has a fixed minimum height so the card does not jump when text appears.

### 3. Replaced dirty-state JS with auto-save IIFE

The previous IIFE snapshotted field values and toggled the button class. It was replaced with:

```javascript
(function () {
  const form = document.getElementById('cfg-edit-form');
  const indicator = document.getElementById('cfg-save-indicator');
  if (!form || !indicator) return;          // no-op when session is active

  let saving = false;
  let pendingTextareaChange = false;

  function showStatus(msg, cls) { ... }

  async function autoSave() {
    // serialises FormData and POSTs to form.action with redirect:'manual'
  }

  // Mode: save + reload
  modeSelect.addEventListener('change', async () => { await autoSave(); window.location.reload(); });

  // Other selects + checkboxes: save on change
  form.querySelectorAll('select:not([name=mode]), input[type=checkbox]')
      .forEach(el => el.addEventListener('change', autoSave));

  // Textarea: save on blur, only if edited
  form.querySelectorAll('textarea').forEach(el => {
    el.addEventListener('input', () => { pendingTextareaChange = true; });
    el.addEventListener('blur', async () => {
      if (!pendingTextareaChange) return;
      pendingTextareaChange = false;
      await autoSave();
    });
  });

  // Launch intercept: flush textarea before navigating away
  document.querySelectorAll('.launch-tool-btn').forEach(btn => {
    btn.closest('form').addEventListener('submit', function (e) {
      if (pendingTextareaChange) {
        e.preventDefault();
        const launchForm = this;
        pendingTextareaChange = false;
        autoSave().then(() => launchForm.submit());
      }
    });
  });
})();
```

The IIFE early-exits when `indicator` is null (i.e. the session is active and the indicator div was not rendered), so all listeners are safely skipped while the session is running.

## Files changed

| File | Change |
|---|---|
| `swarmer/templates/sessions/detail.html` | Remove CSS, replace Save button with indicator, replace dirty-state JS with auto-save JS |
