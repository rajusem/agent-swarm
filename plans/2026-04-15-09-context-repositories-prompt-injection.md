# Feature: Inject Context Repositories into Prompt-Mode Prompt

**Date:** 2026-04-15

## Problem

When launching a prompt-mode session, opencode had no automatic awareness of which repositories were cloned into the workspace or where they lived on disk. Users had to manually include that context in every prompt or hope the agent discovered it on its own.

## Implementation

**File:** `swarmer/k8s_session.py`, lines ~286-296 (prompt-mode command assembly)

Before this change, the prompt was passed to `opencode run` as-is:

```python
else:  # prompt
    cmd_parts = ["opencode", "run", "--model", model]
    if session.resume:
        cmd_parts.append("--continue")
    if session.instruction_prompt:
        cmd_parts.append(session.instruction_prompt)
    main_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    restart_policy = "Never"
```

After the change, a "Context Repositories" block is appended to the prompt when the session has repos configured:

```python
else:  # prompt
    prompt_text = session.instruction_prompt or ""
    if session.repos:
        repo_lines = ["\n\nContext Repositories"]
        for repo in session.repos:
            repo_lines.append(f"- {repo.repo_url} ({repo.branch}) /workspace/{repo.local_path}")
        prompt_text = prompt_text + "\n".join(repo_lines)
    cmd_parts = ["opencode", "run", "--model", model]
    if session.resume:
        cmd_parts.append("--continue")
    if prompt_text:
        cmd_parts.append(prompt_text)
    main_cmd = " ".join(shlex.quote(p) for p in cmd_parts)
    restart_policy = "Never"
```

## Output Format

For a session with two repos, the agent receives:

```
<user prompt>

Context Repositories
- https://github.com/stolostron/jira-mcp-server (main) /workspace/jira-mcp-server
- https://github.com/stolostron/agent-swarm (main) /workspace/agent-swarm
```

Each repo line contains the GitHub URL, branch in parentheses, and the local PVC path (`/workspace/{local_path}`).

## Notes

- If the session has no repos, the block is omitted entirely — the prompt passes through unchanged.
- `session.repos` was already eager-loaded via `selectinload(Session.repos)` in the `session_launch` route (`swarmer/routers/sessions.py:343`), so no additional query changes were needed.
- The `local_path` value comes from `SessionRepo.local_path`, which is either user-supplied or auto-derived from the repo name at add time.
