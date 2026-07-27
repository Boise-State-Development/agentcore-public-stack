# `assistants/` — shared building blocks, not a feature

⚠️ **There is no Assistants feature any more.** There is one noun, and it is **Agent**
(Marketplace D1). Designer Phase 5 retired the Assistant editor: `/assistants`,
`/assistants/new` and `/assistants/:id/edit` are `redirectTo` entries onto `/agents`, and
the pages behind them (`assistants.page`, `assistant-form.page`, `assistant-list`,
`assistant-preview`) were deleted.

What is left in this folder is **the parts the Agent surface still consumes.** It survives
under this name because the underlying record is still an Assistant on the wire — the ids
are identical, `compat.to_agent_view` renders one *as* an Agent, and nothing was migrated.
Renaming the folder would drag the API contract's vocabulary with it, which is a much
larger change than the UI deprecation was.

**Do not add new UI here.** Anything user-facing belongs under `agents/`.

## What lives here, and who uses it

| Path | Consumed by |
|---|---|
| `components/share-assistant-dialog.component.ts` | `agents/agents.page`, `agents/agent-form`, `session/session.page` |
| `components/file-source-browser-dialog`, `web-source-dialog`, `sync-policy-control` | `knowledge-base/knowledge-base-section` |
| `assistant-form/services/preview-chat.service.ts` | `agents/agent-form/components/agent-preview` |
| `services/assistant.service.ts`, `assistant-api.service.ts` | the share dialog, `session/session.page` |
| `services/document`, `file-source`, `web-source`, `sync-policy` | `knowledge-base/knowledge-base-section` |
| `models/*` | `agents/models/agent.model.ts`, `components/topnav`, the above |

`assistant-form/` now contains **only** `services/preview-chat.service.ts`. The page it was
named for is gone; the service stayed because the Designer's preview pane uses it.

`components/assistant-card.component.ts` is **gone** — replaced by
`agents/components/agent-launch-card.component.ts`. It was the last pre-Marketplace surface
and it drew its own avatar from a 26-entry, first-letter-keyed gradient map, so the same
Agent had one tile in the store and a different one in chat. The replacement renders
`app-agent-icon` like every other surface. Don't reintroduce a per-surface identity hash.

## The one behavioral difference worth knowing

`PreviewChatService.sendMessage` takes `liveInstructions` and an `opts` object. The retired
Assistant editor passed the form's **unsaved** instructions, so its preview reflected what
you were typing. The Agent preview deliberately does **not**: an Agent resolves its
instructions, model, tools, skills and memory server-side from the saved record, so sending
them from the client fights the bindings, and a long persona can exceed the `system_prompt`
cap outright (422).

So the Designer's preview reflects what is **saved**, not what is typed. That is intended,
and `agents/agent-form/components/agent-preview.component.spec.ts` pins it — it looks like a
bug, and "fixing" it reintroduces the 422.

## Backend note

`/assistants/*` on the backend is **not** deprecated and is not going away. The Agent routes
are an alias surface over the same records, and two sub-surfaces deliberately stayed put:
`test-chat` (what `preview-chat.service` calls) and the document sub-routes (what the
knowledge-base section calls). See the "Inference API boundary" and import-boundary notes in
the root `CLAUDE.md`.
