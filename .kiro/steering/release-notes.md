---
inclusion: fileMatch
fileMatchPattern: 'RELEASE_NOTES.md'
---

# Writing Release Notes

## Branch Model & Why This Is Hard

This repo uses a squash-merge workflow: `develop` accumulates feature branches via merge commits, and when a release is cut, `develop` is squash-merged into `main`. This means `main` and `develop` have **divergent git histories** — you cannot do a simple `git log main..develop` to get a clean diff. Commit SHAs on `main` don't correspond to anything on `develop`.

## How to Identify What Changed

### Step 1: Find the boundary

Look at the last squash-merge commit on `main` to determine when the previous release was cut:

```bash
git log main --oneline -5
```

Then find the corresponding release tag or date. Use that date as your boundary.

### Step 2: List commits on develop since the boundary

```bash
git log develop --oneline --no-merges --since="<date-of-last-release>" 
```

This gives you the raw commit list, but **do not rely solely on commit messages**. Dependabot commits are usually accurate, but human commits often have vague or incomplete messages.

### Step 3: Inspect the actual code changes

For every non-trivial commit, read the diff or at minimum the `--stat` output:

```bash
git show --stat <sha>
git show --no-patch <sha>   # full commit message
```

For feature commits, read the changed files to understand what was actually built — not just what the message claims. Look for:

- New API endpoints (routes files)
- New or modified models/schemas
- New frontend pages or components
- Infrastructure changes (CDK stacks, config)
- New test files (indicates new functionality)
- Dependency changes (pyproject.toml, package.json)

### Step 4: Group by category

Organize changes into the standard sections used by prior releases. Review the existing release notes in the file for the established pattern. Typical sections include:

- **Highlights** — 2-3 sentence summary of the release theme
- **New features** — each gets its own H2 with subsections for backend/frontend/infra
- **Bug fixes** — concise list
- **Security** — vulnerability patches, CodeQL fixes
- **Dependency upgrades** — table format
- **CI/CD improvements** — workflow changes
- **Test fixes** — test-only changes
- **Deployment notes** — what operators need to do differently

## Writing Style

- Match the tone and depth of the existing release notes in the file. They are detailed and technical — written for developers who will deploy and maintain this system.
- Every feature section should explain **what** changed, **why** it matters, and **how** it works at a technical level.
- Use specific file names, endpoint paths, and class names when relevant.
- Include line counts for large test additions (e.g., "4,200+ lines of new tests").
- For dependency upgrades, use a markdown table with From/To columns.
- The Highlights section should read as a standalone summary — someone skimming only that paragraph should understand the release.

## Header Format

```markdown
# Release Notes — v1.0.0-beta.XX

**Release Date:** <Month Day, Year>
**Previous Release:** v1.0.0-beta.XX-1 (<date>)

---
```

The new release goes at the **top** of the file. Do not modify previous release sections.

## Common Pitfalls

- **Don't trust commit messages blindly.** A commit titled "fix: update models" might contain a new feature with 800 lines of code. Always check the diff.
- **Don't miss Dependabot PRs.** They often bump 10+ packages in a single grouped PR. Check `pyproject.toml`, `package.json`, and workflow files for version changes.
- **Don't forget CI/CD changes.** Workflow file modifications (`.github/workflows/`) are easy to overlook but important for operators.
- **Don't duplicate sections.** If a feature spans backend + frontend + infra, keep it in one section with subsections — don't scatter it across the document.
- **Check the VERSION file and README badge.** These should already be updated via `sync-version.sh` before the release notes are finalized.
