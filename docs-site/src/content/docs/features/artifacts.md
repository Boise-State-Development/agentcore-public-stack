---
title: Artifacts and Artifact Sharing
description: How the agent produces standalone documents, how they are rendered in isolation, and how a user shares one.
sidebar:
  label: Artifacts
  order: 5
---

An **artifact** is a standalone document the agent authors as a first-class
object rather than as text in the chat — an HTML page, a chart, a Markdown
report. It gets its own card in the conversation, its own versioned history, and
its own sandboxed viewer.

The interesting part is not the authoring. It's that an artifact is
**attacker-authored markup rendered in a browser**, so almost every design
decision below is about containing it.

## Producing an artifact

Two agent tools write artifacts: `create_artifact` makes v1, and
`update_artifact` appends a new version. **Versions are immutable and
append-only** — an update writes `V#{n+1}` and re-points a `#HEAD` pointer; it
never rewrites what came before. Nothing in the platform has a `DeleteObject`
grant on artifact content.

That immutability is load-bearing later: it is what lets a share pin one exact
version with no snapshot copy.

Each version lives in two places:

| Where | What |
| --- | --- |
| DynamoDB (`{prefix}-user-artifacts`) | Metadata: title, content type, version, session, and a pointer to the content |
| S3 (`{prefix}-artifacts-content`) | The document bytes themselves |

## Rendering: three layers of isolation

Artifact content is never inlined into the SPA. It is served from a **separate
origin** (`artifacts.<domain>`) and framed, which gives three independent
containment layers:

1. **A separate origin.** The artifact cannot touch the app's cookies, storage,
   or DOM, because the browser treats it as a different site.
2. **A strict CSP**, stamped by both CloudFront and the render Lambda (defense in
   depth, so the policy holds even if the handler is buggy). `connect-src 'none'`
   means artifact JavaScript cannot phone home or exfiltrate anything.
3. **A sandboxed iframe** — `sandbox="allow-scripts"` **without**
   `allow-same-origin`. The framed document is a null origin, so scripts run but
   reach nothing.

To load one, the SPA asks app-api to mint a **render token**: a short-lived
(~120 second) HS256 JWT that pins one exact `(user, artifact, version)`. The
token goes in the iframe URL and is re-minted on every open — never cached.

## Sharing an artifact

A user can share one artifact version with other people. The model deliberately
mirrors conversation sharing, so the two behave alike.

### What a share is

A share is an owner-created, revocable pointer to **one immutable version**.

- **It pins a version, never `#HEAD`.** A link made against v1 keeps showing v1
  after the model writes v2. A pointer that moved under the recipient would be a
  different feature with different consent semantics.
- **Access levels** are `public` — any *authenticated* user with the link — or
  `specific`, an email allowlist matched case-insensitively. There is no
  anonymous access: `public` still sits behind sign-in, exactly as it does for
  conversation shares. Governance here is identity, not content inspection.
- **Revocation** deletes the share. Already-issued render tokens finish their
  ~120 second life, so the revocation window is bounded by the token TTL rather
  than by how long the recipient keeps the tab open.

### Sharing one

Owners share from the **Share** button on the artifact card or in the artifact
panel header. Both share the version currently on screen — the card shows one row
per version, and the panel follows its version menu. The dialog also lists the
artifact's existing links so they can be copied or revoked.

### Opening one

Recipients open `/shared-artifact/{shareId}`, which renders the artifact
full-width in the same sandboxed iframe the owner sees, with a preview/code
toggle and a download. The page is read-only: no re-sharing, no version
switching, no editing.

### How access is actually enforced

This is the part worth understanding before changing anything.

The render Lambda uses the token's `sub` claim purely as the **DynamoDB
partition key** it builds the lookup from. It performs no ownership comparison of
its own and never sees the viewer — **the token is the capability**.

So a share-scoped token is minted with `sub` set to the **owner**, not the
viewer. That is an *address*, not an identity assertion; setting it to the viewer
would simply point the Lambda at the viewer's own partition and 404. The real
viewer travels in a `vwr` claim and the grant it was issued under in `shr`, so
render logs attribute a view to whoever actually looked.

The consequence: **the ACL check in app-api is the only thing standing between
"sharing" and "read any artifact by id."** Every recipient request — metadata,
render token, content — re-checks the share record before resolving the owner.

### Finding what has been shared with you

The library's "Shared with you" tab is backed by `GET /shared-artifacts`, and it
is worth knowing how it is stored, because the obvious answer is wrong.

Every share writes one **fan-out row per recipient**, keyed
`PK=SHARED_WITH#{email}` / `SK=SHARE#{createdAt}#{shareId}`. That is not a
workaround for avoiding an index — `allowedEmails` is a *list*, and a DynamoDB
GSI cannot project one item into several index entries, so any recipient lookup
needs a row per recipient no matter where it lives. Given that, the row belongs
in the recipient's own partition, where the query is already partitioned by the
access dimension, ordered newest-first by the sort key, and paginable with no
filter.

Three properties hold this together:

- **The fan-out row is a pointer.** It carries no title or content type. Share
  rows denormalize those, so copying them per recipient would multiply every
  staleness bug by the size of the allowlist and make a rename cost one write per
  recipient instead of one per share.
- **The read never trusts the pointer.** Each row is resolved through the share
  lookup row and re-checked against `_check_share_access`, so a stranded pointer
  lists nothing and grants nothing. That is what makes the fan-out safe to write
  best-effort, outside the share's two-row transaction — which in turn is what
  keeps the allowlist from being capped at ~40 by `TransactWriteItems`' 100-item
  limit.
- **Fan-out is discovery, never authorization.** A row that failed to write costs
  a recipient a listing, not their access; the link still works and the ACL check
  is unchanged.

Addresses are folded to lower case for the partition key. Share rows store them
exactly as typed and lowercase only at compare time, so skipping that fold
returns an empty inbox to the person a share was addressed to — a wrong answer
that looks exactly like "nobody has shared anything with you".

Only `specific` shares appear. `public` means "any authenticated tenant user",
which has no recipient list to fan out to, so public shares stay link-delivered.

The read path uses per-item `GetItem`, never `BatchGetItem`, for exactly the
reason the delete cascade avoids `BatchWriteItem` — see the note under
[Lifecycle](#lifecycle).

## Lifecycle

Artifacts outlive the turn that produced them, but not the conversation.
Deleting a conversation revokes the share links for every artifact it produced,
as a best-effort background task: a failure leaves an orphan row, never a blocked
delete, and the lookup row is always deleted before the owner row so a
half-finished cleanup can never leave a live link its owner can no longer see.

One implementation note that is easy to get wrong: the cleanup deletes rows with
individual `DeleteItem` calls rather than a batch write. `BatchWriteItem` is its
own IAM action and is **not** authorized by the underlying item permissions the
way `TransactWriteItems` is, so a batch write fails closed with `AccessDenied` in
a deployed environment while passing every local test.

Deleting the artifact **content** on conversation delete is deliberately a
separate question — that is a retention decision about artifacts as a whole,
not about sharing.

## Enabling the feature

Artifacts are enabled by the presence of `ARTIFACTS_RENDER_TOKEN_SECRET_ARN`,
which infrastructure sets only when the artifacts stack is deployed for the
environment. The sharing routes ride the same signal: a share is only ever
consumed by minting a render token, so it cannot be useful without artifacts
being on.

The "Shared with you" inbox has a flag of its own: `ARTIFACT_SHARE_INBOX_ENABLED`
(CDK: `CDK_ARTIFACT_SHARE_INBOX_ENABLED`). Like the other flags in the platform
it is **default on with a kill switch** — only the literal `"false"` disables it,
and an unset variable resolves to on. While off, `GET /shared-artifacts` 404s and
the SPA renders the library without tabs.

It shipped default-off and opt-in in 1.18.0, because the surface landed ahead of
the product decision about it. That decision was made and the inbox went live, so
the default flipped: a deployment that never sets the variable should get the
finished feature rather than silently lose it.

The flag gates the **read only**. Fan-out rows are written by every share
regardless of it. That asymmetry is deliberate: if the writes were gated too,
turning the flag on would reveal an inbox missing every share created while it
was off — a wrong answer rather than an empty one, and one nobody could see was
wrong. Writing the rows regardless makes the toggle complete and instant in
either direction, with no backfill to sequence.

## Artifacts inside a shared conversation

Sharing a conversation shares the artifacts it produced. A recipient sees the
artifact cards where the owner sees them, anchored under the same turns.

The mechanism is worth knowing before changing it: **the conversation share is
the grant.** No artifact share records are created for this. Instead
`create_share` pins the session's artifacts — at the version each stood at right
then — into the conversation's snapshot body, next to the messages.

That does two jobs at once. It keeps the point-in-time promise the snapshot
already makes, so a recipient reading a frozen conversation is not shown an
artifact the transcript around it never describes. And it makes the snapshot the
**allowlist**: `resolve_shared_artifact` will only serve an `(artifact, version)`
pair that appears there, which is what stops a recipient naming an arbitrary
artifact belonging to the same owner. That check matters because the minted
token's `sub` is a partition address rather than an identity — see
[How access is actually enforced](#how-access-is-actually-enforced).

The payoff is that access has one source of truth. Narrow a conversation's
allowlist and its artifacts lock down in the same write; revoke the share and
they go with it. Provisioning parallel artifact shares would instead need a
cascade on every one of those paths, and a missed cascade leaves an artifact
readable after its conversation was locked down.

Shares created before this landed carry no artifact list and read as an empty
one. There is no migration.

## Historical note: how this used to be broken

Until the section above shipped, sharing a *conversation* did not share the
artifacts it produced. A recipient saw nothing where the owner saw artifact
cards — silently, with no placeholder — because artifact hydration goes through
`GET /artifacts?session_id=…`, which filters HEAD rows by the requesting user.

It stayed open as long as it did because the fix needed a consent decision
("should sharing a conversation also share its artifacts?") rather than only
wiring. The answer was yes.
