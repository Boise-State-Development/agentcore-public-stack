import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { logRetentionFor } from '../observability/log-retention';

export interface PlatformCostSyncConstructProps {
  config: AppConfig;
  /**
   * System cost rollup table — the same table the per-model and daily
   * rollups already live in. Platform rows use their own `PLATFORM#*` PK
   * namespace, so this needs no new table, no new GSI and no migration.
   */
  systemCostRollupTable: dynamodb.ITable;
}

/**
 * PlatformCostSyncConstruct — pulls the AWS bill into the admin cost dashboard.
 *
 * The dashboard's own ledger measures Bedrock token spend accurately (within
 * 0.50% of Cost Explorer on prod's September bill) but sees nothing else. On
 * prod that was 38.8% of the bill — $685.96 of $1,767.41 — including
 * `Amazon Bedrock AgentCore` at $201.54/month, the second-largest line in the
 * account. This Lambda closes that gap.
 *
 * SCHEDULED, NOT LIVE. Cost Explorer bills **$0.01 per request**, so a screen
 * that queried it on load would cost ~$10/month per thousand page loads to
 * display costs. One daily tick syncs the current and previous month (two CE
 * calls, ~$0.02/day) into DynamoDB, and the read path never touches CE.
 *
 * OPT-IN, unlike most flags in this repo. The usual posture here is default-on
 * with a kill switch, but this one reaches outside the platform's own
 * resources: it needs `ce:GetCostAndUsage`, which an SCP may deny, against a
 * Cost Explorer that may not be enabled in the account, and it spends money
 * per call. Whether to read the account's billing data is a per-environment
 * scoping decision, so it follows `feedbackEvalSampling`: only an explicit
 * `true` turns it on, and a workflow forwarding an unset variable (an empty
 * string) must never be what enables it.
 *
 * TWO INDEPENDENT STOPS, same as scheduled-runs: the EventBridge rule is not
 * created at all when the feature is off, and the handler additionally gates
 * on `PLATFORM_COST_SYNC_ENABLED` — so an operator can dark-stop via CFN or a
 * plain env flip, whichever is faster.
 *
 * CODE SHIPPING: real code via `lambda.Code.fromAsset` on the normal CDK
 * path, like `token-enrichment` and unlike the bootstrap-container Lambdas.
 * The handler is boto3-only, tiny, and changes rarely, so there is no reason
 * to put it on the fast backend code-deploy path.
 */
export class PlatformCostSyncConstruct extends Construct {
  public readonly syncFunction: lambda.Function;
  public readonly scheduleRule: events.Rule;

  constructor(scope: Construct, id: string, props: PlatformCostSyncConstructProps) {
    super(scope, id);

    const { config, systemCostRollupTable } = props;

    // Auto-generated name (no explicit log group name) so a failed-deploy
    // orphan cannot collide with a redeploy.
    const logGroup = new logs.LogGroup(this, 'PlatformCostSyncLogGroup', {
      retention: logRetentionFor(config),
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.syncFunction = new lambda.Function(this, 'PlatformCostSyncFunction', {
      functionName: getResourceName(config, 'platform-cost-sync'),
      description:
        'Daily Cost Explorer sync — writes PLATFORM#* service-cost rows into the system cost rollup table for the admin dashboard',
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      logGroup,
      code: lambda.Code.fromAsset(
        path.resolve(__dirname, '..', '..', '..', 'lambda-assets', 'platform-cost-sync'),
        { exclude: ['test_*.py', '*_test.py', '__pycache__', '.pytest_cache'] },
      ),
      memorySize: 256,
      // Cost Explorer is slow and occasionally throttles; a sync that takes
      // 40s is normal and there is nothing waiting on it.
      timeout: cdk.Duration.minutes(2),
      environment: {
        DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME: systemCostRollupTable.tableName,
        PLATFORM_COST_SYNC_ENABLED: 'true',
        AWS_ACCOUNT_ID: cdk.Stack.of(this).account,
        // Scope the bill to THIS deployment rather than the whole account.
        // `applyStandardTags` writes `Project: config.projectPrefix` onto
        // every resource the stack creates, so the filter needs no new
        // configuration and a fork inherits it for free.
        //
        // Cost Explorer only honours a cost allocation tag once it has been
        // ACTIVATED in the payer account — an action a linked account cannot
        // perform, and one that is not retroactive. Until then the filter
        // matches nothing and the handler falls back to account scope,
        // recording `scope: "account"` so the dashboard labels the figures
        // instead of overstating them. Setting this to '' forces account
        // scope deliberately.
        PLATFORM_COST_PROJECT_TAG: config.projectPrefix,
        // Deliberately NOT set: the handler's own DEFAULT_EXCLUDED_SERVICES is
        // the source of truth. Setting it here from a config value that a
        // workflow forwards as an empty string is precisely how the browser
        // URL blocklist got silently emptied — and an empty exclusion list
        // here bills another team's Aurora cluster to our users, with no
        // error and only a slightly high total to show for it. An operator
        // who genuinely wants to override sets the env var on the function
        // directly, where the handler's own empty-value guard still applies.
      },
    });

    systemCostRollupTable.grantWriteData(this.syncFunction);

    // Cost Explorer has no resource-level permissions — `ce:*` actions only
    // accept "*". Scoped to the two read actions this handler makes, and
    // granted ONLY to this Lambda: the app-api task role deliberately does
    // not get them, so no request path can be made to spend CE dollars.
    this.syncFunction.addToRolePolicy(
      new iam.PolicyStatement({
        sid: 'ReadCostExplorer',
        effect: iam.Effect.ALLOW,
        actions: ['ce:GetCostAndUsage', 'ce:GetDimensionValues'],
        resources: ['*'],
      }),
    );

    // 07:10 UTC: after the previous UTC day has closed, and offset off the
    // hour because Cost Explorer's own refresh lands on hour boundaries.
    // Daily is the right cadence — CE data itself only refreshes 1-3x/day, so
    // a tighter schedule would pay per call to re-read identical numbers.
    this.scheduleRule = new events.Rule(this, 'PlatformCostSyncSchedule', {
      ruleName: getResourceName(config, 'platform-cost-sync-daily'),
      description:
        'Daily platform cost sync from AWS Cost Explorer (current + previous month)',
      schedule: events.Schedule.cron({ minute: '10', hour: '7' }),
      targets: [new targets.LambdaFunction(this.syncFunction)],
    });
  }
}
