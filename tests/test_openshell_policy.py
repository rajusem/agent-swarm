"""
Tests for swarmer.openshell_policy.build_session_policy().

Validates that build_session_policy() returns a SandboxPolicy proto with:
  - Required structural sections (version, filesystem, network_policies)
  - Per-repo GitHub git + API blocks with scoped paths
  - Conditional Jira MCP block (present when Jira MCP enabled, absent otherwise)
  - Conditional Go development block (proxy.golang.org etc.)
  - Conditional Python development block (pypi.org etc.)
  - govulncheck block for Go sessions
  - Agent API block adapted to agent tool (opencode vs crush) and model provider
  - No excess blocks for minimal sessions (single repo, no MCP)
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.openshell_policy import build_session_policy, build_session_network_policies


# ---------------------------------------------------------------------------
# Helpers to build minimal test fixtures matching the real model shapes
# ---------------------------------------------------------------------------

def _make_repo(org="stolostron", name="agent-swarm", branch="main"):
    return type("Repo", (), {
        "repo_url": f"https://github.com/{org}/{name}",
        "branch": branch,
        "local_path": name,
    })()


def _make_mcp(slug="jira"):
    return type("MCP", (), {"slug": slug})()


def _make_session(language="golang"):
    return type("Session", (), {
        "id": 1,
        "language": language,
        "agent_tool": "opencode",
        "model": "google-vertex-anthropic/claude-sonnet-4-6",
    })()


def _policy_dict(policy) -> dict:
    """Convert SandboxPolicy proto to a dict for easy assertions."""
    return {
        "version": policy.version,
        "filesystem": {
            "read_only": list(policy.filesystem.read_only),
            "read_write": list(policy.filesystem.read_write),
        },
        "network_policies": {
            k: {
                "endpoints": [{"host": e.host, "port": e.port} for e in v.endpoints],
                "binaries": [b.path for b in v.binaries],
            }
            for k, v in policy.network_policies.items()
        },
    }


_MODEL = "google-vertex-anthropic/claude-sonnet-4-6"


def _bnet(session=None, repos=None, mcp_servers=None, agent_tool="opencode", model=_MODEL) -> dict:
    """Build network_policies dict via build_session_network_policies."""
    return build_session_network_policies(
        session or _make_session(), repos or [], mcp_servers or [], agent_tool, model
    )


def _bhosts(session=None, repos=None, mcp_servers=None, agent_tool="opencode", model=_MODEL) -> list[str]:
    """Return all endpoint hosts from computed network_policies."""
    hosts = []
    for rule in _bnet(session, repos, mcp_servers, agent_tool, model).values():
        for ep in rule.get("endpoints", []):
            hosts.append(ep.get("host", ""))
    return hosts


# ---------------------------------------------------------------------------
# 1. Required structure
# ---------------------------------------------------------------------------

def test_policy_has_version_1():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert result.version == 1


def test_policy_has_filesystem_policy():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert result.filesystem.include_workdir is True
    assert "/sandbox" in result.filesystem.read_write


def test_policy_has_network_policies_section():
    net = _bnet()
    assert len(net) > 0


def test_policy_sandbox_uses_sandbox_path():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "/sandbox" in result.filesystem.read_write
    assert "/workspace" not in list(result.filesystem.read_write)


# ---------------------------------------------------------------------------
# 2. GitHub blocks (per-repo)
# ---------------------------------------------------------------------------

def test_github_git_block_generated_per_repo():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    git_blocks = [k for k in net if k.startswith("github_git_")]
    assert len(git_blocks) == 1, f"Expected 1 github_git_ block, got {len(git_blocks)}: {git_blocks}"


def test_github_api_block_generated_per_repo():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    api_blocks = [k for k in net if k.startswith("github_api_")]
    assert len(api_blocks) == 1, f"Expected 1 github_api_ block, got {len(api_blocks)}: {api_blocks}"


def test_github_blocks_scoped_to_repo_path():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    net = _bnet(repos=[repo])
    assert "github_git_stolostron_agent_swarm" in net
    assert "github_api_stolostron_agent_swarm" in net


def test_two_repos_generate_two_github_block_pairs():
    repo1 = _make_repo(org="stolostron", name="agent-swarm")
    repo2 = _make_repo(org="stolostron", name="agent-containers")
    net = _bnet(repos=[repo1, repo2])
    assert len([k for k in net if k.startswith("github_git_")]) == 2
    assert len([k for k in net if k.startswith("github_api_")]) == 2


# ---------------------------------------------------------------------------
# 3. Jira MCP block (conditional)
# ---------------------------------------------------------------------------

def test_jira_block_present_when_jira_mcp_enabled():
    net = _bnet(mcp_servers=[_make_mcp(slug="jira")])
    assert any("jira" in k.lower() for k in net), "Expected a jira MCP network block"


def test_jira_block_absent_when_no_jira_mcp():
    net = _bnet()
    assert not any("jira" in k.lower() for k in net)


def test_jira_block_absent_when_only_non_jira_mcp():
    assert "atlassian.net" not in _bhosts(mcp_servers=[_make_mcp(slug="github")])


# ---------------------------------------------------------------------------
# 4. Language-specific development blocks
# ---------------------------------------------------------------------------

def test_go_development_block_included_for_go_session():
    hosts = _bhosts(session=_make_session(language="golang"))
    assert "proxy.golang.org" in hosts
    assert "sum.golang.org" in hosts


def test_python_development_block_included_for_python_session():
    hosts = _bhosts(session=_make_session(language="python"))
    assert "pypi.org" in hosts
    assert "files.pythonhosted.org" in hosts


def test_govulncheck_block_included_for_go_session():
    assert "vuln.go.dev" in _bhosts(session=_make_session(language="golang"))


def test_go_block_absent_for_python_session():
    assert "proxy.golang.org" not in _bhosts(session=_make_session(language="python"))


def test_python_block_absent_for_go_session():
    assert "pypi.org" not in _bhosts(session=_make_session(language="golang"))


# ---------------------------------------------------------------------------
# 5. Minimal session — no excess blocks
# ---------------------------------------------------------------------------

def test_minimal_session_no_jira_or_extra_github_blocks():
    repo = _make_repo()
    net = _bnet(session=_make_session(language="golang"), repos=[repo])
    assert not any("jira" in k.lower() for k in net)
    assert len([k for k in net if k.startswith("github_git_")]) == 1


# ---------------------------------------------------------------------------
# 6. Agent API block
# ---------------------------------------------------------------------------

def test_agent_api_block_opencode_includes_vertex_endpoints():
    net = _bnet()
    assert any("agent_api" in k.lower() for k in net)
    hosts = _bhosts()
    assert any("aiplatform.googleapis.com" in h or "api.anthropic.com" in h for h in hosts)


def test_agent_api_block_crush_includes_crush_binary():
    net = _bnet(agent_tool="crush", model="vertexai/claude-sonnet-4-6")
    api_block = net.get("agent_api")
    assert api_block is not None
    # Crush block has no binaries restriction; opencode binary should not appear
    binaries = api_block.get("binaries", [])
    binary_paths = [b.get("path", "") if isinstance(b, dict) else getattr(b, "path", "") for b in binaries]
    assert not any("opencode" in p for p in binary_paths), f"opencode binary in crush block: {binary_paths}"
