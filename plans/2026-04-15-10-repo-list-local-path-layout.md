# Fix: Repo List Local Path Layout

**Date:** 2026-04-15

## Problem

In the session dashboard's Git Repositories table, the local path column caused row wrapping because three separate columns (Repo URL, Branch, Local Path) were too wide for the available space.

## Implementation

**File:** `swarmer/templates/sessions/_repo_list.html`, table header and row cells

Merged the three columns into one by combining Repo URL, Branch, and Local Path into a single `<td>`. Branch stays inline with the URL; local path drops to a second line in a muted `<div>`.

Before:
```html
<th>Repo URL</th>
<th>Branch</th>
<th>Local Path</th>
...
<td class="small font-monospace">{{ repo.repo_url }}</td>
<td><code>{{ repo.branch }}</code></td>
<td><code>/workspace/{{ repo.local_path }}</code></td>
```

After:
```html
<th>Repo URL / Local Path</th>
...
<td class="small font-monospace">
  {{ repo.repo_url }} <code>{{ repo.branch }}</code>
  <div class="text-muted"><code>/workspace/{{ repo.local_path }}</code></div>
</td>
```

## Result

Each repo row renders as:
```
https://github.com/org/repo  `main`
/workspace/repo
```

Line 1: URL + branch inline. Line 2: local path, muted, no wrapping across columns.
