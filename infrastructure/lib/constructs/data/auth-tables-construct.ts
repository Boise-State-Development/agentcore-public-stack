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

    // HeadlessGrantUserIndex — sparse reverse lookup of headless-run grants
    // by owner. Grant items (PK=HEADLESS-GRANT#{id}) are the only rows that
    // carry `grant_user_id`, so ordinary session rows never project here.
    // Backs apis/shared/harness/grants.py (scheduled-runs PR-1): the
    // per-owner grant query that replaced the spike's full-table Scan.
    this.bffSessionsTable.addGlobalSecondaryIndex({
      indexName: 'HeadlessGrantUserIndex',
      partitionKey: { name: 'grant_user_id', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'created_at', type: dynamodb.AttributeType.NUMBER },
      projectionType: dynamodb.ProjectionType.ALL,
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

    // EntityTypeIndex — "list every X" without scanning the table
    // (GSI5PK=ENTITY#{type}, GSI5SK=the item's own PK).
    //
    // WHY: this table is shared. Tools, skills, roles, role grants, JWT
    // mappings AND one tool-preferences row PER USER all live in it, so
    // `list_tools`'s Scan reads the whole table and filters down to the tool
    // rows. Scan cost tracks table size, not result size, so that read's cost
    // grows with ENROLLMENT, not with the number of tools: measured on dev,
    // 95 items read to return 24 tools, of which 17 were per-user rows.
    //
    // Deliberately generic rather than a TOOL-only index. `list_roles` and the
    // skills catalog scan this same table for the same reason, and DynamoDB
    // permits only ONE GSI creation per UpdateTable — so a second entity type
    // wanting its own index later would need its own release, and two
    // accumulating into one release rolls the whole stack back (see
    // docs/kaizen and PR #814). One partition per entity type costs nothing
    // extra now and leaves that door open.
    //
    // Sparse by construction: only items that carry GSI5PK are indexed, so
    // adding it changes nothing until rows are stamped. Populated for tools by
    // `ToolDefinition.to_dynamo_item` plus
    // `backend/scripts/backfill_tool_catalog_index.py`.
    this.appRolesTable.addGlobalSecondaryIndex({
      indexName: 'EntityTypeIndex',
      partitionKey: { name: 'GSI5PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI5SK', type: dynamodb.AttributeType.STRING },
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
