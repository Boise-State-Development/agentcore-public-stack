"""The tool specs the terminal declares, in the server's ``ClientToolSpec`` shape.

Descriptions are written for the model. They say what the tool does, that the
user will be asked first, and what the result looks like, because a model that
knows the output is capped asks for ``head`` rather than ``cat`` on a large file.
"""

from __future__ import annotations

from typing import Any

#: Bare names, as declared. The server prefixes them (``local_run_command``) so
#: they cannot collide with its own tools; the ``client_tool_call`` event
#: carries the bare name back.
RUN_COMMAND = "run_command"
READ_FILE = "read_file"
LIST_DIRECTORY = "list_directory"
WRITE_FILE = "write_file"
WORKSPACE_INFO = "workspace_info"

LOCAL_TOOL_NAMES: tuple[str, ...] = (WORKSPACE_INFO, RUN_COMMAND, READ_FILE, LIST_DIRECTORY, WRITE_FILE)


def tool_specs() -> list[dict[str, Any]]:
    """The specs to send as ``client_tools``. Stable across turns on purpose:
    the server folds a digest of this list into its agent cache key, so a list
    that changed every turn would rebuild the agent every turn."""
    return [
        {
            "name": WORKSPACE_INFO,
            "description": (
                "Describe where the user is working on their local machine: the current directory, "
                "whether it is a git repository and if so its root, branch, how far it is ahead of or "
                "behind its upstream, uncommitted changes and the last few commits, plus the project "
                "files present (pyproject.toml, package.json and so on). Read-only; runs no shell. "
                "Call it first when the user refers to their project, repo, branch, the tests, or a "
                "file without giving a path, instead of guessing or asking them. The user approves it "
                "like the other local tools."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": RUN_COMMAND,
            "description": (
                "Run a shell command on the user's local machine, in their current working "
                "directory, and return its combined stdout and stderr plus the exit code. "
                "The user sees the exact command and must approve it before it runs; they may "
                "decline. Output is capped at a few thousand characters, so prefer targeted "
                "commands (head, grep, --max-count) over dumping whole files or logs. Commands "
                "time out after 60 seconds; do not start servers or watchers."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command line to run, as the user would type it."},
                    "reason": {
                        "type": "string",
                        "description": "One sentence on why you need this, shown to the user next to the approval prompt.",
                    },
                },
                "required": ["command"],
            },
        },
        {
            "name": READ_FILE,
            "description": (
                "Read a text file from the user's local machine and return its contents. Paths are "
                "relative to the user's current working directory unless absolute. Capped at 64 KB; "
                "for larger files pass start_line/end_line to read a window. The user approves "
                "each read."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "start_line": {"type": "integer", "description": "1-based first line to return (optional)."},
                    "end_line": {"type": "integer", "description": "1-based last line to return, inclusive (optional)."},
                },
                "required": ["path"],
            },
        },
        {
            "name": LIST_DIRECTORY,
            "description": (
                "List the entries of a directory on the user's local machine, one per line, with a "
                "trailing slash on directories. Relative to the current working directory unless "
                "absolute. Hidden entries are included. The user approves each listing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory to list; defaults to the working directory."}},
            },
        },
        {
            "name": WRITE_FILE,
            "description": (
                "Create or overwrite a text file on the user's local machine with the given "
                "content. Parent directories are created. The user sees the path and a preview of "
                "the content and must approve. Use read_file first when editing an existing file "
                "so you overwrite it with the full intended contents."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    ]
