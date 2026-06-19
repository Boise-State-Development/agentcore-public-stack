import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface AdminTablesConstructProps {
  config: AppConfig;
}

/**
 * AdminTablesConstruct — DynamoDB tables backing user-facing settings
 * and admin-managed surfaces.
 *
 *   - UserSettingsTable    — per-user UI settings and preferences
 *   - UserMenuLinksTable   — admin-managed links rendered in the SPA
 *                            user menu (fixed PK `USER_MENU_LINKS`,
 *                            SK `LINK#<uuid>`)
 *   - SystemPromptsTable   — admin-managed catalog of custom system
 *                            prompts ("Conversation Modes"). PK
 *                            `PROMPT#<uuid>`, SK `METADATA`. Users
 *                            opt in per-conversation via
 *                            SessionPreferences.selectedPromptId.
 */
export class AdminTablesConstruct extends Construct {
  public readonly userSettingsTable: dynamodb.Table;
  public readonly userMenuLinksTable: dynamodb.Table;
  public readonly systemPromptsTable: dynamodb.Table;

  constructor(
    scope: Construct,
    id: string,
    props: AdminTablesConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.userSettingsTable = new dynamodb.Table(this, 'UserSettingsTable', {
      tableName: getResourceName(config, 'user-settings'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });



    this.userMenuLinksTable = new dynamodb.Table(this, 'UserMenuLinksTable', {
      tableName: getResourceName(config, 'user-menu-links'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.systemPromptsTable = new dynamodb.Table(this, 'SystemPromptsTable', {
      tableName: getResourceName(config, 'system-prompts'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    // ── SSM publications (consumed by restore tooling, app-api runtime) ──
    new ssm.StringParameter(this, 'UserSettingsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/settings/user-settings-table-name`,
      stringValue: this.userSettingsTable.tableName,
      description: 'User settings table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'UserMenuLinksTableNameParameter', {
      parameterName: `/${config.projectPrefix}/admin/user-menu-links-table-name`,
      stringValue: this.userMenuLinksTable.tableName,
      description: 'User menu links table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    // System prompts: name + arn published. The arn parameter is consumed
    // by restore tooling and ad-hoc IAM scoping; runtime services use
    // typed construct refs through PlatformComputeRefs and don't read
    // these.
    new ssm.StringParameter(this, 'SystemPromptsTableNameParameter', {
      parameterName: `/${config.projectPrefix}/admin/system-prompts-table-name`,
      stringValue: this.systemPromptsTable.tableName,
      description: 'System prompts DynamoDB table name',
      tier: ssm.ParameterTier.STANDARD,
    });

    new ssm.StringParameter(this, 'SystemPromptsTableArnParameter', {
      parameterName: `/${config.projectPrefix}/admin/system-prompts-table-arn`,
      stringValue: this.systemPromptsTable.tableArn,
      description: 'System prompts DynamoDB table ARN',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
