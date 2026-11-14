# Adding a structured extraction to a skill

A skill can carry a **field set**: a named list of facts to pull out of a document, such as deadlines, amounts, rates, eligibility rules or page limits. When the skill is on and the user attaches documents, the agent runs the field set with the `document_extract` tool. The user gets a table and a spreadsheet, and every value is checked against the passage it came from.

Read this when the skill you are drafting is about pulling the same facts out of a kind of document, or when the user asks for a table, a checklist of values or a comparison across several documents.

## When to offer one

Offer a field set when all of these are true:

- **The skill reads a kind of document** that comes back again and again: solicitations, award notices, contracts, syllabi, invoices, policies.
- **The same facts are wanted every time.** If each request asks something new, plain instructions are better.
- **The facts can be named in advance**, each as one value. "The submission deadline" is a field. "Anything unusual" is not.

Don't add one just because a skill involves documents. A skill that summarizes, critiques or rewrites a document needs instructions, not a field set. When you're unsure, ask the user whether they want the same columns filled in for every document.

## The file

A field set is one reference file named `field-set.<name>.md`. `<name>` is lowercase words joined by hyphens, such as `field-set.award-notice.md`, and it is the name the agent asks for. A skill can carry more than one field set, one file each.

The file has a short frontmatter, then one markdown table with a row per field, in the order the columns should appear:

```markdown
---
title: Award notice
version: 1
---

| name | title | type | find | enum | optional |
|---|---|---|---|---|---|
| sponsor | Sponsor | string | The agency or organization making the award | | no |
| award_amount | Award amount | currency | The total amount awarded for the whole project period, not one year's amount | | no |
| start_date | Start date | date | The date the project period begins | | no |
| indirect_rate | Indirect cost rate | number | The indirect (F&A) cost rate the award allows, as a percentage | | yes |
| cost_share_required | Cost share required | boolean | Whether the award requires the institution to contribute cost share | | no |
| award_type | Award type | enum | Whether this is a grant, a cooperative agreement or a contract | grant, cooperative agreement, contract | no |
| reporting_deadlines | Reporting deadlines | list | Every progress or financial report due date the notice lists | | yes |
```

The columns:

| Column | What goes in it |
|---|---|
| `name` | The field's id, in `snake_case`, starting with a letter. Keep it stable: renaming a field makes old results incomparable with new ones. |
| `title` | The column header people read. |
| `type` | One of `string`, `number`, `currency`, `date`, `boolean`, `enum` or `list`. |
| `find` | **The most important column.** A sentence saying exactly what to look for, and what not to confuse it with. `award_amount` alone tells the model almost nothing; "the total amount awarded for the whole project period, not one year's amount" tells it what to find. |
| `enum` | The allowed values, separated by commas. Required for `enum`, and must be empty for every other type. |
| `optional` | `yes` if a document may legitimately not state it, otherwise `no`. |

Rules the file must follow, or saving it fails with a message saying what to fix:

- **At most 14 fields.** That is the most the extraction can enforce. For more, split them into two field sets with a clear reason for the split.
- Field names are unique, and every field has a `find` phrase.
- A cell cannot contain `|`, an allowed value cannot contain a comma, and each field fits on one row.
- `version` is free text. Raise it when the fields change in a way that matters, so the user can tell results from the old and new versions apart.

## Writing good fields

- **Write the `find` phrase as if briefing a new colleague.** Name the section it usually lives in, the words documents use for it, and the near-miss to avoid ("the full proposal deadline, not the letter-of-intent deadline").
- **One fact per field.** "Deadline and submission method" is two fields.
- **Pick the narrowest type.** A `date` comes back as a date and a `number` as a number, so they can be sorted and compared. Use `currency` for money, which keeps the amount with its currency, and `list` when a document can state several values for one field.
- **Use `enum` for a judgement with fixed answers**, and `boolean` for a yes or no. These are answers inferred from the text, not quoted from it, so the user sees them marked as not directly confirmed.
- **Mark a field optional only if its absence is normal.** A missing required field is reported as missing, which is often exactly what the user needs to know.
- **Get the user to name the fields.** Ask what they copy into their spreadsheet or checklist today. The field set should match their columns, in their order.

## What the instructions must say

The agent doesn't discover a field set unless the skill's instructions tell it to use one. In the instructions:

- **Name the set and when to run it**: "When the user attaches one or more award notices, run the `award-notice` field set with `document_extract`, passing every document in one call."
- **Don't repeat the fields** in the instructions. The tool reads them from the file, and a second copy drifts out of date.
- **Say what to do with the results**, especially across several documents: what to lead with, what counts as a conflict or an outlier, and what to recommend. This is the part only the user's office knows, so ask them. If it runs long, put it in its own reference file and tell the instructions when to read it.
- **Say how to treat each value's status.** A value marked `supported` was found in its cited passage. Anything else must not be stated as fact. Remind the skill to say which values need checking rather than smoothing over them.

## Saving and checking it

- **With the skill tools:** create the skill first, then save the field set with `my_skill_write_file`, using the `field-set.<name>.md` filename and the `reference` kind. The result lists the fields it read, so you can confirm them with the user. If the file has a mistake, the save fails and says what to fix, so correct the file and save it again.
- **Without the tools:** give the field set as its own block with its filename, for the user to upload as a reference file.
- **To change a field set**, read it with `my_skill_read_file`, change only what the user asked for, and save the whole file again.

Structured extraction may not be turned on for everyone. When the save result says it is off, tell the user that the field set is saved and will be used once an administrator turns structured extraction on, and that the rest of the skill works without it.

When the user tries the skill, they turn it on, start a new conversation and attach a document. They should see a table under the answer. If they don't, check that the instructions name the set exactly as the filename does.
