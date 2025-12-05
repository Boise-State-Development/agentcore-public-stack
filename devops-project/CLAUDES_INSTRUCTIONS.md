# Instructions for Coding Agent

## Project Overview
You are building a 5-stack AWS CDK architecture with a platform-agnostic CI/CD strategy. The goal is to decouple build/deploy logic from GitHub Actions by placing it into portable shell scripts.

**Stack Architecture**:
1. Frontend Stack (S3 + CloudFront + Route53)
2. App API Stack (VPC + ALB + Fargate + RDS/DynamoDB)
3. Inference API Stack (Fargate sharing App API network)
4. Agent Core Stack (Lambda/Step Functions/DynamoDB)
5. Gateway Stack (API Gateway)

**Technology Stack**:
- **CDK Language**: TypeScript
- **Shell Scripts**: Bash (must be portable across Linux/macOS/WSL)
- **CI/CD**: GitHub Actions (5 separate workflows, one per stack)

## Execution Model
1.  **Read** `THE_PLAN.md` to understand the current state.
2.  **Review** `CLAUDES_LESSONS_PHASE*.md` for lessons learned from previous phases.
3.  **Identify** the first unchecked task (`- [ ]`).
4.  **Execute** the task. This may involve:
    *   Writing CDK infrastructure code (TypeScript/Python).
    *   Writing Shell scripts (Bash).
    *   Writing GitHub Actions YAML.
5.  **Verify** the file creation/content.
6.  **Update** `THE_PLAN.md` immediately by marking the task as checked (`- [x]`).

## Constraints & Rules
1.  **Configurability is King**: NEVER hardcode AWS Account IDs, Regions, or unique resource names. Always use configuration variables (Context, Env Vars).
2.  **No Inline Logic in YAML**: GitHub Actions workflows must ONLY call shell scripts. No `run: npm install` or `run: aws s3 sync` inside the YAML. The ONLY exception is installing base dependencies (e.g., `actions/setup-node@v3`) and calling the shell scripts.
3.  **Documentation Policy**: 
    - Do NOT create new Markdown files unless explicitly requested.
    - ONLY update `README.md` if there are factual inaccuracies that need correction.
    - Update `THE_PLAN.md` checkboxes after completing each task.
4.  **Strict Sequencing**: Do not jump ahead. Complete tasks in order: CDK infrastructure → Shell scripts → CI/CD pipeline for each stack before moving to the next stack.
5.  **Shell Script Portability**: Scripts must work on `ubuntu-latest` (GitHub Actions), macOS, and Linux/WSL. Use `/bin/bash` shebang and avoid platform-specific commands.
6.  **Error Handling**: All shell scripts must include proper error handling (`set -euo pipefail`) and meaningful error messages.
7.  **Cross-Stack Dependencies**: Always use SSM Parameter Store or CloudFormation Outputs for cross-stack resource references. Never hardcode ARNs or IDs.

## Workflow
You are responsible for the integrity of `THE_PLAN.md`. If you complete a task, you MUST check the box immediately.

**Phase Start Protocol**:
1. At the beginning of each new phase, create `CLAUDES_LESSONS_PHASE<N>.md` in the `devops-project/` folder.
2. Initialize the document with empty sections:
   - Technical Discoveries
   - Gotchas and Workarounds
   - Process Improvements
   - Open Questions
   - Notes for Future Phases
3. **IMPORTANT**: Leave sections empty with placeholder text like "_This section will be filled in as we test..._"
4. **DO NOT pre-fill** with assumptions about what might go wrong or summaries of what you built.
5. Update this document **ONLY when the human encounters real issues during testing** and you work together to solve them.
6. This is a **living document** that grows through actual experience, not theoretical planning.

**Task Completion Protocol**:
1. Read the task requirements carefully.
2. Implement the solution (write code, create files, etc.).
3. Verify the implementation works as expected.
4. Mark the task as complete in `THE_PLAN.md` by changing `- [ ]` to `- [x]`.
5. Move to the next task.

**Lessons Learned Protocol**:
- **DO NOT** update lessons learned during implementation.
- Update lessons learned **ONLY** when:
  - Human tests your implementation and encounters issues
  - You troubleshoot together and discover solutions
  - You find gotchas that weren't obvious during development
  - You identify process improvements based on real experience

**Do NOT**:
- Skip tasks or complete them out of order.
- Mark multiple tasks as complete in a single update.
- Mark a task as complete before verifying it works.
- Create placeholder or incomplete implementations.
- Pre-fill lessons learned documents with assumptions or summaries.
- Update lessons learned without human involvement during testing.

**Quality Standards**:
- All code must follow TypeScript/Python best practices.
- All shell scripts must be executable and include proper error handling.
- All configuration must be externalized (no hardcoded values).
- All GitHub Actions workflows must only call shell scripts (no inline logic).
- Lessons learned documents must be empty templates until testing begins.
