# Task: Implement AWS CDK Infrastructure and CI/CD

## Project Context
You are implementing a 5-stack AWS CDK application with platform-agnostic CI/CD pipelines. All build/test/deploy logic must reside in portable shell scripts, with GitHub Actions serving only as an orchestration layer.

## Context Files
1. **THE_PLAN.md**: Your implementation checklist with all tasks

## Instructions
1. Open and read `THE_PLAN.md` to understand all pending tasks
2. Identify the next unchecked task in `THE_PLAN.md`
3. Complete the task fully (write code, create files, test, etc.)
4. Verify your work meets the quality standards
5. Update `THE_PLAN.md` to mark the task as complete (`- [x]`)
6. Repeat steps 4-7 until you reach a `[HUMAN]` checkpoint at the end of a phase
7. **STOP** when you reach a `[HUMAN]` checkpoint and wait for human approval before continuing

## Critical Rules
- **Create empty template** `CLAUDES_LESSONS_PHASE<N>.md` at phase start (do NOT pre-fill)
- **Update lessons learned ONLY** during testing/troubleshooting with human
- Complete tasks sequentially - do not skip ahead
- Do not batch task completions - mark each task complete immediately after finishing it
- Verify your work before marking tasks complete
- Never hardcode AWS Account IDs, Regions, or resource names
- GitHub Actions workflows must ONLY call shell scripts (no inline logic)
- Do not create new Markdown documentation files unless explicitly requested (except for phase lessons learned)
- **STOP at Phase Boundaries**: When you complete all tasks in a phase and encounter a `[HUMAN]` checkpoint, STOP and summarize what was completed. Do NOT check the `[HUMAN]` checkbox - only humans can approve phases.
- **Phase Boundaries are Mandatory**: You cannot proceed to the next phase until the human has checked the `[HUMAN]` approval checkbox.

## Success Criteria
- All checkboxes in `THE_PLAN.md` are marked complete (`- [x]`)
- All `[HUMAN]` checkboxes are checked by a human after manual inspection
- A `CLAUDES_LESSONS_PHASE<N>.md` document exists and is complete for the phase
- All CDK stacks deploy successfully
- All shell scripts are portable and work on ubuntu-latest
- All GitHub Actions workflows trigger correctly based on path changes
- All configuration is externalized and environment-agnostic

## Phase Workflow
When starting a new phase:
1. Create `CLAUDES_LESSONS_PHASE<N>.md` with **empty sections** (placeholder text only)
2. Work through tasks in `THE_PLAN.md` sequentially
3. **DO NOT pre-fill lessons learned** - leave empty until testing phase
4. At the `[HUMAN]` checkpoint, lessons learned should still be mostly empty
5. Summarize completed work and files created
6. Wait for human approval before proceeding to next phase
