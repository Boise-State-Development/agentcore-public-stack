/**
 * Conversation-search index consumer — CDK assertions
 * (docs/specs/conversation-search.md §4, PR-2b).
 *
 * What a synth can prove and a deploy would only reveal late:
 *   - the two archive rules match exactly `Object Created` / `Object Deleted`
 *     on the archive bucket under `conversations/`, and stay disabled while
 *     the index flag is off;
 *   - events reach the consumer through a queue with a DLQ, in batches of ten
 *     at a concurrency of two (the per-account document quotas are shared
 *     with agent uploads);
 *   - the consumer reads `conversations/*` and nothing else in the bucket,
 *     touches only its own partition of the assistants table, and can create
 *     a knowledge base but never delete one;
 *   - the bootstrap image is byte-stable and carries the handler's module.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import * as fs from 'fs';
import * as path from 'path';

import { AppConfig } from '../lib/config';
import { ConversationArchiveConstruct } from '../lib/constructs/data/conversation-archive-construct';
import {
  CONVERSATION_INDEX_ESM,
  CONVERSATION_INDEX_MAX_RECEIVE_COUNT,
  CONVERSATION_INDEX_TIMEOUT_MINUTES,
  ConversationIndexConstruct,
} from '../lib/constructs/conversation-index/conversation-index-construct';
import { ManagedKbRoleConstruct } from '../lib/constructs/managed-kb/managed-kb-role-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const BOOTSTRAP_DIR = path.resolve(__dirname, '..', 'bootstrap-assets', 'conversation-index');
const HANDLER = 'apis.app_api.conversation_index.consumer.lambda_handler';

interface PolicyStatement {
  Sid?: string;
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
  Condition?: Record<string, Record<string, unknown>>;
}

function synth(enabled = true): Template {
  const app = new cdk.App();
  const stack = new cdk.Stack(app, 'Test', { env: { account: MOCK_ACCOUNT, region: MOCK_REGION } });
  const config: AppConfig = createMockConfig({ conversationIndex: { enabled } });
  const ragData = new RagDataConstruct(stack, 'RagData', { config });
  const archive = new ConversationArchiveConstruct(stack, 'ConversationArchive', { config });
  const managedKbRole = new ManagedKbRoleConstruct(stack, 'ManagedKbRole', {
    config,
    documentsBucket: ragData.documentsBucket,
  });
  new ConversationIndexConstruct(stack, 'ConversationIndex', {
    config,
    archiveBucket: archive.bucket,
    assistantsTable: ragData.assistantsTable,
    managedKbRole,
  });
  return Template.fromStack(stack);
}

function consumerProps(t: Template): Record<string, any> {
  const matches = Object.values(t.findResources('AWS::Lambda::Function'))
    .map((r) => r.Properties)
    .filter((p) => p.ImageConfig?.Command?.[0] === HANDLER);
  expect(matches).toHaveLength(1);
  return matches[0];
}

/** Statements on the consumer's own role policy. */
function consumerStatements(t: Template): PolicyStatement[] {
  const out: PolicyStatement[] = [];
  for (const r of Object.values(t.findResources('AWS::IAM::Policy'))) {
    const roles = JSON.stringify(r.Properties.Roles ?? []);
    if (!roles.includes('ConversationIndexConsumerLambda')) continue;
    out.push(...(r.Properties.PolicyDocument?.Statement ?? []));
  }
  return out;
}

function actions(statements: PolicyStatement[]): string[] {
  return statements.flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action ?? '']));
}

/** The archive event rules (the reconciler's schedule rule has no pattern). */
function rules(t: Template): Record<string, any>[] {
  return Object.values(t.findResources('AWS::Events::Rule'))
    .map((r) => r.Properties)
    .filter((p) => p.EventPattern);
}

const RECONCILER_HANDLER = 'apis.app_api.conversation_index.reconciler.lambda_handler';
const RECONCILER_BOOTSTRAP_DIR = path.resolve(
  __dirname, '..', 'bootstrap-assets', 'conversation-index-reconciler',
);

function reconcilerProps(t: Template): Record<string, any> {
  const matches = Object.values(t.findResources('AWS::Lambda::Function'))
    .map((r) => r.Properties)
    .filter((p) => p.ImageConfig?.Command?.[0] === RECONCILER_HANDLER);
  expect(matches).toHaveLength(1);
  return matches[0];
}

function reconcilerStatements(t: Template): PolicyStatement[] {
  const out: PolicyStatement[] = [];
  for (const r of Object.values(t.findResources('AWS::IAM::Policy'))) {
    const roles = JSON.stringify(r.Properties.Roles ?? []);
    if (!roles.includes('ConversationIndexReconcilerLambda')) continue;
    out.push(...(r.Properties.PolicyDocument?.Statement ?? []));
  }
  return out;
}

describe('ConversationIndexConstruct — rules', () => {
  it('routes Object Created and Object Deleted under conversations/ on the archive bucket', () => {
    const t = synth();
    const detailTypes = rules(t).map((r) => r.EventPattern['detail-type']).sort();
    expect(detailTypes).toEqual([['Object Created'], ['Object Deleted']]);
    for (const rule of rules(t)) {
      expect(rule.EventPattern.source).toEqual(['aws.s3']);
      expect(rule.EventPattern.detail.object.key).toEqual([{ prefix: 'conversations/' }]);
      expect(JSON.stringify(rule.EventPattern.detail.bucket.name)).toContain('ConversationArchiveBucket');
      expect(JSON.stringify(rule.Targets[0].Arn)).toContain('ConversationIndexQueue');
      expect(JSON.stringify(rule.Targets[0].DeadLetterConfig)).toContain('ConversationIndexDlq');
    }
  });

  it('is enabled only when the index flag is on', () => {
    expect(rules(synth(true)).map((r) => r.State)).toEqual(['ENABLED', 'ENABLED']);
    expect(rules(synth(false)).map((r) => r.State)).toEqual(['DISABLED', 'DISABLED']);
  });

  it('mirrors the flag into the consumer environment', () => {
    expect(consumerProps(synth(true)).Environment.Variables.CONVERSATION_INDEX_ENABLED).toBe('true');
    expect(consumerProps(synth(false)).Environment.Variables.CONVERSATION_INDEX_ENABLED).toBe('false');
  });
});

describe('ConversationIndexConstruct — queue and consumer', () => {
  it('drains the queue in batches of ten at a concurrency of two, reporting item failures', () => {
    synth().hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      BatchSize: CONVERSATION_INDEX_ESM.BATCH_SIZE,
      MaximumBatchingWindowInSeconds: CONVERSATION_INDEX_ESM.BATCHING_WINDOW_SECONDS,
      ScalingConfig: { MaximumConcurrency: CONVERSATION_INDEX_ESM.MAX_CONCURRENCY },
      FunctionResponseTypes: ['ReportBatchItemFailures'],
    });
    expect(CONVERSATION_INDEX_ESM.BATCH_SIZE).toBe(10); // the KB document APIs' per-call cap
  });

  it('redrives to a DLQ and gives a slow invocation six timeouts of visibility', () => {
    const t = synth();
    t.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'test-project-conversation-index',
      VisibilityTimeout: CONVERSATION_INDEX_TIMEOUT_MINUTES * 60 * 6,
      RedrivePolicy: {
        maxReceiveCount: CONVERSATION_INDEX_MAX_RECEIVE_COUNT,
        deadLetterTargetArn: { 'Fn::GetAtt': [Match.stringLikeRegexp('ConversationIndexDlq'), 'Arn'] },
      },
      SqsManagedSseEnabled: true,
    });
    t.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'test-project-conversation-index-dlq',
      MessageRetentionPeriod: 14 * 24 * 3600,
    });
  });

  it('lets EventBridge send to the queue', () => {
    synth().hasResourceProperties('AWS::SQS::QueuePolicy', {
      PolicyDocument: {
        Statement: Match.arrayWith([
          Match.objectLike({
            Action: Match.arrayWith(['sqs:SendMessage']),
            Principal: { Service: 'events.amazonaws.com' },
          }),
        ]),
      },
    });
  });

  it('is an arm64 image Lambda on the bootstrap asset with a timeout that covers provisioning', () => {
    const props = consumerProps(synth());
    expect(props.PackageType).toBe('Image');
    expect(props.Architectures).toEqual(['arm64']);
    expect(props.Timeout).toBe(CONVERSATION_INDEX_TIMEOUT_MINUTES * 60);
    // 124 s worst case to ACTIVE plus a six-minute wait for a concurrent provisioner.
    expect(props.Timeout).toBeGreaterThanOrEqual(360 + 124);
  });

  it('carries the tag contract values so the reconciler recognises the knowledge base', () => {
    const vars = consumerProps(synth()).Environment.Variables;
    expect(vars.MANAGED_KB_TAG_VALUE_PREFIX).toBe('test-project');
    expect(vars.MANAGED_KB_TAG_VALUE_ENVIRONMENT).toBeTruthy();
    expect(vars.MANAGED_KB_METRIC_NAMESPACE).toBe('test-project/ManagedKb');
    expect(vars.MANAGED_KB_SERVICE_ROLE_ARN).toBeDefined();
    expect(vars.CONVERSATION_ARCHIVE_BUCKET_NAME).toBeDefined();
    expect(vars.DYNAMODB_ASSISTANTS_TABLE_NAME).toBeDefined();
  });

  it('publishes its function name for the code-deploy step', () => {
    synth().hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/conversation-index/consumer-function-name',
    });
  });
});

describe('ConversationIndexConstruct — IAM', () => {
  it('reads archive objects under conversations/ only', () => {
    const statements = consumerStatements(synth());
    const s3 = statements.filter((s) => actions([s]).some((a) => a.startsWith('s3:')));
    expect(actions(s3).sort()).toEqual(['s3:GetObject', 's3:ListBucket']);
    const read = s3.find((s) => s.Sid === 'ConversationIndexArchiveRead')!;
    expect(JSON.stringify(read.Resource)).toContain('/conversations/*');
    expect(actions(statements)).not.toContain('s3:PutObject');
    expect(actions(statements)).not.toContain('s3:DeleteObject');
  });

  it('touches only the conversations partition of the assistants table', () => {
    const statements = consumerStatements(synth());
    const ddb = statements.filter((s) => actions([s]).some((a) => a.startsWith('dynamodb:')));
    expect(ddb).toHaveLength(1);
    expect(actions(ddb).sort()).toEqual(['dynamodb:GetItem', 'dynamodb:PutItem', 'dynamodb:UpdateItem']);
    expect(ddb[0].Condition).toEqual({
      'ForAllValues:StringEquals': { 'dynamodb:LeadingKeys': ['AST#conversations'] },
    });
  });

  it('can create the knowledge base and ingest or delete documents, but never delete a knowledge base', () => {
    const granted = actions(consumerStatements(synth()));
    for (const needed of [
      'bedrock:CreateKnowledgeBase',
      'bedrock:GetKnowledgeBase',
      'bedrock:CreateDataSource',
      'bedrock:TagResource',
      'bedrock:IngestKnowledgeBaseDocuments',
      'bedrock:DeleteKnowledgeBaseDocuments',
      'iam:PassRole',
    ]) {
      expect(granted).toContain(needed);
    }
    expect(granted).not.toContain('bedrock:DeleteKnowledgeBase');
    expect(granted).not.toContain('bedrock:DeleteDataSource');
  });

  it('may pass the KB service role to Bedrock and nothing else', () => {
    const pass = consumerStatements(synth()).find((s) => s.Sid === 'ManagedKbCreatePassServiceRole')!;
    expect(pass.Condition).toEqual({ StringEquals: { 'iam:PassedToService': 'bedrock.amazonaws.com' } });
  });
});

describe('conversation-index bootstrap asset', () => {
  it('contains exactly the Dockerfile and the stub module', () => {
    // Anything else here moves the asset digest and reverts the function to
    // the stub on the next platform deploy.
    expect(fs.readdirSync(BOOTSTRAP_DIR).sort()).toEqual(['Dockerfile', 'consumer.py']);
  });

  it('pins its base image by digest and COPYs the handler module', () => {
    const dockerfile = fs.readFileSync(path.join(BOOTSTRAP_DIR, 'Dockerfile'), 'utf8');
    const from = dockerfile.split('\n').filter((l) => l.startsWith('FROM '));
    expect(from).toHaveLength(1);
    expect(from[0]).toMatch(/@sha256:[0-9a-f]{64}$/);
    expect(dockerfile).toContain('apis/app_api/conversation_index/consumer.py');
  });

  it('produces an identical image digest across independent synths', () => {
    const first = JSON.stringify(consumerProps(synth()).Code?.ImageUri);
    const second = JSON.stringify(consumerProps(synth()).Code?.ImageUri);
    expect(first).toBe(second);
    expect(first).not.toBe('undefined');
  });
});

describe('ConversationIndexConstruct — daily reconciler', () => {
  it('is a second arm64 function on its own bootstrap asset', () => {
    const t = synth();
    const props = reconcilerProps(t);
    expect(props.Architectures).toEqual(['arm64']);
    expect(props.Timeout).toBe(15 * 60);
    // Its own asset, so the consumer's stub digest is untouched.
    expect(JSON.stringify(props.Code.ImageUri)).not.toBe(JSON.stringify(consumerProps(t).Code.ImageUri));
  });

  it('runs daily whatever the index flag says', () => {
    for (const enabled of [true, false]) {
      const schedules = Object.values(synth(enabled).findResources('AWS::Events::Rule'))
        .map((r) => r.Properties)
        .filter((p) => p.ScheduleExpression);
      expect(schedules).toHaveLength(1);
      expect(schedules[0].ScheduleExpression).toBe('cron(0 10 * * ? *)');
      expect(schedules[0].State).toBe('ENABLED');
    }
  });

  it('carries the retention setting and the archive bucket', () => {
    const env = reconcilerProps(synth()).Environment.Variables;
    expect(env.CONVERSATION_RETENTION_DAYS).toBe('365');
    expect(env.CONVERSATION_ARCHIVE_BUCKET_NAME).toBeDefined();
    expect(env.DYNAMODB_ASSISTANTS_TABLE_NAME).toBeDefined();
  });

  it('lists and deletes documents, but can neither ingest nor create nor delete a knowledge base', () => {
    const granted = actions(reconcilerStatements(synth()));
    expect(granted).toEqual(expect.arrayContaining([
      'bedrock:ListKnowledgeBaseDocuments',
      'bedrock:DeleteKnowledgeBaseDocuments',
    ]));
    for (const forbidden of [
      'bedrock:IngestKnowledgeBaseDocuments',
      'bedrock:CreateKnowledgeBase',
      'bedrock:DeleteKnowledgeBase',
      'iam:PassRole',
      'dynamodb:PutItem',
      'dynamodb:UpdateItem',
    ]) {
      expect(granted).not.toContain(forbidden);
    }
  });

  it('deletes archive objects under conversations/ only', () => {
    const del = reconcilerStatements(synth()).find((s) => s.Sid === 'ConversationReconcilerArchiveExpire');
    expect(del?.Action).toBe('s3:DeleteObject');
    expect(JSON.stringify(del?.Resource)).toContain('/conversations/*');
  });

  it('reads only the conversations partition of the assistants table', () => {
    const record = reconcilerStatements(synth()).find((s) => s.Sid === 'ConversationReconcilerKbRecordRead');
    expect(record?.Action).toBe('dynamodb:GetItem');
    expect(record?.Condition?.['ForAllValues:StringEquals']?.['dynamodb:LeadingKeys']).toEqual(['AST#conversations']);
  });

  it('publishes its function name for the code-deploy step', () => {
    synth().hasResourceProperties('AWS::SSM::Parameter', {
      Name: '/test-project/conversation-index/reconciler-function-name',
    });
  });

  it('has a byte-stable bootstrap asset holding exactly the stub', () => {
    expect(fs.readdirSync(RECONCILER_BOOTSTRAP_DIR).sort()).toEqual(['Dockerfile', 'reconciler.py']);
    const dockerfile = fs.readFileSync(path.join(RECONCILER_BOOTSTRAP_DIR, 'Dockerfile'), 'utf8');
    expect(dockerfile).toContain('apis/app_api/conversation_index/reconciler.py');
    expect(dockerfile.split('\n').find((l) => l.startsWith('FROM '))).toMatch(/@sha256:[0-9a-f]{64}$/);
    const first = JSON.stringify(reconcilerProps(synth()).Code?.ImageUri);
    expect(JSON.stringify(reconcilerProps(synth()).Code?.ImageUri)).toBe(first);
  });
});
