# Organizing the Agents and Projects pages

**Status:** PR-1 (backend storage and routes) merged in #2363; PR-2 (Hide, the Hidden filter, My agents filters) in review. Spec written 2026-11-23 against `develop` @ `b510150b1`.

**Flag:** `ORGANIZE_LISTS_ENABLED` / `CDK_ORGANIZE_LISTS_ENABLED`, app-api only, `features.organizeLists`.
In development: off by default and in prod, on in the `development` GitHub environment and
`environment.development.ts` once PR-1 and PR-2 have landed (§9).

## 1. Problem

People get invited to Shared Projects and shared Agents they did not choose, and the two list pages
show everything they can reach, in an order they cannot change. A member of five projects and a dozen
shared Agents has no way to put the three they use every day first, or to get the ones they were added
to "just in case" out of the way, short of giving up access.

The ask, in the user's words: hide some of what is shared, and reorder by drag and drop.

## 2. What exists today

| Surface | Where | What a user can do now |
|---|---|---|
| My agents (`/agents`) | `agents/agents.page.ts` | Grid or list view (per device, `LocalSettingsService`). Owned Agents first, then shared-with-me, each most-recent first (`list_agents_endpoint`). No filters. Pin to sidebar. A recipient cannot remove themselves from a share. |
| Pinned (`/agents/pinned`) | `agents/pinned/pinned.page.ts` | The effective pin list (own + role-seeded). Unpin. No reorder. |
| Projects (`/projects`) | `projects/projects.page.ts` | Filter chips All / Mine / Shared with me / Archived, client-side over one `GET /projects`. **Leave** (`DELETE /projects/{id}/members/me`), which drops access. Owners can archive. |
| Sidebar | `session-list.ts`, `SidebarLayoutService` | Pinned Agents above pinned conversations; drag a row onto Pinned to pin it, off to unpin. The **Edit sidebar** dialog shows, hides and reorders navigation entries by drag and by keyboard. |

Two pieces are already half of this design:

- **Pins carry an `order`.** `PINNED_AGENTS` on the user-settings table stores
  `{ pinned: [{ agentId, order, pinnedAt }], dismissed: [...] }`. `add_pin` only ever appends
  (`max(order) + 1`), and no route writes `order` otherwise. The effective list is sorted
  `(locked desc, role priority desc, order asc, name asc)` (`pin_service.py`).
- **The Edit sidebar dialog is the interaction to copy** (`edit-sidebar-dialog.component.ts`): CDK
  drag with a keyboard move on the handle (CDK drag is pointer-only), changes applied on screen at
  once, persisted in one write when the dialog closes.

## 3. Goals and non-goals

**Goals**
- A member can take a shared Agent or a project out of their list without losing access, and get it
  back.
- A member can put the items they use first, by drag or by keyboard.
- A newly shared item is noticed, not buried at the bottom of a custom order.
- The Pinned order is one order: the Pinned page and the sidebar agree.

**Non-goals**
- Folders, tags or groups. Revisit only if Hide and order are not enough (§10, Q3).
- Removing yourself from an Agent share. That is a real gap, but it is an access change with its own
  audit and owner-visibility questions; Hide covers the curation need without it.
- Muting a project's notifications. Hide is a view choice; the inbox keeps working.
- Anything the model sees. No change reaches the turn path (§8).

## 4. Decisions

**D1. Hide is personal and reversible, and is not Leave.** Hiding changes the hider's own list and
nothing else: no access change, no notice to the owner or other members, no audit row. Leave stays
where it is (the project's breadcrumb menu) for a member who wants out.

**D2. Hidden items stay reachable.** A hidden Agent still opens from a link, still runs, and still
answers to `@` when the user types its name. A hidden project's conversations stay in the sidebar's
time buckets, and its inbox entries still arrive. Hide removes the row from the list page and nothing
else.

**D3. A preference is never pruned because access changed.** The rule pins already follow
(`pin_service.py`, rule 1): ids the caller can no longer reach are ignored when the list renders, and
kept in storage. An Agent that goes private for an afternoon and comes back keeps its place and its
hidden state.

**D4. Custom order is a mode, not drag on the live grid.** Cards and rows are click-to-open with up to
five action buttons; free drag on them misfires, and it is worse on touch. An **Arrange** button
switches the page into an arranging list (§6.3). The default view keeps today's behaviour.

**D5. Three sorts: Recently added, Name, Custom.** Recently added is today's order and stays the
default. Custom uses the saved order and is selected automatically the first time a user saves an
arrangement. The sort is part of the saved arrangement, so it follows the user across devices (unlike
grid/list, which is density and stays per device).

**D6. New arrivals go first.** An item that is in neither the saved order nor the hidden set is *new*.
Under Custom, new items render above the ordered ones, most recent first, with a **New** badge. The
next arrangement write (Arrange → Done, or any Hide) folds them into the order where they sit, which
clears the badge. A user who has never saved an arrangement sees no badges: with no arrangement,
everything would be "new".

**D7. The Pinned order is the pin list's `order`.** Reordering on the Pinned page (and, in PR-4, inside
the sidebar's Pinned group) writes `PinnedAgentRef.order` through a new route. There is no second order
for pins. Locked role pins (D9.4 of the marketplace spec) stay first and are not draggable; their slot
is the server's sort, not the user's. Dragging an unlocked role-seeded pin writes an own ref for it,
which is exactly what pinning it by hand does today (rule 4: the seed is converted to the user's own
pin and survives the role pin's removal).

**D8. Hiding a pinned Agent unpins it.** Otherwise the sidebar would show an Agent the user hid from
the page. The SPA calls the existing `DELETE /agents/{id}/pin` before writing the arrangement, so the
dismissal tombstone is written as for any unpin. A **locked** pin cannot be hidden: the row offers no
Hide, since its Pin slot already shows the lock. Undo after hiding a pinned Agent re-pins it.

**D9. Anything in the list can be hidden, owned items included.** One rule is easier to explain than
"shared only", and an author with a pile of abandoned drafts has the same problem. Owners still have
Archive (projects) and Delete; Hide does not replace either.

## 5. Data model

One item per user per surface on the **user-settings table**, beside `PINNED_AGENTS`:

```
PK = USER#{user_id}
SK = "ARRANGEMENT#agents" | "ARRANGEMENT#projects"
{
  sort:      "recent" | "name" | "custom",
  order:     [id, ...],        // custom order, first is top
  hidden:    [id, ...],
  updatedAt: ISO-8601
}
```

**Why not a field on the `SETTINGS` item, like `sidebarItems`?** The turn path reads that item on
every turn (`_load_user_settings` in `inference_api/chat/routes.py`, for personal instructions and
memory mode). `sidebarItems` is a dozen short ids; an arrangement can be hundreds of ids, and every
byte of it would be read before every first token for no reason. A separate item is read only by the
two list pages.

**Caps.** `order` and `hidden` at most 500 ids each, ids validated against the surface's id pattern.
Over the cap the route refuses with 422. When the SPA hits it, it drops ids that are not in the
current list (D3's kept-but-unreachable ids are the only ones that can go), oldest first.

**Concurrency.** A `PUT` replaces the item. Two tabs saving at once: last write wins, the same
trade-off `PINNED_AGENTS` makes. The SPA always sends the full arrangement it was given plus its own
edits, so unreachable ids survive a write (D3).

## 6. SPA

### 6.1 Shared pieces

- `ListArrangementService` (one per surface, `providedIn: 'root'`): loads the arrangement once per
  session, exposes `sort`, `orderIndex` (a `Map<id, number>`), `hiddenIds` (a `Set`), and
  `arrange(items)`, a pure function from the loaded list to `{ visible, hidden, newIds }`. Writes are
  optimistic and roll back on failure, the `AgentPinService` pattern.
- A load failure leaves the page in today's order with nothing hidden. An arrangement is an
  enhancement; the page must never fail because it did not load.
- `arrange()` lives in one tested function so the two pages cannot drift.

### 6.2 Filters

- **Projects:** add **Hidden (n)** after Archived. Hidden shows hidden, non-archived projects; Archived
  shows every archived project, hidden or not, with a small "Hidden" label on the hidden ones. All,
  Mine and Shared with me leave hidden projects out.
- **My agents:** add the same chips, All / Mine / Shared with me / Hidden, from `isSharedWithMe`. The
  chips run client-side over the one list, as Projects does.
- A **Hide** action joins each row's actions (an eye-slash icon, "Hide from this list"); in the Hidden
  filter it reads **Unhide**. Hiding shows an info toast with **Undo**.

### 6.3 Arrange mode

- **Arrange** sits next to the sort control. It turns the page into a single-column list (whatever the
  grid/list choice) of the current filter's items, each with a drag handle, a keyboard move
  (the Edit sidebar dialog's handle behaviour: arrow keys move, focus follows the moved row), and a
  Hide checkbox. **Done** saves once; **Cancel** discards.
- Only All is arrangeable. Mine and Shared with me are subsets; reordering a subset would have to
  invent positions for the items it cannot see. Arrange from another filter switches to All first.
- While arranging, row clicks do nothing and action buttons are hidden, so a drag cannot open an item.
- On a phone the drag handle is the only drag target, and the keyboard/button move ("Move up",
  "Move down" in a row menu) is the reliable path.

### 6.4 Pinned order

- **PR-3:** the Pinned page gets the same Arrange mode over the pin list, writing the pin order (D7).
- **PR-4:** the sidebar's Pinned group becomes sortable within itself (`cdkDropListSortingDisabled`
  off for that list), writing the same route. Dropping onto and off Pinned keeps pinning and
  unpinning as today.

### 6.5 Accessibility

- The sort control is a `radiogroup`, as the grid/list toggle is.
- A move announces its result through a live region ("Moved Budget Review to position 2 of 9").
- The Hide checkbox's label names the item. Arrange and Done are real buttons; Escape cancels.

## 7. API

All under `get_current_user_from_session`, refused with 404 while the flag is off, auth checked first
so a signed-out caller gets 401 whether or not the flag is on.

| Route | Body / response | Notes |
|---|---|---|
| `GET /users/me/arrangements/{surface}` | `{ sort, order, hidden, updatedAt }` | `surface` is `agents` or `projects`; no item reads as `{ sort: "recent", order: [], hidden: [] }`. |
| `PUT /users/me/arrangements/{surface}` | same, minus `updatedAt` | Replaces the item. Caps and id patterns validated (§5). |
| `PUT /agents/pins/order` | `{ agentIds: [...] }` → `AgentPinsResponse` | Rewrites `order` for the listed ids as their positions; listed unlocked seeds become own refs (D7); locked ids are ignored; unlisted own refs keep their relative order after the listed ones. Gated like the other pin routes (`require_agents_enabled`), not by `ORGANIZE_LISTS_ENABLED`: it extends an existing feature (CLAUDE.md, "Not everything new needs a flag"). The SPA's Arrange on the Pinned page is still behind `features.organizeLists`. Ships with PR-3. |

New code: `apis/app_api/arrangements/` (routes) and `apis/shared/arrangements/` (model + repository),
mirroring `apis/shared/assistants/pins.py`. The pin-order write goes in `pins.py` beside `add_pin`.

## 8. Cost and the turn path

Nothing here reaches the system prompt, `toolConfig`, history, or any `Before*` hook. The arrangement
items are read only by the two list pages; §5 keeps them off the `SETTINGS` item precisely so the
turn path's settings read does not grow. No new AWS resources: two new SK values on an existing
table, no new index. No TTFT inventory change.

## 9. Delivery

| PR | Contents | Verified by |
|---|---|---|
| PR-1 | Backend: `apis/shared/arrangements`, the two routes, the flag (backend, CDK config + test, `platform.yml`), caps, D3 behaviour | Route tests incl. flag off → 404, unauthenticated → 401, caps → 422, unknown ids survive a PUT |
| PR-2 | SPA: `ListArrangementService`, `arrange()`, Hide / Unhide with Undo, the Hidden filter on both pages, Agents filter chips, D8 unpin-on-hide | `arrange()` unit tests (new, hidden, unreachable ids); page specs; axe; dev check with a second persona sharing into the first |
| PR-3 | SPA: sort control, Arrange mode on both pages and the Pinned page; backend `PUT /agents/pins/order` | Keyboard and pointer reorder specs; pin-order route tests incl. locked and seeded pins; dev check that the sidebar follows the Pinned page |
| PR-4 | SPA: reorder inside the sidebar's Pinned group | Session-list spec for sort-within vs pin/unpin drops |

Turn `features.organizeLists` and `CDK_ORGANIZE_LISTS_ENABLED` on for dev after PR-2. Promotion to a
default-on feature switch is its own change once PR-3 has run in dev.

## 10. Open questions

1. **Should Hide also drop the Agent out of `@`-mention suggestions?** D2 keeps it typeable. The menu
   lists "own Agents plus pins" (`AgentMentionService`), and a shared Agent counts as own there, so a
   hidden shared Agent still shows when the user types `@` with no letters. Recommendation: leave it
   out of the empty-query suggestions, keep it in name matches.
2. **Does New clear on open, as well as on the next arrangement write?** Clearing on open needs a
   write on navigation. Recommendation: no, keep D6 as written; revisit if users report badges lingering.
3. **Sections or folders on My agents** (Pinned / Mine / Shared, or user-named groups). Recommendation:
   wait for usage of Hide and Custom first.
4. **Self-removal from an Agent share** (§3 non-goal). Worth its own spec if Hide does not satisfy the
   people asking.
