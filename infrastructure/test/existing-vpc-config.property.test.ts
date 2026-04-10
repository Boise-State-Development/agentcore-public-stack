import * as cdk from 'aws-cdk-lib';
import * as fc from 'fast-check';
import { loadConfig, ExistingVpcConfig } from '../lib/config';

/**
 * Property-Based Tests for Existing VPC Configuration
 *
 * Feature: existing-vpc-support
 *
 * These tests use fast-check to verify that the ExistingVpcConfig loading,
 * validation, and precedence logic holds across a wide range of generated inputs.
 */

// ============================================================
// Custom Arbitraries
// ============================================================

/** Generates a valid VPC ID matching vpc-[a-z0-9]+ */
function validVpcId(): fc.Arbitrary<string> {
  return fc.stringMatching(/^[a-z0-9]{1,17}$/).map((s: string) => `vpc-${s}`);
}

/** Generates a valid subnet ID matching subnet-[a-z0-9]+ */
function validSubnetId(): fc.Arbitrary<string> {
  return fc.stringMatching(/^[a-z0-9]{1,17}$/).map((s: string) => `subnet-${s}`);
}

/** Generates an array of n valid AZ strings like us-east-1a */
function validAzList(n: number): fc.Arbitrary<string[]> {
  const azLetters = 'abcdef'.split('');
  const regions = ['us-east-1', 'us-west-2', 'eu-west-1', 'ap-southeast-1'];
  return fc
    .tuple(
      fc.constantFrom(...regions),
      fc.shuffledSubarray(azLetters, { minLength: n, maxLength: n }),
    )
    .map(([region, letters]) => letters.map((l) => `${region}${l}`));
}

/** Generates a complete valid ExistingVpcConfig with matching counts */
function validExistingVpcConfig(): fc.Arbitrary<ExistingVpcConfig> {
  return fc
    .integer({ min: 2, max: 6 })
    .chain((azCount) =>
      fc.tuple(
        validVpcId(),
        validAzList(azCount),
        fc.array(validSubnetId(), { minLength: azCount, maxLength: azCount }),
        fc.array(validSubnetId(), { minLength: azCount, maxLength: azCount }),
        fc.option(fc.constantFrom('10.0.0.0/16', '172.16.0.0/12', '192.168.0.0/24'), {
          nil: undefined,
        }),
      ),
    )
    .map(([vpcId, azs, publicSubnets, privateSubnets, cidr]) => ({
      vpcId,
      availabilityZones: azs,
      publicSubnetIds: publicSubnets,
      privateSubnetIds: privateSubnets,
      ...(cidr ? { vpcCidrBlock: cidr } : {}),
    }));
}

/** Generates an invalid VPC ID (does NOT match vpc-[a-z0-9]+, but is truthy so parseExistingVpcConfig assembles it) */
function invalidVpcId(): fc.Arbitrary<string> {
  return fc.oneof(
    fc.constant('vpc-'),
    fc.constant('VPC-abc123'),
    fc.constant('vpc_abc123'),
    fc.constant('abc123'),
    fc.constant('vpc-ABC'),
    fc.constant('vpc-abc-def'),
    fc.constant('ec2-abc123'),
  );
}

/** Generates an invalid subnet ID (does NOT match subnet-[a-z0-9]+, but is non-empty) */
function invalidSubnetId(): fc.Arbitrary<string> {
  return fc.oneof(
    fc.constant('subnet-'),
    fc.constant('SUBNET-abc123'),
    fc.constant('subnet_abc123'),
    fc.constant('abc123'),
    fc.constant('subnet-ABC'),
    fc.constant('sub-abc123'),
  );
}

// ============================================================
// Helpers
// ============================================================

/** Base CDK context required by loadConfig() for all tests */
function baseContext(): Record<string, unknown> {
  return {
    projectPrefix: 'test-project',
    awsAccount: '123456789012',
    awsRegion: 'us-east-1',
    production: false,
    retainDataOnDelete: false,
    vpcCidr: '10.0.0.0/16',
    corsOrigins: 'http://localhost:4200',
    frontend: { enabled: true, cloudFrontPriceClass: 'PriceClass_100' },
    appApi: { enabled: true, cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 2 },
    inferenceApi: {
      enabled: true,
      cpu: 256,
      memory: 512,
      desiredCount: 1,
      maxCapacity: 2,
      logLevel: 'INFO',
    },
    gateway: {
      enabled: true,
      apiType: 'REST',
      throttleRateLimit: 100,
      throttleBurstLimit: 50,
      enableWaf: false,
    },
    assistants: { enabled: true },
    fileUpload: {
      enabled: true,
      maxFileSizeBytes: 10485760,
      maxFilesPerMessage: 5,
      userQuotaBytes: 104857600,
      retentionDays: 30,
    },
    ragIngestion: {
      enabled: true,
      lambdaMemorySize: 3008,
      lambdaTimeout: 900,
      embeddingModel: 'amazon.titan-embed-text-v2',
      vectorDimension: 1024,
      vectorDistanceMetric: 'cosine',
    },
    fineTuning: { enabled: false },
    tags: { ManagedBy: 'CDK' },
  };
}

/** Create a CDK App with base context and optional existingVpc context */
function createApp(existingVpc?: ExistingVpcConfig): cdk.App {
  const ctx: Record<string, unknown> = { ...baseContext() };
  if (existingVpc) {
    ctx.existingVpc = existingVpc;
  }
  return new cdk.App({ context: ctx });
}

/** Environment variable keys used for existing VPC config */
const ENV_KEYS = [
  'CDK_EXISTING_VPC_ID',
  'CDK_EXISTING_VPC_AZS',
  'CDK_EXISTING_VPC_PUBLIC_SUBNET_IDS',
  'CDK_EXISTING_VPC_PRIVATE_SUBNET_IDS',
  'CDK_EXISTING_VPC_CIDR',
] as const;

/** Clean up all existing VPC env vars */
function cleanEnvVars(): void {
  for (const key of ENV_KEYS) {
    delete process.env[key];
  }
}

/** Set env vars from an ExistingVpcConfig */
function setEnvVars(vpc: ExistingVpcConfig): void {
  process.env.CDK_EXISTING_VPC_ID = vpc.vpcId;
  process.env.CDK_EXISTING_VPC_AZS = vpc.availabilityZones.join(',');
  process.env.CDK_EXISTING_VPC_PUBLIC_SUBNET_IDS = vpc.publicSubnetIds.join(',');
  process.env.CDK_EXISTING_VPC_PRIVATE_SUBNET_IDS = vpc.privateSubnetIds.join(',');
  if (vpc.vpcCidrBlock) {
    process.env.CDK_EXISTING_VPC_CIDR = vpc.vpcCidrBlock;
  }
}

// ============================================================
// Property Tests
// ============================================================

describe('Existing VPC Config Property Tests', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    cleanEnvVars();
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  // ----------------------------------------------------------
  // Property 1: Config round-trip from CDK context
  // Feature: existing-vpc-support, Property 1: Config round-trip from CDK context
  // **Validates: Requirements 1.1, 1.3, 8.1**
  // ----------------------------------------------------------
  it('Property 1: valid ExistingVpcConfig set as CDK context round-trips through loadConfig()', () => {
    fc.assert(
      fc.property(validExistingVpcConfig(), (vpc) => {
        const app = createApp(vpc);
        const config = loadConfig(app);

        expect(config.existingVpc).toBeDefined();
        expect(config.existingVpc!.vpcId).toBe(vpc.vpcId);
        expect(config.existingVpc!.availabilityZones).toEqual(vpc.availabilityZones);
        expect(config.existingVpc!.publicSubnetIds).toEqual(vpc.publicSubnetIds);
        expect(config.existingVpc!.privateSubnetIds).toEqual(vpc.privateSubnetIds);
        if (vpc.vpcCidrBlock) {
          expect(config.existingVpc!.vpcCidrBlock).toBe(vpc.vpcCidrBlock);
        }
      }),
      { numRuns: 100 },
    );
  });

  // ----------------------------------------------------------
  // Property 2: Config round-trip from environment variables
  // Feature: existing-vpc-support, Property 2: Config round-trip from environment variables
  // **Validates: Requirements 1.4**
  // ----------------------------------------------------------
  it('Property 2: valid ExistingVpcConfig set as env vars round-trips through loadConfig()', () => {
    fc.assert(
      fc.property(validExistingVpcConfig(), (vpc) => {
        try {
          setEnvVars(vpc);
          const app = createApp(); // no existingVpc in context
          const config = loadConfig(app);

          expect(config.existingVpc).toBeDefined();
          expect(config.existingVpc!.vpcId).toBe(vpc.vpcId);
          expect(config.existingVpc!.availabilityZones).toEqual(vpc.availabilityZones);
          expect(config.existingVpc!.publicSubnetIds).toEqual(vpc.publicSubnetIds);
          expect(config.existingVpc!.privateSubnetIds).toEqual(vpc.privateSubnetIds);
          if (vpc.vpcCidrBlock) {
            expect(config.existingVpc!.vpcCidrBlock).toBe(vpc.vpcCidrBlock);
          }
        } finally {
          cleanEnvVars();
        }
      }),
      { numRuns: 100 },
    );
  });

  // ----------------------------------------------------------
  // Property 3: Environment variable precedence over CDK context
  // Feature: existing-vpc-support, Property 3: Environment variable precedence over CDK context
  // **Validates: Requirements 8.2**
  // ----------------------------------------------------------
  it('Property 3: env vars take precedence over CDK context for existingVpc', () => {
    fc.assert(
      fc.property(
        validExistingVpcConfig(),
        validExistingVpcConfig(),
        (envVpc, ctxVpc) => {
          // Skip if both configs happen to be identical (can't verify precedence)
          fc.pre(envVpc.vpcId !== ctxVpc.vpcId);

          try {
            setEnvVars(envVpc);
            const app = createApp(ctxVpc);
            const config = loadConfig(app);

            expect(config.existingVpc).toBeDefined();
            // Env vars should win
            expect(config.existingVpc!.vpcId).toBe(envVpc.vpcId);
            expect(config.existingVpc!.availabilityZones).toEqual(envVpc.availabilityZones);
            expect(config.existingVpc!.publicSubnetIds).toEqual(envVpc.publicSubnetIds);
            expect(config.existingVpc!.privateSubnetIds).toEqual(envVpc.privateSubnetIds);
          } finally {
            cleanEnvVars();
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  // ----------------------------------------------------------
  // Property 4: Invalid field rejection
  // Feature: existing-vpc-support, Property 4: Invalid field rejection
  // **Validates: Requirements 2.1, 2.2, 2.3, 2.4**
  // ----------------------------------------------------------
  it('Property 4: config with at least one invalid field causes loadConfig() to throw', () => {
    // Strategy: generate a valid config then corrupt exactly one field
    const invalidConfigs = fc.oneof(
      // Invalid vpcId
      fc.tuple(invalidVpcId(), validExistingVpcConfig()).map(([badId, vpc]) => ({
        ...vpc,
        vpcId: badId,
      })),
      // AZ count < 2
      fc.tuple(validVpcId(), validSubnetId(), validSubnetId()).map(([vpcId, pub1, priv1]) => ({
        vpcId,
        availabilityZones: ['us-east-1a'],
        publicSubnetIds: [pub1],
        privateSubnetIds: [priv1],
      })),
      // AZ count > 6
      validVpcId().map((vpcId) => ({
        vpcId,
        availabilityZones: ['us-east-1a', 'us-east-1b', 'us-east-1c', 'us-east-1d', 'us-east-1e', 'us-east-1f', 'us-west-2a'],
        publicSubnetIds: Array.from({ length: 7 }, (_, i) => `subnet-pub${i}`),
        privateSubnetIds: Array.from({ length: 7 }, (_, i) => `subnet-priv${i}`),
      })),
      // Invalid public subnet ID
      fc.tuple(invalidSubnetId(), validExistingVpcConfig()).map(([badSubnet, vpc]) => ({
        ...vpc,
        publicSubnetIds: [badSubnet, ...vpc.publicSubnetIds.slice(1)],
      })),
      // Invalid private subnet ID
      fc.tuple(invalidSubnetId(), validExistingVpcConfig()).map(([badSubnet, vpc]) => ({
        ...vpc,
        privateSubnetIds: [badSubnet, ...vpc.privateSubnetIds.slice(1)],
      })),
      // Fewer than 2 public subnets
      fc.tuple(validVpcId(), validSubnetId()).map(([vpcId, sub]) => ({
        vpcId,
        availabilityZones: ['us-east-1a', 'us-east-1b'],
        publicSubnetIds: [sub],
        privateSubnetIds: ['subnet-aaa', 'subnet-bbb'],
      })),
      // Fewer than 2 private subnets
      fc.tuple(validVpcId(), validSubnetId()).map(([vpcId, sub]) => ({
        vpcId,
        availabilityZones: ['us-east-1a', 'us-east-1b'],
        publicSubnetIds: ['subnet-aaa', 'subnet-bbb'],
        privateSubnetIds: [sub],
      })),
    );

    fc.assert(
      fc.property(invalidConfigs, (vpc) => {
        const app = createApp(vpc as ExistingVpcConfig);
        expect(() => loadConfig(app)).toThrow();
      }),
      { numRuns: 100 },
    );
  });

  // ----------------------------------------------------------
  // Property 5: Subnet count must match AZ count
  // Feature: existing-vpc-support, Property 5: Subnet count must match AZ count
  // **Validates: Requirements 2.5, 2.6**
  // ----------------------------------------------------------
  it('Property 5: mismatched subnet/AZ counts cause loadConfig() to throw', () => {
    // Generate a valid config then add or remove a subnet to create a mismatch
    const mismatchedConfigs = fc.oneof(
      // Public subnet count != AZ count (extra public subnet)
      validExistingVpcConfig().chain((vpc) =>
        validSubnetId().map((extra) => ({
          ...vpc,
          publicSubnetIds: [...vpc.publicSubnetIds, extra],
        })),
      ),
      // Private subnet count != AZ count (extra private subnet)
      validExistingVpcConfig().chain((vpc) =>
        validSubnetId().map((extra) => ({
          ...vpc,
          privateSubnetIds: [...vpc.privateSubnetIds, extra],
        })),
      ),
      // Public subnet count != AZ count (one fewer public subnet, only when azCount > 2)
      validExistingVpcConfig()
        .filter((vpc) => vpc.availabilityZones.length > 2)
        .map((vpc) => ({
          ...vpc,
          publicSubnetIds: vpc.publicSubnetIds.slice(0, -1),
        })),
      // Private subnet count != AZ count (one fewer private subnet, only when azCount > 2)
      validExistingVpcConfig()
        .filter((vpc) => vpc.availabilityZones.length > 2)
        .map((vpc) => ({
          ...vpc,
          privateSubnetIds: vpc.privateSubnetIds.slice(0, -1),
        })),
    );

    fc.assert(
      fc.property(mismatchedConfigs, (vpc) => {
        const app = createApp(vpc as ExistingVpcConfig);
        expect(() => loadConfig(app)).toThrow();
      }),
      { numRuns: 100 },
    );
  });

  // ----------------------------------------------------------
  // Property 8: vpcCidr validation bypass for imported VPCs
  // Feature: existing-vpc-support, Property 8: vpcCidr validation bypass for imported VPCs
  // **Validates: Requirements 6.1**
  // ----------------------------------------------------------
  it('Property 8: loadConfig() does not throw VPC CIDR error when existingVpc is present', () => {
    const badCidrs = fc.oneof(
      fc.constant(''),
      fc.constant('not-a-cidr'),
      fc.constant('999.999.999.999/99'),
      fc.constant('abc'),
      fc.constant('10.0.0/16'),
      fc.constant('10.0.0.0'),
    );

    fc.assert(
      fc.property(validExistingVpcConfig(), badCidrs, (vpc, badCidr) => {
        const ctx: Record<string, unknown> = { ...baseContext(), vpcCidr: badCidr };
        ctx.existingVpc = vpc;
        const app = new cdk.App({ context: ctx });

        // Should NOT throw a VPC CIDR validation error
        const config = loadConfig(app);
        expect(config.existingVpc).toBeDefined();
        expect(config.existingVpc!.vpcId).toBe(vpc.vpcId);
      }),
      { numRuns: 100 },
    );
  });
});
