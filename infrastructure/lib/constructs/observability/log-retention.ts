import * as cdk from 'aws-cdk-lib';
import * as logs from 'aws-cdk-lib/aws-logs';
import { IConstruct } from 'constructs';

import { AppConfig } from '../../config';

/**
 * Map `observability.logRetentionDays` to the CloudWatch enum.
 *
 * ## Why a helper rather than a literal per log group
 *
 * Before this existed, every log group in the stack hardcoded
 * `RetentionDays.ONE_WEEK` (and Memory's used `ONE_MONTH`), which meant
 * retention could not be changed without editing a dozen constructs, and the one
 * that differed did so silently rather than deliberately. One configured value
 * now drives all of them.
 *
 * Deliberately NOT a prod/non-prod branch. This repo is forked by many
 * institutions: a fork with one environment should not have to reason about a
 * `production` boolean, and a fork with three should not be limited to two.
 * Per-environment differences belong in the forker's own deployment config and
 * arrive here as a single number.
 *
 * ## Why the mapping is explicit
 *
 * `logs.RetentionDays` is a string-valued enum, not a numeric one, so a number
 * cannot be cast into it. The set below is CloudWatch's complete list of
 * accepted retention values — an arbitrary number is rejected by CloudFormation
 * at deploy time, which is why `validateConfig()` also checks the configured
 * value against the same list and fails at synth with a message naming the valid
 * options.
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

/**
 * The retention every log group in this stack should use.
 *
 * @throws if the configured value is not one CloudWatch accepts. `loadConfig()`
 *   validates this too, so reaching the throw here means a construct was handed
 *   a config that never went through the loader (a hand-built test fixture).
 */
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
 * Aspect that forces the configured retention onto EVERY log group in the stack,
 * including ones this codebase never declares.
 *
 * ## Why calling `logRetentionFor()` at each declaration site is not enough
 *
 * CDK creates log groups on our behalf for its own machinery, and it gives them
 * its own default retention — measured at **731 days** (two years) for both the
 * `AwsCustomResource` provider Lambda and the `BucketDeployment` Lambda in this
 * stack. Those groups are invisible in the source: no construct here declares
 * them, so no amount of discipline at the declaration sites would have caught
 * them, and the source guard that forbids hardcoded `RetentionDays` cannot see
 * them either.
 *
 * They were found by diffing a real `cdk synth` against the configured value —
 * which is also the reason the unit tests missed them. A bare `new cdk.App()` in
 * a test does not carry the feature flags from `cdk.json` that cause CDK to
 * materialise these groups as explicit resources, so the template a test sees and
 * the template a deploy produces genuinely differ here.
 *
 * An Aspect visits the synthesized construct tree, so it catches every group
 * regardless of who declared it. The per-site `logRetentionFor()` calls are kept
 * anyway: they make the intent legible where the log group is defined, and they
 * mean the value is right even if this Aspect is ever removed.
 */
export class LogRetentionAspect implements cdk.IAspect {
  private readonly retentionInDays: number;

  constructor(config: AppConfig) {
    // Validate through the same helper, so a bad value fails here too rather
    // than silently leaving CDK's default in place.
    logRetentionFor(config);
    this.retentionInDays = config.observability.logRetentionDays;
  }

  public visit(node: IConstruct): void {
    if (node instanceof logs.CfnLogGroup) {
      node.retentionInDays = this.retentionInDays;
    }
  }
}
