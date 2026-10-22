# Notice

This skill is adapted from **skill-creator** by Anthropic, PBC, part of the open-source Anthropic skills collection (https://github.com/anthropics/skills), licensed under the Apache License, Version 2.0. A copy of the license is in `references/LICENSE.txt`.

## Changes from the original

The authoring method is kept: capturing intent with an interview, writing a description that gets the skill used, progressive disclosure, explaining the reason behind instructions, and generalizing from feedback. The text was rewritten for this platform:

- The evaluation harness was removed: test files, parallel and baseline runs, benchmark scripts, the review viewer and the description-optimization loop. They need subagents and script execution, which this platform does not provide. Testing is the user trying prompts in a new conversation (`references/testing.md`).
- It was rewritten for this platform's skill tools (`my_skill_*`), its approval step for every write, and its rules: skills are off until turned on under Customize → Skills; a new skill is not usable in the conversation that created it; files are flat; scripts are stored but never run; `allowed-tools` is advisory; and per-person and per-skill limits apply.
- A fallback for users without the tools was added: copying the skill into Customize → Skills → Add.
- Guidance was added against carrying unreviewed third-party text, or secrets, into a skill.
