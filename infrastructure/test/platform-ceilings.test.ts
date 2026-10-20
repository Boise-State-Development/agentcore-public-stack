/**
 * One synth-time guard for the platform ceilings `PlatformStack` can hit at
 * DEPLOY time, measured against the flag set a deployment actually turns on.
 *
 * Two ceilings live here, because nothing else looks at them before a deploy:
 *
 *   - CloudFormation's **500 resources per stack**. Single-stack architecture
 *     (CLAUDE.md) against a hard limit, reached by accretion: dev's live
 *     PlatformStack measured **494 of 500** on 2026-10-16 and again on
 *     2026-10-20 (492 on 10-13). The guard this file replaces asserted `< 465`
 *     against the *default* flag set, so it stayed comfortably green while the
 *     real deployment sat six resources from a deploy-time hard failure.
 *   - IAM's **6,144-character managed policy** and **10,240-character role
 *     inline policy** limits. CloudFormation rejects an oversized policy with
 *     `ServiceLimitExceeded` / `PolicySize: 6144` and rolls the stack back;
 *     that failed a dev Platform deploy on 2026-10-08 (app-api's
 *     `OverflowPolicy3`). Until now every grant near the limit carried its own
 *     hand-counted note instead ("5,473 of 6,144", "5,921 of 6,144").
 *
 * The other two ceilings already have guards of their own and are deliberately
 * NOT duplicated here: the Runtime's 2,560-byte V2 environment payload
 * (`runtime-environment-payload-guard.test.ts`) and its 50 environment
 * variables (`runtime-env-var-limit.test.ts`). One guard per ceiling, one file.
 *
 * **Why the flag set matters.** Opt-in features synthesize resources — the
 * large-document pipeline about 11, the attachments KB about 2 — so a guard
 * run against defaults measures a stack nobody deploys. This file synthesizes
 * twice: once with the shipped defaults `loadConfig()` produces, and once with
 * every opt-in flag on. `flagsLeftOff()` keeps the all-on helper honest: a new
 * flag that `allFlagsOn()` forgets fails a test of its own, by name.
 *
 * Both synths run **non-production with a domain configured**, which is the
 * expensive shape: `production: true` sheds 15 resources (measured — buckets
 * keep their contents instead of getting auto-delete custom resources), and a
 * configured domain adds its DNS records. Dev is non-production with a domain.
 * Both also carry `cdk.json`'s CDK feature flags, which a bare `new cdk.App()`
 * does not, because they change what is synthesized.
 *
 * The project prefix is the **longest a deployment may use** (21 characters,
 * `validateConfig`). Resource counts are identical at every prefix length;
 * resolved policy sizes grow about 9 bytes per prefix character across the
 * fullest policy (measured: 5,774 / 5,855 / 5,936 at 3 / 12 / 21 characters),
 * so measuring at the maximum keeps the IAM guard from passing here and
 * failing for a deployment whose prefix is longer than the test's.
 *
 * **Re-validating these numbers.** A change to this file deploys nothing —
 * `platform.yml`'s path filters cover `lib/`, `bin/`, `bootstrap-assets/`,
 * `assets/` and the platform scripts, not `test/` — which is correct, but it
 * means a deploy will never tell you whether this synth still matches reality.
 * Read the live stack instead, and do it whenever a constant here is edited or
 * the numbers in it are quoted somewhere that matters:
 *
 *     AWS_PROFILE=<env> aws cloudformation list-stack-resources \
 *       --stack-name <prefix>-PlatformStack --region <region> \
 *       --query 'length(StackResourceSummaries)'
 *
 * For a policy, take the `AWS::IAM::ManagedPolicy` physical id from the same
 * listing, then `iam get-policy-version` and measure the document as compact
 * JSON (`json.dumps(doc, separators=(',', ':'))`) — IAM does not count
 * whitespace, so compact length is the comparable figure. Check first that no
 * commit has touched `infrastructure/lib` since the stack's `LastUpdatedTime`,
 * or you are comparing against a template this code did not produce.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

import { AppConfig, loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { mockSsmContext } from './helpers/mock-config';

/** CloudFormation's hard per-stack resource limit. */
const CFN_RESOURCE_LIMIT = 500;

/**
 * `AWS::CDK::Metadata`, which the CDK **CLI** adds to every deployed template
 * (version reporting, on unless `versionReporting: false` — `cdk.json` does not
 * set it) and which `Template.fromStack` never produces. Dev's live stack
 * carries it, and so does `cdk synth` of this app; a unit-test synth therefore
 * under-counts every real deploy by exactly one resource, so the count compared
 * against the ceiling adds it back.
 */
const DEPLOY_ONLY_RESOURCES = 1;

/**
 * The all-on count this test refuses to let pass.
 *
 * 500 is the hard failure, and the margin below it is **reaction room only**: a
 * ceiling reached at deploy time is reached by a rolled-back stack, not by CI.
 * Hence 497, three resources of headroom.
 *
 * It is reaction room rather than a modelling allowance because the synth is
 * calibrated against the live stack rather than guessed. Measured on dev
 * (`dev-boisestateai-v2-PlatformStack`, 2026-10-20, template synthesized from
 * this same commit of `infrastructure/lib`): **494 live** against **494** here.
 * Every difference by resource type: +1 Route53 RecordSet dev has from its own
 * subdomain configuration, −1 CloudWatch Alarm this worst case enables and dev
 * does not. They cancel, so treat the agreement as ±1 rather than exact.
 *
 * **If this test fails, reclaim headroom — do not raise this number.** The
 * levers are known and scheduled (composite or metric-math alarms in place of
 * per-resource ones, merged managed policies, shared EventBridge targets);
 * see docs/kaizen/reviews/2026-10-16.md ▸ Proposal #2. Raising the constant
 * buys a green build and spends the margin that exists to stop a rollback.
 */
const ALL_ON_RESOURCE_CEILING = 497;

/** IAM: a managed policy document may not exceed 6,144 characters. */
const IAM_MANAGED_POLICY_LIMIT = 6144;

/** IAM: a role's inline policy documents may not exceed 10,240 characters. */
const IAM_ROLE_INLINE_POLICY_LIMIT = 10240;

/**
 * Headroom under an IAM limit that is too little for one more grant, so it has
 * to be read as "full" rather than as room. A statement with a Sid, two or
 * three actions and a pair of ARNs renders to roughly this much.
 */
const IAM_TREAT_AS_FULL_BYTES = 256;

/**
 * The longest prefix `validateConfig` accepts (`/^[a-z][a-z0-9-]{1,20}$/`).
 * Policy sizes scale with it; see the file comment.
 */
const MAX_LENGTH_PREFIX = 'platform-ceiling-test';

/**
 * Context for the expensive deployed shape: non-production (auto-delete custom
 * resources present), a custom domain with DNS records and certificates.
 */
const DEPLOYED_SHAPE: Record<string, unknown> = {
  projectPrefix: MAX_LENGTH_PREFIX,
  awsRegion: 'us-west-2',
  awsAccount: '123456789012',
  vpcCidr: '10.0.0.0/16',
  production: false,
  retainDataOnDelete: false,
  domainName: 'example.com',
  infrastructureHostedZoneDomain: 'example.com',
  manageDnsRecords: true,
  certificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/test',
  corsOrigins: 'http://localhost:4200',
  frontend: {
    cloudFrontPriceClass: 'PriceClass_100',
    certificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/test',
  },
  appApi: { cpu: 1024, memory: 2048, desiredCount: 1, maxCapacity: 2 },
  inferenceApi: {},
  fineTuning: {},
  artifacts: {
    retentionDays: 90,
    extraFrameAncestors: [],
    certificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/test',
  },
  mcpSandbox: {
    extraFrameAncestors: [],
    certificateArn: 'arn:aws:acm:us-east-1:123456789012:certificate/test',
  },
  ragIngestion: {
    additionalCorsOrigins: '',
    lambdaMemorySize: 10240,
    lambdaTimeout: 900,
    embeddingModel: 'amazon.titan-embed-text-v2',
    vectorDimension: 1024,
    vectorDistanceMetric: 'cosine',
  },
  // Costs one resource (measured: 492 -> 493), and dev configures it. Omitting
  // it is the same trap `runtime-env-var-limit.test.ts` records against itself
  // ("without it this test counts 44 while prod and dev deploy 47"): a block
  // that only synthesizes when a value is present makes a minimal config
  // under-count, and a ceiling guard that under-counts reports headroom no
  // environment has.
  tokenExchange: {
    url: 'https://tokens.example.com/v2/oauth/token',
    clientId: 'test-client',
  },
};

/**
 * `cdk.json`'s own context — CDK feature flags, nothing app-specific — so these
 * synths produce the template `cdk deploy` would. A bare `new cdk.App()` gets
 * none of them (the CLI, not the library, reads `cdk.json`), and several change
 * what is synthesized. Without this the count is off by a couple of resources
 * against the stack a deployment actually gets, which is the one number this
 * file exists to be right about.
 */
function cdkJsonContext(): Record<string, unknown> {
  const raw = fs.readFileSync(path.resolve(__dirname, '..', 'cdk.json'), 'utf-8');
  return (JSON.parse(raw).context ?? {}) as Record<string, unknown>;
}

function newApp(): cdk.App {
  return new cdk.App({ context: { ...cdkJsonContext(), ...DEPLOYED_SHAPE } });
}

/**
 * The shipped defaults, read from `loadConfig()` rather than restated, so a
 * change to a default moves the "defaults" column of this test instead of
 * being invisible to it. (`createMockConfig` is the suite's all-off baseline,
 * which is neither the shipped default nor anything a deployment runs.)
 */
function loadDefaults(): AppConfig {
  return loadConfig(newApp());
}

/**
 * Every opt-in switch on, respecting the dependencies synth enforces:
 * `attachmentKb` and `folderAttachments` both refuse without
 * `largeDocumentAttachments`.
 *
 * `ttftProbe` is operational configuration rather than a feature flag, but it
 * is opt-in and it costs a resource, so the worst case includes it.
 */
function allFlagsOn(base: AppConfig): AppConfig {
  return {
    ...base,
    conversationRetentionPrunesSessions: true,
    conversationRetentionPruneArmed: true,
    kbSync: { enabled: true },
    managedKb: {
      ...base.managedKb,
      newDefault: true,
      migrationEnabled: true,
      reconcilerArmed: true,
      docReconcilerArmed: true,
    },
    scheduledRuns: { enabled: true },
    platformCosts: { enabled: true },
    feedbackEvalSampling: { enabled: true },
    memorySpaces: { enabled: true },
    projects: { enabled: true },
    userGrants: { enabled: true },
    sessionPrewarm: { enabled: true },
    clientTurnTiming: { enabled: true },
    ttftProbe: { userId: 'ceilings-probe-user', modelId: 'us.anthropic.claude-haiku-4-5' },
    conversationIndex: { enabled: true },
    conversationSearch: { enabled: true },
    platformSelfService: { enabled: true },
    skills: { enabled: true },
    skillAuthoring: { enabled: true },
    wordTrackedChanges: { enabled: true },
    assistantKbFullContext: { enabled: true },
    // Large documents gate the next two: synth refuses either without it.
    largeDocumentAttachments: { enabled: true },
    attachmentKb: { enabled: true },
    folderAttachments: { enabled: true },
    folderSync: { enabled: true },
    agents: { enabled: true },
    agentMarketplace: { enabled: true },
    dictation: { ...base.dictation, enabled: true },
    fineTuning: { ...base.fineTuning, enabled: true },
    artifacts: { ...base.artifacts, shareInboxEnabled: true },
    frontend: { ...base.frontend, accessLogsEnabled: true },
    observability: {
      ...base.observability,
      alarmTopicEnabled: true,
      xrayInsightsNotifications: true,
      agentCoreApplicationLogsEnabled: true,
      runtimeLogRetentionSweepEnabled: true,
    },
    mcpIdentity: { tokenEnrichment: { enabled: true, accessTokenClaims: { department: 'x' } } },
    cognito: { ...base.cognito, selfSignUpEnabled: true },
  };
}

/**
 * Booleans in `AppConfig` that are a deployment's shape rather than a feature
 * switch, and which `allFlagsOn` deliberately leaves alone. `production` and
 * `retainDataOnDelete` make the stack *smaller* (no auto-delete custom
 * resources), so the worst case keeps them off; `manageDnsRecords` is already
 * on in `DEPLOYED_SHAPE`.
 */
const NOT_FEATURE_SWITCHES = new Set(['production', 'retainDataOnDelete', 'manageDnsRecords']);

/** Every boolean leaf in a config that is still `false`, by dotted path. */
function flagsLeftOff(config: AppConfig): string[] {
  const off: string[] = [];
  const walk = (node: unknown, path: string): void => {
    if (typeof node === 'boolean') {
      if (!node && !NOT_FEATURE_SWITCHES.has(path)) off.push(path);
      return;
    }
    if (node && typeof node === 'object' && !Array.isArray(node)) {
      for (const [key, value] of Object.entries(node)) {
        walk(value, path ? `${path}.${key}` : key);
      }
    }
  };
  walk(config, '');
  return off.sort();
}

function synth(config: AppConfig): Template {
  const app = newApp();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: config.awsAccount, region: config.awsRegion },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

const PHYSICAL_NAME_PROPERTY: Record<string, string> = {
  'AWS::DynamoDB::Table': 'TableName',
  'AWS::S3::Bucket': 'BucketName',
  'AWS::Lambda::Function': 'FunctionName',
  'AWS::SQS::Queue': 'QueueName',
  'AWS::IAM::Role': 'RoleName',
  'AWS::SNS::Topic': 'TopicName',
  'AWS::Logs::LogGroup': 'LogGroupName',
  'AWS::SecretsManager::Secret': 'Name',
  'AWS::StepFunctions::StateMachine': 'StateMachineName',
};

function arnFor(type: string, name: string, region: string, account: string): string {
  switch (type) {
    case 'AWS::DynamoDB::Table': return `arn:aws:dynamodb:${region}:${account}:table/${name}`;
    case 'AWS::S3::Bucket': return `arn:aws:s3:::${name}`;
    case 'AWS::Lambda::Function': return `arn:aws:lambda:${region}:${account}:function:${name}`;
    case 'AWS::SQS::Queue': return `arn:aws:sqs:${region}:${account}:${name}`;
    case 'AWS::IAM::Role': return `arn:aws:iam::${account}:role/${name}`;
    case 'AWS::SNS::Topic': return `arn:aws:sns:${region}:${account}:${name}`;
    case 'AWS::KMS::Key':
      return `arn:aws:kms:${region}:${account}:key/12345678-1234-1234-1234-123456789012`;
    case 'AWS::Logs::LogGroup': return `arn:aws:logs:${region}:${account}:log-group:${name}:*`;
    case 'AWS::SecretsManager::Secret':
      return `arn:aws:secretsmanager:${region}:${account}:secret:${name}-AbCdEf`;
    default: return `arn:aws:service:${region}:${account}:resource/${name}`;
  }
}

/**
 * Resolve a synthesized policy document far enough to measure what IAM will
 * receive. CloudFormation applies the limit to the RESOLVED document, and
 * `JSON.stringify` of the template's own document measures the intrinsics
 * instead — `{"Fn::GetAtt":["Foo","Arn"]}` is not the length of an ARN.
 *
 * Worse, CDK's own packing of overflow policies estimates every unresolved ARN
 * at a flat 150 characters (`DEFAULT_ARN_SIZE_ESTIMATE` in aws-iam) and packs
 * to 6,000, so CDK itself does not know whether a policy it produced fits.
 *
 * Accuracy, against dev's live policies read with `iam get-policy-version`
 * (2026-10-20, same commit of `infrastructure/lib` as the deployed template):
 * the tightest policy, app-api's `OverflowPolicy3`, is **5,913 here against
 * 5,921 live** — 8 characters, and it is the one that has actually failed a
 * deploy. The Runtime's `OverflowPolicy1` is 5,470 here against 5,443 live, 27
 * characters high. This is also why the `cdk.json` feature flags and the
 * longest legal prefix matter: without them those two come out 180 and 440
 * characters from what dev deploys.
 *
 * One known under-model, on a role with room to spare rather than the tight
 * one: dev's Runtime role packs a second overflow policy (code-interpreter and
 * browser grants, whose ARNs carry real provisioned AgentCore resource ids)
 * that this synth folds into one, because `mockSsmContext`'s stand-in values
 * are shorter than the real ids. Read the Runtime role's figure as a floor.
 * Its tightest policy has 701 characters free live, so nothing turns on it
 * today.
 */
function makeResolver(
  resources: Record<string, { Type: string; Properties?: Record<string, unknown> }>,
  region: string,
  account: string,
): (node: unknown) => unknown {
  const physical = (logicalId: string): string => {
    const resource = resources[logicalId];
    if (!resource) return logicalId;
    const property = PHYSICAL_NAME_PROPERTY[resource.Type];
    const name = property ? resource.Properties?.[property] : undefined;
    if (typeof name === 'string') return name;
    // CloudFormation auto-names from the logical id plus a unique suffix.
    return `${logicalId}-ABCD1234`;
  };

  const resolve = (node: unknown): unknown => {
    if (Array.isArray(node)) return node.map(resolve);
    if (!node || typeof node !== 'object') return node;

    const entries = node as Record<string, unknown>;
    const keys = Object.keys(entries);
    if (keys.length === 1) {
      const key = keys[0];
      const value = entries[key];
      if (key === 'Ref' && typeof value === 'string') {
        if (value === 'AWS::Partition') return 'aws';
        if (value === 'AWS::Region') return region;
        if (value === 'AWS::AccountId') return account;
        if (value === 'AWS::URLSuffix') return 'amazonaws.com';
        if (value === 'AWS::StackName') return 'PlatformStack';
        return physical(value);
      }
      if (key === 'Fn::GetAtt') {
        const [logicalId, attribute] = Array.isArray(value)
          ? (value as string[])
          : String(value).split('.');
        const type = resources[logicalId]?.Type ?? '';
        return String(attribute).endsWith('Arn')
          ? arnFor(type, physical(logicalId), region, account)
          : physical(logicalId);
      }
      if (key === 'Fn::Join') {
        const [separator, parts] = value as [string, unknown[]];
        return (resolve(parts) as unknown[])
          .map((part) => (typeof part === 'string' ? part : JSON.stringify(part)))
          .join(separator);
      }
      if (key === 'Fn::Sub') {
        const body = Array.isArray(value) ? (value as string[])[0] : value;
        return String(body).replace(/\$\{([^}]+)\}/g, (_match, ref: string) => {
          if (ref === 'AWS::Region') return region;
          if (ref === 'AWS::AccountId') return account;
          if (ref === 'AWS::Partition') return 'aws';
          return physical(ref.split('.')[0]);
        });
      }
      if (key === 'Fn::ImportValue') return `imported-${JSON.stringify(resolve(value))}`;
    }

    const out: Record<string, unknown> = {};
    for (const key of keys) out[key] = resolve(entries[key]);
    return out;
  };

  return resolve;
}

interface SizedPolicy {
  logicalId: string;
  type: 'AWS::IAM::Policy' | 'AWS::IAM::ManagedPolicy';
  bytes: number;
  limit: number;
}

function policySizes(template: Template, config: AppConfig): SizedPolicy[] {
  const resources = template.toJSON().Resources as Record<
    string,
    { Type: string; Properties?: Record<string, unknown> }
  >;
  const resolve = makeResolver(resources, config.awsRegion, config.awsAccount);
  const sized: SizedPolicy[] = [];
  for (const [logicalId, resource] of Object.entries(resources)) {
    if (resource.Type !== 'AWS::IAM::Policy' && resource.Type !== 'AWS::IAM::ManagedPolicy') {
      continue;
    }
    sized.push({
      logicalId,
      type: resource.Type,
      bytes: JSON.stringify(resolve(resource.Properties?.PolicyDocument)).length,
      limit: resource.Type === 'AWS::IAM::ManagedPolicy'
        ? IAM_MANAGED_POLICY_LIMIT
        : IAM_ROLE_INLINE_POLICY_LIMIT,
    });
  }
  // Tightest headroom first: that is the order a reader of the output wants.
  return sized.sort((a, b) => (a.limit - a.bytes) - (b.limit - b.bytes));
}

/** Resources this synth produced. */
function resourceCount(template: Template): number {
  return Object.keys(template.toJSON().Resources).length;
}

/** What CloudFormation is actually asked to create — see DEPLOY_ONLY_RESOURCES. */
function deployedResourceCount(template: Template): number {
  return resourceCount(template) + DEPLOY_ONLY_RESOURCES;
}

describe('platform ceilings', () => {
  let defaults: AppConfig;
  let allOn: AppConfig;
  let defaultsTemplate: Template;
  let allOnTemplate: Template;

  beforeAll(() => {
    defaults = loadDefaults();
    allOn = allFlagsOn(defaults);
    defaultsTemplate = synth(defaults);
    allOnTemplate = synth(allOn);
  });

  it('turns every opt-in flag on in the all-on config', () => {
    // Self-policing: a flag added to AppConfig that allFlagsOn() does not set
    // fails here by name, instead of silently measuring a stack that is one
    // feature short of the worst case.
    const off = flagsLeftOff(allOn);
    if (off.length > 0) {
      throw new Error(
        `allFlagsOn() left ${off.length} switch(es) off: ${off.join(', ')}. `
        + 'This guard measures the ceilings against every opt-in feature, so a new flag '
        + 'has to be added to allFlagsOn() — otherwise the counts below describe a stack '
        + 'nobody deploys. If the field is a deployment shape rather than a feature '
        + 'switch, add it to NOT_FEATURE_SWITCHES with a reason.',
      );
    }
    expect(off).toEqual([]);
  });

  it('only adds resources when flags are turned on', () => {
    // Sanity on the two synths: if all-on were not the larger stack, the
    // ceiling below would be measured against the wrong one.
    expect(resourceCount(allOnTemplate)).toBeGreaterThan(resourceCount(defaultsTemplate));
  });

  it('stays under the resource ceiling with every opt-in flag on', () => {
    const count = deployedResourceCount(allOnTemplate);

    // Thrown rather than asserted so the guidance travels with the failure —
    // Jest's expect() carries no message argument.
    if (count > ALL_ON_RESOURCE_CEILING) {
      throw new Error(
        `PlatformStack synthesizes ${count} resources with every opt-in flag on; `
        + `this guard allows ${ALL_ON_RESOURCE_CEILING} of CloudFormation's `
        + `${CFN_RESOURCE_LIMIT}. The margin is not spare room: this count is calibrated `
        + "against dev's live stack (494 live, 494 here on 2026-10-20), so it is three "
        + 'resources of room to react, and a stack that reaches the limit reaches it as a '
        + 'rolled-back deploy rather than as a red build. '
        + 'RECLAIM HEADROOM — do not raise the ceiling. The levers are composite or '
        + 'metric-math alarms in place of per-resource ones, merged managed policies, and '
        + 'shared EventBridge targets; see docs/kaizen/reviews/2026-10-16.md Proposal #2.',
      );
    }
    expect(count).toBeLessThanOrEqual(ALL_ON_RESOURCE_CEILING);
  });

  it('stays under the resource ceiling with the shipped defaults', () => {
    // A deployment that turns nothing on still has to deploy.
    expect(deployedResourceCount(defaultsTemplate)).toBeLessThanOrEqual(ALL_ON_RESOURCE_CEILING);
  });

  it('keeps every IAM policy inside the limit CloudFormation enforces', () => {
    for (const [label, template, config] of [
      ['shipped defaults', defaultsTemplate, defaults],
      ['every flag on', allOnTemplate, allOn],
    ] as [string, Template, AppConfig][]) {
      const policies = policySizes(template, config);
      expect(policies.length).toBeGreaterThan(0);

      for (const policy of policies) {
        // CDK packs its overflow policies against its own 6,000 estimate with
        // every unresolved ARN counted as a flat 150 characters, so CDK cannot
        // catch this. The deploy does, by rolling the stack back.
        if (policy.bytes > policy.limit) {
          throw new Error(
            `${policy.logicalId} renders to ${policy.bytes} characters (${label}); IAM allows `
            + `${policy.limit} for ${policy.type === 'AWS::IAM::ManagedPolicy'
              ? 'a managed policy' : "a role's inline policies"}. CloudFormation fails the `
            + 'deploy with ServiceLimitExceeded and rolls the stack back. Give the new grant '
            + 'its own iam.ManagedPolicy rather than adding it to the role (a role may attach '
            + '10 managed policies), which also keeps CDK from repacking the overflow '
            + 'policies and replacing them all.',
          );
        }
        expect(policy.bytes).toBeLessThanOrEqual(policy.limit);
      }
    }
  });

  it('reports both counts and the tightest policies, so the next PR knows what it has', () => {
    // Not assertions — numbers in the test output. The ceilings above are the
    // gate; this is the early warning, and it is why both synths are printed:
    // a PR sees its own headroom change without reading this file.
    const defaultCount = deployedResourceCount(defaultsTemplate);
    const allOnCount = deployedResourceCount(allOnTemplate);
    console.log(
      `[resource headroom] shipped defaults: ${defaultCount}/${CFN_RESOURCE_LIMIT}; `
      + `every opt-in flag on: ${allOnCount}/${CFN_RESOURCE_LIMIT} `
      + `(${ALL_ON_RESOURCE_CEILING - allOnCount} under this guard's `
      + `${ALL_ON_RESOURCE_CEILING}, ${CFN_RESOURCE_LIMIT - allOnCount} under CloudFormation's `
      + `${CFN_RESOURCE_LIMIT})`,
    );

    const policies = policySizes(allOnTemplate, allOn);
    for (const policy of policies.slice(0, 5)) {
      console.log(
        `[policy headroom] ${policy.logicalId}: ${policy.bytes}/${policy.limit} characters, `
        + `${policy.limit - policy.bytes} free`,
      );
    }

    // A policy with less headroom than one grant needs is full. app-api's
    // OverflowPolicy3 already sits here; reclaiming that is not this test's job,
    // but a PR that adds another one should not be able to do it unseen.
    const full = policies.filter((p) => p.limit - p.bytes < IAM_TREAT_AS_FULL_BYTES);
    for (const policy of full) {
      console.log(
        `[policy FULL] ${policy.logicalId}: ${policy.bytes}/${policy.limit} characters, only `
        + `${policy.limit - policy.bytes} free — less than the ~${IAM_TREAT_AS_FULL_BYTES} one `
        + 'more grant renders to. The next grant on that role needs its own managed policy '
        + '(a role may attach 10), not another statement here.',
      );
    }

    expect(defaultCount).toBeGreaterThan(0);
    expect(allOnCount).toBeGreaterThan(0);
  });
});
