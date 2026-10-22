import * as cdk from 'aws-cdk-lib';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
import * as sns from 'aws-cdk-lib/aws-sns';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';
import { AlarmFactory, ALARM_PERIOD } from './alarm-factory';

/** The namespace AgentCore publishes service metrics to. */
const AGENTCORE_NAMESPACE = 'AWS/Bedrock-AgentCore';
/** The namespace bedrock-runtime inference publishes to. */
const BEDROCK_NAMESPACE = 'AWS/Bedrock';

/**
 * Alarm-name-safe label for a Bedrock model id.
 *
 * `ModelId` arrives in three spellings for the same underlying model: a bare id
 * (`amazon.titan-embed-text-v2:0`), an inference-profile id
 * (`global.anthropic.claude-sonnet-5`), and a full foundation-model ARN
 * (`arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-haiku-...`).
 * The ARN form is reduced to its last path segment, so an ARN-keyed entry and a
 * bare-id entry for the same model produce the same readable label rather than
 * one unreadable one. Quotas differ per inference profile, so `us.` and
 * `global.` variants deliberately remain distinct.
 */
function modelSlug(modelId: string): string {
  const bare = modelId.includes('/')
    ? modelId.slice(modelId.lastIndexOf('/') + 1)
    : modelId;
  return bare
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

export interface AiPathAlarmsConstructProps {
  config: AppConfig;
  /** AgentCore Memory ARN. The `Resource` dimension value is the full ARN. */
  memoryArn: string;
  /** AgentCore Gateway ARN. Also a full ARN in the `Resource` dimension. */
  gatewayArn: string;
  /**
   * AgentCore Code Interpreter **ID**, not ARN.
   *
   * Not an oversight — see the class docstring. Code Interpreter publishes a
   * bare id in `Resource` where Memory and Gateway publish ARNs.
   */
  codeInterpreterId: string;
  /** Platform alarm topic. Undefined leaves these alarms console-only. */
  alarmTopic?: sns.ITopic;
}

/**
 * Alarms for the managed AI services a chat turn depends on. These failures
 * present as application bugs — a Bedrock throttle reaches the user as a chat
 * that never responds, a Memory error as an agent that has forgotten the
 * conversation.
 *
 * Dimension values were enumerated with `aws cloudwatch list-metrics`, which
 * surfaced one asymmetry: Memory and Gateway publish `Resource` as a full ARN,
 * but Code Interpreter publishes a bare id.
 *
 * Memory and Code Interpreter publish a stream per API operation, so those alarms
 * sum operations via metric math (CloudWatch caps that at 10 metrics).
 *
 * NOT alarmed: Cognito, because AWS/Cognito on the ESSENTIALS feature plan
 * publishes only success metrics (failure metrics need Plus) — the auth-path
 * signal is the token-enrichment Lambda instead. And Browser, which has no metric
 * streams.
 */
export class AiPathAlarmsConstruct extends Construct {
  constructor(scope: Construct, id: string, props: AiPathAlarmsConstructProps) {
    super(scope, id);

    const { config, memoryArn, gatewayArn, codeInterpreterId } = props;
    const alarms = new AlarmFactory(this, config, props.alarmTopic);
    const errorThreshold = config.observability.agentCoreErrorThreshold;

    /** An AgentCore metric for one resource + operation. */
    const acMetric = (
      metricName: string,
      resource: string,
      operation: string,
    ) => new cloudwatch.Metric({
      namespace: AGENTCORE_NAMESPACE,
      metricName,
      dimensionsMap: { Resource: resource, Operation: operation },
      statistic: 'Sum',
      period: ALARM_PERIOD,
    });

    /** Sum one metric across several operations for a single resource. */
    const sumAcrossOperations = (
      metricName: string,
      resource: string,
      operations: string[],
    ): cloudwatch.IMetric => {
      const usingMetrics: Record<string, cloudwatch.IMetric> = {};
      operations.forEach((op, i) => {
        usingMetrics[`op${i}`] = acMetric(metricName, resource, op);
      });
      return new cloudwatch.MathExpression({
        expression: operations.map((_, i) => `op${i}`).join(' + '),
        usingMetrics,
        period: ALARM_PERIOD,
      });
    };

    // ============================================================
    // Bedrock inference
    // ============================================================

    const bedrockMetric = (metricName: string, statistic = 'Sum') => new cloudwatch.Metric({
      namespace: BEDROCK_NAMESPACE,
      metricName,
      // Account-wide roll-up. A per-ModelId variant exists, but models are
      // managed through the admin UI at runtime so a synth-time set would drift.
      statistic,
      period: ALARM_PERIOD,
    });

    // Had no metric streams when verified — never fired, rather than absent.
    // NOT_BREACHING keeps it quiet until the first occurrence.
    alarms.alarm('BedrockThrottleAlarm', {
      name: 'bedrock-invocation-throttles',
      alarmDescription:
        'Bedrock is throttling model invocations — the account is at a model TPM/RPM '
        + 'quota. Users see chats that never respond. Needs a quota increase or less '
        + 'traffic, not a code fix.',
      metric: bedrockMetric('InvocationThrottles'),
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarms.alarm('BedrockServerErrorAlarm', {
      name: 'bedrock-invocation-server-errors',
      alarmDescription:
        'Bedrock returned server-side errors on model invocation — AWS-side fault, not '
        + 'application code.',
      metric: bedrockMetric('InvocationServerErrors'),
      threshold: errorThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Quota usage is the only leading indicator in this construct: it climbs
    // before throttling starts. It has to be measured PER MODEL, because a TPM
    // quota is per model and per inference profile. The account-wide roll-up has
    // no single denominator to be a percentage of — summing a 40,000,000-quota
    // profile with a 200,000-quota one produces a number comparable to nothing,
    // and it hides the model closest to its own ceiling, which is usually the
    // one with the smallest quota rather than the most traffic.
    //
    // Statistic stays Maximum, not Average: the quota is per *minute*, the
    // period is 5 minutes, and the underlying data is 1-minute, so Maximum reads
    // the peak minute in each window. Averaging would dilute a real spike below
    // the threshold.
    //
    // No configured quotas means no alarms here at all; the backstop is
    // bedrock-invocation-throttles. See OBSERVABILITY_DEFAULT_BEDROCK_TPM_QUOTAS.
    const quotaPercent = config.observability.bedrockTpmQuotaPercent;
    for (const [modelId, tpmQuota] of Object.entries(config.observability.bedrockTpmQuotas)) {
      const slug = modelSlug(modelId);

      alarms.alarm(`BedrockQuotaUsageAlarm-${slug}`, {
        name: `bedrock-tpm-quota-usage-${slug}`,
        alarmDescription:
          `Estimated TPM quota usage for ${modelId} reached ${quotaPercent}% of its `
          + `configured ${tpmQuota} TPM quota. FIRST: confirm ${tpmQuota} is still the live `
          + 'quota — it is configured by hand and nothing checks it automatically '
          + '(aws service-quotas list-service-quotas --service-code bedrock). If it is '
          + 'current, this is the leading indicator for bedrock-invocation-throttles: '
          + 'request an increase now, because increases take lead time. The metric excludes '
          + 'quota Bedrock reserves up front from max_tokens, so real pressure can be higher.',
        metric: new cloudwatch.Metric({
          namespace: BEDROCK_NAMESPACE,
          metricName: 'EstimatedTPMQuotaUsage',
          dimensionsMap: { ModelId: modelId },
          statistic: 'Maximum',
          period: ALARM_PERIOD,
        }),
        threshold: Math.floor((tpmQuota * quotaPercent) / 100),
        evaluationPeriods: 3,
        comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
        treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
      });
    }

    // ============================================================
    // AgentCore Memory
    // ============================================================

    // Extraction and Consolidation are excluded: async background strategies
    // whose failure does not break a live turn.
    const MEMORY_HOT_PATH = [
      'CreateEvent',
      'RetrieveMemoryRecords',
      'GetMemoryRecord',
      'ListEvents',
      'GetMemory',
    ];

    alarms.expressionAlarm('MemorySystemErrorAlarm', {
      name: 'agentcore-memory-system-errors',
      alarmDescription:
        'AgentCore Memory returned server-side errors on the conversation hot path '
        + '(CreateEvent / RetrieveMemoryRecords / GetMemoryRecord / ListEvents / '
        + 'GetMemory). Users experience this as an agent that has forgotten the '
        + 'conversation.',
      expression: sumAcrossOperations('SystemErrors', memoryArn, MEMORY_HOT_PATH),
      threshold: errorThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarms.expressionAlarm('MemoryThrottleAlarm', {
      name: 'agentcore-memory-throttles',
      alarmDescription:
        'AgentCore Memory is throttling hot-path requests — turns are failing to persist '
        + 'or to retrieve context. Needs a quota review.',
      expression: sumAcrossOperations('Throttles', memoryArn, MEMORY_HOT_PATH),
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // AgentCore Gateway (MCP tools)
    // ============================================================

    // The roll-up across MCP methods; a per-Method set would multiply with
    // every tool the gateway exposes.
    const gatewayDimensions = {
      Resource: gatewayArn,
      Operation: 'InvokeGateway',
      Protocol: 'MCP',
    };

    alarms.alarm('GatewaySystemErrorAlarm', {
      name: 'agentcore-gateway-system-errors',
      alarmDescription:
        'AgentCore Gateway returned server-side errors on MCP calls — tool invocations '
        + 'are failing for reasons outside the tool Lambda itself.',
      metric: new cloudwatch.Metric({
        namespace: AGENTCORE_NAMESPACE,
        metricName: 'SystemErrors',
        dimensionsMap: gatewayDimensions,
        statistic: 'Sum',
        period: ALARM_PERIOD,
      }),
      threshold: errorThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    alarms.alarm('GatewayThrottleAlarm', {
      name: 'agentcore-gateway-throttles',
      alarmDescription:
        'AgentCore Gateway is throttling MCP calls — agents will lose tool access.',
      metric: new cloudwatch.Metric({
        namespace: AGENTCORE_NAMESPACE,
        metricName: 'Throttles',
        dimensionsMap: gatewayDimensions,
        statistic: 'Sum',
        period: ALARM_PERIOD,
      }),
      threshold: 0,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ============================================================
    // AgentCore Code Interpreter
    // ============================================================

    // `Resource` is a bare id here, NOT an ARN as it is for Memory and Gateway.
    // An ARN would match no stream and the alarm would stay green.
    alarms.expressionAlarm('CodeInterpreterSystemErrorAlarm', {
      name: 'agentcore-code-interpreter-system-errors',
      alarmDescription:
        'AgentCore Code Interpreter returned server-side errors on session start, '
        + 'invoke, or stop. Users experience this as charts and data analysis silently '
        + 'failing to appear.',
      expression: sumAcrossOperations('SystemErrors', codeInterpreterId, [
        'StartCodeInterpreterSession',
        'InvokeCodeInterpreter',
        'StopCodeInterpreterSession',
      ]),
      threshold: errorThreshold,
      evaluationPeriods: 2,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // Account-level gauge (only a Service dimension), matching the quota it
    // consumes.
    alarms.alarm('CodeInterpreterActiveSessionAlarm', {
      name: 'agentcore-code-interpreter-active-sessions',
      alarmDescription:
        'Concurrent AgentCore Code Interpreter sessions are unusually high — approaching '
        + 'the account session quota, past which new sessions are refused.',
      metric: new cloudwatch.Metric({
        namespace: AGENTCORE_NAMESPACE,
        metricName: 'ActiveSessionCount',
        dimensionsMap: { Service: 'AgentCore.CodeInterpreter' },
        statistic: 'Maximum',
        period: cdk.Duration.minutes(5),
      }),
      threshold: 50,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });
  }
}
