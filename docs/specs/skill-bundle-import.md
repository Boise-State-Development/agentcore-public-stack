# Skill bundle import (zip or public GitHub path)

**Status:** in development behind `SKILL_BUNDLE_IMPORT_ENABLED` (off by default).
**Extends:** `skills-as-agent-primitive.md` §7 ("zip support still deferred"), which
this closes for **My Skills**. The admin catalog keeps its single-`SKILL.md` import.

## 1. Why

A team that keeps a skill in a GitHub repo (a contracts team's playbooks, edited
often) can today only import its `SKILL.md` into the form and then upload every
other file one at a time. Our storage is already an agentskills.io bundle
(`SKILL.md`, `references/`, `scripts/`, `assets/`, see `shared/skills/bundle.py`),
so a bundle maps onto what we store with a few rules (§4). This adds one way in
(a `.zip`, or a public GitHub URL) and one way to refresh from the same source.

## 2. Decisions

- **D1. Two sources, one pipeline.** A `.zip` the user uploads, or a public
  GitHub URL. Both become the same in-memory archive and go through the same
  parser, bounds and diff. No new dependency: `zipfile` (stdlib) reads both,
  because GitHub serves a repository as a zip archive.
- **D2. GitHub over codeload only.** We fetch
  `https://codeload.github.com/{owner}/{repo}/zip/{ref|HEAD}`, a URL we build
  from validated parts (§5), never one the user typed. One request, no API
  token, no `api.github.com` rate limit shared across our egress IP. The archive's
  zip comment is the commit SHA, which we record.
- **D3. Preview, then apply.** `preview` parses and diffs without writing; `apply`
  re-reads the source, recomputes the diff and writes. The preview returns a
  `bundleHash`; apply refuses with 409 if the source no longer hashes the same
  (a push landed in between) or the target skill's `updatedAt` moved (someone
  edited it), so nothing is written that the user did not see.
- **D4. The bundle is the source of truth on re-import.** A re-import replaces
  the description, instructions, `allowed-tools` and other frontmatter, writes
  added and changed files, leaves unchanged files untouched (no S3 write), and
  **deletes** files the bundle no longer has, including ones uploaded by hand.
  The preview lists each one as `removed` before anything happens. The display
  name is kept: a user may have renamed the skill, and the frontmatter `name` is
  a slug.
- **D5. Re-import never cuts a version.** It writes the live row only, exactly as
  the edit form does. Versions are cut on pin (`shared/skills/versions.py`); a
  project pinned to `skill@3` keeps running v3 and shows *Update available*
  until someone clicks Update (`POST /projects/{id}/skills/{skillId}/pin`).
  `importSource` is not a snapshot field, so it never changes a version's
  `content_hash`.
- **D6. Security bounds fail the whole import; content rules skip one file.**
  Traversal, absolute paths, encryption and oversize archives are refusals (a
  well-formed bundle never has them). A file that is not on the type allowlist,
  is too large, is a symlink, or is an invalid field set is **skipped** with its
  reason in the preview, so a bundle with one `.svg` can still be imported.
- **D7. Feature switch, off by default.** It widens what users can do (app-api
  makes outbound requests on a user's behalf), so per CLAUDE.md it is a flag of
  its own, app-api only, permanent once finished. The Runtime is unaffected.

## 3. Surface

All under `/skills/mine`, session cookie auth, owner-scoped through
`UserSkillService`, 404 after authentication while the flag is off.

| Route | Does |
|---|---|
| `POST /skills/mine/import/preview` | Parse + diff, write nothing. Multipart: `file` (zip) **or** `url`; optional `path`, `skillId` (diff against that owned skill). |
| `POST /skills/mine/import` | Create a new skill from the bundle. Same fields + optional `displayName`, `expectedBundleHash`. |
| `POST /skills/mine/{skillId}/import` | Re-import into an owned skill. Same fields + `expectedBundleHash`, `expectedUpdatedAt`. |

The preview returns the parsed skill (name, description, instruction length),
per-field change flags when there is a target, and one row per file:
`{path, filename, kind, size, status, reason?}` with `status` one of `added`,
`changed`, `unchanged`, `removed`, `skipped`. An archive holding several skills
and no `path` returns `candidates` (each directory with a `SKILL.md`) instead,
and the SPA asks which one.

An imported skill's row carries `importSource`:
`{type, url?, owner?, repo?, ref?, path, commitSha?, filename?, bundleHash, importedAt}`.
The edit page shows it and offers **Refresh from GitHub**, which previews the
same source again.

## 4. Mapping a bundle onto our layout

The bundle root is the directory holding `SKILL.md` (after stripping a zip's
single top-level folder, which GitHub archives and most zip tools add).

- `SKILL.md`: YAML frontmatter (`yaml.safe_load`) → `description`,
  `allowed-tools` (string or list), the rest into `skillMetadata` minus the keys
  the projection owns; the body → `instructions`. `name` → the new skill's
  display name (`mcp-builder` → "Mcp builder"), editable before create.
- Kind from the first directory: `references/` or `reference/` → reference,
  `scripts/` → script, `assets/` → asset. Anything else (root files such as
  `FORMS.md`, or `examples/`) → reference if its type is text, else asset.
- Our manifest is flat and keyed by filename across kinds, so a file's stored
  name is its **basename**. When two files share a basename, both take their
  path below the kind directory joined with `-` (`legal/nda.md` → `legal-nda.md`).
  A name that still fails the filename rule or collides is skipped.
- `read_skill_file` falls back to the basename when a `dir/file` path misses, so
  a `SKILL.md` that links `reference/evaluation.md` still resolves after import.
  This is a lookup on a miss inside a tool call, so it adds nothing to the prompt
  or the turn path. Only reading falls back: running a skill script
  (`WORD_SKILL_SCRIPTS_ENABLED`) keeps the strict lookup, and a path with a `..`
  segment never falls back.
- Ignored entirely (not listed): directories, any path segment starting with `.`
  (`.git*`, `.DS_Store`) and `__MACOSX/`.

## 5. Bounds

| Bound | Value | On breach |
|---|---|---|
| Uploaded zip | 10 MiB | 413-style 400, nothing read |
| GitHub archive | 25 MiB, streamed, aborted past it | 400 ("too large, upload a zip of the skill") |
| Entries in the archive | 5,000 | 400 |
| `SKILL.md` | 200 KiB (it lands in the DynamoDB row) | 400 |
| Files imported | 50 (`MAX_RESOURCES_PER_SKILL`) | 400 naming the count |
| One file | 1 MiB (`MAX_RESOURCE_BYTES`) | file skipped |
| Imported bytes, total | 20 MiB | 400 |
| Description | 1,024 chars | 400 |

Every entry is read through a bounded reader (limit + 1 bytes), so a declared
size that lies cannot inflate memory (zip bombs). Entry names are refused if
absolute, if any segment is `..`, or if they hold a backslash, NUL or drive
letter; encrypted entries are refused. Symlinks (Unix mode `S_IFLNK`) are
skipped: their bytes are a target path, not content. Types go through
`resource_types.resolve_upload_content_type` and field sets through
`validate_field_set_upload`, the same checks as a hand upload, because every
write still goes through `SkillBundleFiles`.

**SSRF.** The user's URL is parsed, never fetched. Accepted shapes:
`https://github.com/{owner}/{repo}[.git]`, `…/tree/{ref}/{path}`,
`…/blob/{ref}/{path}/SKILL.md`, and
`https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}`; https only, no
userinfo or port. `owner`, `repo` and `ref` must match GitHub's own character
rules (a `ref` is one segment, so a branch with `/` in its name is not
reachable by URL; a commit SHA or tag is). The fetch goes to the one host we
name, `codeload.github.com`, with redirects off, a 20 s timeout, no cookies or
credentials, and through `PublicOnlyTransport` (the crawler's resolve-then-
connect check), so even a poisoned resolver cannot point it inside the VPC. A
404 reads "not found, or the repository is not public". Private repositories are
out of scope.

**Event loop.** Parsing the archive and the S3 puts and deletes run in a worker
thread (`run_blocking`), never on app-api's single loop.

## 6. Write order on apply

1. Re-read the source; refuse on a hash or `updatedAt` mismatch (D3).
2. Put added and changed files to their bundle keys.
3. One row write: fields, manifest and `importSource`, then the `SKILL.md`
   projection and a freshness invalidation, as an edit does.
4. Delete the keys the manifest no longer names (best effort, as today).

A failure after step 2 leaves new bytes under some live keys while the row
still describes the old manifest: the same exposure a hand re-upload has today.
Pinned versions read frozen content-addressed copies, so no project sees it.

## 7. Not in this change

Private repositories (would need a GitHub connector token), branch names with
`/`, admin-catalog bundle import, scheduled auto-refresh (would quietly change a
skill under its users; refresh stays a click).

Exporting a skill as a zip was built separately, with no flag of its own:
`GET /skills/mine/{skill_id}/export` (the owner only) and
`GET /admin/skills/{skill_id}/export` (`admin.skills`, catalog skills only), each
with an optional `?version=n` for a frozen version. The archive is
`{slug}/SKILL.md` plus `references/`, `scripts/` and `assets/`, with `SKILL.md`
rendered from the row, and this importer reads it back to the same skill
(`apis/shared/skills/bundle_export.py`; the **Download skill** action on the
skill detail page and the admin skill page).
