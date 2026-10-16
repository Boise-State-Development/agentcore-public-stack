/**
 * AgentCore Runtime V2 environment-payload guard.
 *
 * The V2 Runtime caps a runtime's environment variables at **2,560 bytes**
 * (docs/specs/agentcore-runtime-v2.md §3 B4). V1 has no such limit, and
 * CloudFormation only finds out when it calls UpdateAgentRuntime — after
 * synth, after tsc, after jest, after CI is green. On dev (2026-10-09) the
 * update failed with "The environment variable payload is 3007 bytes,
 * exceeding the 2560-byte maximum supported for V2 agents", the rollback hit
 * the same limit, and the stack sat in UPDATE_ROLLBACK_FAILED until a manual
 * recovery. Every platform deploy and every inference-api image roll was
 * blocked meanwhile.
 *
 * This aspect makes that a synth-time error instead. It estimates the payload
 * from the resolved template, so a deployment that selects `V2` with too large
 * a payload fails `cdk synth` (and therefore `cdk deploy`) with a message that
 * says what to do, before anything reaches AWS. On `V1` the same estimate is a
 * warning once the payload would block a later switch to V2.
 *
 * The estimate errs high on purpose. AWS's count is not our sum of key and
 * value lengths: for dev, Σ(len(key)+len(value)) was 2,938 against AWS's
 * 3,007 (+69 for 49 variables). We charge two extra bytes per variable, which
 * put dev at 3,036 — above AWS's figure. Values the template cannot resolve
 * to a literal (a GetAtt, a CloudFormation parameter) are charged a
 * conservative fixed length. The point is a guard that never says "fits" when
 * AWS would say "too big".
 */
import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import { IConstruct } from 'constructs';

/** AWS's limit on a V2 runtime's environment payload, in bytes (B4, verified 2026-10-09). */
export const RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES = 2560;

/**
 * Our own budget, about 20% under the limit: room for AWS's unexplained
 * overhead and for growth. Spec §7.5. Exceeding it is a warning on V1 and V2
 * alike; exceeding the limit on V2 is an error.
 */
export const RUNTIME_ENV_PAYLOAD_BUDGET_BYTES = 2000;

/** Per-variable overhead charged on top of key and value lengths (see the header). */
export const PER_VARIABLE_OVERHEAD_BYTES = 2;

/** Length assumed for a value the template cannot resolve to a literal. */
export const UNRESOLVED_VALUE_BYTES = 128;

export interface PayloadEstimate {
  /** Estimated bytes AWS will count for this runtime's environment. */
  bytes: number;
  /** Number of variables. */
  variables: number;
  /** Variables whose value could not be resolved to a literal and were charged UNRESOLVED_VALUE_BYTES. */
  unresolved: string[];
  /** Per-variable byte charges, largest first, for the failure message. */
  largest: Array<{ name: string; bytes: number }>;
}

type CfnValue = string | number | boolean | null | undefined | CfnValue[] | { [key: string]: CfnValue };

/**
 * Estimate the byte length of one resolved CloudFormation value.
 *
 * `stack.resolve()` turns tokens into intrinsics. A `Ref` to a resource whose
 * own name property is literal (every table, bucket and secret we name with
 * `getResourceName`) resolves to that name; `Fn::Join` is summed; pseudo
 * parameters get their real widths. Anything else is charged
 * UNRESOLVED_VALUE_BYTES and reported.
 */
export function estimateValueLength(
  stack: cdk.Stack,
  value: CfnValue,
  unresolved: string[],
  variableName: string,
  depth = 0,
): number {
  if (value === undefined || value === null) return 0;
  if (typeof value === 'string') return Buffer.byteLength(value, 'utf8');
  if (typeof value === 'number' || typeof value === 'boolean') return String(value).length;
  if (depth > 8) {
    unresolved.push(variableName);
    return UNRESOLVED_VALUE_BYTES;
  }
  if (Array.isArray(value)) {
    return value.reduce<number>((sum, part) => sum + estimateValueLength(stack, part, unresolved, variableName, depth + 1), 0);
  }

  const keys = Object.keys(value);
  if (keys.length === 1) {
    const [fn] = keys;
    const arg = value[fn];
    if (fn === 'Ref' && typeof arg === 'string') {
      if (arg === 'AWS::AccountId') return 12;
      if (arg === 'AWS::Region') return 14; // 'ap-southeast-2', the widest region name
      if (arg === 'AWS::Partition') return 'aws-us-gov'.length;
      if (arg === 'AWS::StackName') return 128;
      const named = literalNameOfResource(stack, arg);
      if (named !== undefined) return estimateValueLength(stack, named, unresolved, variableName, depth + 1);
      unresolved.push(variableName);
      return UNRESOLVED_VALUE_BYTES;
    }
    if (fn === 'Fn::Join' && Array.isArray(arg) && arg.length === 2) {
      const [delimiter, parts] = arg as [CfnValue, CfnValue];
      const list = Array.isArray(parts) ? parts : [];
      const delimiterLength = typeof delimiter === 'string' ? delimiter.length : 0;
      const body = list.reduce<number>(
        (sum, part) => sum + estimateValueLength(stack, part, unresolved, variableName, depth + 1), 0);
      return body + Math.max(0, list.length - 1) * delimiterLength;
    }
    if (fn === 'Fn::GetAtt' && Array.isArray(arg) && arg.length === 2 && typeof arg[1] === 'string') {
      // A physical id (MemoryId, CodeInterpreterId, BrowserId: `<name>-<10 chars>`,
      // ~40 bytes in practice) is charged 64; any other attribute the full fallback.
      unresolved.push(variableName);
      return arg[1].endsWith('Id') ? GENERATED_ID_BYTES : UNRESOLVED_VALUE_BYTES;
    }
    if (fn === 'Fn::Select' || fn === 'Fn::Split') {
      // CDK renders `secret.secretName` as Select/Split/Join over the secret's
      // ARN. When the expression bottoms out in a resource we named, the result
      // is at most that name plus Secrets Manager's `-XXXXXX` suffix.
      const named = nameBehindExpression(stack, arg);
      if (named !== undefined) {
        return estimateValueLength(stack, named, unresolved, variableName, depth + 1) + SECRET_SUFFIX_BYTES;
      }
    }
  }
  unresolved.push(variableName);
  return UNRESOLVED_VALUE_BYTES;
}

/** Length assumed for a service-generated physical id read through Fn::GetAtt. */
export const GENERATED_ID_BYTES = 64;

/** Secrets Manager appends `-` plus six characters to a secret's name in its ARN. */
export const SECRET_SUFFIX_BYTES = 7;

/** Walk a Select/Split/Ref expression to the first named resource it references. */
function nameBehindExpression(stack: cdk.Stack, value: CfnValue, depth = 0): CfnValue | undefined {
  if (depth > 8 || value === null || value === undefined || typeof value !== 'object') return undefined;
  if (Array.isArray(value)) {
    for (const part of value) {
      const found = nameBehindExpression(stack, part, depth + 1);
      if (found !== undefined) return found;
    }
    return undefined;
  }
  if (typeof value.Ref === 'string') return literalNameOfResource(stack, value.Ref);
  for (const inner of Object.values(value)) {
    const found = nameBehindExpression(stack, inner, depth + 1);
    if (found !== undefined) return found;
  }
  return undefined;
}

/**
 * The literal name property of the resource with this logical id, if it has one.
 *
 * Reads the resource's rendered CloudFormation rather than a per-type
 * property, so a table, bucket, secret or workload identity are all handled
 * by one lookup.
 */
function literalNameOfResource(stack: cdk.Stack, logicalId: string): CfnValue | undefined {
  for (const child of stack.node.findAll()) {
    if (!cdk.CfnResource.isCfnResource(child)) continue;
    if (stack.resolve(child.logicalId) !== logicalId) continue;
    // `_toCloudFormation` is how CfnResource renders itself at synth; it is the
    // only type-agnostic way to read the resolved property bag.
    const rendered = stack.resolve(
      (child as unknown as { _toCloudFormation(): { Resources?: Record<string, { Properties?: Record<string, CfnValue> }> } })
        ._toCloudFormation(),
    );
    const props = rendered?.Resources?.[logicalId]?.Properties ?? {};
    for (const key of ['TableName', 'BucketName', 'SecretName', 'Name', 'AgentRuntimeName', 'IndexName', 'VectorBucketName']) {
      if (props[key] !== undefined) return props[key];
    }
    return undefined;
  }
  return undefined;
}

/** Estimate the payload of one runtime's resolved EnvironmentVariables map. */
export function estimateRuntimeEnvironmentPayload(
  stack: cdk.Stack,
  environment: Record<string, CfnValue>,
): PayloadEstimate {
  const unresolved: string[] = [];
  const charges: Array<{ name: string; bytes: number }> = [];
  let bytes = 0;
  for (const [name, value] of Object.entries(environment)) {
    const charge = Buffer.byteLength(name, 'utf8')
      + estimateValueLength(stack, value, unresolved, name)
      + PER_VARIABLE_OVERHEAD_BYTES;
    charges.push({ name, bytes: charge });
    bytes += charge;
  }
  charges.sort((a, b) => b.bytes - a.bytes);
  return {
    bytes,
    variables: charges.length,
    unresolved: Array.from(new Set(unresolved)).sort(),
    largest: charges.slice(0, 8),
  };
}

/**
 * Aspect: visits every `AWS::BedrockAgentCore::Runtime` in scope and checks
 * its environment payload against the V2 limit.
 *
 * - `V2` and over the limit → **error** (synth fails; nothing reaches AWS).
 * - over the limit on `V1` → warning: V2 is blocked for this deployment.
 * - over the budget but under the limit → warning: headroom is thin.
 */
export class RuntimeEnvironmentPayloadGuard implements cdk.IAspect {
  constructor(private readonly platformVersion: string) {}

  public visit(node: IConstruct): void {
    if (!(node instanceof bedrock.CfnRuntime)) return;
    const stack = cdk.Stack.of(node);
    const environment = (stack.resolve(node.environmentVariables) ?? {}) as Record<string, CfnValue>;
    const estimate = estimateRuntimeEnvironmentPayload(stack, environment);
    const where = `${node.node.path} (${estimate.variables} variables, ~${estimate.bytes} bytes estimated` +
      (estimate.unresolved.length ? `, ${estimate.unresolved.length} unresolved: ${estimate.unresolved.join(', ')}` : '') +
      ')';
    const biggest = estimate.largest.map((c) => `${c.name}=${c.bytes}B`).join(', ');
    const remedy =
      `Shrink the Runtime's environment before selecting V2: drop variables the code derives from PROJECT_PREFIX ` +
      `(backend/src/apis/shared/config/derived_environment.json), retire dead ones, or fold booleans. ` +
      `See docs/specs/agentcore-runtime-v2.md §7. Largest: ${biggest}.`;

    if (estimate.bytes > RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES) {
      if (this.platformVersion === 'V2') {
        cdk.Annotations.of(node).addError(
          `${where} exceeds the ${RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES}-byte environment limit of the V2 AgentCore Runtime. ` +
          `UpdateAgentRuntime would be rejected and CloudFormation's rollback would fail the same way, leaving the stack ` +
          `UPDATE_ROLLBACK_FAILED (it did on dev, 2026-10-09). Set CDK_AGENTCORE_RUNTIME_PLATFORM_VERSION back to V1, or ${remedy}`,
        );
      } else {
        cdk.Annotations.of(node).addWarning(
          `${where} exceeds the ${RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES}-byte environment limit of the V2 AgentCore Runtime; ` +
          `this deployment cannot switch to V2 until it shrinks. ${remedy}`,
        );
      }
      return;
    }
    if (estimate.bytes > RUNTIME_ENV_PAYLOAD_BUDGET_BYTES) {
      cdk.Annotations.of(node).addWarning(
        `${where} is over the ${RUNTIME_ENV_PAYLOAD_BUDGET_BYTES}-byte budget (limit ${RUNTIME_ENV_PAYLOAD_LIMIT_V2_BYTES} on V2). ` +
        `Headroom is thin; the next variable may be the one that blocks V2. ${remedy}`,
      );
    }
  }
}
