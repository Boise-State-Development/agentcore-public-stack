# Trying and improving a skill

There is no automated evaluation harness here. Testing means the user tries realistic prompts in a new conversation with the skill turned on, then tells you what happened. Keep this loop light: a couple of rounds usually settles a skill.

## Before testing

- **Propose two or three realistic prompts**, written the way a real person types: concrete, with detail and some backstory. Not "review this proposal", but "my NSF CAREER draft is due Friday, can you check the broader impacts section against the solicitation and tell me what's weak". For a skill whose output can be checked (a format, a calculation, a fixed set of steps), say what a correct result should contain.
- **Add one or two near-miss prompts** that share words with the skill but should *not* use it. These check that the description isn't so broad that it fires on everything.
- Let the user change the prompts. They know their real cases better than you do.

## Running a test

1. The user turns the skill on under **Customize → Skills**.
2. They start a **new conversation** and type one of the prompts as written. To check whether the skill gets picked up on its own, they should not use the slash command. To check only the instructions, they can use the slash command to force the skill on.
3. They come back here and tell you what happened. A paste of the response, or a description of it, is enough.

## Reading the results

- **The skill wasn't used when it should have been.** That's a description problem. Make it name the situation more directly, or add the words the user actually used.
- **The skill was used when it shouldn't have been.** The description is too broad. Narrow it, or say what it is not for.
- **The skill was used but the result was wrong.** That's an instructions problem. Find the step the model skipped or misread.
- **The result was right but slow or roundabout.** Something in the instructions is making the model do unneeded work. Cut it.

## Improving

- **Generalize from the feedback.** The skill will see many prompts besides these few. Fix the underlying misunderstanding, not the specific example. If a fix starts to look like a special case for one test, try explaining the goal differently instead.
- **Keep it lean.** Remove instructions that aren't changing the result.
- **Explain why** rather than adding stronger wording. "Check totals because funders reject budgets that don't add up" works better than "ALWAYS CHECK TOTALS".
- **Turn repeated work into a reference file.** If every test had the model rebuilding the same template, checklist or worked example, write it once as a reference file and point the instructions at it. Make it a reference file, not a script, because scripts don't run here.
- **Edit in place, and keep the name.** The user's on/off setting, and any agents or projects that use the skill, follow the same skill as it changes.
- **Name and description changes are visible.** Every conversation that has the skill turned on sees them, so make those changes deliberately.

Stop when the user is happy, when the results are consistently good, or when another round isn't making a difference.
