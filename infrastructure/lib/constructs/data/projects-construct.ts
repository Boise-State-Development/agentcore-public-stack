import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface ProjectsConstructProps {
  config: AppConfig;
}

/**
 * ProjectsConstruct — the Shared Projects single-table (docs/specs/shared-projects.md §3.1).
 *
 * A Project is a composition: its harness is a hidden Agent record in
 * `rag-assistants` and its memory is a Memory Space. This table holds only what
 * belongs to the project itself — META, email-keyed `MEMBER#` rows (the one
 * thing access checks read), pointers to schedules / shared tasks / personal
 * spaces, output-library rows, `COST#` monthly rollups, and the per-user
 * `NOTIF#` inbox.
 *
 *   - base table   `PK = PROJECT#<id>` | `USER#<userId>` — a project's rows; a user's inbox
 *   - OwnerIndex   `GSI1PK = OWNER#<userId>`            — projects a user owns
 *   - MemberIndex  `GSI2PK = MEMBER#<email>`            — projects shared with a user
 *
 * Both indexes ship with the table. The one-GSI-per-deploy limit applies to
 * `UpdateTable` only; `CreateTable` accepts both. A future index on this table
 * is an `UpdateTable` and must land alone (see `gsi-update-limit.test.ts`).
 *
 * `ttl` expires notification rows (90 days). Project rows carry no TTL:
 * deletion is explicit (archive, then purge).
 *
 * The table name is a contract with the backend. inference-api derives it as
 * `${PROJECT_PREFIX}-projects` rather than taking a `DYNAMODB_PROJECTS_TABLE_NAME`
 * env var, because the AgentCore Runtime caps environment variables at 50 and has
 * almost none free (`runtime-env-var-limit.test.ts`). app-api is on ECS and gets
 * the name explicitly.
 */
export class ProjectsConstruct extends Construct {
  public readonly projectsTable: dynamodb.Table;

  constructor(scope: Construct, id: string, props: ProjectsConstructProps) {
    super(scope, id);

    const { config } = props;

    this.projectsTable = new dynamodb.Table(this, 'ProjectsTable', {
      tableName: getResourceName(config, 'projects'),
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });

    this.projectsTable.addGlobalSecondaryIndex({
      indexName: 'OwnerIndex',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    this.projectsTable.addGlobalSecondaryIndex({
      indexName: 'MemberIndex',
      partitionKey: { name: 'GSI2PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI2SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
  }
}
