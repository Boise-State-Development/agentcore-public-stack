/**
 * DynamoDB grants scoped to this deployment's tables by name prefix rather than
 * one statement per table.
 *
 * Every table in PlatformStack is named `{prefix}-{suffix}` by
 * `getResourceName`, so `table/{prefix}-*` names all of them — and, because
 * IAM's `*` also matches `/`, each one's `/index/*` too. One statement replaces
 * a table ARN, an index ARN and a repeated action list per table, which was
 * about half of app-api's policy text and the reason its overflow policies sat
 * at IAM's 6,144-character limit (`platform-ceilings.test.ts`).
 *
 * **The prefix collision.** `projectPrefix` may contain hyphens
 * (`/^[a-z][a-z0-9-]{1,20}$/`), so `dev-*` also matches every table of a second
 * deployment prefixed `dev-foo` in the same account. `otherDeploymentsTablesDeny`
 * closes that: it denies the same pattern on any table whose `Project` tag is
 * present and is not this deployment's. Every table this codebase creates
 * carries that tag (`applyStandardTags`), so another deployment's tables are
 * refused and this one's are untouched.
 *
 * It is a Deny gated on the tag being *present*, not an Allow gated on it
 * matching, because DynamoDB ABAC can be switched off per account and Region:
 * there, tag conditions evaluate as if no tags exist. An Allow requiring the
 * tag would then refuse every table and take the deployment down; this Deny
 * simply never fires, leaving the prefix scoping alone. So the residual
 * exposure is (a) an account with DynamoDB ABAC off, and (b) a table outside
 * this stack, without a `Project` tag, whose name happens to start with
 * `{prefix}-`. Neither can arise from two deployments of this stack in an
 * account where ABAC is on, which is the default.
 */
import * as iam from 'aws-cdk-lib/aws-iam';

import { AppConfig, getProjectTag } from '../../config';

/** The item-level read and write actions app-api's table grants have always carried. */
export const TABLE_READ_WRITE_ACTIONS: readonly string[] = [
  'dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem',
  'dynamodb:DeleteItem', 'dynamodb:Query', 'dynamodb:Scan',
  'dynamodb:BatchGetItem', 'dynamodb:BatchWriteItem',
];

/** Every table this deployment names, and every index on them. */
export function deploymentTablesArnPattern(config: AppConfig): string {
  return `arn:aws:dynamodb:${config.awsRegion}:${config.awsAccount}:table/${config.projectPrefix}-*`;
}

/** Read-write on every table (and index) this deployment names. */
export function deploymentTablesReadWrite(config: AppConfig, sid: string): iam.PolicyStatement {
  return new iam.PolicyStatement({
    sid,
    effect: iam.Effect.ALLOW,
    actions: [...TABLE_READ_WRITE_ACTIONS],
    resources: [deploymentTablesArnPattern(config)],
  });
}

/**
 * Refuse a table the prefix pattern matches but another deployment owns. See
 * the file comment for why the condition requires the tag to be present.
 */
export function otherDeploymentsTablesDeny(config: AppConfig, sid: string): iam.PolicyStatement {
  return new iam.PolicyStatement({
    sid,
    effect: iam.Effect.DENY,
    actions: ['dynamodb:*'],
    resources: [deploymentTablesArnPattern(config)],
    conditions: {
      Null: { 'aws:ResourceTag/Project': 'false' },
      StringNotEquals: { 'aws:ResourceTag/Project': getProjectTag(config) },
    },
  });
}
