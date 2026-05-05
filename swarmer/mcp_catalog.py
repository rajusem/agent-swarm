"""
Registry of well-known MCP servers with pre-configured OAuth details.

Each entry contains the defaults needed to add the server to a workspace.
OAuth discovery and dynamic client registration happen at connect time.
"""

MCP_SERVER_CATALOG: list[dict] = [
    {
        "slug": "atlassian-jira",
        "display_name": "Atlassian Jira (Rovo)",
        "description": (
            "Jira Cloud MCP server powered by Atlassian Rovo. "
            "Read/write issues, search projects, manage boards, and more."
        ),
        "server_url": "https://mcp.atlassian.com/v1/mcp",
        "server_type": "http",
        "authorization_endpoint": "https://mcp.atlassian.com/v1/authorize",
        "token_endpoint": "https://cf.mcp.atlassian.com/v1/token",
        "registration_endpoint": "https://cf.mcp.atlassian.com/v1/register",
        "scopes": "read:jira-work write:jira-work",
        "icon": "fas fa-bug",
        "color": "blue",
    },
]


def get_catalog_entry(slug: str) -> dict | None:
    for entry in MCP_SERVER_CATALOG:
        if entry["slug"] == slug:
            return entry
    return None
