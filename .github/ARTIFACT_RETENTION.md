# Artifact Retention Policy

This document defines the retention periods for CI/CD artifacts uploaded by GitHub Actions workflows.

## Retention Tiers

| Artifact Type | Retention | Examples | Rationale |
|---|---|---|---|
| Docker image tarballs | 1 day | `app-api-image.tar`, `inference-api-image.tar`, `rag-ingestion-image.tar` | Ephemeral build artifacts; images are pushed to ECR for long-term storage |
| CDK build artifacts | 1 day | `infrastructure-build` (compiled JS/TS), `gateway-cdk-templates` | Intermediate build outputs consumed by downstream jobs in the same run |
| CDK synthesized templates | 7 days | `*-cdk-synth` (CloudFormation templates in `cdk.out/`) | Needed for deploy jobs and debugging failed deployments |
| Frontend build output | 7 days | `frontend-build` (Angular dist/) | Consumed by deploy jobs; useful for debugging build issues |
| Test results and coverage | 7 days | `frontend-test-results`, `backend-coverage` | Debugging window for failed PRs and test regressions |
| Deployment outputs | 30 days | `*-outputs.json` (CDK stack outputs) | Audit trail for deployments; useful for rollback investigations |
| Coverage comparison reports | 30 days | `coverage-comparison` | Trend analysis across nightly runs |
| Trivy scan reports | 30 days | `trivy-scan-reports` | Security audit trail for container vulnerability findings |

## Verification

All `retention-days` values in workflow files must match this policy. The property test at `backend/tests/supply_chain/test_artifact_retention.py` validates consistency within each artifact category.
