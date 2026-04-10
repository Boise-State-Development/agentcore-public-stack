# Requirements Document

## Introduction

This feature adds support for importing an existing VPC into the InfrastructureStack instead of always creating a new one. Organizations that operate hub-and-spoke network topologies, shared VPCs, or centralized NAT architectures need to deploy the application into a pre-provisioned VPC rather than letting CDK create one from scratch. When an optional `existingVpc` configuration block is present, the stack imports the VPC via `Vpc.fromVpcAttributes()` and skips VPC creation. When absent, behavior remains identical to today. Downstream stacks (app-api, inference-api, gateway, frontend) require zero changes because they already consume network resources via SSM parameters.

## Glossary

- **Infrastructure_Stack**: The CDK stack (`InfrastructureStack`) that provisions foundational shared resources including VPC, ALB, ECS Cluster, security groups, DynamoDB tables, and SSM parameters.
- **Config_Loader**: The `loadConfig()` function in `infrastructure/lib/config.ts` that reads CDK context and environment variables to produce an `AppConfig` object.
- **Existing_VPC_Config**: The optional `existingVpc` configuration block within `AppConfig` that contains all attributes required to import a pre-existing VPC.
- **VPC_Importer**: The code path within Infrastructure_Stack that calls `Vpc.fromVpcAttributes()` to import an existing VPC instead of creating a new one.
- **SSM_Exporter**: The set of `ssm.StringParameter` constructs in Infrastructure_Stack that publish VPC ID, subnet IDs, availability zones, and CIDR to SSM Parameter Store for cross-stack consumption.
- **Downstream_Stack**: Any CDK stack (AppApiStack, InferenceApiStack, GatewayStack, FrontendStack) that imports network resources from SSM parameters written by Infrastructure_Stack.
- **Hub_And_Spoke_Topology**: A network architecture where a spoke VPC routes egress traffic through a Transit Gateway to a centralized security VPC that hosts shared NAT gateways.
- **Standalone_Topology**: A network architecture where the VPC has its own NAT gateways providing internet egress for private subnets.

## Requirements

### Requirement 1: Optional Existing VPC Configuration Block

**User Story:** As a platform engineer, I want to provide an optional `existingVpc` configuration block in CDK context, so that I can import a pre-existing VPC instead of creating a new one.

#### Acceptance Criteria

1. THE Config_Loader SHALL accept an optional `existingVpc` object in CDK context containing `vpcId`, `availabilityZones`, `publicSubnetIds`, `privateSubnetIds`, and optionally `vpcCidrBlock`.
2. WHEN the `existingVpc` block is absent from CDK context, THE Config_Loader SHALL return an `AppConfig` with `existingVpc` set to `undefined`.
3. WHEN the `existingVpc` block is present, THE Config_Loader SHALL parse all provided fields into a typed `ExistingVpcConfig` object.
4. THE Config_Loader SHALL support loading `existingVpc` fields from environment variables with the `CDK_EXISTING_VPC_` prefix as an alternative to CDK context.

### Requirement 2: Existing VPC Configuration Validation

**User Story:** As a platform engineer, I want the CDK stack to validate my existing VPC configuration at synth time, so that I catch misconfigurations before deployment.

#### Acceptance Criteria

1. WHEN the `existingVpc` block is present, THE Config_Loader SHALL validate that `vpcId` matches the pattern `vpc-[a-z0-9]+`.
2. WHEN the `existingVpc` block is present, THE Config_Loader SHALL validate that `availabilityZones` contains between 2 and 6 entries.
3. WHEN the `existingVpc` block is present, THE Config_Loader SHALL validate that `publicSubnetIds` contains at least 2 entries and each entry matches the pattern `subnet-[a-z0-9]+`.
4. WHEN the `existingVpc` block is present, THE Config_Loader SHALL validate that `privateSubnetIds` contains at least 2 entries and each entry matches the pattern `subnet-[a-z0-9]+`.
5. WHEN the `existingVpc` block is present, THE Config_Loader SHALL validate that the count of `publicSubnetIds` equals the count of `availabilityZones`.
6. WHEN the `existingVpc` block is present, THE Config_Loader SHALL validate that the count of `privateSubnetIds` equals the count of `availabilityZones`.
7. IF any validation check fails, THEN THE Config_Loader SHALL throw an error with a descriptive message identifying the failing field and expected format.

### Requirement 3: VPC Import via fromVpcAttributes

**User Story:** As a platform engineer, I want the InfrastructureStack to import my existing VPC using `Vpc.fromVpcAttributes()`, so that CDK does not create duplicate network resources.

#### Acceptance Criteria

1. WHEN the `existingVpc` configuration is present, THE Infrastructure_Stack SHALL call `Vpc.fromVpcAttributes()` with the provided `vpcId`, `availabilityZones`, `publicSubnetIds`, and `privateSubnetIds`.
2. WHEN the `existingVpc` configuration is present, THE Infrastructure_Stack SHALL skip creation of the `new ec2.Vpc()` construct entirely.
3. WHEN the `existingVpc` configuration is absent, THE Infrastructure_Stack SHALL create a new VPC using `new ec2.Vpc()` with the existing `vpcCidr`, `maxAzs`, and `natGateways` settings.
4. THE Infrastructure_Stack SHALL assign the imported or created VPC to the same `this.vpc` property so that all downstream resource creation (ALB, ECS Cluster, security groups) uses the VPC without conditional branching.

### Requirement 4: SSM Parameter Export Consistency

**User Story:** As a platform engineer, I want the same SSM parameters to be published regardless of whether the VPC is created or imported, so that downstream stacks work without modification.

#### Acceptance Criteria

1. THE SSM_Exporter SHALL write the VPC ID to `/${projectPrefix}/network/vpc-id` for both created and imported VPCs.
2. THE SSM_Exporter SHALL write comma-separated private subnet IDs to `/${projectPrefix}/network/private-subnet-ids` for both created and imported VPCs.
3. THE SSM_Exporter SHALL write comma-separated public subnet IDs to `/${projectPrefix}/network/public-subnet-ids` for both created and imported VPCs.
4. THE SSM_Exporter SHALL write comma-separated availability zones to `/${projectPrefix}/network/availability-zones` for both created and imported VPCs.
5. WHEN the `existingVpc` configuration includes `vpcCidrBlock`, THE SSM_Exporter SHALL write the provided CIDR to `/${projectPrefix}/network/vpc-cidr`.
6. WHEN the `existingVpc` configuration omits `vpcCidrBlock`, THE SSM_Exporter SHALL write the value from `vpc.vpcCidrBlock` to `/${projectPrefix}/network/vpc-cidr`.

### Requirement 5: Downstream Stack Compatibility

**User Story:** As a platform engineer, I want downstream stacks to continue working without any code changes when I switch between a new VPC and an imported VPC, so that the migration is non-disruptive.

#### Acceptance Criteria

1. THE Downstream_Stack instances (AppApiStack, InferenceApiStack, GatewayStack, FrontendStack) SHALL import network resources exclusively via SSM parameters and require zero code changes.
2. THE Infrastructure_Stack SHALL place the ALB in public subnets of the imported VPC using the same `vpcSubnets` selection as the created VPC path.
3. THE Infrastructure_Stack SHALL create the ECS Cluster within the imported VPC using the same configuration as the created VPC path.
4. THE Infrastructure_Stack SHALL create security groups within the imported VPC using the same rules as the created VPC path.

### Requirement 6: VPC CIDR Validation Bypass for Imported VPCs

**User Story:** As a platform engineer, I want the `vpcCidr` validation to be skipped when I import an existing VPC, so that I am not forced to provide a CIDR that is only used for VPC creation.

#### Acceptance Criteria

1. WHEN the `existingVpc` configuration is present, THE Config_Loader SHALL skip validation of the `vpcCidr` field.
2. WHEN the `existingVpc` configuration is absent, THE Config_Loader SHALL continue to validate the `vpcCidr` field as it does today.

### Requirement 7: CI/CD Pipeline Configuration Support

**User Story:** As a DevOps engineer, I want to configure the existing VPC via GitHub Actions environment variables, so that I can manage VPC configuration through the standard CI/CD pipeline.

#### Acceptance Criteria

1. THE load-env.sh script SHALL export `CDK_EXISTING_VPC_ID`, `CDK_EXISTING_VPC_AZS`, `CDK_EXISTING_VPC_PUBLIC_SUBNET_IDS`, `CDK_EXISTING_VPC_PRIVATE_SUBNET_IDS`, and `CDK_EXISTING_VPC_CIDR` from environment variables with fallback to CDK context.
2. THE build_cdk_context_params function SHALL include `existingVpc.*` context parameters only when the corresponding environment variables are set and non-empty.
3. THE synth.sh and deploy.sh scripts for the infrastructure stack SHALL pass `existingVpc.*` context parameters when the environment variables are set.
4. WHEN none of the `CDK_EXISTING_VPC_*` environment variables are set, THE CI/CD pipeline SHALL behave identically to today with no existing VPC configuration passed.

### Requirement 8: CDK Context JSON Configuration Support

**User Story:** As a platform engineer, I want to configure the existing VPC directly in `cdk.context.json`, so that I can manage VPC configuration alongside other infrastructure settings.

#### Acceptance Criteria

1. THE Config_Loader SHALL read the `existingVpc` block from `cdk.context.json` when present.
2. WHEN both environment variables and CDK context provide existing VPC configuration, THE Config_Loader SHALL give precedence to environment variables.
3. THE `cdk.context.json` SHALL support the `existingVpc` block as a nested object with keys `vpcId`, `availabilityZones`, `publicSubnetIds`, `privateSubnetIds`, and `vpcCidrBlock`.
