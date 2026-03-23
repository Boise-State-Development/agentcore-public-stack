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
| `merge-validation:<base>:<overlay>` | Deploy `<base>`, overlay `<overlay>`, teardown |
| `all` | All of the above with defaults (`develop` for tests/deploy, `main`→`develop` for MV) |

### Examples
```
test-backend-develop
deploy-main,test-frontend-main
merge-validation:main:feature/my-branch
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
3. 🤖 AI analysis creates GitHub issues for coverage gaps (`test-coverage` + `nightly-build` labels)

### Deploy Track
Full pipeline: infra → rag → inference → app → frontend + gateway → smoke test → teardown

### Merge Validation
Deploys base branch, then overlays another branch on top — catches CDK/infra incompatibilities before merging.

## Debugging Failed Runs

| Problem | Fix |
|---------|-----|
| Nothing runs on schedule | Set `NIGHTLY_TRACKS` repo variable |
| Deploy fails | Check AWS credentials + CDK variables in development environment |
| Teardown fails | Manually empty S3 buckets + `npx cdk destroy --all --force` |
| MV overlay fails | Intended — overlay branch has infra incompatibilities with base |
| Coverage analysis fails | Check that test jobs uploaded artifacts |

## Cost Considerations

Deploy tracks spin up ECS Fargate + ALB for ~2 hours, then tear down.
- Estimated cost per deploy track: ~$0.25/night
- Tests-only tracks: free (GitHub-hosted runners)

## More Details

See [NIGHTLY-BUILD.md](./NIGHTLY-BUILD.md) for full documentation.
