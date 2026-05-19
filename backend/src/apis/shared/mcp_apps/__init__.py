"""Shared MCP Apps host-renderer support (SEP-1865).

PR #5 of `docs/kaizen/scoping/mcp-apps-host-renderer.md`. Lives in
`apis.shared` because both the inference-api `/invocations` app-tool-call
dispatch (publisher) and the `agents` stream coordinator (subscriber) need
it, and they must not import from each other.
"""
