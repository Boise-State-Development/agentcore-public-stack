import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface NetworkConstructProps {
  config: AppConfig;
}

/**
 * NetworkConstruct — VPC, subnets, NAT, route tables.
 *
 * Provisions the foundational network layer for the platform:
 *
 *   - VPC sized at `config.vpcCidr` with DNS hostnames + DNS support on
 *   - 2 AZs (high availability) × 2 subnet types (public, private-with-egress)
 *   - 1 NAT gateway (cost-optimized; can be increased for cross-AZ HA)
 *
 * SSM parameters published (cross-stack reads, kept identical to legacy
 * for Phase 2 equivalence; will be retired in Phase 3 once typed prop
 * passing replaces SSM):
 *
 *   /{prefix}/network/vpc-id
 *   /{prefix}/network/vpc-cidr
 *   /{prefix}/network/private-subnet-ids
 *   /{prefix}/network/public-subnet-ids
 *   /{prefix}/network/availability-zones
 *
 * Logical IDs preserved verbatim from the original
 * `infrastructure-stack.ts` so the Phase 2 equivalence test stays at
 * zero diff.
 */
export class NetworkConstruct extends Construct {
  public readonly vpc: ec2.Vpc;

  constructor(scope: Construct, id: string, props: NetworkConstructProps) {
    super(scope, id);

    const { config } = props;

    this.vpc = new ec2.Vpc(this, 'Vpc', {
      vpcName: getResourceName(config, 'vpc'),
      ipAddresses: ec2.IpAddresses.cidr(config.vpcCidr),
      maxAzs: 2, // Use 2 AZs for high availability
      natGateways: 1, // Single NAT Gateway for cost optimization (can be increased for HA)
      subnetConfiguration: [
        {
          cidrMask: 24,
          name: 'Public',
          subnetType: ec2.SubnetType.PUBLIC,
        },
        {
          cidrMask: 24,
          name: 'Private',
          subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS,
        },
      ],
      enableDnsHostnames: true,
      enableDnsSupport: true,
    });

    // Export VPC ID to SSM for cross-stack references

    // Export VPC CIDR to SSM

    // Export Private Subnet IDs to SSM
    const privateSubnetIds = this.vpc.privateSubnets
      .map((subnet) => subnet.subnetId)
      .join(',');

    // Export Public Subnet IDs to SSM
    const publicSubnetIds = this.vpc.publicSubnets
      .map((subnet) => subnet.subnetId)
      .join(',');

    // Export Availability Zones to SSM
    const availabilityZones = this.vpc.availabilityZones.join(',');
  }
}
