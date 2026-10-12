# Writing a skill

## The description

The description is the single most important line in a skill. The model reads the list of turned-on skills, with their descriptions, and decides from that alone whether to use one. If the description doesn't connect to what the user is asking, the instructions are never read.

- **Say what it does and when to use it.** Put every "when to use" signal in the description, not in the instructions: the model doesn't see the instructions until it has already chosen the skill.
- **Lean slightly pushy.** Models tend to under-use skills, so name the situations outright. Weak: "Helps review grant proposals." Better: "Review a grant proposal against the funder's criteria and give scored, line-level feedback. Use this whenever the user shares a proposal, a draft narrative or a budget justification, or asks whether something is ready to submit, even if they don't say 'review'."
- **Use the words people actually say.** Include the phrasings and file types a user would really type, not just the formal name of the task.
- **Describe real value.** A model uses a skill when the task is complex or specialized enough to benefit. A skill for "read this file" won't be picked up however it is described, so describe what the skill adds.
- **Keep it on one line** and within 1,024 characters. Avoid a colon followed by a space if the user may import the skill as a `SKILL.md`, since the importer reads simple `key: value` lines.

## The name

Keep it short and plain ("Grant Review", not "Advanced Grant Proposal Review Assistant v2"). The skill's id comes from the name: lowercased, with underscores, and a number appended if the id is taken. The model sees a hyphenated form of that id. Renaming later changes what the model sees, so settle the name early.

## Structure of the instructions

- **Start with the goal**, and what "done" looks like.
- **Then the workflow**, as steps in order.
- **Then the output format**, if there is one.
- **Then edge cases** and what to do when something is missing.

Keep the instructions under about 300 lines. Move detail into reference files and say exactly when to read each one, for example: "Before filling in a form, read `references/forms.md`." A reference file over about 300 lines should start with a short table of contents.

When a skill covers several variants (one funder's rules versus another's, or one department's process versus another's), give each variant its own reference file. The model then reads only the file it needs.

## Writing style

- **Use the imperative**: "Check the budget totals", not "The budget totals should be checked."
- **Explain why.** Instead of `ALWAYS` or `NEVER` in capitals, give the reason. A model that understands why a step matters handles the cases the instructions didn't anticipate. If you find yourself writing in capitals, rewrite it as a reason.
- **Stay general.** Write for every request the skill will see, not just the example you discussed. Examples illustrate a rule; they shouldn't become the rule.
- **Give templates for output formats.** If the result must follow a fixed shape, show the shape:

  ```markdown
  ## Feedback format
  # Summary (2-3 sentences)
  ## Scores
  ## Line-level comments
  ```

- **Show examples as input-and-output pairs** when the transformation is easier to see than to describe.
- **Draft, then reread it with fresh eyes**, and cut anything that isn't pulling its weight.

## Tools

A skill can use only the tools the user has turned on. If the skill depends on one, name it as the user would see it in their tool list, and say what to do when it isn't available. For example: "If you can't search the web, ask the user to paste the funder's guidelines."

## What to leave out

- Things the model already knows how to do well.
- Long background that doesn't change what the model does.
- Passwords, keys, tokens and other secrets.
- Code that would need to run: scripts are stored but never executed here.
