---
title: User Menu Links
description: Add external links or in-app rich-text pages to every user's menu.
sidebar:
  order: 11
---

Customization group. **User Menu Links** adds entries to the user menu, the
menu at the bottom of the sidebar. Use it for
things like a privacy policy, an acceptable-use page or a help desk link.
Managed under `/admin/manage-user-menu-links`, scope `admin.user_menu_links`.

User menu links have no feature flag. They are stored in their own DynamoDB
table; if `DYNAMODB_USER_MENU_LINKS_TABLE_NAME` is not set, the repository is
disabled and the menu shows no custom links.

## Link types

| Type | Shown in the form as | What a user gets |
|---|---|---|
| `external` | External URL (new tab) | A menu item that opens the URL in a new tab (`rel="noopener noreferrer"`) |
| `modal` | In-app modal (rich text) | A menu item that opens a dialog titled with the link's label, rendering its Markdown body |

The menu item's icon is set by the type: an "open in new window" arrow for an
external link, a document icon for a modal. It is not configurable.

## Fields

| Field | Required | Limits | Notes |
|---|---|---|---|
| Label | Yes | 1 to 64 characters | The menu item's text, and the modal's title |
| Type | Yes | `external` or `modal`; the form defaults to `external` | |
| Order | No | 0 to 10,000; defaults to 0 | Lower numbers appear first |
| Visible to users | No | Defaults to on (`enabled: true`) | Off hides the link without deleting it |
| URL | For `external` | Up to 2,048 characters; must start with `http://` or `https://` | |
| Body | For `modal` | Up to 50,000 characters of Markdown | The form shows a live preview |

The modal body is rendered with the same Markdown renderer as assistant
messages: headings, lists, links, code and emphasis. Links in it open in a new
tab.

## Validation

The backend enforces the rules, not only the form:

- An `external` link must have a URL, and a `modal` link must have a body.
- The URL must start with `http://` or `https://`. Anything else, such as a
  `javascript:` URL, is rejected even when the API is called directly.
- On a partial update the rules are checked again after the changes are merged
  into the stored link, so a change of type cannot leave a link without the
  field it needs.

A request that breaks a rule is rejected and nothing is saved.

## Where links appear

Enabled links appear in the user menu for **every signed-in user**, in their
own section below the built-in items (Admin Dashboard for admins, Settings,
What's New and keyboard shortcuts) and above Logout. There is no per-role visibility: a link
is either visible to everyone or hidden.

Links are sorted by **Order**, then by label (case-insensitive) for links with
the same order.

The SPA loads the list once per page load. An admin's own menu refreshes after
each create, update or delete; other users see a change the next time they load
the app.

Changes to user menu links are not recorded in the
[audit log](/agentcore-public-stack/admin/overview/#audit-log). The service
records who created each link (`created_by`) and when it was created and last
updated.

## Routes

| Route | Purpose |
|---|---|
| `GET /admin/user-menu-links/?enabled_only=` | Every link, enabled and hidden |
| `GET /admin/user-menu-links/{link_id}` | One link |
| `POST /admin/user-menu-links/` | Create a link |
| `PATCH /admin/user-menu-links/{link_id}` | Partial update |
| `DELETE /admin/user-menu-links/{link_id}` | Delete a link |
| `GET /user-menu-links/` | Any signed-in user: enabled links, in display order |

All admin routes require `admin.user_menu_links`, which a system admin can
delegate to a role. See [Admin Overview](/agentcore-public-stack/admin/overview/#admin-scopes).

## Example

An external link to a policy page:

```json
{
  "label": "Privacy policy",
  "kind": "external",
  "order": 10,
  "enabled": true,
  "url": "https://example.com/privacy"
}
```

A modal with short usage guidance:

```json
{
  "label": "Acceptable use",
  "kind": "modal",
  "order": 20,
  "body_markdown": "## Acceptable use\n\nDo not paste passwords or other credentials into a conversation."
}
```
