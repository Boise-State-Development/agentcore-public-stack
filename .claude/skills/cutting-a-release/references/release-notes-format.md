# `RELEASE_NOTES.md` — structure & spotlight template

Narrative, benefit-first release notes for product owners, operators, and the
developers who deploy this system. Detailed and technical, but always lead with
the outcome. The new release goes at the **top**; never modify previous entries.

> **Patch releases use the short form.** The full section order and feature-
> spotlight template below describe a **feature/minor or major** release. For a
> patch, keep only the **Header**, a short **Highlights** paragraph, the relevant
> **per-category bullets**, and **Deployment notes** — omit feature spotlights,
> per-layer subsections, and the test-coverage section.

## Header

```markdown
# Release Notes — vX.Y.Z

**Release Date:** <Month Day, Year>
**Previous Release:** vX.Y.(Z-1) (<date>)

---
```

## Section order (feature / minor / major)

1. **Highlights** — 3–5 sentence standalone summary. Someone reading only this
   paragraph should grasp the release theme, the 2–3 biggest features, and whether
   any action is required.
2. **Feature spotlights** — one `##` per major feature, with backend / frontend /
   infrastructure / test-coverage subsections. Narrative depth belongs here.
3. **🐛 Bug fixes** — lead with the user-visible symptom, then the root cause.
4. **🔒 Security** — CVEs, CodeQL findings, auth hardening.
5. **⚡ Performance** — measurable improvements only.
6. **⚠️ Breaking changes** — migration steps required. Omit if none.
7. **🏗️ Infrastructure** — new resources, SSM params, IAM changes operators must know.
8. **🔧 CI/CD** — workflow/pipeline changes, GitHub Actions upgrade table.
9. **📦 Dependencies** — markdown table with From/To columns, grouped by component.
10. **🧪 Test coverage** — line counts + scope for notable additions (optional).
11. **🚀 Deployment notes** — what operators must do differently. Always include,
    even if the answer is "no special steps."

## Feature spotlight template

```markdown
## <Feature Name>

<1–2 sentence user-facing summary: what changed and why it matters.>

### Backend
- <file / module> — <what changed>

### Frontend
- <component> — <what changed>

### Infrastructure
- <CDK stack / resource> — <what changed>

### Test Coverage
<N>+ lines of new tests covering <scope>.
```

## Writing style

- Match the tone and depth of the existing entries — written for developers who
  deploy and maintain this system.
- Every feature section explains **what** changed, **why** it matters, and **how**
  it works at a technical level.
- Use specific file names, endpoint paths, and class names.
- Include line counts for large test additions (e.g. "4,200+ lines of new tests").
- Dependency upgrades use a markdown table with From/To columns.
- Lead with the user/operator outcome; follow with the mechanism.

## Translating technical → user-benefit

| Engineering commit | Release-note framing |
|---|---|
| "Implemented caching layer on tool catalog" | "Tool admin changes now propagate to chat on the next turn (previously required a restart). Backed by a TTL-cached DynamoDB snapshot." |
| "Fixed null pointer in session metadata write" | "Resolved an issue where sessions could accumulate duplicate sidebar entries." |
| "Added OAuth2 USER_FEDERATION flow" | "Users can now connect external MCP tools (Google, Microsoft, GitHub, Canvas) with one-click consent directly from chat." |
| "Refactored OAuth extractor" | *(exclude — no user impact)* |

## Pitfalls

- **Don't modify previous entries** — only prepend the new one.
- **Don't use `{#custom-anchor}` heading suffixes** — GitHub renders them as
  literal text. Prefer plain text over fragile intra-doc anchors.
- **Don't invent PR numbers** — omit if unconfirmed.
- **Don't duplicate narrative across categories** — a feature spanning
  backend+frontend+infra stays in one spotlight with subsections.
