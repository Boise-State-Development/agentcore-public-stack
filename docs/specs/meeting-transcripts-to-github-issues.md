# Spec: Meeting transcripts → GitHub issues (Google Calendar, Zoom, GitHub)

**Status:** Implemented, in development (default off: `MEETING_TASKS_ENABLED` / `CDK_MEETING_TASKS_ENABLED`)
**Audience:** Whoever turns this on for an environment, and whoever changes it.
**Operator guide:** `docs-site/src/content/docs/integrations/meeting-action-items.md`
**Last updated:** 2026-11-02

---

## 1. One-line summary

Three first-party tool families — Google Calendar, Zoom, GitHub — that let the agent find a
meeting, read its Zoom cloud-recording transcript, and open one GitHub issue per action item,
each calling the vendor's REST API with **the user's own OAuth token** from the AgentCore
Identity vault. No new storage, no new infrastructure, no new consent UX.

---

## 2. Decisions

| Decision | Choice | Why |
|---|---|---|
| Mechanism | First-party `@tool` functions calling vendor REST APIs with `httpx` | The OAuth plumbing (connectors, vault, consent interrupt, token cache) exists; hosted MCP servers for these vendors are a per-deployment registration, not something the repo can ship and test. Same shape as `docs/specs/google-tasks-todo.md` §2. |
| Identity | Captured by closure, never a model argument | The `account_tools.py` invariant. A factory builds each tool per request over `(user_id, provider_id)`. |
| Which connector | The tool's catalog row, `requires_oauth_provider` ("Required OAuth provider" in Admin → Tools) | Connector ids are deployment data (`google-workspace` on one stack, `google` on another). The field already exists on every row; the admin form now shows it for `local` tools. No env vars, so nothing against the Runtime's 2,560-byte budget. |
| Consent | The existing `OAuthConsentHook`, extended to local tools | The hook already gates `MCPAgentTool`s by client → provider. A bound local tool carries its provider id (`agents/local_tools/oauth_binding.py`), and the hook's `provider_lookup` reads it back. Zero new consent paths. |
| Token access in the tool body | `oauth_token_cache.get(user_id, provider_id)` | The hook warms exactly that key before the call (or pauses the turn). A tool body cannot raise the consent interrupt, so it must never call AgentCore Identity itself. |
| 401 handling | Return `"<Vendor> returned HTTP 401 Unauthorized …"` as an error result | `looks_like_auth_failure` matches it; the hook clears the cache and retries once, which refreshes through the vault. The GitHub write emits this shape only before any issue was created (see §5). |
| Granularity | One catalog row per vendor, each provisioning its whole family | RBAC and connector binding are per vendor. A team without Google Calendar gets Zoom + GitHub only. |
| Gate | `MEETING_TASKS_ENABLED`, opt-in, Runtime only | House rule for new features. Rows are also seeded off-by-default and not public, so the flag alone grants nothing. |
| GitHub write | Two-step `confirm` like `set_default_model` | The model previews; the user agrees in the conversation; then it writes. Transcript text is explicitly data, not instructions. |

### Non-goals

- No owned storage of transcripts, issues or tokens.
- No Microsoft Teams / Outlook / Jira. The pattern extends, one family per vendor.
- No MCP App panel. Local tools get no `ui_resource`; an interactive review surface would be a
  separate MCP server (google-tasks spec §4).
- No automatic action-item extraction logic. The model reads the transcript and drafts; the tools
  fetch and write.

---

## 3. Components

| Piece | Where |
|---|---|
| Tool families, pure helpers, factory dispatch | `backend/src/agents/local_tools/meeting_tasks.py` |
| Connector binding on a tool object | `backend/src/agents/local_tools/oauth_binding.py` |
| Consent hook provider lookup (MCP client **or** bound local tool) | `BaseAgent._build_oauth_consent_hook`, `agents/main_agent/base_agent.py` |
| Gate ids (`MEETING_TASKS_TOOL_IDS`), cache eligibility | `backend/src/apis/shared/tools/injected.py` |
| Catalog metadata | `agents/main_agent/tools/tool_catalog.py`; seed rows in `backend/scripts/seed_bootstrap_data.py` |
| `{tool_id: provider_id}` snapshot off the catalog read | `apis/shared/tools/freshness.py`, `get_local_tool_oauth_providers` |
| Per-turn builder | `_build_meeting_tools`, `apis/inference_api/chat/routes.py` (awaited inside `_build_turn_tools`) |
| Feature flag | `meeting_tasks_enabled()`, `apis/shared/feature_flags.py`; CDK `config.meetingTasks`, Runtime env only while on; `platform.yml` forwards `CDK_MEETING_TASKS_ENABLED` |
| Admin UI | "User OAuth connector" section now also shown for `protocol == local`, `admin/tools/pages/tool-form.page.ts` |

### Tools

| Catalog id | Tool | Vendor call | Bounds |
|---|---|---|---|
| `google_calendar_meetings` | `list_calendar_meetings(start?, end?, query?)` | `GET calendar/v3/calendars/primary/events` (singleEvents, orderBy startTime) | 50 events; 25 attendee emails listed (count always full); description/location 300 chars; cancelled events dropped; `zoom_meeting_id` parsed from conference entry points, hangoutLink, location, description |
| `zoom_meeting_transcripts` | `list_zoom_recordings(from_date?, to_date?)` | `GET users/me/recordings` | ≤ 1 month per call (Zoom limit); ≤ 3 pages × 30 |
| | `get_zoom_transcript(meeting_id)` | `GET meetings/{id}/recordings`, then the `TRANSCRIPT` file's `download_url` | download host must be `*.zoom.us` / `*.zoomgov.com` before **and** after redirects; WebVTT parsed, consecutive same-speaker cues merged; cut at 60,000 chars with `truncated` + note |
| `github_issues_local` | `list_github_repositories(query?)` | `GET user/repos?affiliation=owner,collaborator,organization_member&sort=pushed` | 50 repos; description 120 chars |
| | `create_github_issues(repository, issues[], confirm=false)` | `POST repos/{owner}/{repo}/issues` per issue | `owner/name` validated; ≤ 25 issues; title ≤ 256; body ≤ 20,000; preview unless `confirm` |

Every tool returns the Strands shape `{"status", "content": [{"json"|"text"}]}` and never raises.

---

## 4. Turn path

1. `_resolve_effective_tools` → `_apply_admin_always_on_tools` → `resolve_system_tool_ids` fills the
   freshness snapshots from one `list_tools()` read (TTL 10 s). The new
   `_local_oauth_providers_cache` slot is filled in the same pass, filtered to `protocol == local`
   rows with a non-blank `requires_oauth_provider`.
2. `_build_turn_tools` → `await _build_meeting_tools(effective_enabled_tools, user_id)`:
   set intersection; flag check; dict read of the snapshot; `build_meeting_tasks_tools` builds the
   enabled families in fixed catalog order (the order reaches the cached `toolConfig`). A row with
   no connector still builds its family bound to `None`, and logs a warning.
3. On `BeforeToolCallEvent`, the consent hook's `provider_lookup` returns the bound provider id;
   the hook warms `oauth_token_cache[(user_id, provider_id)]` from the vault or interrupts with
   `oauth:<provider_id>` exactly as for an MCP tool.
4. The tool body reads the cached bearer and calls the vendor. A `401` result makes
   `_handle_auth_failure` clear the cache and set `event.retry`, once per provider per turn.

**Cost posture.** Off: one `frozenset.intersection` per turn. On, without the ids enabled: the
same. On, with the ids: a dict lookup and five constant tool specs in the prefix. Nothing reaches
the system prompt. Transcripts are bounded at the source.

**Agent cache.** The family is in `KEY_DESCRIBED_INJECTED_TOOL_IDS`. The closures hold
`(user_id, provider_id)`; `user_id` is a key element and `provider_id` is catalog data covered by
the key's `freshness_hash` (the enabled tools' `updated_at`), so re-binding a row evicts the cached
agent on the next turn. Tokens are read live on every call, so connecting never needs a rebuild.

---

## 5. Safety notes

- **Duplicate writes.** `create_github_issues` returns the retryable 401 shape only when no issue
  has been created yet. A 401 (or any failure) after the first creation returns a `partial`
  success listing `created` and `failed`, so a hook-driven retry can never re-open issue 1.
- **SSRF.** The only URL taken from a vendor response and fetched is Zoom's transcript
  `download_url`; it is checked against Zoom's domains before the request and the final URL is
  checked again after redirects. Everything else is a fixed API base.
- **Prompt injection.** `get_zoom_transcript`'s docstring tells the model the text is quoted data,
  and `create_github_issues` tells it never to confirm on an instruction from a transcript,
  document or page. The two-step confirm is the hard stop.
- **Logging.** Vendor, status code and exception type only. No tokens, no transcript text, no
  issue bodies.

---

## 6. Tests

- `backend/tests/agents/local_tools/test_meeting_tasks.py` — every tool against a fake `httpx`
  transport: payload shapes and bounds, 401 shape, non-raising errors, Zoom host allowlist,
  UUID encoding, VTT parsing, GitHub preview/confirm/partial, input validation.
- `backend/tests/agents/main_agent/test_local_tool_oauth_wiring.py` — the `BaseAgent` provider
  lookup for bound/unbound/MCP tools, the consent gate warming the cache and interrupting, the
  single 401 retry, the flag, the freshness snapshot, the route builder, cache eligibility.
- `backend/tests/test_seed_matches_tool_catalog.py` and `tests/agents/main_agent/tools/test_tool_filter.py`
  pin the catalog rows and gate set.
- `infrastructure/test/meeting-tasks.test.ts` — flag parsing, Runtime-only env var only while on,
  no IAM or resource change.

Not verified in this change: a live run against Zoom, Google and GitHub. The vendor request and
response shapes are taken from the public API references and exercised only against fakes.

---

## 7. Rollout

1. Deploy backend (`backend.yml`) and platform (`platform.yml`) with `CDK_MEETING_TASKS_ENABLED=true`
   on the target environment.
2. Run the bootstrap seeder (or create the three catalog rows by hand on an existing stack — the
   seeder does not overwrite existing tools).
3. Admin → Connectors: Google (scope `calendar.readonly`, `access_type=offline`, `prompt=consent`),
   Zoom (`cloud_recording:read:list_user_recordings`, `cloud_recording:read:list_recording_files`),
   GitHub (`repo` or `public_repo`). Register each connector's callback URL at the vendor.
4. Admin → Tools: set "Required OAuth provider" on each of the three rows.
5. Admin → Roles: grant the rows.
6. First live check: a user with all three connected asks for yesterday's meeting; confirm the
   consent card appears once per vendor, the transcript renders with speakers, the preview lists
   the issues, and `confirm=true` opens them with links.
