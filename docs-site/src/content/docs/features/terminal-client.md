---
title: Terminal Client
description: The same assistant in your shell — aps-tui, with local tools that run on your machine.
sidebar:
  order: 10
---

`aps-tui` is a terminal client for this platform. It talks to the same App API
as the web app and holds a real session, so you get the agent, your models and
tools, projects, agents, skills, conversation modes and saved conversations,
under the same roles and quotas. It adds one thing the browser cannot: with
your approval, the agent can read and write files and run commands on your
machine.

It is a Python application built on [Textual](https://textual.textualize.io/),
shipped as a wheel on each GitHub Release. It is not published to PyPI, because
it only works against a deployment of this repository.

## Install

You need [uv](https://docs.astral.sh/uv/installation/). Copy the install line
from the release page of the deployment you use; it has this shape:

```bash
uv tool install https://github.com/<org>/<repo>/releases/download/v<version>/aps_tui-<version>-py3-none-any.whl
aps-tui
```

The wheel is the same for every deployment: nothing in it names a host. On
first launch the client asks for the address of your deployment (type the web
app's address; `/api` is added for you), checks that it answers, and signs you
in through your browser. Upgrading is the same command for a newer release
with `--force`.

A wheel is data, not an executable, so neither Windows SmartScreen nor macOS
Gatekeeper has anything to warn about.

## How it signs in

The client never talks to Cognito. `aps-tui login --sso` (or the first-run
setup) starts a **device-authorization flow** against the App API: it shows a
short code and a URL, opens your browser when it can, and polls until you
approve the sign-in on the web app's own login page. What comes back is a
sealed platform session, stored in your OS keyring and presented on every
request as `Authorization: BFF <session>`.

Why this design: the token the agent forwards to MCP servers is always one the
platform minted, so adding the terminal changed nothing downstream and needed
no identity-provider configuration. Details and the verified flow are in
`docs/specs/CLI_DEVICE_AUTH_SPEC.md`.

An API key (`aps-tui login` without `--sso`) also works, but only for
single-turn chat through `/chat/api-converse`: no tools, no memory, no saved
conversations.

## Everything is a slash command

There are no function keys and no command palette. Type `/` and a completion
menu lists every command; Tab or Enter completes the highlighted one. The keys
you already know do what you expect: Enter sends, Esc stops the turn, Ctrl+Q
quits, Up/Down move through history.

| Command | What it does |
|---|---|
| `/model [name]` | Choose the model (your deployment's catalogue) |
| `/tools [local on\|off]` | Choose tools; switch local tools on or off |
| `/project [name\|off\|shared]` | Work in a project; browse what teammates shared |
| `/agent [name\|off]` | Talk to one of your agents, or one shared or pinned |
| `/skills` | Choose which skills the agent may use; `/<skill> …` in a message invokes one |
| `/mode [name\|off]` | Set the conversation mode (a named system prompt) |
| `/effort [level\|off]` | How hard the model thinks, for models that offer a choice |
| `/conversations [search]` | Browse, open, rename or delete saved conversations |
| `/attach <path>` | Upload a file and send it with your next message |
| `/artifacts [dir]` | Save an artifact the agent made to disk |
| `/new`, `/rename`, `/status`, `/theme`, `/help` | What they say |

The agent is told it is in a terminal (`client_surface: "terminal"`), so it
points you at slash commands rather than buttons and never emits KaTeX or
Mermaid, which a terminal would show as noise.

## Local tools

With local tools on (the default when signed in), the agent can call
`local_read_file`, `local_write_file`, `local_list_directory`,
`local_run_command` and `workspace_info`. Every call pauses the turn and shows
you exactly what it wants to do, the full command or path, before anything
runs. You approve one call, approve for the session, or decline. There is no
allow-list to configure: the prompt showing the exact command is the control.

`workspace_info` gives the agent your working directory, git branch, changed
file names and recent commits, never file contents, so "fix the failing test"
has something to work from.

Switch them off with `/tools local off` or `aps-tui --no-local-tools`.

## Interrupts are answered in the terminal

Anything that pauses a turn in the web app pauses it here too, with a prompt
in the transcript: tool approvals, the agent's clarifying questions, OAuth
consent for an MCP connector, a browser sign-in. A paused turn outlives the
client: quit mid-prompt, and reopening the conversation shows the prompt again.

## What stays in the web app

Management surfaces: editing an agent's instructions or knowledge base,
project membership, the memory editor, schedules, the admin dashboard. The
terminal can *use* all of those things; changing them is a web task. Artifacts
cannot be rendered in a terminal, so `/artifacts` saves the source to disk
instead. The full comparison is in `docs/specs/TUI_WEB_PARITY_SPEC.md`.

## For forks

Nothing in the client, its tests or its docs names a deployment; the base URL
is always the user's. The release workflow builds the wheel with
`scripts/tui/build.sh` and attaches it using only `GITHUB_TOKEN`, so a fork's
own release page serves its own users once the repository guard in
`release.yml` is changed to the fork's name. A fork without releases can
install straight from git:

```bash
uv tool install "git+https://github.com/<org>/<repo>@<tag>#subdirectory=tui"
```

The detailed reference, including configuration, logging and running against a
local App API, is [`tui/README.md`](https://github.com/Boise-State-Development/agentcore-public-stack/blob/main/tui/README.md).
