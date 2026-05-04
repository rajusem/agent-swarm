"""GitHub API helpers used by the sessions router.

Kept in a standalone module (no FastAPI / SQLAlchemy imports) so the logic
can be unit-tested without standing up the full application stack.
"""

import asyncio
import re
from urllib.parse import urlparse

import httpx


def github_slug(url: str) -> str | None:
    """Extract 'owner/repo' from a GitHub URL, or None if not a GitHub URL.

    Handles both HTTPS (https://github.com/owner/repo) and SSH
    (git@github.com:owner/repo) formats.  Only the exact host ``github.com``
    (or ``www.github.com``) is accepted to prevent false matches on hosts like
    ``notgithub.com``.
    """
    # SSH format: git@github.com:owner/repo[.git]
    ssh_match = re.match(r"^git@github\.com:(?P<slug>[^/]+/[^/]+?)(?:\.git)?$", url)
    if ssh_match:
        return ssh_match.group("slug")

    # HTTPS format: https://github.com/owner/repo[.git]
    parsed = urlparse(url)
    if parsed.hostname not in ("github.com", "www.github.com"):
        return None
    m = re.match(r"^/(?P<slug>[^/]+/[^/]+?)(?:\.git)?/?$", parsed.path)
    return m.group("slug") if m else None


async def fetch_repo_info(repos: list, pat: str | None) -> dict:
    """Return per-repo visibility and push-access info via the GitHub API.

    Result shape: {repo_id: {"is_public": bool|None, "can_push": bool|None}}
    None means the check could not be performed (non-GitHub URL, API error, etc.)
    """
    headers = {"Accept": "application/vnd.github+json"}
    if pat:
        headers["Authorization"] = f"token {pat}"

    async def _check(client: httpx.AsyncClient, repo) -> tuple[int, dict]:
        slug = github_slug(repo.repo_url)
        if not slug:
            return repo.id, {"is_public": None, "can_push": None}
        try:
            r = await client.get(
                f"https://api.github.com/repos/{slug}", headers=headers
            )
            if r.status_code == 200:
                data = r.json()
                perms = data.get("permissions", {})
                return repo.id, {
                    "is_public": not data.get("private", True),
                    "can_push": perms.get("push"),
                }
            # 404 → private repo the token can't see (or doesn't exist)
            if r.status_code == 404:
                return repo.id, {"is_public": None, "can_push": False if pat else None}
            # Any other non-200 (rate-limit, 5xx, …) — don't infer push access
            return repo.id, {"is_public": None, "can_push": None}
        except Exception:
            return repo.id, {"is_public": None, "can_push": None}

    async with httpx.AsyncClient(timeout=5) as client:
        results = await asyncio.gather(*[_check(client, r) for r in repos])
    return dict(results)


async def list_repos_for_pat(pat) -> list[dict] | str:
    """Fetch all repos accessible via a GitHubPAT, paginated up to 500.

    If pat.github_org is set, lists repos from that org via GET /orgs/{org}/repos.
    Otherwise lists the authenticated user's repos via GET /user/repos.

    Returns a list of repo dicts (keys: full_name, private, updated_at, description)
    or a string error message on failure.

    The ``pat`` argument must expose:
      - pat.pat       (str)  — the raw token value
      - pat.github_org (str) — org name or empty string
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"token {pat.pat}",
    }
    if pat.github_org:
        url: str | None = f"https://api.github.com/orgs/{pat.github_org}/repos"
        params: dict = {"per_page": 100, "sort": "updated"}
    else:
        url = "https://api.github.com/user/repos"
        params = {"per_page": 100, "sort": "updated", "affiliation": "owner,collaborator"}

    repos: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            while url and len(repos) < 500:
                r = await client.get(url, headers=headers, params=params)
                params = {}  # pagination: subsequent URLs already carry params
                if r.status_code != 200:
                    ct = r.headers.get("content-type", "")
                    msg = (
                        r.json().get("message", "unknown error")
                        if ct.startswith("application/json")
                        else r.text
                    )
                    return f"GitHub API error {r.status_code}: {msg}"
                repos.extend(r.json())
                # Follow Link header for next page
                next_url: str | None = None
                for part in r.headers.get("link", "").split(","):
                    if 'rel="next"' in part:
                        next_url = part.split(";")[0].strip().strip("<>")
                url = next_url
    except Exception as exc:
        return f"Failed to contact GitHub API: {exc}"

    return repos
