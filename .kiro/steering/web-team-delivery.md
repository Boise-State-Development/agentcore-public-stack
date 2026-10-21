---
inclusion: always
---

# Web Team delivery workflow

Work is tracked on GitHub Project 7 in `Boise-State-Development`
(https://github.com/orgs/Boise-State-Development/projects/7). Playbook:
`Boise-State-Development/delivery-playbook`.

## Rules

- Every change starts from a GitHub issue in the repo being changed. If none
  exists, create one (Status Triage) before coding.
- Branch name: `feature/<issue#>-<short-slug>`, from `develop`.
- Pull request description includes `Closes #<issue>` (or
  `Closes Boise-State-Development/<repo>#<issue>`). That keyword moves the
  card: PR opened → In Review; merged → Awaiting Release; production deploy →
  Done.
- **Done means running in production**, not merged.
- Never write student IDs, employee IDs, personal names, usernames, or email
  addresses in issues, PRs, commits, code comments, or test fixtures. Refer to
  people by role or office. Summarize emails/tickets; never paste them.
- Out-of-scope problems you notice become new issues (Source = Team), not
  extra changes in the current PR.
- Confirm the AWS profile targets a non-production account before running
  anything against AWS. Production changes go through the repo's deploy
  pipeline.

## Board statuses

Triage → Backlog → Ready → In Progress → In Review → Awaiting Release → Done

## Fields

Priority (P0 Urgent … P3 Low) · Points (Fibonacci 1–34; split >13) · Sprint
(two weeks, Wed→Tue) · App · Source · Health (On track / At risk / Blocked —
always comment why) · Ticket (ServiceNow number) · Released in.

## Commands

```bash
# start: assign, move to In Progress, branch
gh issue edit <n> --add-assignee @me
gh issue develop <n> --base develop --name feature/<n>-<slug> --checkout
# board ids for item-edit: delivery-playbook/project/state.json
gh project item-list 7 --owner Boise-State-Development --format json --limit 1000
gh project item-edit --project-id PVT_kwDODgQDQ84BnamJ --id <item> --field-id <f> --single-select-option-id <o>
# file discovered work
gh api -X POST repos/Boise-State-Development/<repo>/issues -f title=... -f body=... -f type=Task
gh project item-add 7 --owner Boise-State-Development --url <issue-url>
# open PR
gh pr create --title "..." --body "Closes #<n> ..."
```
