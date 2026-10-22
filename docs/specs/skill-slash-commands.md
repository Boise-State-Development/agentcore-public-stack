# Skill slash commands

Typing `/web-research` in the composer invokes that skill for **that message**. It is the
sibling of the `@`-mention (Marketplace D11): same menu shape, same keyboard, same
"rides one turn, does not bind the conversation" semantics.

The menu's last row is **Browse skills →**, which goes to Customize → Skills.

## Scope: the skills the user has turned on

The menu lists exactly the skills already switched on for the conversation — the same set
the turn already discloses to the model in `<available_skills>`.

That is the decision the whole design rests on. Because the invoked skill is already in
`enabled_skills`, a slash command **changes nothing about the cacheable prefix**. The
system prompt, the `toolConfig` and the disclosure block are byte-identical whether or not
a command was used. The entire cost of the feature is one short directive appended to the
turn's user message.

Offering a switched-off skill would have meant one of two bad things:

- **Widening the disclosure on the fly** — a new `<available_skills>` entry mid-session
  rewrites a 30k–150k-token prefix at the cache-write premium, triggered by a keystroke.
- **Showing a command that does nothing** — the directive would name a skill the model
  cannot see, and the model would burn a tool call discovering that.

Turning a skill on stays where it belongs: the Customize page, which the menu links to.

An Agent-bound conversation resolves through `visibleSkills` / `isSkillShownEnabled` like
every other skill surface, so it offers that Agent's bound skills — again, exactly the set
the turn will disclose.

## The text is the binding

Unlike the `@` menu, there is **no remembered pick**. The invoked set is derived from the
composer text on every keystroke:

```
/web-research what is the top headline on npr.org?
└─ findSkillCommands() → ['web-research'] → ['web_research']
```

A slug is a single unambiguous token (an Agent name is not — it contains spaces, which is
why the `@` menu has to remember what was picked). Deriving is therefore exact, and it buys
two things:

- A hand-typed command works identically to a menu pick.
- The chip and what gets sent **cannot disagree**. The chip's `✕` removes the `/slug` from
  the text, because that is the only place the binding lives.

### The token rule

`/` is ordinary punctuation, so the rule has to keep the menu shut far more often than it
opens it. A command must start a word **and** must not be followed by another `/`:

| Input | Result |
|---|---|
| `/web-research …` | command |
| `use /web-research, then …` | command (ordinary punctuation after is fine) |
| `and/or`, `24/7` | prose — does not start a word |
| `https://x.com/docs`, `src/app/docx` | prose — same reason |
| `/usr/bin/env` | prose — *starts* a word, excluded by the trailing-slash half |
| `/not-a-skill` | prose — only slugs the user can invoke resolve |

That last-but-one row is the one that matters: an absolute path starts a word exactly like
a command does, so without the trailing-slash clause a skill slugged `usr` would be invoked
silently. The same rule is implemented three times — the composer's caret-anchored token,
`findSkillCommands`, and the thread renderer's `splitSkillCommands` — and all three must
agree, or a message would render as something different from what it sent.

## The slug is served, not derived

`GET /skills/` returns a `slug` per skill, computed with `slugify_skill_name` — the same
function that produces the `Skill.name` the `AgentSkills` plugin injects and the key its
`skills` tool accepts. The SPA never re-implements the rule; a client that drifted would
write a command the model cannot resolve.

`slug` is **optional** in the SPA's `UserSkill`. The SPA and the backend deploy
independently and in no enforced order, so a client that lands first must degrade to "no
slash commands", not to a menu of `/undefined`.

## Wire format

The SPA sends `invoked_skills` alongside `enabled_skills`:

```jsonc
{
  "message": "/web-research what is the top headline on npr.org?",
  "enabled_skills": ["docx", "rubric_authoring", "web_research"],
  "invoked_skills": ["web_research"]   // always a subset
}
```

`_resolve_invoked_skill_slugs` intersects it against the turn's **effective** skill set —
the same narrow-never-grant rule `_apply_enabled_skills_filter` applies, re-run because an
Agent's skill bindings can still replace that set afterwards. The result is ordered by the
effective set rather than by the request, so two turns naming the same skills produce
byte-identical text.

## Why a directive and not a pre-load

The only activation path is the plugin's own `skills` tool, which the *model* calls. So
"explicit" is expressed as an instruction:

```
[The user invoked the `web-research` skill with a slash command. Activate it with the
`skills` tool before answering, and follow the loaded instructions for this message.]
```

Pre-loading the instructions server-side would duplicate the plugin's response formatting
and bypass its activation-state tracking, for the sake of saving one tool call.

The note is appended **last**, after every prepended note (interruption, attachment
recovery, app context), so it sits closest to the model's first token. It rides
`original_message`, so the thread shows the user only what they typed — the literal
`/slug` — while the note stays an honest part of persisted history. It costs one line as
input this turn and as cached history thereafter; the disclosure block it points at is in
the prefix either way.

## Not compatible with mid-turn steering

A steer lands as a text block on the *tool-result* message of a turn whose skills were
already resolved, so its directive would have nothing to attach to. A queued follow-up
carrying a slash command is therefore never armed — it flushes as a normal turn, the same
way a follow-up with an attachment or an `@`-mention does.

## Gating

None of its own. It rides `SKILLS_ENABLED`: with skills off, `GET /skills/` 404s, the
command list is empty, and the menu never opens. The two embedded previews (Agent Designer,
marketplace test drive) pass `[showSkillCommands]="false"` for the same reason they pass
`[showAgentMentions]="false"` — those panes exercise one Agent whose skills the Agent
dictates.

## Contrast, and why the chip is neutral

**Do not use `bg-primary-50` / `-100` / `-200` as a tint.** The `primary` scale is generated
from `#0033a0` by lightness offset alone — `oklch(from #0033a0 calc(l + 0.4) c h)` — so it
keeps the full chroma of Boise State blue at every step. `primary-50` resolves to
**rgb(118, 179, 255)**: a saturated mid-blue, not the pale wash its name implies. Used as a
chip fill it reads as a blue blob behind small text. The `state-*` scales *are* real tints
(`state-success-50` is `rgb(240, 253, 244)`); `primary` is the exception, and the naming
hides it.

So the chip and the menu's icon tile use **neutral surfaces with the brand blue in the
text**: white / `gray-100` in light, `gray-700` in dark, label `primary-accessible`
(`#0033a0`) / `primary-50`.

Measured, composited against the real page background:

| Element | Light | Dark | Bar |
|---|---|---|---|
| Chip label (12px, 500) | 10.60 | 4.74 | 4.5 (AA normal text) |
| Chip `✕` glyph | 4.84 | 3.96 | 3.0 (UI component) |
| Chip border | 1.47 | 2.13 | — (decorative; the label carries the meaning) |
| Menu icon tile | 9.63 | 4.74 | 3.0 (decorative, `aria-hidden`) |
| Menu `/slug` | 16.13 | 10.30 | 4.5 |

Two traps worth keeping written down:

- On `gray-700`, `primary-200` measures **3.42** and `primary-100` **4.02** — both fail.
  `primary-50` (4.74) is the only step that clears AA on that surface. On `gray-800` the
  whole range passes, but a `gray-800` chip disappears into the composer, which is also
  `gray-800`.
- Verify light mode with **both** levers — remove `dark` from `<html>` *and* emulate
  `prefers-color-scheme: light`. The class alone leaves the `dark:` variants applying, and a
  "light-mode" screenshot silently shows dark.
