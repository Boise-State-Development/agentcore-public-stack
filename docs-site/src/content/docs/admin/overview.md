---
title: Admin Overview
description: Who can open the admin console at /admin, how delegated admin scopes work, and what each page in the admin navigation is for.
sidebar:
  order: 1
---

The admin console lives at `/admin` in the SPA. It is where administrators
manage spend and quotas, the model and tool catalogs, the Agent Marketplace,
roles and identity providers, and a few pieces of the user-facing UI.

Admin access is not all-or-nothing. A **system admin** sees every page. Other
admins hold one or more **admin scopes**, each of which opens one area of the
console and the matching backend routes.

## Who is an admin

| Kind | How they get it | What they see |
|---|---|---|
| System admin | Holds the `system_admin` AppRole | Every page and every admin route |
| Delegated admin | Holds an AppRole that grants one or more admin scopes | Only the pages their scopes open |

Both kinds see **Admin Dashboard** in the user menu, and both pass the
console's entry guard. A user with neither is sent back to the home page.

On the backend:

- `require_admin` is `require_app_roles("system_admin")`: full admins only.
- `require_admin_scope("admin.<area>")` guards one delegated area. It passes a
  `system_admin` (who satisfies every scope implicitly) or anyone whose roles
  grant that scope. It fails closed: if permission resolution raises, the
  request is denied with a 403.

Every admin route declares exactly one scope, and
`tests/architecture/test_admin_scope_coverage.py` enforces that. The SPA checks
the same scope on each page (`adminScopeGuard`), but that is a convenience so a
delegated admin never clicks into a 403. The server is the boundary.

## Admin scopes

Scopes are a closed list defined in code
(`backend/src/apis/shared/rbac/admin_scopes.py`), never free text. A system
admin grants them on a role's form in [Roles](/agentcore-public-stack/admin/roles/),
and they are stored on the role as `granted_admin_scopes`. There is no
wildcard: full admin is spelled `system_admin`.

| Scope | Group | Opens |
|---|---|---|
| `admin.costs` | Usage & Spend | Cost Analytics and Feedback |
| `admin.quota` | Usage & Spend | Quotas (tiers, assignments, overrides, events) |
| `admin.quota_requests` | Usage & Spend | Quota Requests (the request queue and extensions) |
| `admin.aws_limits` | Usage & Spend | AWS Limits |
| `admin.fine_tuning` | Usage & Spend | Fine-Tuning (access, jobs and costs) |
| `admin.models` | AI Configuration | Models, including the Bedrock, Gemini and OpenAI model listings |
| `admin.tools` | AI Configuration | Tools |
| `admin.skills` | AI Configuration | Skills |
| `admin.agent_templates` | AI Configuration | Agent Templates |
| `admin.connectors` | AI Configuration | Connectors (OAuth providers for external MCP tools) |
| `admin.file_sources` | AI Configuration | No console page; `GET /admin/file-source-adapters` |
| `admin.export_targets` | AI Configuration | No console page; `GET /admin/export-target-adapters` |
| `admin.marketplace` | Agent Marketplace | Every Agent Marketplace page |
| `admin.projects` | Agent Marketplace | No console page; the `/admin/projects` routes (see [Projects](/agentcore-public-stack/admin/projects/)) |
| `admin.users` | Identity & Access | Users |
| `admin.system_prompts` | Customization | Conversation Modes |
| `admin.user_menu_links` | Customization | User Menu Links |
| `admin.announcements` | Customization | Announcements |

### Scopes that cannot be delegated

Four scopes exist in the registry but can never be granted to a role. Role
validation (`validate_admin_scopes`) rejects them, so they stay with
`system_admin`:

| Scope | Why it stays with system admins |
|---|---|
| `admin.roles` | Whoever can edit a role can grant themselves anything |
| `admin.auth_providers` | Identity provider claim mapping decides which roles resolve, so it is role administration by another route |
| `admin.user_grants` | A direct grant to one user is role editing by another route |
| `admin.audit` | The audit log records what admins do, including refused escalation attempts |

## The admin layout

Every admin route sits under the parent `/admin` route, which declares
`data.chrome = 'admin'`. The app shell reads that flag and makes two changes:

- **The sidebar body becomes the admin navigation.** The chat sidebar's
  conversation list is replaced by the admin nav. The sidebar keeps its frame
  (logo, collapse control, user menu), and **Back to Chat** at the top leaves the
  console.
- **The content area loses its width limit**, so wide pages (cost tables, the
  model catalog, the role matrix) use the full width.

On small screens the admin layout has its own button to open the nav.

The nav only lists entries the current admin's scopes open, and drops a group
whose entries are all hidden. Opening `/admin` itself redirects to the first
page in a fixed landing list, in navigation order, that the admin can open. For
a system admin that is Cost Analytics.

## Navigation

| Group | Entry | Route | What it is for | Docs |
|---|---|---|---|---|
| Usage & Spend | Cost Analytics | `/admin/costs` | Spend dashboards, per-session call anatomy, exports | [Cost Analytics](/agentcore-public-stack/admin/cost-analytics/) |
| Usage & Spend | Feedback | `/admin/feedback` | Where users pressed thumbs down, split by configuration arm (scope `admin.costs`) | |
| Usage & Spend | Quotas | `/admin/quota` | Quota tiers, assignments, overrides and quota events | [Quotas](/agentcore-public-stack/admin/quotas/) |
| Usage & Spend | Quota Requests | `/admin/quota-requests` | Decide user requests for more quota and manage extensions. Badged with the pending count. **Only when `features.quotaRequests` is on** | [Quotas](/agentcore-public-stack/admin/quotas/) |
| Usage & Spend | Fine-Tuning | `/admin/fine-tuning` | Fine-tuning access, jobs and costs | [Fine-Tuning](/agentcore-public-stack/admin/fine-tuning/) |
| Usage & Spend | AWS Limits | `/admin/aws-limits` | The AWS account's Service Quotas for this stack, and increase requests. **Only when `features.awsLimits` is on** | [AWS Limits](/agentcore-public-stack/admin/aws-limits/) |
| AI Configuration | Models | `/admin/manage-models` | The managed-model catalog and provider model listings | [Models](/agentcore-public-stack/admin/models/) |
| AI Configuration | Tools | `/admin/tools` | The tool catalog and which roles may use each tool | [Tools](/agentcore-public-stack/admin/tools/) |
| AI Configuration | Skills | `/admin/skills` | Skill bundles, their reference files and role grants | |
| AI Configuration | Agent Templates | `/admin/agent-templates` | The templates users start new agents from | |
| AI Configuration | Connectors | `/admin/connectors` | OAuth providers used by external MCP tools | [Connectors](/agentcore-public-stack/admin/connectors/) |
| Agent Marketplace | Review Queue | `/admin/marketplace/review` | Agent submissions waiting for review. Badged with the pending count | |
| Agent Marketplace | Reports | `/admin/marketplace/reports` | User reports against listings. Badged with the open count | |
| Agent Marketplace | Listings | `/admin/marketplace/listings` | Every agent submitted to the store, and takedowns | |
| Agent Marketplace | Store Front | `/admin/marketplace/store-front` | Curate the storefront | |
| Agent Marketplace | Categories | `/admin/marketplace/categories` | Marketplace categories | |
| Agent Marketplace | Publishers | `/admin/marketplace/publishers` | Who a listing is attributed to in Discover | |
| Agent Marketplace | Default Pins | `/admin/marketplace/default-pins` | Agents every member of a role starts with | |
| Identity & Access | Users | `/admin/users` | Browse and search the user directory | [Users](/agentcore-public-stack/admin/users/) |
| Identity & Access | Roles | `/admin/roles` | AppRoles, their grants and admin scopes (system admin only) | [Roles](/agentcore-public-stack/admin/roles/) |
| Identity & Access | Auth Providers | `/admin/auth-providers` | Identity providers and claim mapping (system admin only) | [Auth Providers](/agentcore-public-stack/admin/auth-providers/) |
| Identity & Access | Audit Log | `/admin/audit` | The administrative audit trail (system admin only) | See below |
| Customization | Announcements | `/admin/manage-announcements` | Author and publish feature announcements shown to all users | |
| Customization | User Menu Links | `/admin/manage-user-menu-links` | Extra links in every user's menu | [User Menu Links](/agentcore-public-stack/admin/user-menu-links/) |
| Customization | Conversation Modes | `/admin/system-prompts` | Admin-authored system prompts users can apply to a conversation | [Conversation Modes](/agentcore-public-stack/admin/conversation-modes/) |

The two flagged entries are compile-time SPA flags, off by default and in the
production build. Their routes use `canMatch`, so in a build with the flag off
the page does not exist. Each also has a backend flag that must be on: see the
linked pages.

Some entries are shown without an SPA flag but depend on a backend switch. When
the switch is off, the page loads and its API returns 404:

| Entry | Backend switch |
|---|---|
| Skills | `SKILLS_ENABLED` (default on; `=false` unmounts the admin API) |
| Announcements | `ANNOUNCEMENTS_ENABLED` (default on; `=false` unmounts the admin API) |
| Agent Marketplace pages | `AGENT_MARKETPLACE_ENABLED` (default on; `=false` makes the routes 404) |
| Fine-Tuning | `FINE_TUNING_ENABLED` (the admin API is mounted only when it is `true`) |

## Audit log

**Audit Log** (`/admin/audit`) is the durable record of administrative changes:
who did what, to which target, when, and whether it was allowed. Each record
carries the actor, the target, the outcome (`allowed` or `denied`), the fields
that changed and their before and after values, and a reason on a denied
attempt.

It records a closed set of actions:

| Area | Actions |
|---|---|
| Roles | Created, updated, deleted, synced, and mutations the guard refused (`app_role.mutation_denied`) |
| Shared Projects | Lifecycle, membership, instructions, model, tools, skills, knowledge, shared tasks and outputs, memory, schedules |
| Direct user grants | `user_grant.updated`, `user_grant.deleted` |
| Quota requests and extensions | Approvals, denials, information requests, refused self-decisions, extension grants and revokes, and re-closed chargeback periods |
| AWS Limits | `aws_limit.increase_requested` |

Edits to the other catalogs (models, tools, conversation modes, user menu links
and so on) are not in this set.

The log is read-only. The console browses it one month at a time, newest first,
and the API also reads one target's or one admin's full history:

| Route | Purpose |
|---|---|
| `GET /admin/audit/?month=YYYY-MM` | Recent activity for a month (defaults to the current month), paginated |
| `GET /admin/audit/actions` | The list of audited action ids |
| `GET /admin/audit/targets/{target_id}?target_type=` | Everything that happened to one target (defaults to a role) |
| `GET /admin/audit/actors/{actor_user_id}` | Everything one admin did |

There is no edit or delete endpoint. Records expire after 365 days through the
table's TTL. All four routes use `require_admin`, so only system admins can
read the log.

## Admin API routes

Admin backend routes live in App API under `/admin/<domain>/`, one package per
area in `backend/src/apis/app_api/admin/<domain>/`, each guarded by its scope.
User-facing routes for the same data sit under `/<domain>/`: for example,
`/admin/system-prompts` is the admin catalog and `/system-prompts` is the
user-facing list. Like every App API route, admin routes authenticate from the
BFF session cookie, not a bearer token.
