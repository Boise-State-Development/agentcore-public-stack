import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags } from './config';

export interface InfrastructureStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Infrastructure Stack - Shared Network Resources
 * 
 * This stack creates foundational resources shared by all application stacks:
 * - VPC with public/private subnets across multiple AZs
 * - Application Load Balancer (ALB) in public subnets
 * - ECS Cluster for application workloads
 * - Security groups for ALB and ECS
 * - SSM parameters for cross-stack references
 * 
 * This stack should be deployed FIRST, before any application stacks.
 */
export class InfrastructureStack extends cdk.Stack {
  public readonly vpc: ec2.Vpc;
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly albListener: elbv2.ApplicationListener;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly ecsCluster: ecs.Cluster;

  constructor(scope: Construct, id: string, props: InfrastructureStackProps) {
    super(scope, id, props);

    const { config } = props;

    // Apply standard tags
    applyStandardTags(this, config);

    // ============================================================
    // VPC - Network Foundation
    // ============================================================
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
    new ssm.StringParameter(this, 'VpcIdParameter', {
      parameterName: `/${config.projectPrefix}/network/vpc-id`,
      stringValue: this.vpc.vpcId,
      description: 'Shared VPC ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export VPC CIDR to SSM
    new ssm.StringParameter(this, 'VpcCidrParameter', {
      parameterName: `/${config.projectPrefix}/network/vpc-cidr`,
      stringValue: this.vpc.vpcCidrBlock,
      description: 'Shared VPC CIDR block',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export Private Subnet IDs to SSM
    const privateSubnetIds = this.vpc.privateSubnets.map(subnet => subnet.subnetId).join(',');
    new ssm.StringParameter(this, 'PrivateSubnetIdsParameter', {
      parameterName: `/${config.projectPrefix}/network/private-subnet-ids`,
      stringValue: privateSubnetIds,
      description: 'Comma-separated list of private subnet IDs',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export Public Subnet IDs to SSM
    const publicSubnetIds = this.vpc.publicSubnets.map(subnet => subnet.subnetId).join(',');
    new ssm.StringParameter(this, 'PublicSubnetIdsParameter', {
      parameterName: `/${config.projectPrefix}/network/public-subnet-ids`,
      stringValue: publicSubnetIds,
      description: 'Comma-separated list of public subnet IDs',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export Availability Zones to SSM
    const availabilityZones = this.vpc.availabilityZones.join(',');
    new ssm.StringParameter(this, 'AvailabilityZonesParameter', {
      parameterName: `/${config.projectPrefix}/network/availability-zones`,
      stringValue: availabilityZones,
      description: 'Comma-separated list of availability zones',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Security Groups
    // ============================================================
    
    // ALB Security Group - Allow HTTP/HTTPS from internet
    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: getResourceName(config, 'alb-sg'),
      description: 'Security group for Application Load Balancer',
      allowAllOutbound: true,
    });

    this.albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(80),
      'Allow HTTP traffic from internet'
    );

    this.albSecurityGroup.addIngressRule(
      ec2.Peer.anyIpv4(),
      ec2.Port.tcp(443),
      'Allow HTTPS traffic from internet'
    );

    // Export ALB Security Group ID to SSM
    new ssm.StringParameter(this, 'AlbSecurityGroupIdParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-security-group-id`,
      stringValue: this.albSecurityGroup.securityGroupId,
      description: 'ALB Security Group ID',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Application Load Balancer
    // ============================================================
    this.alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc: this.vpc,
      internetFacing: true,
      loadBalancerName: getResourceName(config, 'alb'),
      securityGroup: this.albSecurityGroup,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PUBLIC,
      },
    });

    // Export ALB ARN to SSM
    new ssm.StringParameter(this, 'AlbArnParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-arn`,
      stringValue: this.alb.loadBalancerArn,
      description: 'Application Load Balancer ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export ALB DNS name to SSM
    new ssm.StringParameter(this, 'AlbDnsNameParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-dns-name`,
      stringValue: this.alb.loadBalancerDnsName,
      description: 'Application Load Balancer DNS name',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Create default HTTP listener
    this.albListener = this.alb.addListener('HttpListener', {
      port: 80,
      protocol: elbv2.ApplicationProtocol.HTTP,
      defaultAction: elbv2.ListenerAction.fixedResponse(404, {
        contentType: 'text/plain',
        messageBody: 'Not Found - No matching route',
      }),
    });

    // Export ALB Listener ARN to SSM
    new ssm.StringParameter(this, 'AlbListenerArnParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-listener-arn`,
      stringValue: this.albListener.listenerArn,
      description: 'Application Load Balancer HTTP Listener ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // ECS Cluster
    // ============================================================
    this.ecsCluster = new ecs.Cluster(this, 'EcsCluster', {
      clusterName: getResourceName(config, 'ecs-cluster'),
      vpc: this.vpc,
      containerInsightsV2: ecs.ContainerInsights.ENABLED, // Enable CloudWatch Container Insights
    });

    // Export ECS Cluster Name to SSM
    new ssm.StringParameter(this, 'EcsClusterNameParameter', {
      parameterName: `/${config.projectPrefix}/network/ecs-cluster-name`,
      stringValue: this.ecsCluster.clusterName,
      description: 'ECS Cluster Name',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export ECS Cluster ARN to SSM
    new ssm.StringParameter(this, 'EcsClusterArnParameter', {
      parameterName: `/${config.projectPrefix}/network/ecs-cluster-arn`,
      stringValue: this.ecsCluster.clusterArn,
      description: 'ECS Cluster ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Route53 Hosted Zone (Optional)
    // ============================================================
    if (config.infrastructureHostedZoneDomain) {
      const hostedZone = new route53.PublicHostedZone(this, 'HostedZone', {
        zoneName: config.infrastructureHostedZoneDomain,
        comment: `Hosted zone for ${config.projectPrefix}`,
      });

      // Export Hosted Zone ID to SSM
      new ssm.StringParameter(this, 'HostedZoneIdParameter', {
        parameterName: `/${config.projectPrefix}/network/hosted-zone-id`,
        stringValue: hostedZone.hostedZoneId,
        description: 'Route53 Hosted Zone ID',
        tier: ssm.ParameterTier.STANDARD,
      });

      // Export Hosted Zone Name to SSM
      new ssm.StringParameter(this, 'HostedZoneNameParameter', {
        parameterName: `/${config.projectPrefix}/network/hosted-zone-name`,
        stringValue: hostedZone.zoneName,
        description: 'Route53 Hosted Zone Name',
        tier: ssm.ParameterTier.STANDARD,
      });

      // CloudFormation Output for Hosted Zone
      new cdk.CfnOutput(this, 'HostedZoneId', {
        value: hostedZone.hostedZoneId,
        description: 'Route53 Hosted Zone ID',
        exportName: `${config.projectPrefix}-hosted-zone-id`,
      });

      new cdk.CfnOutput(this, 'HostedZoneNameServers', {
        value: cdk.Fn.join(', ', hostedZone.hostedZoneNameServers || []),
        description: 'Route53 Hosted Zone Name Servers',
        exportName: `${config.projectPrefix}-hosted-zone-ns`,
      });
    }

    // ============================================================
    // CloudFormation Outputs
    // ============================================================
    new cdk.CfnOutput(this, 'VpcId', {
      value: this.vpc.vpcId,
      description: 'VPC ID',
      exportName: `${config.projectPrefix}-vpc-id`,
    });

    new cdk.CfnOutput(this, 'AlbDnsName', {
      value: this.alb.loadBalancerDnsName,
      description: 'Application Load Balancer DNS Name',
      exportName: `${config.projectPrefix}-alb-dns-name`,
    });

    new cdk.CfnOutput(this, 'EcsClusterName', {
      value: this.ecsCluster.clusterName,
      description: 'ECS Cluster Name',
      exportName: `${config.projectPrefix}-ecs-cluster-name`,
    });
  }
}
