# Networking Patterns

## VPC Configuration

```typescript
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import { getResourceName } from './config';

const vpc = new ec2.Vpc(this, 'Vpc', {
  vpcName: getResourceName(config, 'vpc'),
  ipAddresses: ec2.IpAddresses.cidr(config.vpcCidr),  // e.g., '10.0.0.0/16'
  maxAzs: 2,              // High availability across 2 AZs
  natGateways: 1,         // Single NAT for cost (increase for HA)

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
```

## Security Groups

### ALB Security Group
```typescript
const albSg = new ec2.SecurityGroup(this, 'AlbSg', {
  vpc: vpc,
  securityGroupName: getResourceName(config, 'alb-sg'),
  description: 'Security group for Application Load Balancer',
  allowAllOutbound: true,
});

// Allow HTTP from internet
albSg.addIngressRule(
  ec2.Peer.anyIpv4(),
  ec2.Port.tcp(80),
  'Allow HTTP from anywhere'
);

// Allow HTTPS from internet
albSg.addIngressRule(
  ec2.Peer.anyIpv4(),
  ec2.Port.tcp(443),
  'Allow HTTPS from anywhere'
);
```

### ECS Security Group
```typescript
const ecsSg = new ec2.SecurityGroup(this, 'EcsSg', {
  vpc: vpc,
  securityGroupName: getResourceName(config, 'ecs-sg'),
  description: 'Security group for ECS tasks',
  allowAllOutbound: true,
});

// Only allow traffic from ALB
ecsSg.addIngressRule(
  albSg,
  ec2.Port.tcp(8000),
  'Allow traffic from ALB'
);
```

### Lambda Security Group
```typescript
const lambdaSg = new ec2.SecurityGroup(this, 'LambdaSg', {
  vpc: vpc,
  securityGroupName: getResourceName(config, 'lambda-sg'),
  description: 'Security group for Lambda functions',
  allowAllOutbound: true,
});
```

### Database Security Group
```typescript
const dbSg = new ec2.SecurityGroup(this, 'DatabaseSg', {
  vpc: vpc,
  securityGroupName: getResourceName(config, 'database-sg'),
  description: 'Security group for databases',
  allowAllOutbound: false,  // Databases don't need outbound
});

// Allow from ECS tasks
dbSg.addIngressRule(
  ecsSg,
  ec2.Port.tcp(5432),
  'Allow PostgreSQL from ECS'
);

// Allow from Lambda
dbSg.addIngressRule(
  lambdaSg,
  ec2.Port.tcp(5432),
  'Allow PostgreSQL from Lambda'
);
```

## Application Load Balancer

```typescript
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';

const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
  vpc: vpc,
  loadBalancerName: getResourceName(config, 'alb'),
  internetFacing: true,
  securityGroup: albSg,
  vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
});
```

## ALB with HTTPS

```typescript
import * as acm from 'aws-cdk-lib/aws-certificatemanager';

// Import certificate
const certificate = acm.Certificate.fromCertificateArn(
  this,
  'Certificate',
  config.certificateArn
);

// HTTPS listener (443)
const httpsListener = alb.addListener('HttpsListener', {
  port: 443,
  protocol: elbv2.ApplicationProtocol.HTTPS,
  certificates: [certificate],
  defaultAction: elbv2.ListenerAction.fixedResponse(404, {
    contentType: 'text/plain',
    messageBody: 'Not Found',
  }),
});

// HTTP redirect to HTTPS (80 -> 443)
const httpListener = alb.addListener('HttpListener', {
  port: 80,
  protocol: elbv2.ApplicationProtocol.HTTP,
  defaultAction: elbv2.ListenerAction.redirect({
    protocol: 'HTTPS',
    port: '443',
    permanent: true,
  }),
});
```

## Target Groups

```typescript
const targetGroup = new elbv2.ApplicationTargetGroup(this, 'AppTargetGroup', {
  vpc: vpc,
  targetGroupName: getResourceName(config, 'app-tg'),
  port: 8000,
  protocol: elbv2.ApplicationProtocol.HTTP,
  targetType: elbv2.TargetType.IP,

  healthCheck: {
    enabled: true,
    path: '/health',
    interval: cdk.Duration.seconds(30),
    timeout: cdk.Duration.seconds(5),
    healthyThresholdCount: 2,
    unhealthyThresholdCount: 3,
    healthyHttpCodes: '200',
  },

  deregistrationDelay: cdk.Duration.seconds(30),
});
```

## Listener Rules

```typescript
// Add target group with path-based routing
httpsListener.addTargetGroups('AppTarget', {
  targetGroups: [appTargetGroup],
  priority: 1,  // Lower = higher priority
  conditions: [
    elbv2.ListenerCondition.pathPatterns(['/api/*']),
  ],
});

// Host-based routing
httpsListener.addTargetGroups('ApiSubdomain', {
  targetGroups: [apiTargetGroup],
  priority: 2,
  conditions: [
    elbv2.ListenerCondition.hostHeaders(['api.example.com']),
  ],
});
```

## Route53 Integration

```typescript
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';

// Create or import hosted zone
const hostedZone = new route53.PublicHostedZone(this, 'HostedZone', {
  zoneName: config.domainName,
  comment: `Hosted zone for ${config.projectPrefix}`,
});

// A record for ALB
new route53.ARecord(this, 'AlbARecord', {
  zone: hostedZone,
  recordName: 'api',  // api.example.com
  target: route53.RecordTarget.fromAlias(
    new route53Targets.LoadBalancerTarget(alb)
  ),
});
```

## ECS Cluster

```typescript
import * as ecs from 'aws-cdk-lib/aws-ecs';

const cluster = new ecs.Cluster(this, 'EcsCluster', {
  clusterName: getResourceName(config, 'cluster'),
  vpc: vpc,
  containerInsights: true,  // Enable Container Insights
});
```

## VPC Endpoints (for private subnets)

```typescript
// ECR API endpoint
vpc.addInterfaceEndpoint('EcrApiEndpoint', {
  service: ec2.InterfaceVpcEndpointAwsService.ECR,
  privateDnsEnabled: true,
  subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
});

// ECR Docker endpoint
vpc.addInterfaceEndpoint('EcrDkrEndpoint', {
  service: ec2.InterfaceVpcEndpointAwsService.ECR_DOCKER,
  privateDnsEnabled: true,
  subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
});

// S3 gateway endpoint (free)
vpc.addGatewayEndpoint('S3Endpoint', {
  service: ec2.GatewayVpcEndpointAwsService.S3,
  subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
});

// DynamoDB gateway endpoint (free)
vpc.addGatewayEndpoint('DynamoDbEndpoint', {
  service: ec2.GatewayVpcEndpointAwsService.DYNAMODB,
  subnets: [{ subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS }],
});

// CloudWatch Logs endpoint
vpc.addInterfaceEndpoint('CloudWatchLogsEndpoint', {
  service: ec2.InterfaceVpcEndpointAwsService.CLOUDWATCH_LOGS,
  privateDnsEnabled: true,
  subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
});

// Secrets Manager endpoint
vpc.addInterfaceEndpoint('SecretsManagerEndpoint', {
  service: ec2.InterfaceVpcEndpointAwsService.SECRETS_MANAGER,
  privateDnsEnabled: true,
  subnets: { subnetType: ec2.SubnetType.PRIVATE_WITH_EGRESS },
});
```

## Exporting Network Resources to SSM

```typescript
// VPC
new ssm.StringParameter(this, 'VpcIdParam', {
  parameterName: `/${config.projectPrefix}/network/vpc-id`,
  stringValue: vpc.vpcId,
});

new ssm.StringParameter(this, 'VpcCidrParam', {
  parameterName: `/${config.projectPrefix}/network/vpc-cidr`,
  stringValue: vpc.vpcCidrBlock,
});

// Availability Zones
new ssm.StringParameter(this, 'AzsParam', {
  parameterName: `/${config.projectPrefix}/network/availability-zones`,
  stringValue: vpc.availabilityZones.join(','),
});

// Subnets
new ssm.StringParameter(this, 'PrivateSubnetsParam', {
  parameterName: `/${config.projectPrefix}/network/private-subnet-ids`,
  stringValue: vpc.privateSubnets.map(s => s.subnetId).join(','),
});

new ssm.StringParameter(this, 'PublicSubnetsParam', {
  parameterName: `/${config.projectPrefix}/network/public-subnet-ids`,
  stringValue: vpc.publicSubnets.map(s => s.subnetId).join(','),
});

// ECS Cluster
new ssm.StringParameter(this, 'ClusterNameParam', {
  parameterName: `/${config.projectPrefix}/network/ecs-cluster-name`,
  stringValue: cluster.clusterName,
});

// ALB
new ssm.StringParameter(this, 'AlbArnParam', {
  parameterName: `/${config.projectPrefix}/network/alb-arn`,
  stringValue: alb.loadBalancerArn,
});

new ssm.StringParameter(this, 'AlbDnsParam', {
  parameterName: `/${config.projectPrefix}/network/alb-dns-name`,
  stringValue: alb.loadBalancerDnsName,
});

new ssm.StringParameter(this, 'ListenerArnParam', {
  parameterName: `/${config.projectPrefix}/network/alb-listener-arn`,
  stringValue: httpsListener.listenerArn,
});

new ssm.StringParameter(this, 'AlbSgParam', {
  parameterName: `/${config.projectPrefix}/network/alb-security-group-id`,
  stringValue: albSg.securityGroupId,
});
```

## Importing Network Resources

```typescript
// Import VPC
const vpcId = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/vpc-id`
);

const vpc = ec2.Vpc.fromVpcAttributes(this, 'ImportedVpc', {
  vpcId: vpcId,
  availabilityZones: cdk.Fn.split(',', ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/network/availability-zones`
  )),
  privateSubnetIds: cdk.Fn.split(',', ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/network/private-subnet-ids`
  )),
  publicSubnetIds: cdk.Fn.split(',', ssm.StringParameter.valueForStringParameter(
    this,
    `/${config.projectPrefix}/network/public-subnet-ids`
  )),
});

// Import ECS Cluster
const clusterName = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/ecs-cluster-name`
);

const cluster = ecs.Cluster.fromClusterAttributes(this, 'ImportedCluster', {
  clusterName: clusterName,
  vpc: vpc,
  securityGroups: [],
});

// Import ALB Listener
const listenerArn = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/alb-listener-arn`
);

const albSgId = ssm.StringParameter.valueForStringParameter(
  this,
  `/${config.projectPrefix}/network/alb-security-group-id`
);

const listener = elbv2.ApplicationListener.fromApplicationListenerAttributes(
  this,
  'ImportedListener',
  {
    listenerArn: listenerArn,
    securityGroup: ec2.SecurityGroup.fromSecurityGroupId(this, 'AlbSg', albSgId),
  }
);
```
