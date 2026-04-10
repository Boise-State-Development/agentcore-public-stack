import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fc from 'fast-check';
import { InfrastructureStack } from '../lib/infrastructure-stack';
import { createMockConfig, mockEnv } from './helpers/mock-config';
import { ExistingVpcConfig } from '../lib/config';

/**
 * Property-Based Tests for InfrastructureStack with Existing VPC
 *
 * Feature: existing-vpc-support
 *
 * These tests use fast-check to verify that the InfrastructureStack behaves
 * correctly when synthesized with an imported VPC configuration across a
 * wide range of generated inputs.
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

/** Generates n unique subnet IDs */
function uniqueSubnetIds(n: number, prefix: string): fc.Arbitrary<string[]> {
  return fc
    .uniqueArray(fc.stringMatching(/^[a-z0-9]{1,17}$/), { minLength: n, maxLength: n })
    .map((ids) => ids.map((s) => `subnet-${prefix}${s}`));
}

/** Generates a complete valid ExistingVpcConfig with matching counts and unique subnet IDs */
function validExistingVpcConfig(): fc.Arbitrary<ExistingVpcConfig> {
  return fc
    .integer({ min: 2, max: 6 })
    .chain((azCount) =>
      fc.tuple(
        validVpcId(),
        validAzList(azCount),
        uniqueSubnetIds(azCount, 'pub'),
        uniqueSubnetIds(azCount, 'priv'),
        // Always include vpcCidrBlock to avoid CDK error when accessing vpc.vpcCidrBlock
        // on an imported VPC (fromVpcAttributes does not support vpcCidrBlock lookup)
        fc.constantFrom('10.0.0.0/16', '172.16.0.0/12', '192.168.0.0/24'),
      ),
    )
    .map(([vpcId, azs, publicSubnets, privateSubnets, cidr]) => ({
      vpcId,
      availabilityZones: azs,
      publicSubnetIds: publicSubnets,
      privateSubnetIds: privateSubnets,
      vpcCidrBlock: cidr,
    }));
}

// ============================================================
// Property Tests
// ============================================================

describe('InfrastructureStack Property Tests', () => {
  // ----------------------------------------------------------
  // Property 6: Imported VPC skips VPC creation and preserves downstream resources
  // Feature: existing-vpc-support, Property 6: Imported VPC skips VPC creation and preserves downstream resources
  // **Validates: Requirements 3.1, 3.2, 5.2, 5.3, 5.4**
  // ----------------------------------------------------------
  it('Property 6: imported VPC produces zero VPC/Subnet/NAT resources and preserves ALB, ECS, SecurityGroup', () => {
    fc.assert(
      fc.property(validExistingVpcConfig(), (vpc) => {
        const app = new cdk.App();
        const config = createMockConfig({ existingVpc: vpc });
        const stack = new InfrastructureStack(app, 'TestStack', {
          config,
          env: mockEnv(config),
        });
        const template = Template.fromStack(stack);

        // No VPC, Subnet, or NAT Gateway resources should be created
        template.resourceCountIs('AWS::EC2::VPC', 0);
        template.resourceCountIs('AWS::EC2::Subnet', 0);
        template.resourceCountIs('AWS::EC2::NatGateway', 0);

        // Downstream resources must still exist
        const albs = template.findResources('AWS::ElasticLoadBalancingV2::LoadBalancer');
        expect(Object.keys(albs).length).toBeGreaterThanOrEqual(1);

        const clusters = template.findResources('AWS::ECS::Cluster');
        expect(Object.keys(clusters).length).toBeGreaterThanOrEqual(1);

        const securityGroups = template.findResources('AWS::EC2::SecurityGroup');
        expect(Object.keys(securityGroups).length).toBeGreaterThanOrEqual(1);
      }),
      // CDK stack synthesis is relatively slow; 50 iterations keeps test time reasonable
      { numRuns: 50 },
    );
  }, 120_000);

  // ----------------------------------------------------------
  // Property 7: SSM network parameter completeness
  // Feature: existing-vpc-support, Property 7: SSM network parameter completeness
  // **Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5**
  // ----------------------------------------------------------
  it('Property 7: imported VPC produces all 5 network SSM parameters', () => {
    const EXPECTED_SUFFIXES = [
      'vpc-id',
      'private-subnet-ids',
      'public-subnet-ids',
      'availability-zones',
      'vpc-cidr',
    ];

    fc.assert(
      fc.property(validExistingVpcConfig(), (vpc) => {
        const app = new cdk.App();
        const config = createMockConfig({ existingVpc: vpc });
        const stack = new InfrastructureStack(app, 'TestStack', {
          config,
          env: mockEnv(config),
        });
        const template = Template.fromStack(stack);

        const ssmParams = template.findResources('AWS::SSM::Parameter');
        const paramNames = Object.values(ssmParams).map(
          (r: any) => r.Properties.Name as string,
        );

        for (const suffix of EXPECTED_SUFFIXES) {
          const found = paramNames.some((name) => name.includes(`/network/${suffix}`));
          expect(found).toBe(true);
        }
      }),
      // CDK stack synthesis is relatively slow; 50 iterations keeps test time reasonable
      { numRuns: 50 },
    );
  }, 120_000);
});
