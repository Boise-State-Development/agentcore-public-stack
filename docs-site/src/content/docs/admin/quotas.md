---
title: Quotas
description: Quota tiers, assignments, overrides, and quota requests and extensions.
sidebar:
  order: 3
---

Usage & Spend group. Quotas cap how much each user can spend on model calls in a
period (usually a month). Managed under `/admin/quota`, scope `admin.quota`.

## How a user's limit is decided

1. **Tier.** The first match wins: an active **override**, a direct user
   assignment, an AppRole assignment, a JWT role assignment, an email-domain
   assignment, then the default tier. The tier sets the period limit, the
   warning ladder (50%, 75%, the soft limit, 90%), the per-conversation notice
   share, and whether reaching the limit blocks or only warns.
2. **Extensions** (when quota requests are on, see below) are added on top:
   `effective limit = base limit + every active extension`. Warnings, the
   per-conversation notice and the block all run on the effective limit.

Enforcement is computed on every turn from live spend, so nothing has to be
"unblocked". Tier resolution is cached for five minutes per user, except when a
turn would be blocked: that turn re-reads the user's quota before refusing, so
an approval or a new override takes effect on the user's next message. The
Usage page's status read (`GET /costs/quota-status`) always reads fresh, so it
shows a decision as soon as the user's notification arrives.

## Overrides

An override **replaces** the user's tier for a date range: a custom limit, or
unlimited. Use it for "unlimited for this person". It leaves no boundary between
the central allowance and the extra, so it cannot be charged back; use an
extension for that.

## Quota requests and extensions

Off by default while in development: set `CDK_QUOTA_REQUESTS_ENABLED=true`
(app-api) and build the SPA with `features.quotaRequests: true`. Spec:
`docs/specs/quota-requests-and-extensions.md`.

Users ask from the composer's quota tab (from 80% used, and when blocked) or
from **Settings → Usage**, which also lists their requests and splits the quota
bar into the central allowance and each extension.

- **Requests.** A user asks for an amount per period, through a date, with a
  justification and, if the deployment collects one, a funding source
  (`POST /quota/requests`). One open request per user. An admin approves
  (creating an extension), asks for information (usually "which fund?") or
  denies with a note. Nobody can decide their own request or extend their own
  quota.
- **Extensions.** Additive and period-scoped: `$X` more in every period between
  two dates. Each is **centrally funded** or **charged back** to a funding
  source. A **standing** extension is a per-user chargeback arrangement: past
  the central allowance the user keeps going, charged to their fund, up to a
  per-period cap (or uncapped, which is never the default). Revoking ends an
  extension now; extensions are never deleted.
- **Policy** (`GET`/`PUT /admin/quota-requests/policy`): request presets and
  bounds, whether and how the request form asks for a funding source, the
  funding-field schema (for example fund / org / program, or a cost center),
  standing defaults, the fiscal year end, and who is notified of new requests.
- **Scope.** `admin.quota_requests` governs the request queue and extensions,
  separately from `admin.quota`, so approving requests can be delegated without
  the tier editor.
- **Audit and notifications.** Approvals, denials, information requests,
  extension grants and revokes, and refused self-decisions are audited. The
  requester is notified in-app of each decision.

| Route | Purpose |
|---|---|
| `GET /admin/quota-requests?status=` | The queue (pending by default) |
| `GET /admin/quota-requests/summary` | Pending and info-requested counts |
| `GET /admin/quota-requests/{id}` | The request with live usage, prior periods and history |
| `POST /admin/quota-requests/{id}/approve` / `request-info` / `deny` | Decide |
| `GET` / `POST /admin/quota-extensions` | List, or extend a user directly |
| `POST /admin/quota-extensions/{id}/revoke` | End one now |
| `GET` / `PUT /admin/quota-requests/policy` | The request policy |

The chargeback report (who consumed what against which fund, per month) is
being built and is not available yet.
