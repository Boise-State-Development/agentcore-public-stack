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

    // The only leading indicator here: quota usage climbs before throttling.
    alarms.alarm('BedrockQuotaUsageAlarm', {
      name: 'bedrock-tpm-quota-usage',
      alarmDescription:
        'Estimated Bedrock tokens-per-minute quota usage is high. This is the leading '
        + 'indicator for bedrock-invocation-throttles — acting on it means requesting a '
        + 'quota increase before users see failures rather than after.',
      metric: bedrockMetric('EstimatedTPMQuotaUsage', 'Maximum'),
      threshold: 80,
      evaluationPeriods: 3,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

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
