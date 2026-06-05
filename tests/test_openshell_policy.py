"""
Tests for swarmer.openshell_policy.build_session_policy().

Validates that build_session_policy() returns a valid YAML string with:
  - Required structural sections (version, filesystem_policy, network_policies)
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
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from swarmer.openshell_policy import build_session_policy


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


def _parse(yaml_str: str) -> dict:
    return yaml.safe_load(yaml_str)


# ---------------------------------------------------------------------------
# 1. Required structure
# ---------------------------------------------------------------------------

def test_policy_has_version_1():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert _parse(result)["version"] == 1


def test_policy_has_filesystem_policy():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "filesystem_policy" in _parse(result)


def test_policy_has_network_policies_section():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "network_policies" in _parse(result)


def test_policy_sandbox_uses_sandbox_path():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "/sandbox" in result
    assert "/workspace" not in result


# ---------------------------------------------------------------------------
# 2. GitHub blocks (per-repo)
# ---------------------------------------------------------------------------

def test_github_git_block_generated_per_repo():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    result = build_session_policy(_make_session(), repos=[repo], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    net = _parse(result)["network_policies"]
    git_blocks = [k for k in net if k.startswith("github_git_")]
    assert len(git_blocks) == 1, f"Expected 1 github_git_ block, got {len(git_blocks)}: {git_blocks}"


def test_github_api_block_generated_per_repo():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    result = build_session_policy(_make_session(), repos=[repo], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    net = _parse(result)["network_policies"]
    api_blocks = [k for k in net if k.startswith("github_api_")]
    assert len(api_blocks) == 1, f"Expected 1 github_api_ block, got {len(api_blocks)}: {api_blocks}"


def test_github_blocks_scoped_to_repo_path():
    repo = _make_repo(org="stolostron", name="agent-swarm")
    result = build_session_policy(_make_session(), repos=[repo], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "stolostron" in result


def test_two_repos_generate_two_github_block_pairs():
    repo1 = _make_repo(org="stolostron", name="agent-swarm")
    repo2 = _make_repo(org="stolostron", name="agent-containers")
    result = build_session_policy(_make_session(), repos=[repo1, repo2], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    net = _parse(result)["network_policies"]
    assert len([k for k in net if k.startswith("github_git_")]) == 2
    assert len([k for k in net if k.startswith("github_api_")]) == 2


# ---------------------------------------------------------------------------
# 3. Jira MCP block (conditional)
# ---------------------------------------------------------------------------

def test_jira_block_present_when_jira_mcp_enabled():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[_make_mcp(slug="jira")], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    net = _parse(result)["network_policies"]
    assert any("jira" in k.lower() for k in net), "Expected a jira MCP network block"


def test_jira_block_absent_when_no_jira_mcp():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    net = _parse(result)["network_policies"]
    assert not any("jira" in k.lower() for k in net)


def test_jira_block_absent_when_only_non_jira_mcp():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[_make_mcp(slug="github")], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "atlassian.net" not in result


# ---------------------------------------------------------------------------
# 4. Language-specific development blocks
# ---------------------------------------------------------------------------

def test_go_development_block_included_for_go_session():
    result = build_session_policy(_make_session(language="golang"), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "proxy.golang.org" in result
    assert "sum.golang.org" in result


def test_python_development_block_included_for_python_session():
    result = build_session_policy(_make_session(language="python"), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "pypi.org" in result
    assert "files.pythonhosted.org" in result


def test_govulncheck_block_included_for_go_session():
    result = build_session_policy(_make_session(language="golang"), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "vuln.go.dev" in result


def test_go_block_absent_for_python_session():
    result = build_session_policy(_make_session(language="python"), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "proxy.golang.org" not in result


def test_python_block_absent_for_go_session():
    result = build_session_policy(_make_session(language="golang"), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    assert "pypi.org" not in result


# ---------------------------------------------------------------------------
# 5. Minimal session — no excess blocks
# ---------------------------------------------------------------------------

def test_minimal_session_no_jira_or_extra_github_blocks():
    repo = _make_repo()
    result = build_session_policy(_make_session(language="golang"), repos=[repo], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    net = _parse(result)["network_policies"]
    assert not any("jira" in k.lower() for k in net)
    assert len([k for k in net if k.startswith("github_git_")]) == 1


# ---------------------------------------------------------------------------
# 6. Agent API block
# ---------------------------------------------------------------------------

def test_agent_api_block_opencode_includes_vertex_endpoints():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="opencode", model="google-vertex-anthropic/claude-sonnet-4-6")
    net = _parse(result)["network_policies"]
    assert any("agent_api" in k.lower() for k in net)
    assert "aiplatform.googleapis.com" in result or "api.anthropic.com" in result


def test_agent_api_block_crush_includes_crush_binary():
    result = build_session_policy(_make_session(), repos=[], mcp_servers=[], agent_tool="crush", model="vertexai/claude-sonnet-4-6")
    assert "crush" in result.lower()
    net = _parse(result)["network_policies"]
    api_blocks = {k: v for k, v in net.items() if "agent_api" in k.lower()}
    for block in api_blocks.values():
        paths = [b.get("path", "") for b in block.get("binaries", []) if isinstance(b, dict)]
        assert all("opencode" not in p for p in paths), f"opencode binary should not appear in Crush block: {paths}"
