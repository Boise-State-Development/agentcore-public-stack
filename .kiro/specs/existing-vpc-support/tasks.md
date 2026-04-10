# Implementation Plan: Existing VPC Support

## Overview

Add an optional `existingVpc` configuration block that allows importing a pre-existing VPC via `Vpc.fromVpcAttributes()` instead of always creating a new one. Changes are confined to `config.ts`, `infrastructure-stack.ts`, CI/CD scripts, and corresponding tests. Downstream stacks require zero changes.

## Tasks

- [x] 1. Add ExistingVpcConfig interface and extend AppConfig
  - [x] 1.1 Define `ExistingVpcConfig` interface and add optional `existingVpc` field to `AppConfig` in `infrastructure/lib/config.ts`
    - Add `ExistingVpcConfig` interface with `vpcId`, `availabilityZones`, `publicSubnetIds`, `privateSubnetIds`, `vpcCidrBlock?`
    - Add `existingVpc?: ExistingVpcConfig` to `AppConfig`
    - _Requirements: 1.1, 1.3_

  - [x] 1.2 Extend `loadConfig()` to parse `existingVpc` from environment variables and CDK context
    - Read `CDK_EXISTING_VPC_ID`, `CDK_EXISTING_VPC_AZS`, `CDK_EXISTING_VPC_PUBLIC_SUBNET_IDS`, `CDK_EXISTING_VPC_PRIVATE_SUBNET_IDS`, `CDK_EXISTING_VPC_CIDR` with fallback to `existingVpc.*` context
    - Split comma-separated env var strings into arrays
    - Only assemble `ExistingVpcConfig` when `vpcId` is present
    - Set `config.existingVpc` to `undefined` when absent
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 8.1, 8.2_

  - [x] 1.3 Add `validateExistingVpcConfig()` function and integrate into `validateConfig()`
    - Validate `vpcId` matches `/^vpc-[a-z0-9]+$/`
    - Validate `availabilityZones` has 2–6 entries
    - Validate `publicSubnetIds` has ≥2 entries, each matches `/^subnet-[a-z0-9]+$/`
    - Validate `privateSubnetIds` has ≥2 entries, each matches `/^subnet-[a-z0-9]+$/`
    - Validate `publicSubnetIds.length === availabilityZones.length`
    - Validate `privateSubnetIds.length === availabilityZones.length`
    - Skip `vpcCidr` validation when `existingVpc` is present
    - Throw descriptive errors identifying the failing field
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7, 6.1, 6.2_

  - [x] 1.4 Update `createMockConfig` helper in `infrastructure/test/helpers/mock-config.ts`
    - Add `existingVpc` to the mock config overrides support
    - _Requirements: 1.1_

- [x] 2. Checkpoint - Ensure config changes compile and existing tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Add fast-check and write config property-based tests
  - [x] 3.1 Add `fast-check` as a devDependency to `infrastructure/package.json`
    - Run `npm install --save-dev fast-check` in `infrastructure/`
    - _Requirements: (testing infrastructure)_

  - [x] 3.2 Write property test: Config round-trip from CDK context
    - **Property 1: Config round-trip from CDK context**
    - **Validates: Requirements 1.1, 1.3, 8.1**

  - [x] 3.3 Write property test: Config round-trip from environment variables
    - **Property 2: Config round-trip from environment variables**
    - **Validates: Requirements 1.4**

  - [x] 3.4 Write property test: Environment variable precedence over CDK context
    - **Property 3: Environment variable precedence over CDK context**
    - **Validates: Requirements 8.2**

  - [x] 3.5 Write property test: Invalid field rejection
    - **Property 4: Invalid field rejection**
    - **Validates: Requirements 2.1, 2.2, 2.3, 2.4**

  - [x] 3.6 Write property test: Subnet count must match AZ count
    - **Property 5: Subnet count must match AZ count**
    - **Validates: Requirements 2.5, 2.6**

  - [x] 3.7 Write property test: vpcCidr validation bypass for imported VPCs
    - **Property 8: vpcCidr validation bypass for imported VPCs**
    - **Validates: Requirements 6.1**

- [x] 4. Modify InfrastructureStack to support VPC import
  - [x] 4.1 Change `this.vpc` type from `ec2.Vpc` to `ec2.IVpc` and add VPC branch logic in `infrastructure/lib/infrastructure-stack.ts`
    - When `config.existingVpc` is present, call `ec2.Vpc.fromVpcAttributes()` and skip `new ec2.Vpc()`
    - When absent, create VPC as today
    - Assign result to `this.vpc` so downstream resources (ALB, ECS, SGs) work unchanged
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

  - [x] 4.2 Adjust SSM parameter exports for imported VPC path
    - For subnet IDs and AZs: use config values when imported, `this.vpc` properties when created
    - For VPC CIDR: use `config.existingVpc.vpcCidrBlock` if provided, else `this.vpc.vpcCidrBlock`
    - Ensure all 5 network SSM parameters are written for both paths
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6_

  - [x] 4.3 Write property test: Imported VPC skips VPC creation and preserves downstream resources
    - **Property 6: Imported VPC skips VPC creation and preserves downstream resources**
    - **Validates: Requirements 3.1, 3.2, 5.2, 5.3, 5.4**

  - [x] 4.4 Write property test: SSM network parameter completeness
    - **Property 7: SSM network parameter completeness**
    - **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5**

- [x] 5. Checkpoint - Ensure stack synth works for both VPC paths
  - Ensure all tests pass, ask the user if questions arise.

- [x] 6. Update CI/CD scripts for existing VPC environment variables
  - [x] 6.1 Update `scripts/common/load-env.sh`
    - Add exports for `CDK_EXISTING_VPC_ID`, `CDK_EXISTING_VPC_AZS`, `CDK_EXISTING_VPC_PUBLIC_SUBNET_IDS`, `CDK_EXISTING_VPC_PRIVATE_SUBNET_IDS`, `CDK_EXISTING_VPC_CIDR` with fallback to context file
    - Add corresponding entries to `build_cdk_context_params()` that conditionally include `existingVpc.*` context keys
    - Add display lines in the config output section
    - _Requirements: 7.1, 7.2, 7.4_

  - [x] 6.2 Update `scripts/stack-infrastructure/synth.sh` and `scripts/stack-infrastructure/deploy.sh`
    - Add conditional `--context existingVpc.*` parameters when environment variables are set
    - Ensure both scripts have identical context parameters
    - _Requirements: 7.3_

- [x] 7. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- The design uses TypeScript throughout (CDK + Jest + fast-check)
- `fast-check` must be installed as a devDependency before property tests can run (task 3.1)
- Properties 6 and 7 involve CDK stack synthesis which is slower; iteration count may be adjusted
- Downstream stacks (app-api, inference-api, gateway, frontend) require zero code changes
- Each property test references its design document property number for traceability
