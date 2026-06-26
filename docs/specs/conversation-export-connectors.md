# Save Conversations to Connected Apps (Export Targets)

**Status:** Draft / proposal
**Author:** (spec)
**Related:** connector + file-source import pattern (`apis/app_api/file_sources/`, `apis/app_api/connectors/`), OAuth provider model (`apis/shared/oauth/`)

## Summary

We already let a user connect an app (Google Drive) and pull documents **into** an
Assistant's knowledge base. This spec extends the same connector + adapter pattern
in the opposite direction: let a user push a **conversation transcript out** to a
connected app — "Save this chat to Google Drive."

The key architectural move is to recognize that the existing pattern has two layers,
and only one of them is direction-specific:

- **Auth layer** (`OAuthProvider` + AgentCore Identity + consent UX) — provider-agnostic
  and **direction-agnostic**. Reused as-is.
- **Capability layer** (`FileSourceAdapter` registry) — direction-specific. Today it
  only models **sources** (read: `list_roots`/`browse`/`search`/`download`). We add a
  parallel **destination/export-target** capability (write: `create_document`).

Because the capability layer is a registry keyed by a code-shipped adapter, this design
is generic from day one: Google Drive is the first export target, and adding OneDrive /
SharePoint / Dropbox / Box later is "write one adapter class + register it + an admin maps
a connector" — exactly the cost of adding a new file source today.

## Goals

- Let a user save a full conversation transcript to a connected app they own.
- Reuse the existing connector model, RBAC visibility gate, AgentCore Identity token
  mint, and the connect/consent/disconnect UX with **zero changes to the auth layer**.
- Make export **generic across providers** via an `ExportTargetAdapter` registry that
  mirrors the `FileSourceAdapter` registry. Drive is the reference implementation.
- Keep the write in `app-api` (user-facing, deterministic action), honoring the
  inference-api boundary rule.

## Non-goals (v1)

- Continuous/auto sync of conversations to Drive. v1 is an explicit, one-shot "Save"
  action.
- Round-tripping (importing a saved transcript back as a conversation).
- An agent-callable `save_conversation` tool. Designed-for but deferred (see
  [Future: conversational surface](#future-conversational-surface)).
- Org-managed shared destinations / write to *another* user's drive.

---

## Background: how import works today (the pattern we're extending)

```
CONNECTOR (OAuthProvider, DynamoDB)           ← auth layer (provider-agnostic)
  provider_type: GOOGLE
  scopes: [drive.readonly]
  allowed_roles: [...]                          ← RBAC visibility
  file_source_adapter_id: "google-drive"        ← maps connector → capability

FileSourceAdapter (registry, code-shipped)     ← capability layer (READ)
  metadata{key, compatible_provider_types, required_scopes}
  list_roots / browse / search / download

Flow: connect (AgentCore 3LO consent) → pick files → POST /assistants/{id}/documents/import
      → resolve_file_source() + require_file_source_token() → adapter.download() → S3 → ingest
```

Concrete anchors:
- Connector model: [`OAuthProvider`](../../backend/src/apis/shared/oauth/models.py) — note the existing `file_source_adapter_id` field (models.py:124) we will mirror.
- Capability contract: [`FileSourceAdapter`](../../backend/src/apis/app_api/file_sources/adapter.py)
- Registry: [`registry.py`](../../backend/src/apis/app_api/file_sources/registry.py)
- Drive adapter: [`google_drive.py`](../../backend/src/apis/app_api/file_sources/adapters/google_drive.py)
- Resolve connector → adapter + token: [`resolve_file_source` / `require_file_source_token`](../../backend/src/apis/app_api/file_sources/service.py)
- Consent UX (connect / status / complete / disconnect): [`connectors/routes.py`](../../backend/src/apis/app_api/connectors/routes.py)
- Admin adapter dropdown: [`admin/file_sources/routes.py`](../../backend/src/apis/app_api/admin/file_sources/routes.py)
- Transcript retrieval: [`get_messages(session_id, user_id, ...)`](../../backend/src/apis/shared/sessions/messages.py) (messages.py:534)

**The asymmetry that matters:** the Drive adapter is read-only by construction —
`drive.readonly` scope, and the contract has no write method. Export inverts the data
flow (write a file out) and needs a write scope. That is the whole of the new work.

---

## Design

### 1. New capability: `ExportTargetAdapter` (mirrors `FileSourceAdapter`)

New package `apis/app_api/export_targets/` parallel to `file_sources/`.

```python
# apis/app_api/export_targets/adapter.py
@dataclass(frozen=True)
class ExportTargetMetadata:
    key: str                                   # e.g. "google-drive"
    display_name: str                          # "Google Drive"
    icon: str
    compatible_provider_types: Tuple[OAuthProviderType, ...]
    required_scopes: Tuple[str, ...]           # Drive: (DRIVE_FILE_SCOPE,)
    # What document formats this target can accept / convert to:
    supported_formats: Tuple[ExportFormat, ...]  # e.g. (GOOGLE_DOC, MARKDOWN, PDF)

@dataclass(frozen=True)
class CreatedFile:
    file_id: str
    name: str
    web_view_link: Optional[str]               # surfaced to the SPA as "Open in Drive"

class ExportTargetAdapter(ABC):
    @property
    @abstractmethod
    def metadata(self) -> ExportTargetMetadata: ...

    @abstractmethod
    async def list_destinations(self, access_token: str) -> List[SourceRoot]:
        """Top-level write locations (e.g. My Drive, shared drives). Optional folder picker."""

    @abstractmethod
    async def create_document(
        self,
        access_token: str,
        *,
        content: bytes,
        name: str,
        source_mime_type: str,        # what we're uploading, e.g. text/html
        target_format: ExportFormat,  # how it should land, e.g. GOOGLE_DOC
        parent_id: Optional[str],     # destination folder, None = drive root
    ) -> CreatedFile: ...
```

Registry `apis/app_api/export_targets/registry.py` is a 1:1 copy of the file-source
registry pattern (process-wide singleton, code-shipped, immutable at runtime), seeded with
`GoogleDriveExportAdapter()`.

> **Decided (was OQ-1): parallel registries, not a write method bolted onto
> `FileSourceAdapter`.** A connector can legitimately be a source but not a destination (or
> vice-versa), required scopes differ, and the existing `FileSourceAdapter` docstring is
> explicit that adapters are read-shaped. Two small registries read more honestly than one
> adapter with half its methods raising `NotImplementedError`. The browse/roots contract is
> near-identical between the two — acceptable duplication for an honest contract.

### 2. Google Drive export adapter

`apis/app_api/export_targets/adapters/google_drive.py`

- Scope: `https://www.googleapis.com/auth/drive.file` — **least privilege**: grants
  create + access to files the app created, not read over the user's whole drive.
- `create_document` → `POST https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&supportsAllDrives=true`
  with metadata `{name, mimeType: <target>, parents: [parent_id?]}` + media body.
  - **`target_format = GOOGLE_DOC` (default):** metadata `mimeType: application/vnd.google-apps.document`,
    upload a **`text/html` body** (the render step converts Markdown → HTML first) → Drive's
    HTML-import converter produces a **native Google Doc with real Docs styling**. See
    [Markdown → Google Doc fidelity](#markdown--google-doc-fidelity) below — this is why we go
    via HTML rather than uploading raw Markdown (which would land as literal `**asterisks**`).
  - `target_format = MARKDOWN` → plain `text/markdown` file, no conversion (portable export).
  - `target_format = PDF` → upload `application/pdf` bytes (render step produces them).
- `list_destinations` → My Drive + shared drives (same shape as the import adapter's
  `list_roots`). With the combined-scope connector (decided, §3) this is backed by the import
  adapter's existing `browse()`.

#### <a name="markdown--google-doc-fidelity"></a>Markdown → Google Doc fidelity

We render the transcript Markdown to **sanitized HTML**, then upload as `text/html` against the
Google Doc target. Drive's HTML import maps the common elements to native Docs styling:

| Markdown | Google Doc result | Fidelity |
|---|---|---|
| `#` / `##` / `###` | Heading 1/2/3 paragraph styles | high |
| bold / italic / links | bold / italic / hyperlinks | high |
| bullet / numbered lists | native Docs lists | high |
| tables | Docs tables | good |
| blockquotes, `---` | indented quote, horizontal rule | good |
| fenced / inline code | preformatted / monospace text, **no syntax highlighting** | partial |
| inline images | embedded — data-URI images need to be hosted or skipped | needs care |

PR-2 should also **spike Google's newer native Markdown→Docs import** (upload `text/markdown`
against the Doc target): if it round-trips fenced code and images better than HTML import, prefer
it; otherwise HTML import is the robust default. Either way the adapter interface is unchanged —
only the `source_mime_type` we hand it differs.

### 3. Folder picker & scope — **decided: combined-scope connector**

`drive.file` can *write into* a folder given its id, but cannot *list* arbitrary existing
folders to discover that id. **Decision (was OQ-4):** one Drive connector carries the combined
scope set **`[drive.readonly, drive.file]`**. This is the same connector used for document
import, now also mapped as an export target. Consequences:

- The **import adapter's existing `browse()`/`list_roots()` powers the destination folder
  picker for free** — no Google Picker JS, no separate write-only connector.
- A **single connection covers import *and* export** — one row in the connector catalog, one
  consent.
- **Re-consent cost:** adding `drive.file` to an already-mapped Drive connector changes its
  `scopes_hash`; existing import users must re-consent once. Surface this in the admin UI the
  same way other scope changes are surfaced today (see [Security & correctness](#security--correctness-notes)).

Phasing of the picker UI itself is independent of this scope decision:
- **v1:** ship with a **default app folder** (`/<App Name>/Conversations/`, creatable under
  `drive.file`) and surface the returned `web_view_link`. No picker UI yet.
- **v2:** wire the reused import browse dialog as a destination picker (now unblocked by the
  combined scope — `parentId` simply flows into `create_document`).

### 4. Transcript → document rendering (user-selectable elements)

New `apis/app_api/export_targets/render.py`:

- Page through the entire session via `get_messages(session_id, user_id, limit, next_token)`
  until `next_token` is exhausted (export needs the *full* transcript, not one page).
- Render messages to an intermediate **HTML** (Markdown → HTML), then hand bytes to the
  adapter. HTML is the universal source: Drive's HTML import yields a styled Google Doc, and
  the same HTML feeds an `.md` emit or an HTML→PDF pass.
- Reuse opportunity: the artifact-render / docling infra already in the repo for any HTML→PDF
  need, rather than adding a new renderer dependency.

**Decided (was OQ-2): the user picks what's included, via checkboxes in the export dialog.**
The render is driven by an `include` flag set carried on the export request, so the same
endpoint produces a lean or a full transcript without server-side guesswork:

```python
class ExportInclude(BaseModel):       # all default per the table below
    user_messages: bool = True        # always on; shown disabled/checked in the UI
    assistant_messages: bool = True   # always on; shown disabled/checked in the UI
    tool_calls: bool = True           # tool name + collapsed input/result
    images: bool = True               # inline image blocks (data-URI handling, §2)
    citations: bool = True            # source citations / references
    reasoning: bool = False           # model reasoningContent blocks — default OFF
    timestamps: bool = False          # per-message time — default OFF
```

| Checkbox | Default | Notes |
|---|---|---|
| Messages (user + assistant) | on, locked | the transcript itself; not unselectable |
| Tool calls & results | on | rendered collapsed/labeled |
| Images | on | inline; raw document blobs always become a `[attached: name]` placeholder |
| Citations | on | — |
| Reasoning | off | can be verbose / not user-facing |
| Timestamps | off | — |

The SPA renders these as a checkbox group in the "Save to…" dialog; unchecked elements are
skipped at render time. Defaults match the table so the common case is one click.

### 5. Resolve + token helpers (mirror file-source service)

`apis/app_api/export_targets/service.py` mirrors
[`file_sources/service.py`](../../backend/src/apis/app_api/file_sources/service.py) exactly:

- `resolve_export_target(connector_id, user, provider_repo, role_service) -> (OAuthProvider, ExportTargetAdapter)`
  — RBAC visibility gate + `export_target_adapter_id` lookup + registry resolve (404/403).
- `require_export_target_token(provider, user_id) -> str` — calls
  `get_token_for_user(... custom_parameters=custom_parameters_for(..., force_authentication=True))`,
  returns the access token or raises 409 (not connected) / 503 (no workload context).

This is a near-verbatim copy; the only differences are the field name
(`export_target_adapter_id`) and the registry it resolves against.

### 6. Export endpoint (app-api)

```
POST /sessions/{session_id}/export
  body: {
    connectorId: str,
    format?: "google_doc" | "markdown" | "pdf",   # default "google_doc"
    parentId?: str,                                # destination folder (v2 picker); omit = app folder
    include?: ExportInclude,                       # §4 checkboxes; omitted = defaults
  }
  auth: Depends(get_current_user_from_session)     # cookie, per the auth-dependency rule
```

Flow:
1. Verify the session belongs to `current_user` (reuse the session ownership check used by
   `GET /sessions/{id}/messages`).
2. `provider, adapter = resolve_export_target(connectorId, ...)`.
3. `token = require_export_target_token(provider, user_id)` — **409 path is the consent hook**
   (see next section).
4. `content = render(session_id, user_id, format)`.
5. `created = adapter.create_document(token, content=..., name=<session title>, ...)`.
6. Return `{ fileId, name, webViewLink }` (200). Optionally persist an **export receipt** on
   session metadata (mirrors `DocumentProvenance`: connector id, adapter key, remote file id,
   link, timestamp) so the UI can show "Saved to Drive · Open".

Lives in `app-api`, **not** inference-api: it is a user-facing, non-invocation HTTP path, and
app-api can mint per-user tokens via the `AGENTCORE_RUNTIME_WORKLOAD_NAME` workload identity —
the exact mechanism the import + connectors routes already rely on.

### 7. Not-connected → consent (reuse existing UX verbatim)

When `require_export_target_token` raises 409, the SPA runs the **identical** consent flow the
connectors settings page already implements — no new consent machinery:

1. `POST /connectors/{connectorId}/initiate-consent` → `{ authorizationUrl }`
2. open popup → user consents → `POST /connectors/complete-consent { sessionUri, providerId }`
3. retry `POST /sessions/{session_id}/export`.

`initiate-consent`/`complete-consent`/`status`/`disconnect` are already provider-agnostic —
they key only on `provider_id`. They need **no changes**.

### 8. Admin surface

Mirror the file-source admin dropdown:
- Add `export_target_adapter_id: Optional[str]` to `OAuthProvider`,
  `OAuthProviderCreate`, `OAuthProviderUpdate`, `OAuthProviderResponse`, and the
  Dynamo (de)serializers in [`models.py`](../../backend/src/apis/shared/oauth/models.py)
  (mirrors `file_source_adapter_id` line-for-line).
- Add `GET /admin/export-target-adapters` (copy of
  [`admin/file_sources/routes.py`](../../backend/src/apis/app_api/admin/file_sources/routes.py))
  so the connector form can render an "Export target" dropdown.
- Admin-route validation mirrors the file-source validation at
  `admin/oauth/routes.py:196/271/381` (adapter exists + provider-type compatible +
  warn if connector scopes don't cover `required_scopes`).

### 9. Frontend

- **Chat surface:** a "Save to…" action (overflow menu on a conversation) opens a dialog with:
  (a) connector picker (reuse `GET /connectors/` filtered to export-capable ones), (b) format
  choice (Google Doc default / Markdown / PDF), (c) the **include checkbox group** from §4
  (messages locked-on; tool calls / images / citations on; reasoning / timestamps off). It then
  calls the export endpoint; on 409 runs the consent popup and retries; on success shows
  "Saved · Open in Drive" using `webViewLink`.
- Reuse the connector consent service already powering the settings page.
- (v2) destination folder picker reusing the import file-browser dialog component — unblocked
  by the combined-scope connector (§3).

---

## Generalization: adding the next export target

This is the payoff of the registry approach. To add **OneDrive** (or Dropbox, Box, SharePoint):

1. Write `OneDriveExportAdapter(ExportTargetAdapter)` (Graph API `PUT /me/drive/...`),
   `metadata.compatible_provider_types = (OAuthProviderType.MICROSOFT,)`,
   `required_scopes = ("Files.ReadWrite",)`.
2. Register it in `export_targets/registry.py`.
3. Admin creates a Microsoft connector and maps `export_target_adapter_id = "onedrive"`.

No changes to the endpoint, the consent flow, RBAC, the render step, or the SPA. The auth
layer already enumerates `MICROSOFT`, `SLACK`, `SALESFORCE`, `ZOOM`, `CANVAS`, `CUSTOM`
(`OAuthProviderType`), so new providers are connector config, not new auth code — the same
property that makes import provider-agnostic today.

---

## Security & correctness notes

- **Least privilege:** prefer `drive.file` over `drive`/`drive.readonly` for the write path.
  The app can only touch files it created. Combined-scope connectors (for the folder picker)
  add `drive.readonly` deliberately and visibly.
- **customParameters are part of the token-vault key.** The export token request **must** use
  `custom_parameters_for(..., force_authentication=True)`, identical to the file-source and
  connector paths, or AgentCore reports consent-required against a usable vaulted token. This
  is already encoded in `require_file_source_token`; copy it exactly.
- **Re-consent on scope change.** Adding `drive.file` to an already-connected Drive connector
  changes its `scopes_hash`; existing users must re-consent. Surface this in the admin UI the
  same way scope changes are surfaced today.
- **Ownership:** the export endpoint writes to the *requesting user's own* drive via their own
  vaulted token. No cross-user or service-account writes in v1.
- **Session ownership check** on the export endpoint must match the read path so a user can
  only export their own conversations.
- **AgentCore Memory is read-OK for display.** `get_messages` already backs the SPA history
  view, so the transcript is retrievable; just remember to page to completion.

## Testing

- Adapter unit tests with `httpx.MockTransport` (the import adapter is the template —
  `GoogleDriveAdapter` takes an injectable transport for exactly this).
- `resolve_export_target` / `require_export_target_token` tests mirror the file-source service
  tests (404 not-an-export-target, 403 RBAC, 409 not-connected, 503 no-workload).
- Render tests: multi-page session, tool-call rendering, empty conversation.
- Architecture boundary test stays green: `export_targets/` lives under `apis/app_api/`, only
  imports from `apis.shared` and `apis.app_api` (no inference-api / agents coupling).
- Backend pytest is **not** run in CI — full local suite is the correctness gate for the
  shared `OAuthProvider` change.

## Rollout

- Feature-flag the chat "Save to…" action (env flag on app-api + SPA) so it ships dark until
  an export connector is configured.
- No CDK changes required for the core feature: it reuses the existing OAuth provider table,
  workload identity, and app-api service. (A combined-scope or new connector is admin config,
  not infra.)

## Suggested PR sequence

1. **PR-1 — data + capability scaffold:** `export_target_adapter_id` on `OAuthProvider`
   (+ models/serializers), `ExportTargetAdapter` contract + registry, admin
   `GET /admin/export-target-adapters`, admin form validation. No behavior yet.
2. **PR-2 — Drive export adapter + render:** `GoogleDriveExportAdapter.create_document`
   (default app-folder), `render.py`, service resolve/token helpers. Unit tests.
3. **PR-3 — export endpoint:** `POST /sessions/{id}/export` + optional export receipt on
   session metadata.
4. **PR-4 — SPA:** "Save to…" action, connector picker, 409→consent retry, success/Open link.
5. **PR-5 (v2) — folder picker:** combined-scope connector + reuse the import browse dialog as
   a destination picker.
6. **PR-6 (deferred) — agent tool:** `save_conversation` riding the `oauth_required` SSE gate.

## Decisions

- **D-1 (was OQ-1) — Parallel `ExportTargetAdapter` registry**, not a write method bolted onto
  `FileSourceAdapter`. Cleaner contract; source ≠ destination. (§1)
- **D-2 (was OQ-2) — User-selectable elements via checkboxes.** The export request carries an
  `include` flag set; messages are locked-on, tool calls / images / citations default on,
  reasoning / timestamps default off. (§4)
- **D-3 (was OQ-3) — Default to a native Google Doc**, rendered Markdown → HTML → Doc so
  Markdown formatting maps to real Docs styling; also offer Markdown and PDF. PR-2 spikes
  Google's native Markdown→Docs import as a possible better path for code/images. (§2)
- **D-4 (was OQ-4) — One combined-scope Drive connector** (`[drive.readonly, drive.file]`),
  used for both import and export. Unlocks reusing the import browse UI as a destination picker;
  costs a one-time re-consent for existing import users. (§3)

### Remaining to confirm

- **R-1.** Data-URI / inline image handling for the Google Doc path (host vs. skip vs. embed).
  Resolve during PR-2 alongside the native-Markdown-import spike.
- **R-2.** Whether to persist an export receipt on session metadata in v1 (for a "Saved · Open"
  affordance that survives reload) or just return the link. Lean: persist — it's cheap and
  mirrors `DocumentProvenance`.

## <a name="future-conversational-surface"></a>Future: conversational surface

Once the deterministic endpoint exists, a `save_conversation` **agent tool** is a thin add: it
rides the existing `oauth_required` SSE consent gate
([`oauth_consent.py`](../../backend/src/agents/main_agent/session/hooks/oauth_consent.py))
and `OAuthRequiredEvent` so "save this chat to my Drive" works mid-conversation. Deferred from
v1 to keep the first cut deterministic and owned by app-api.
