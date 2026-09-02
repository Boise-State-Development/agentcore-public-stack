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
 * AiPathAlarmsConstruct — alarms for the managed AI services a chat turn depends
 * on: Bedrock inference, AgentCore Memory, Gateway, and Code Interpreter.
 *
 * These are the dependencies whose failures look like application bugs. A
 * Bedrock throttle reaches a user as a chat that will not respond; a Memory
 * error reaches them as an agent that has forgotten the conversation. With no
 * alarms here the first hypothesis is always "our code broke", when the cause is
 * a quota or an AWS-side fault two layers down.
 *
 * ## Every dimension value here was read off the live account
 *
 * Metric names and dimension keys were enumerated with
 * `aws cloudwatch list-metrics` rather than taken from documentation, because
 * this construct's sibling (the AgentCore Runtime alarms) had spent its entire
 * life watching three metric names that exist in no namespace at all — sitting
 * in INSUFFICIENT_DATA and reading as healthy.
 *
 * That sweep turned up an inconsistency worth stating plainly, because it is
 * invisible until an alarm silently matches nothing:
 *
 *   - Memory publishes `Resource` as a full ARN
 *     (`arn:aws:bedrock-agentcore:...:memory/name-SUFFIX`)
 *   - Gateway publishes `Resource` as a full ARN
 *   - **Code Interpreter publishes `Resource` as a bare id** (`name-SUFFIX`),
 *     with no ARN prefix
 *
 * ## Metric math, because these metrics are dimensioned per Operation
 *
 * Unlike the Runtime, whose `Operation` is always `InvokeAgentRuntime`, Memory
 * and Code Interpreter publish a separate stream per API operation, so a single
 * alarm has to sum the operations that matter. Each expression stays well inside
 * CloudWatch's limit of 10 individual metrics per math-expression alarm.
 *
 * ## What is deliberately NOT alarmed
 *
 * **Cognito.** The plan called for a sign-in failure alarm. `AWS/Cognito`
 * publishes only `SignInSuccesses`, `SignUpSuccesses`, `TokenRefreshSuccesses`
 * and `FederationSuccesses` — there is no failure or throttle metric, because
 * those require the Cognito **Plus** feature plan and this pool runs on
 * `ESSENTIALS`. An alarm on a non-existent metric is exactly the dead alarm this
 * effort exists to remove, so it is omitted rather than written hopefully. The
 * real auth-path failure signal is the token-enrichment Lambda's `Errors`
 * metric, covered by LambdaAlarmsConstruct.
 *
 * **AgentCore Browser.** No metric streams exist for it in the account — the
 * feature is provisioned but unused. Same reasoning: the alarm would be blind.
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
      // No dimensions: the account-wide roll-up. A per-ModelId variant exists,
      // but models are added and removed through the admin UI at runtime, so a
      // per-model alarm set fixed at synth time would drift out of step with
      // whatever is actually enabled.
      statistic,
      period: ALARM_PERIOD,
    });

    // Bedrock throttling is the most likely cause of "the chat is broken" that
    // is not a bug. Threshold 0: a throttle means the account is at a model's
    // TPM/RPM quota, which does not resolve without less traffic or a quota
    // increase.
    //
    // This metric had NO streams when the bindings were verified, meaning it has
    // never fired rather than that it does not exist. NOT_BREACHING keeps the
    // alarm quiet until the first real occurrence.
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

    // Saturation, and the only LEADING indicator in this file: quota usage
    // climbing toward the limit is visible before throttling starts. Unlike the
    // two metrics above, this one has live data in the account today.
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

    // The operations on the conversation hot path. CreateEvent writes each turn,
    // RetrieveMemoryRecords reads context back, and the Get/List calls serve the
    // memory dashboard.
    //
    // Extraction and Consolidation are excluded on purpose: they are
    // asynchronous background strategies whose failures do not break a live
    // turn, so including them would make this alarm fire for something no user
    // ever notices.
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

    // Gateway streams carry Protocol, plus a Method dimension for the specific
    // MCP call. The [Resource, Operation, Protocol] set is the roll-up across
    // methods, which is what an alarm wants — a per-Method alarm set would
    // multiply with every tool the gateway exposes.
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

    // NOTE the `Resource` value: a bare id, NOT an ARN. Memory and Gateway above
    // both use full ARNs for the same dimension key. Verified by enumerating the
    // live streams — passing an ARN here would produce an alarm that matches
    // nothing and stays permanently green.
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

    // Concurrent session ceiling. Published per service type with only a
    // `Service` dimension, so this is an account-level gauge rather than a
    // per-resource one — which is the right granularity, since the quota it
    // consumes is also account-level.
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
