import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface AuthTablesConstructProps {
  config: AppConfig;
}

/**
 * AuthTablesConstruct — DynamoDB tables backing authentication.
 *
 *   - OidcStateTable           — distributed state for OIDC flow
 *   - BFFSessionsTable         — BFF token-handler sessions
 *   - UsersTable               — user profiles synced from JWT
 *   - AppRolesTable            — role definitions and permission mappings
 *   - ApiKeysTable             — API keys for programmatic model access
 */
export class AuthTablesConstruct extends Construct {
  public readonly oidcStateTable: dynamodb.Table;
  public readonly bffSessionsTable: dynamodb.Table;
  public readonly usersTable: dynamodb.Table;
  public readonly appRolesTable: dynamodb.Table;
  public readonly apiKeysTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: AuthTablesConstructProps) {
    super(scope, id);

    const { config } = props;

    // OidcState Table - Distributed state storage for OIDC authentication
    this.oidcStateTable = new dynamodb.Table(this, 'OidcStateTable', {
      tableName: getResourceName(config, 'oidc-state'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'expiresAt',
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });



    // BFF Sessions Table - per-session Cognito tokens (httpOnly cookie -> tokens)
    this.bffSessionsTable = new dynamodb.Table(this, 'BFFSessionsTable', {
      tableName: getResourceName(config, 'bff-sessions'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });



    // Users Table - User profiles synced from JWT
    this.usersTable = new dynamodb.Table(this, 'UsersTable', {
      tableName: getResourceName(config, 'users'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.usersTable.addGlobalSecondaryIndex({
      indexName: 'UserIdIndex',
      partitionKey: { name: 'userId', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.usersTable.addGlobalSecondaryIndex({
      indexName: 'EmailIndex',
      partitionKey: { name: 'email', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.usersTable.addGlobalSecondaryIndex({
      indexName: 'EmailDomainIndex',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['userId', 'email', 'name', 'status'],
    });

    this.usersTable.addGlobalSecondaryIndex({
      indexName: 'StatusLoginIndex',
      partitionKey: { name: 'GSI3PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI3SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['userId', 'email', 'name', 'emailDomain'],
    });



    // AppRoles Table - Role definitions and permission mappings
    this.appRolesTable = new dynamodb.Table(this, 'AppRolesTable', {
      tableName: getResourceName(config, 'app-roles'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.appRolesTable.addGlobalSecondaryIndex({
      indexName: 'JwtRoleMappingIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.appRolesTable.addGlobalSecondaryIndex({
      indexName: 'ToolRoleMappingIndex',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['roleId', 'displayName', 'enabled'],
    });

    this.appRolesTable.addGlobalSecondaryIndex({
      indexName: 'ModelRoleMappingIndex',
      partitionKey: { name: 'GSI3PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI3SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.INCLUDE,
      nonKeyAttributes: ['roleId', 'displayName', 'enabled'],
    });

    // SkillOwnerIndex — reverse lookup of skills by owner (GSI4PK=OWNER#{ownerId},
    // GSI4SK=SKILL#{skillId}). Unused in v1 (admin lists scan SKILL# items), but
    // provisioned now so the Phase-2 "list my skills" query needs no table
    // migration. See docs/specs/admin-skills-rbac-tool-binding.md (§5).
    this.appRolesTable.addGlobalSecondaryIndex({
      indexName: 'SkillOwnerIndex',
      partitionKey: { name: 'GSI4PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI4SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    new ssm.StringParameter(this, 'AppRolesTableNameParameter', {
      parameterName: `/${config.projectPrefix}/rbac/app-roles-table-name`,
      stringValue: this.appRolesTable.tableName,
      description: 'AppRoles table name for RBAC',
      tier: ssm.ParameterTier.STANDARD,
    });


    // ApiKeys Table - API keys for programmatic access
    this.apiKeysTable = new dynamodb.Table(this, 'ApiKeysTable', {
      tableName: getResourceName(config, 'api-keys'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
      timeToLiveAttribute: 'ttl',
    });

    this.apiKeysTable.addGlobalSecondaryIndex({
      indexName: 'KeyHashIndex',
      partitionKey: { name: 'keyHash', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ── SSM publications (consumed by restore tooling, app-api/inference-api runtime) ──
    new ssm.StringParameter(this, 'UsersTableNameParameter', {
      parameterName: `/${config.projectPrefix}/users/users-table-name`,
      stringValue: this.usersTable.tableName,
      description: 'Users table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'ApiKeysTableNameParameter', {
      parameterName: `/${config.projectPrefix}/auth/api-keys-table-name`,
      stringValue: this.apiKeysTable.tableName,
      description: 'API keys table name',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
