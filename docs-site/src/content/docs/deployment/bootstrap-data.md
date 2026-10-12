---
title: Bootstrap Data Seeding
description: One-time post-deploy data seeding.
sidebar:
  order: 5
---

`bootstrap-data-seeding.yml` is a one-time, post-deploy step that seeds the
default configuration the application needs to be usable. You run it once after
the first full deploy, and again only when the default catalog changes.

## What gets seeded

The workflow runs `scripts/stack-bootstrap/seed.sh`, which resolves the target
table names from SSM and runs `backend/scripts/seed_bootstrap_data.py` to upsert:

- **Default models** — the AI models available out of the box.
- **RBAC roles** — the baseline role definitions.
- **Quota tiers** — default usage limits.
- **Tool catalog** — the built-in tool entries.
- **Skills** — two admin-catalog skills. *Web Research Assistant* is granted to the
  `default` role. *Skill Creator* (helps users write their own skills, adapted from
  Anthropic's Apache-2.0 `skill-creator`) is granted to **no role**: grant it to a pilot
  role under **Admin → Roles** when you are ready. It saves skills directly for users
  who also have the Skill Authoring tool (`SKILL_AUTHORING_ENABLED`), and hands everyone
  else a draft to add under Customize → Skills.

## Safe to re-run

Seeding is **idempotent** — re-runs upsert rather than duplicate, so it's safe to
run again any time the default catalog changes (a new default tool, model, or
role). Re-running won't disturb data an admin has since customized through the
dashboard.

## Authentication is not seeded

There's no auth-provider seeding. Cognito's **first-boot flow** handles initial
access: the first person to open the application creates the admin account
directly, and that account automatically becomes the system admin. Federated
identity providers (Entra ID, Okta, Google) are added later from the admin
dashboard — no redeploy or seed required.

## When to re-run

| You changed | Re-run |
|-------------|--------|
| Default tools, models, roles, or seeded skills | `bootstrap-data-seeding.yml` |

Routine application and infrastructure changes don't need a re-seed.
