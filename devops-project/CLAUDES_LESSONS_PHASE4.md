# Phase 4: Lessons Learned - AWS Bedrock AgentCore Runtime Refactoring

## Overview
This document captures lessons learned during Phase 4 implementation: Refactoring the Inference API Stack from ECS/Fargate to AWS Bedrock AgentCore Runtime.

---

## Technical Challenges

### Challenge 1: CDK Bootstrap Loading App Context

**Issue:**
Running `cdk bootstrap` from within a CDK app directory (containing cdk.json) causes it to load the entire CDK application and its configuration, triggering config.ts logging and potentially using incorrect context values.

**Root Cause:**
CDK CLI automatically searches for and loads the CDK app in the current directory when running any command, including bootstrap. This causes the configuration loading logic to execute even though bootstrap doesn't need app-specific context - it only needs AWS account and region.

**Solution:**
Always run `cdk bootstrap` from a neutral directory (without cdk.json) to avoid loading the CDK app:
```bash
# Bad - runs from CDK app directory
cd infrastructure
cdk bootstrap aws://${ACCOUNT}/${REGION}

# Good - runs from parent directory
cd project-root
cdk bootstrap aws://${ACCOUNT}/${REGION}
```

**Lesson Learned:**
- `cdk bootstrap` should never be passed `--context` parameters (they trigger app loading)
- Run `cdk bootstrap` from outside the CDK app directory to prevent unwanted app initialization
- Only `cdk synth` and `cdk deploy` should load the app and receive context parameters
- This prevents unexpected side effects like logging, incorrect context values, or bootstrap failures

---

### Challenge 2: [To be filled during testing/troubleshooting]

**Issue:**
[Description of the issue]

**Root Cause:**
[What caused the problem]

**Solution:**
[How it was resolved]

**Lesson Learned:**
[Key takeaway]

---

## Configuration Issues

### Issue 1: [To be filled during testing/troubleshooting]

**Problem:**
[Description]

**Resolution:**
[How it was fixed]

**Best Practice:**
[What to do in the future]

---

## AWS-Specific Learnings

### Learning 1: [To be filled during testing/troubleshooting]

**Topic:**
[Subject area]

**Discovery:**
[What was learned]

**Application:**
[How to apply this knowledge]

---

## Build & Deployment Insights

### Insight 1: [To be filled during testing/troubleshooting]

**Observation:**
[What was noticed]

**Implication:**
[Why it matters]

**Action Taken:**
[How it was addressed]

---

## Testing Observations

### Observation 1: [To be filled during testing/troubleshooting]

**Test Case:**
[What was being tested]

**Result:**
[Outcome]

**Adjustment:**
[Changes made based on result]

---

## Future Recommendations

### Recommendation 1: [To be filled during testing/troubleshooting]

**Area:**
[Domain of improvement]

**Suggestion:**
[What to do]

**Rationale:**
[Why this is important]

---

## Summary

[To be completed at the end of Phase 4 testing]

**Total Issues Resolved:** [N]
**Major Learnings:** [N]
**Time Spent on Phase:** [X hours/days]
**Phase Status:** [In Progress / Complete]

---

*Note: This document should be updated throughout Phase 4 implementation, especially during testing and troubleshooting activities.*
