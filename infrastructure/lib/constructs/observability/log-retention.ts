import * as cdk from 'aws-cdk-lib';
import * as logs from 'aws-cdk-lib/aws-logs';
import { IConstruct } from 'constructs';

import { AppConfig } from '../../config';

/**
 * CloudWatch's accepted retention values.
 *
 * Mapped explicitly because `logs.RetentionDays` is a string-valued enum, so a
 * number cannot be cast into it.
 */
const RETENTION_BY_DAYS: Record<number, logs.RetentionDays> = {
  1: logs.RetentionDays.ONE_DAY,
  3: logs.RetentionDays.THREE_DAYS,
  5: logs.RetentionDays.FIVE_DAYS,
  7: logs.RetentionDays.ONE_WEEK,
  14: logs.RetentionDays.TWO_WEEKS,
  30: logs.RetentionDays.ONE_MONTH,
  60: logs.RetentionDays.TWO_MONTHS,
  90: logs.RetentionDays.THREE_MONTHS,
  120: logs.RetentionDays.FOUR_MONTHS,
  150: logs.RetentionDays.FIVE_MONTHS,
  180: logs.RetentionDays.SIX_MONTHS,
  365: logs.RetentionDays.ONE_YEAR,
  400: logs.RetentionDays.THIRTEEN_MONTHS,
  545: logs.RetentionDays.EIGHTEEN_MONTHS,
  731: logs.RetentionDays.TWO_YEARS,
  1096: logs.RetentionDays.THREE_YEARS,
  1827: logs.RetentionDays.FIVE_YEARS,
  2192: logs.RetentionDays.SIX_YEARS,
  2557: logs.RetentionDays.SEVEN_YEARS,
  2922: logs.RetentionDays.EIGHT_YEARS,
  3288: logs.RetentionDays.NINE_YEARS,
  3653: logs.RetentionDays.TEN_YEARS,
};

/** Retention for every log group in this stack. */
export function logRetentionFor(config: AppConfig): logs.RetentionDays {
  const days = config.observability.logRetentionDays;
  const retention = RETENTION_BY_DAYS[days];
  if (!retention) {
    throw new Error(
      `Invalid observability.logRetentionDays: ${days}. `
      + `CloudWatch accepts only: ${Object.keys(RETENTION_BY_DAYS).join(', ')}.`,
    );
  }
  return retention;
}

/**
 * Forces the configured retention onto every log group, including ones CDK
 * creates for its own machinery (the AwsCustomResource and BucketDeployment
 * provider Lambdas both default to 731 days and are declared nowhere here).
 *
 * The per-site logRetentionFor() calls are kept for legibility; this catches what
 * they cannot see.
 */
export class LogRetentionAspect implements cdk.IAspect {
  private readonly retentionInDays: number;

  constructor(config: AppConfig) {
    // Validate via the same helper rather than silently leaving CDK's default.
    logRetentionFor(config);
    this.retentionInDays = config.observability.logRetentionDays;
  }

  public visit(node: IConstruct): void {
    if (node instanceof logs.CfnLogGroup) {
      node.retentionInDays = this.retentionInDays;
    }
  }
}
