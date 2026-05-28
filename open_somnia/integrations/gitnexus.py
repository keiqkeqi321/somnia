from __future__ import annotations

from typing import Any


SERVER_NAME = "gitnexus"

READ_ONLY_TOOLS = frozenset(
    {
        "api_impact",
        "context",
        "cypher",
        "detect_changes",
        "group_list",
        "impact",
        "list_repos",
        "query",
        "route_map",
        "shape_check",
        "tool_map",
    }
)

MUTATING_OR_EXPENSIVE_TOOLS = frozenset(
    {
        "group_sync",
        "rename",
    }
)


def split_mcp_tool_name(tool_name: str) -> tuple[str, str] | None:
    parts = str(tool_name or "").split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    server_name = parts[1].strip().lower()
    remote_tool = parts[2].strip()
    if not server_name or not remote_tool:
        return None
    return server_name, remote_tool


def is_gitnexus_tool_name(tool_name: str) -> bool:
    parsed = split_mcp_tool_name(tool_name)
    return parsed is not None and parsed[0] == SERVER_NAME


def gitnexus_remote_tool_name(tool_name: str) -> str | None:
    parsed = split_mcp_tool_name(tool_name)
    if parsed is None or parsed[0] != SERVER_NAME:
        return None
    return parsed[1]


def is_read_only_gitnexus_tool(tool_name: str) -> bool:
    remote_tool = gitnexus_remote_tool_name(tool_name)
    return remote_tool in READ_ONLY_TOOLS


def should_allow_gitnexus_tool_without_authorization(tool_name: str) -> bool:
    return is_read_only_gitnexus_tool(tool_name)


def gitnexus_is_available(runtime: Any) -> bool:
    mcp_registry = getattr(runtime, "mcp_registry", None)
    if mcp_registry is None:
        return False
    all_servers = getattr(mcp_registry, "all_servers", []) or []
    found_configured_server = False
    for server in all_servers:
        if str(getattr(server, "name", "")).strip().lower() == SERVER_NAME:
            found_configured_server = True
            if not bool(getattr(server, "enabled", True)):
                return False
    server_tools = getattr(mcp_registry, "server_tools", {}) or {}
    has_registered_tools = any(str(name).strip().lower() == SERVER_NAME for name in server_tools)
    if found_configured_server:
        return has_registered_tools
    return has_registered_tools


def prompt_guidance(runtime: Any) -> str:
    if not gitnexus_is_available(runtime):
        return ""
    return (
        "GitNexus integration:\n"
        "- GitNexus is available through MCP-backed code intelligence tools.\n"
        "- When repository instructions require GitNexus, treat those requirements as binding while the tools are available; do not downgrade MUST/NEVER language to suggestions.\n"
        "- Use read-only GitNexus tools for code graph context, symbol impact, route/API shape checks, and change-scope verification when they fit the task.\n"
        "- Before editing an existing function, class, method, API route handler, or shared symbol, run the relevant GitNexus impact/context check when available.\n"
        "- If a specific GitNexus symbol lookup fails, retry with a narrower target, file_path, or route before falling back to focused generic search.\n"
        "- Before committing or finalizing a broad code change, use GitNexus change detection when available.\n"
        "- If GitNexus reports a stale or missing index, say so and fall back to focused generic tools unless the user authorizes index maintenance.\n"
        "- GitNexus tools that rename code, rebuild indexes, sync generated registries, or otherwise mutate workspace state require normal Somnia authorization."
    )
