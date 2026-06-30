# Nightly Build Quick Reference

## Automatic Execution
Runs every night at **2 AM Mountain Time (9 AM UTC)**.
Reads the `NIGHTLY_TRACKS` repository variable. If unset, nothing runs (fork-safe).

## Manual Execution
Go to: **Actions** → **Nightly Build & Test** → **Run workflow**

| Input | Default | Description |
|-------|---------|-------------|
| `tracks` | `all` | Comma-separated tracks (see below) |
| `skip_teardown` | `false` | Leave resources deployed for debugging |

## Track Vocabulary

| Track | What it does |
|-------|-------------|
| `test-backend-<branch>` | Backend tests + coverage against `<branch>` |
| `test-frontend-<branch>` | Frontend tests + coverage against `<branch>` |
| `deploy-<branch>` | Full stack deploy from `<branch>` + smoke test + teardown |
| `e2e-<branch>` | Full stack deploy from `<branch>` + Playwright E2E tests + teardown |
| `scan-images-<branch>` | Build + Trivy-scan Docker images from `<branch>` |
| `all` | All of the above with defaults (`develop` for tests/deploy/e2e) |

### Examples
```
test-backend-develop
deploy-main,test-frontend-main
e2e-develop
all
```

## Setting Up

Set `NIGHTLY_TRACKS` in **Settings → Secrets and variables → Actions → Variables**:
- `all` — full suite
- `test-backend-develop,test-frontend-develop` — tests only
- *(empty/unset)* — disabled (safe for forks)

## What Each Track Does

### Test Tracks
1. ✅ Install dependencies + run tests with coverage
2. 📊 Compare coverage against previous baseline

### Deploy Track
Full pipeline: platform → backend code deploys → frontend → smoke test → teardown

### E2E Track
Deploys a full stack and runs Playwright E2E tests against it, then tears down.

## Debugging Failed Runs

| Problem | Fix |
|---------|-----|
| Nothing runs on schedule | Set `NIGHTLY_TRACKS` repo variable |
| Deploy fails | Check AWS credentials + CDK variables in development environment |
| Teardown fails | Manually empty S3 buckets + `npx cdk destroy --all --force` |
| Coverage analysis fails | Check that test jobs uploaded artifacts |

## Cost Considerations

Deploy tracks spin up ECS Fargate + ALB for ~2 hours, then tear down.
- Estimated cost per deploy track: ~$0.25/night
- Tests-only tracks: free (GitHub-hosted runners)

## More Details

See [NIGHTLY-BUILD.md](./NIGHTLY-BUILD.md) for full documentation.
