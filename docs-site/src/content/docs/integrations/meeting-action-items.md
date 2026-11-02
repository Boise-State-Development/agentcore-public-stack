---
title: Meeting action items (Zoom, Google Calendar, GitHub)
description: Turn a recorded meeting into GitHub issues — find it on the calendar, read the Zoom transcript, open one issue per action item.
sidebar:
  order: 4
---

:::note[In development — default off]
These tools ship dark. Set the `CDK_MEETING_TASKS_ENABLED` GitHub environment
variable to `true` and deploy `platform.yml` to turn them on. See
[Feature flags](/configuration/feature-flags/).
:::

A user asks: *"Pull the transcript from yesterday's sprint planning and open
GitHub issues for the action items."* The agent finds the meeting on the user's
Google Calendar, downloads the Zoom cloud-recording transcript, drafts one issue
per action item, shows the list, and opens the issues in the repository the
user names once they confirm.

Three first-party tool families do this. Each calls the vendor's REST API
directly with **the user's own OAuth token**, obtained through the same
connector flow the platform already uses for OAuth-gated MCP servers. Nothing is
stored on the platform: no transcripts, no vendor tokens outside the AgentCore
Identity vault.

| Catalog tool (grant per role) | Tools the agent sees | Vendor API |
|---|---|---|
| `google_calendar_meetings` | `list_calendar_meetings` | Google Calendar v3, `calendars/primary/events` |
| `zoom_meeting_transcripts` | `list_zoom_recordings`, `get_zoom_transcript` | Zoom v2, `users/me/recordings`, `meetings/{id}/recordings`, transcript download |
| `github_issues` | `list_github_repositories`, `create_github_issues` | GitHub REST, `user/repos`, `repos/{owner}/{repo}/issues` |

The three are independent. Grant only Zoom + GitHub to a team that does not use
Google Calendar; the agent asks for the meeting id instead.

## Setup

### 1. Create the connectors (Admin → Connectors)

Create one connector per vendor. Each needs an OAuth app registered at the
vendor with the **callback URL the connector page shows after you create it**
as an authorized redirect URI.

| Connector | Provider type | Scopes to configure | Vendor-side notes |
|---|---|---|---|
| Google Calendar | Google | `https://www.googleapis.com/auth/calendar.readonly` | Custom OAuth parameters `access_type=offline` and `prompt=consent`, or Google issues no refresh token and users reconnect hourly. An existing Google connector (for example the Drive connector used by folder attachments) can be reused by adding this scope to it — every user is then re-prompted once for the widened scope. |
| Zoom | Zoom | Granular: `cloud_recording:read:list_user_recordings`, `cloud_recording:read:list_recording_files`. Classic: `recording:read` | A **user-managed** OAuth app in the Zoom App Marketplace. Cloud recording and audio transcription must be enabled for the account, or no recording ever has a transcript. |
| GitHub | GitHub | `repo` (private repositories) or `public_repo` | A GitHub OAuth App. Organizations with OAuth App access restrictions must approve the app before issues can be opened in their repositories. |

### 2. Bind each tool to its connector (Admin → Tools)

Open each of the three catalog entries and set **Required OAuth provider** to the
matching connector. This is the link the runtime uses: the tool calls the vendor
with a token from *that* connector, and the user is asked to connect it the first
time a tool runs. A tool left on "None" still appears for granted users but
answers every call with "no connector configured", so the gap is visible rather
than silent.

Only the three meeting tools read this field on a built-in tool; leave it on
"None" for every other built-in tool.

### 3. Grant the tools to roles (Admin → Roles)

All three rows are seeded `enabledByDefault: false` and not public. Grant them
to the roles that should have them. Users then turn them on in the composer's
tool picker like any other tool, and the picker shows the "connect" affordance
until they have connected the vendor.

### 4. Turn the feature on

Set `CDK_MEETING_TASKS_ENABLED=true` in the GitHub environment and run the
platform deploy. The flag reaches the AgentCore Runtime only; with it off the
tools are never built, whatever the catalog says.

## What users see

1. The first call to a vendor pauses the turn on the standard consent card
   ("Connect Zoom"). After the popup completes, the turn resumes where it
   paused. Connections are managed under **Settings → Connectors**.
2. `get_zoom_transcript` returns the transcript as `[HH:MM:SS] Speaker: text`
   lines, with consecutive lines from one speaker merged. A transcript over
   60,000 characters (roughly a 60–90 minute meeting) is cut, and the result
   says so.
3. `create_github_issues` is a two-step write. The first call validates the
   repository and every issue and returns a preview; the model shows it and
   calls again with `confirm=true` only after the user agrees in the
   conversation. The result links each issue it opened.

## Limits and behaviour worth knowing

- **Zoom date ranges are one month** per `list_zoom_recordings` call (a Zoom API
  limit). A meeting id resolves to its most recent recorded instance; pass the
  recording `uuid` for an older one.
- **Transcripts appear after Zoom finishes processing** the recording, usually
  within an hour of the meeting ending. Until then `has_transcript` is false
  and `get_zoom_transcript` says so.
- **Only Zoom links the regex recognises** (`zoom.us/j/<id>`, `/w/`, `/s/`)
  yield a `zoom_meeting_id` from a calendar event. A vanity link
  (`zoom.us/my/room`) does not; the agent falls back to `list_zoom_recordings`.
- **Transcript downloads are pinned to Zoom's domains.** A `download_url` that
  does not point at `*.zoom.us` / `*.zoomgov.com`, or a redirect that leaves
  them, is refused.
- **Transcript text is data.** The tool result tells the model to report what
  was said and not to follow instructions inside it, and `create_github_issues`
  refuses to confirm on anything but the user's say-so in the conversation.
- **Expired tokens refresh silently.** A `401` from any vendor is reported in
  the shape the consent hook recognises; it clears the cached token and retries
  the call once with a refreshed one. For the GitHub write this happens only
  before the first issue is created, so a retry can never open duplicates.

## Cost

Nothing recurring on the platform side. A transcript enters the conversation
once, as a bounded tool result, and is then part of that conversation's history
like any other tool result. The five tool specs are constant per deployment, so
they sit in the prompt-cached prefix.
