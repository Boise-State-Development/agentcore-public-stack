import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';
import { AppConfig, getResourceName, applyStandardTags, getRemovalPolicy, getAutoDeleteObjects } from './config';

export interface InfrastructureStackProps extends cdk.StackProps {
  config: AppConfig;
}

/**
 * Infrastructure Stack - Shared Network Resources and Core Tables
 * 
 * This stack creates foundational resources shared by all application stacks:
 * - VPC with public/private subnets across multiple AZs
 * - Application Load Balancer (ALB) in public subnets
 * - ECS Cluster for application workloads
 * - Security groups for ALB and ECS
 * - Core DynamoDB tables (Users, AppRoles, OAuth, OIDC)
 * - KMS keys and Secrets Manager for OAuth
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
  public readonly authSecret: secretsmanager.Secret;

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
    // Authentication Secret
    // ============================================================
    
    // Create a secret for authentication (e.g., JWT signing key, session secret, etc.)
    // This secret value should be rotated regularly in production
    this.authSecret = new secretsmanager.Secret(this, 'AuthenticationSecret', {
      secretName: getResourceName(config, 'auth-secret'),
      description: 'Authentication secret for JWT signing, session encryption, and other auth operations',
      generateSecretString: {
        secretStringTemplate: JSON.stringify({ description: 'Authentication Secret' }),
        generateStringKey: 'secret',
        excludePunctuation: true,
        includeSpace: false,
        passwordLength: 64,
      },
      removalPolicy: getRemovalPolicy(config),
    });

    // Export Authentication Secret ARN to SSM
    new ssm.StringParameter(this, 'AuthSecretArnParameter', {
      parameterName: `/${config.projectPrefix}/auth/secret-arn`,
      stringValue: this.authSecret.secretArn,
      description: 'Authentication Secret ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

    // Export Authentication Secret Name to SSM
    new ssm.StringParameter(this, 'AuthSecretNameParameter', {
      parameterName: `/${config.projectPrefix}/auth/secret-name`,
      stringValue: this.authSecret.secretName,
      description: 'Authentication Secret Name',
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

    // ============================================================
    // ALB Listeners (HTTP and optional HTTPS)
    // ============================================================
    if (config.certificateArn) {
      // Import certificate from ARN
      const certificate = acm.Certificate.fromCertificateArn(
        this,
        'Certificate',
        config.certificateArn
      );

      // Create HTTPS listener - this is where backend services attach
      this.albListener = this.alb.addListener('HttpsListener', {
        port: 443,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [certificate],
        defaultAction: elbv2.ListenerAction.fixedResponse(404, {
          contentType: 'text/plain',
          messageBody: 'Not Found - No matching route',
        }),
      });

      // Export HTTPS Listener ARN to SSM
      new ssm.StringParameter(this, 'AlbHttpsListenerArnParameter', {
        parameterName: `/${config.projectPrefix}/network/alb-https-listener-arn`,
        stringValue: this.albListener.listenerArn,
        description: 'Application Load Balancer HTTPS Listener ARN',
        tier: ssm.ParameterTier.STANDARD,
      });

      // HTTP listener only redirects to HTTPS (no target groups here)
      const httpRedirectListener = this.alb.addListener('HttpListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS',
          port: '443',
          permanent: true,
        }),
      });
    } else {
      // Create default HTTP listener (no certificate)
      this.albListener = this.alb.addListener('HttpListener', {
        port: 80,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.fixedResponse(404, {
          contentType: 'text/plain',
          messageBody: 'Not Found - No matching route',
        }),
      });
    }

    // Export ALB Listener ARN to SSM (primary listener for backend services)
    new ssm.StringParameter(this, 'AlbListenerArnParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-listener-arn`,
      stringValue: this.albListener.listenerArn,
      description: 'Application Load Balancer Primary Listener ARN (HTTPS if cert provided, HTTP otherwise)',
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
    // Core DynamoDB Tables (OAuth, RBAC, Users)
    // ============================================================
    // These tables are shared by both App API and Inference API stacks
    // to avoid circular dependencies. They must be created in the
    // Infrastructure Stack (foundation layer) and imported by other stacks.

    // OidcState Table - Distributed state storage for OIDC authentication
    const oidcStateTable = new dynamodb.Table(this, "OidcStateTable", {
      tableName: getResourceName(config, "oidc-state"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: "expiresAt",
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // Store OIDC state table name in SSM
    new ssm.StringParameter(this, "OidcStateTableNameParameter", {
      parameterName: `/${config.projectPrefix}/auth/oidc-state-table-name`,
      stringValue: oidcStateTable.tableName,
      description: "OIDC state table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "OidcStateTableArnParameter", {
      parameterName: `/${config.projectPrefix}/auth/oidc-state-table-arn`,
      stringValue: oidcStateTable.tableArn,
      description: "OIDC state table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // Users Table - User profiles synced from JWT for admin lookup
    const usersTable = new dynamodb.Table(this, "UsersTable", {
      tableName: getResourceName(config, "users"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // UserIdIndex - O(1) lookup by userId for admin deep links
    usersTable.addGlobalSecondaryIndex({
      indexName: "UserIdIndex",
      partitionKey: {
        name: "userId",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // EmailIndex - O(1) lookup by email for search
    usersTable.addGlobalSecondaryIndex({
      indexName: "EmailIndex",
      partitionKey: {
        name: "email",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // EmailDomainIndex - Browse users by company/domain, sorted by last login
    usersTable.addGlobalSecondaryIndex({
      indexName: "EmailDomainIndex",
      partitionKey: {
        name: "GSI2PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI2SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["userId", "email", "name", "status"],
    });

    // StatusLoginIndex - Browse users by status, sorted by last login
    usersTable.addGlobalSecondaryIndex({
      indexName: "StatusLoginIndex",
      partitionKey: {
        name: "GSI3PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI3SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["userId", "email", "name", "emailDomain"],
    });

    // Store users table name in SSM
    new ssm.StringParameter(this, "UsersTableNameParameter", {
      parameterName: `/${config.projectPrefix}/users/users-table-name`,
      stringValue: usersTable.tableName,
      description: "Users table name for admin user lookup",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "UsersTableArnParameter", {
      parameterName: `/${config.projectPrefix}/users/users-table-arn`,
      stringValue: usersTable.tableArn,
      description: "Users table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // AppRoles Table - Role definitions and permission mappings
    const appRolesTable = new dynamodb.Table(this, "AppRolesTable", {
      tableName: getResourceName(config, "app-roles"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: JwtRoleMappingIndex - Fast lookup: "Given JWT role X, what AppRoles apply?"
    // This is the critical index for authorization performance
    appRolesTable.addGlobalSecondaryIndex({
      indexName: "JwtRoleMappingIndex",
      partitionKey: {
        name: "GSI1PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI1SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI2: ToolRoleMappingIndex - Reverse lookup: "What AppRoles grant access to tool X?"
    // Used for bidirectional sync when updating tool permissions
    appRolesTable.addGlobalSecondaryIndex({
      indexName: "ToolRoleMappingIndex",
      partitionKey: {
        name: "GSI2PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI2SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["roleId", "displayName", "enabled"],
    });

    // GSI3: ModelRoleMappingIndex - Reverse lookup: "What AppRoles grant access to model X?"
    // Used for bidirectional sync when updating model permissions
    appRolesTable.addGlobalSecondaryIndex({
      indexName: "ModelRoleMappingIndex",
      partitionKey: {
        name: "GSI3PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI3SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["roleId", "displayName", "enabled"],
    });

    // Store AppRoles table name in SSM
    new ssm.StringParameter(this, "AppRolesTableNameParameter", {
      parameterName: `/${config.projectPrefix}/rbac/app-roles-table-name`,
      stringValue: appRolesTable.tableName,
      description: "AppRoles table name for RBAC",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "AppRolesTableArnParameter", {
      parameterName: `/${config.projectPrefix}/rbac/app-roles-table-arn`,
      stringValue: appRolesTable.tableArn,
      description: "AppRoles table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // ApiKeys Table - API keys for programmatic access to models
    const apiKeysTable = new dynamodb.Table(this, "ApiKeysTable", {
      tableName: getResourceName(config, "api-keys"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      timeToLiveAttribute: "ttl",
    });

    // KeyHashIndex - O(1) lookup by key hash for API key authentication
    apiKeysTable.addGlobalSecondaryIndex({
      indexName: "KeyHashIndex",
      partitionKey: {
        name: "keyHash",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store API Keys table name in SSM
    new ssm.StringParameter(this, "ApiKeysTableNameParameter", {
      parameterName: `/${config.projectPrefix}/auth/api-keys-table-name`,
      stringValue: apiKeysTable.tableName,
      description: "API Keys table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "ApiKeysTableArnParameter", {
      parameterName: `/${config.projectPrefix}/auth/api-keys-table-arn`,
      stringValue: apiKeysTable.tableArn,
      description: "API Keys table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // KMS Key for encrypting OAuth user tokens at rest
    const oauthTokenEncryptionKey = new kms.Key(this, "OAuthTokenEncryptionKey", {
      alias: getResourceName(config, "oauth-token-key"),
      description: "KMS key for encrypting OAuth user tokens at rest",
      enableKeyRotation: true,
      removalPolicy: getRemovalPolicy(config),
    });

    // OAuth Providers Table - Admin-configured OAuth provider settings
    const oauthProvidersTable = new dynamodb.Table(this, "OAuthProvidersTable", {
      tableName: getResourceName(config, "oauth-providers"),
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: EnabledProvidersIndex - Query enabled providers for user display
    oauthProvidersTable.addGlobalSecondaryIndex({
      indexName: "EnabledProvidersIndex",
      partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // OAuth User Tokens Table - User-connected OAuth tokens (KMS encrypted)
    const oauthUserTokensTable = new dynamodb.Table(this, "OAuthUserTokensTable", {
      tableName: getResourceName(config, "oauth-user-tokens"),
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
      encryptionKey: oauthTokenEncryptionKey,
    });

    // GSI1: ProviderUsersIndex - List users connected to a provider (admin view)
    oauthUserTokensTable.addGlobalSecondaryIndex({
      indexName: "ProviderUsersIndex",
      partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Secrets Manager for OAuth client secrets
    const oauthClientSecretsSecret = new secretsmanager.Secret(this, "OAuthClientSecretsSecret", {
      secretName: getResourceName(config, "oauth-client-secrets"),
      description: "OAuth provider client secrets (JSON: {provider_id: secret})",
      removalPolicy: getRemovalPolicy(config),
    });

    // Store OAuth resource names in SSM
    new ssm.StringParameter(this, "OAuthProvidersTableNameParameter", {
      parameterName: `/${config.projectPrefix}/oauth/providers-table-name`,
      stringValue: oauthProvidersTable.tableName,
      description: "OAuth providers table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "OAuthProvidersTableArnParameter", {
      parameterName: `/${config.projectPrefix}/oauth/providers-table-arn`,
      stringValue: oauthProvidersTable.tableArn,
      description: "OAuth providers table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "OAuthUserTokensTableNameParameter", {
      parameterName: `/${config.projectPrefix}/oauth/user-tokens-table-name`,
      stringValue: oauthUserTokensTable.tableName,
      description: "OAuth user tokens table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "OAuthUserTokensTableArnParameter", {
      parameterName: `/${config.projectPrefix}/oauth/user-tokens-table-arn`,
      stringValue: oauthUserTokensTable.tableArn,
      description: "OAuth user tokens table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "OAuthTokenEncryptionKeyArnParameter", {
      parameterName: `/${config.projectPrefix}/oauth/token-encryption-key-arn`,
      stringValue: oauthTokenEncryptionKey.keyArn,
      description: "KMS key ARN for OAuth token encryption",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "OAuthClientSecretsArnParameter", {
      parameterName: `/${config.projectPrefix}/oauth/client-secrets-arn`,
      stringValue: oauthClientSecretsSecret.secretArn,
      description: "Secrets Manager ARN for OAuth client secrets",
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Shared DynamoDB Tables (Quota Management)
    // ============================================================
    // These tables are shared by both App API and Inference API stacks
    // to avoid circular dependencies. They must be created in the
    // Infrastructure Stack (foundation layer) and imported by other stacks.

    // UserQuotas Table - Quota assignments for users and roles
    const userQuotasTable = new dynamodb.Table(this, "UserQuotasTable", {
      tableName: getResourceName(config, "user-quotas"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: AssignmentTypeIndex - Query assignments by type, sorted by priority
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: "AssignmentTypeIndex",
      partitionKey: {
        name: "GSI1PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI1SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI2: UserAssignmentIndex - Query direct user assignments (O(1) lookup)
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: "UserAssignmentIndex",
      partitionKey: {
        name: "GSI2PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI2SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI3: RoleAssignmentIndex - Query role-based assignments, sorted by priority
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: "RoleAssignmentIndex",
      partitionKey: {
        name: "GSI3PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI3SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI4: UserOverrideIndex - Query active overrides by user, sorted by expiry
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: "UserOverrideIndex",
      partitionKey: {
        name: "GSI4PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI4SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // GSI6: AppRoleAssignmentIndex - Query quota assignments by AppRole ID
    userQuotasTable.addGlobalSecondaryIndex({
      indexName: "AppRoleAssignmentIndex",
      partitionKey: {
        name: "GSI6PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI6SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store UserQuotas table name and ARN in SSM
    new ssm.StringParameter(this, "UserQuotasTableNameParameter", {
      parameterName: `/${config.projectPrefix}/quota/user-quotas-table-name`,
      stringValue: userQuotasTable.tableName,
      description: "UserQuotas table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "UserQuotasTableArnParameter", {
      parameterName: `/${config.projectPrefix}/quota/user-quotas-table-arn`,
      stringValue: userQuotasTable.tableArn,
      description: "UserQuotas table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // QuotaEvents Table - Quota usage event tracking
    const quotaEventsTable = new dynamodb.Table(this, "QuotaEventsTable", {
      tableName: getResourceName(config, "quota-events"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI5: TierEventIndex - Query events by tier for analytics
    quotaEventsTable.addGlobalSecondaryIndex({
      indexName: "TierEventIndex",
      partitionKey: {
        name: "GSI5PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI5SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store QuotaEvents table name and ARN in SSM
    new ssm.StringParameter(this, "QuotaEventsTableNameParameter", {
      parameterName: `/${config.projectPrefix}/quota/quota-events-table-name`,
      stringValue: quotaEventsTable.tableName,
      description: "QuotaEvents table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "QuotaEventsTableArnParameter", {
      parameterName: `/${config.projectPrefix}/quota/quota-events-table-arn`,
      stringValue: quotaEventsTable.tableArn,
      description: "QuotaEvents table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // SessionsMetadata Table - Message-level metadata for cost tracking
    const sessionsMetadataTable = new dynamodb.Table(this, "SessionsMetadataTable", {
      tableName: getResourceName(config, "sessions-metadata"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      timeToLiveAttribute: "ttl",
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: UserTimestampIndex - Query messages by user and time range
    sessionsMetadataTable.addGlobalSecondaryIndex({
      indexName: "UserTimestampIndex",
      partitionKey: {
        name: "GSI1PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI1SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // SessionLookupIndex - Direct session lookup by ID and per-session cost queries
    // Enables:
    //   - O(1) session lookup: GSI_PK=SESSION#{session_id}, GSI_SK=META
    //   - Per-session costs: GSI_PK=SESSION#{session_id}, GSI_SK begins_with C#
    sessionsMetadataTable.addGlobalSecondaryIndex({
      indexName: "SessionLookupIndex",
      partitionKey: {
        name: "GSI_PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI_SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store SessionsMetadata table name and ARN in SSM
    new ssm.StringParameter(this, "SessionsMetadataTableNameParameter", {
      parameterName: `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-name`,
      stringValue: sessionsMetadataTable.tableName,
      description: "SessionsMetadata table name for message-level cost tracking",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "SessionsMetadataTableArnParameter", {
      parameterName: `/${config.projectPrefix}/cost-tracking/sessions-metadata-table-arn`,
      stringValue: sessionsMetadataTable.tableArn,
      description: "SessionsMetadata table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // UserCostSummary Table - Pre-aggregated cost summaries for fast quota checks
    const userCostSummaryTable = new dynamodb.Table(this, "UserCostSummaryTable", {
      tableName: getResourceName(config, "user-cost-summary"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI2: PeriodCostIndex - Query top users by cost for admin dashboard
    // Enables efficient "top N users by cost" queries without table scans
    userCostSummaryTable.addGlobalSecondaryIndex({
      indexName: "PeriodCostIndex",
      partitionKey: {
        name: "GSI2PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI2SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ["userId", "totalCost", "totalRequests", "lastUpdated"],
    });

    // Store UserCostSummary table name and ARN in SSM
    new ssm.StringParameter(this, "UserCostSummaryTableNameParameter", {
      parameterName: `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-name`,
      stringValue: userCostSummaryTable.tableName,
      description: "UserCostSummary table name for aggregated cost summaries",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "UserCostSummaryTableArnParameter", {
      parameterName: `/${config.projectPrefix}/cost-tracking/user-cost-summary-table-arn`,
      stringValue: userCostSummaryTable.tableArn,
      description: "UserCostSummary table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // SystemCostRollup Table - Pre-aggregated system-wide metrics for admin dashboard
    const systemCostRollupTable = new dynamodb.Table(this, "SystemCostRollupTable", {
      tableName: getResourceName(config, "system-cost-rollup"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // Store SystemCostRollup table name and ARN in SSM
    new ssm.StringParameter(this, "SystemCostRollupTableNameParameter", {
      parameterName: `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-name`,
      stringValue: systemCostRollupTable.tableName,
      description: "SystemCostRollup table name for admin dashboard aggregates",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "SystemCostRollupTableArnParameter", {
      parameterName: `/${config.projectPrefix}/cost-tracking/system-cost-rollup-table-arn`,
      stringValue: systemCostRollupTable.tableArn,
      description: "SystemCostRollup table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // ManagedModels Table - Model management and pricing data
    const managedModelsTable = new dynamodb.Table(this, "ManagedModelsTable", {
      tableName: getResourceName(config, "managed-models"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: ModelIdIndex - Query by modelId for duplicate checking
    managedModelsTable.addGlobalSecondaryIndex({
      indexName: "ModelIdIndex",
      partitionKey: {
        name: "GSI1PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI1SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store ManagedModels table name and ARN in SSM
    new ssm.StringParameter(this, "ManagedModelsTableNameParameter", {
      parameterName: `/${config.projectPrefix}/admin/managed-models-table-name`,
      stringValue: managedModelsTable.tableName,
      description: "ManagedModels table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "ManagedModelsTableArnParameter", {
      parameterName: `/${config.projectPrefix}/admin/managed-models-table-arn`,
      stringValue: managedModelsTable.tableArn,
      description: "ManagedModels table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // User Settings Table
    // PK (String) SK (String)
    // ============================================================
    const userSettingsTable = new dynamodb.Table(this, "UserSettingsTable", {
      tableName: getResourceName(config, "user-settings"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // Store UserSettings table name and ARN in SSM
    new ssm.StringParameter(this, "UserSettingsTableNameParameter", {
      parameterName: `/${config.projectPrefix}/settings/user-settings-table-name`,
      stringValue: userSettingsTable.tableName,
      description: "User settings DynamoDB table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "UserSettingsTableArnParameter", {
      parameterName: `/${config.projectPrefix}/settings/user-settings-table-arn`,
      stringValue: userSettingsTable.tableArn,
      description: "User settings DynamoDB table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // AuthProviders Table - OIDC authentication provider configuration
    const authProvidersTable = new dynamodb.Table(this, "AuthProvidersTable", {
      tableName: getResourceName(config, "auth-providers"),
      partitionKey: { name: "PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "SK", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // GSI1: EnabledProvidersIndex - Query enabled auth providers for login page
    authProvidersTable.addGlobalSecondaryIndex({
      indexName: "EnabledProvidersIndex",
      partitionKey: { name: "GSI1PK", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "GSI1SK", type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store AuthProviders table name and ARN in SSM
    new ssm.StringParameter(this, "AuthProvidersTableNameParameter", {
      parameterName: `/${config.projectPrefix}/auth/auth-providers-table-name`,
      stringValue: authProvidersTable.tableName,
      description: "Auth providers table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "AuthProvidersTableArnParameter", {
      parameterName: `/${config.projectPrefix}/auth/auth-providers-table-arn`,
      stringValue: authProvidersTable.tableArn,
      description: "Auth providers table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "AuthProvidersStreamArnParameter", {
      parameterName: `/${config.projectPrefix}/auth/auth-providers-stream-arn`,
      stringValue: authProvidersTable.tableStreamArn!,
      description: "DynamoDB Stream ARN for auth providers table",
      tier: ssm.ParameterTier.STANDARD,
    });

    // Secrets Manager for auth provider client secrets
    const authProviderSecretsSecret = new secretsmanager.Secret(this, "AuthProviderSecretsSecret", {
      secretName: getResourceName(config, "auth-provider-secrets"),
      description: "OIDC authentication provider client secrets (JSON: {provider_id: secret})",
      removalPolicy: getRemovalPolicy(config),
    });

    // Store auth provider secrets ARN in SSM
    new ssm.StringParameter(this, "AuthProviderSecretsArnParameter", {
      parameterName: `/${config.projectPrefix}/auth/auth-provider-secrets-arn`,
      stringValue: authProviderSecretsSecret.secretArn,
      description: "Secrets Manager ARN for auth provider client secrets",
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // File Upload Storage (S3 + DynamoDB)
    // ============================================================
    // These resources are shared by both App API (uploads) and Inference API
    // (reads). They must live in the Infrastructure Stack to avoid a circular
    // dependency: InferenceApiStack (tier 2) deploys before AppApiStack (tier 3).

    // Build CORS origins for file upload bucket
    const fileUploadCorsOrigins: string[] = (() => {
      const origins = new Set<string>();
      origins.add('http://localhost:4200');
      if (config.domainName) {
        origins.add(`https://${config.domainName}`);
      }
      if (config.fileUpload?.corsOrigins) {
        config.fileUpload.corsOrigins.split(',').map(o => o.trim()).filter(Boolean).forEach(o => origins.add(o));
      }
      return Array.from(origins);
    })();

    // S3 Bucket for user file uploads
    const userFilesBucket = new s3.Bucket(this, "UserFilesBucket", {
      bucketName: getResourceName(config, "user-file-uploads", config.awsAccount),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
      cors: [
        {
          allowedOrigins: fileUploadCorsOrigins,
          allowedMethods: [s3.HttpMethods.GET, s3.HttpMethods.PUT, s3.HttpMethods.HEAD],
          allowedHeaders: ["Content-Type", "Content-Length", "x-amz-*"],
          exposedHeaders: ["ETag", "Content-Length", "Content-Type"],
          maxAge: 3600,
        },
      ],
      lifecycleRules: [
        {
          id: "transition-to-ia",
          transitions: [
            {
              storageClass: s3.StorageClass.INFREQUENT_ACCESS,
              transitionAfter: cdk.Duration.days(30),
            },
          ],
        },
        {
          id: "transition-to-glacier",
          transitions: [
            {
              storageClass: s3.StorageClass.GLACIER_INSTANT_RETRIEVAL,
              transitionAfter: cdk.Duration.days(90),
            },
          ],
        },
        {
          id: "expire-objects",
          expiration: cdk.Duration.days(config.fileUpload?.retentionDays || 365),
        },
        {
          id: "abort-incomplete-multipart",
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(1),
        },
      ],
    });

    // DynamoDB Table for file metadata
    /**
     * Schema:
     *   PK: USER#{userId}, SK: FILE#{uploadId} - File metadata
     *   PK: USER#{userId}, SK: QUOTA - User storage quota tracking
     *   GSI1PK: CONV#{sessionId}, GSI1SK: FILE#{uploadId} - Query files by conversation
     */
    const userFilesTable = new dynamodb.Table(this, "UserFilesTable", {
      tableName: getResourceName(config, "user-file-uploads"),
      partitionKey: {
        name: "PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "SK",
        type: dynamodb.AttributeType.STRING,
      },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecovery: true,
      timeToLiveAttribute: "ttl",
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      removalPolicy: getRemovalPolicy(config),
    });

    // GSI1: SessionIndex - Query files by conversation/session
    userFilesTable.addGlobalSecondaryIndex({
      indexName: "SessionIndex",
      partitionKey: {
        name: "GSI1PK",
        type: dynamodb.AttributeType.STRING,
      },
      sortKey: {
        name: "GSI1SK",
        type: dynamodb.AttributeType.STRING,
      },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // Store file upload resource names in SSM
    new ssm.StringParameter(this, "UserFilesBucketNameParameter", {
      parameterName: `/${config.projectPrefix}/user-file-uploads/bucket-name`,
      stringValue: userFilesBucket.bucketName,
      description: "User files S3 bucket name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "UserFilesBucketArnParameter", {
      parameterName: `/${config.projectPrefix}/user-file-uploads/bucket-arn`,
      stringValue: userFilesBucket.bucketArn,
      description: "User files S3 bucket ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "UserFilesTableNameParameter", {
      parameterName: `/${config.projectPrefix}/user-file-uploads/table-name`,
      stringValue: userFilesTable.tableName,
      description: "User files metadata table name",
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, "UserFilesTableArnParameter", {
      parameterName: `/${config.projectPrefix}/user-file-uploads/table-arn`,
      stringValue: userFilesTable.tableArn,
      description: "User files metadata table ARN",
      tier: ssm.ParameterTier.STANDARD,
    });

    // ============================================================
    // Route53 Hosted Zone Lookup (Optional)
    // ============================================================
    // The hosted zone and certificates are created outside this stack
    // (manually or via a separate DNS stack). This stack looks up the
    // existing hosted zone and creates A records for the ALB.
    if (config.infrastructureHostedZoneDomain && config.infrastructureHostedZoneDomain.trim() !== '') {
      const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
        domainName: config.infrastructureHostedZoneDomain,
      });

      // Export Hosted Zone ID to SSM for cross-stack references
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

      // ============================================================
      // Route53 A Record for ALB (Optional)
      // ============================================================
      if (config.albSubdomain) {
        const albRecordName = `${config.albSubdomain}.${config.infrastructureHostedZoneDomain}`;

        new route53.ARecord(this, 'AlbARecord', {
          zone: hostedZone,
          recordName: config.albSubdomain,
          target: route53.RecordTarget.fromAlias(
            new route53Targets.LoadBalancerTarget(this.alb)
          ),
          comment: `A record for ALB - points ${albRecordName} to load balancer`,
        });

        if (config.certificateArn) {
          new cdk.CfnOutput(this, 'AlbUrlHttps', {
            value: `https://${albRecordName}`,
            description: 'Application Load Balancer HTTPS URL (HTTP redirects here)',
            exportName: `${config.projectPrefix}-alb-url-https`,
          });
        }
      }
    }

    // ============================================================
    // ALB URL Export (Always)
    // ============================================================
    // Determine the ALB URL to export
    // Priority: Custom domain (if configured) > ALB DNS name
    let albUrl: string;
    let albUrlDescription: string;
    
    if (config.infrastructureHostedZoneDomain && config.albSubdomain) {
      // Use custom domain URL
      const albRecordName = `${config.albSubdomain}.${config.infrastructureHostedZoneDomain}`;
      const protocol = config.certificateArn ? 'https' : 'http';
      albUrl = `${protocol}://${albRecordName}`;
      albUrlDescription = 'Application Load Balancer Custom Domain URL';
    } else {
      // Use ALB DNS name as fallback
      const protocol = config.certificateArn ? 'https' : 'http';
      albUrl = `${protocol}://${this.alb.loadBalancerDnsName}`;
      albUrlDescription = 'Application Load Balancer URL (DNS name)';
    }
    
    // Export ALB URL to SSM - used by frontend stack for runtime config
    new ssm.StringParameter(this, 'AlbUrlParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-url`,
      stringValue: albUrl,
      description: albUrlDescription,
      tier: ssm.ParameterTier.STANDARD,
    });

    // Construct OAuth callback URL
    const oauthCallbackUrl = config.domainName
      ? `https://${config.domainName}/auth/callback`
      : `${albUrl}/auth/callback`;

    // Export OAuth callback URL for runtime provisioner
    new ssm.StringParameter(this, 'OAuthCallbackUrlParameter', {
      parameterName: `/${config.projectPrefix}/oauth/callback-url`,
      stringValue: oauthCallbackUrl,
      description: 'OAuth callback URL for authentication provider configuration',
      tier: ssm.ParameterTier.STANDARD,
    });

    // CloudFormation Output for ALB URL
    new cdk.CfnOutput(this, 'AlbUrl', {
      value: albUrl,
      description: albUrlDescription,
      exportName: `${config.projectPrefix}-alb-url`,
    });

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

    new cdk.CfnOutput(this, 'AuthSecretArn', {
      value: this.authSecret.secretArn,
      description: 'Authentication Secret ARN',
      exportName: `${config.projectPrefix}-auth-secret-arn`,
    });

    new cdk.CfnOutput(this, 'AuthSecretName', {
      value: this.authSecret.secretName,
      description: 'Authentication Secret Name',
      exportName: `${config.projectPrefix}-auth-secret-name`,
    });
  }
}
