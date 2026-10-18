/**
 * The Runtime's read of assistant knowledge-base source files in rag-documents.
 *
 * `analyze_spreadsheet` runs on the AgentCore Runtime and, for a spreadsheet in
 * an assistant's knowledge base, downloads the original upload with
 * `s3.get_object` on the rag-documents bucket
 * (`agents/builtin_tools/spreadsheet_analysis/analyze_tool.py::_download_file`).
 * Those uploads live under `assistants/{assistant_id}/documents/...`. The role
 * had no read on that bucket, so the call was refused in cloud while working
 * locally, where developer credentials stand in for the role.
 *
 * The grant is read-only and limited to `assistants/*`, and it rides the
 * existing `RuntimeMemoryStatsPolicy` managed policy rather than the role's
 * inline policy: the role's statements already spill into CDK overflow
 * policies near IAM's 6,144-character cap, and a new managed policy would be
 * another resource on a stack near CloudFormation's 500.
 *
 * Compute roles exist only after `wireCompute()`, which `platform-stack.test.ts`
 * never calls, so this test builds its own wired stack.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

interface Statement {
  Sid?: string;
  Action?: string | string[];
  Resource?: unknown;
}

function actions(st: Statement): string[] {
  return Array.isArray(st.Action) ? st.Action : [st.Action ?? ''];
}

function onRagDocuments(st: Statement): boolean {
  return JSON.stringify(st.Resource ?? '').includes('RagDocumentsBucket');
}

describe('Runtime rag-documents read grant', () => {
  let template: Template;

  beforeAll(() => {
    const config = createMockConfig();
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  function runtimeStatements(): Statement[] {
    return [
      ...Object.entries(template.findResources('AWS::IAM::Policy')),
      ...Object.entries(template.findResources('AWS::IAM::ManagedPolicy')),
    ]
      .filter(([id, r]) =>
        id.includes('AgentCoreRuntimeExecutionRole') ||
        JSON.stringify(r.Properties.Roles ?? '').includes('AgentCoreRuntimeExecutionRole'),
      )
      .flatMap(([, r]) => r.Properties.PolicyDocument?.Statement ?? []);
  }

  it('grants GetObject on assistants/* of rag-documents through RuntimeMemoryStatsPolicy', () => {
    const policies = Object.entries(template.findResources('AWS::IAM::ManagedPolicy')).filter(([id]) =>
      id.includes('RuntimeMemoryStatsPolicy'),
    );
    expect(policies).toHaveLength(1);
    const [, policy] = policies[0];
    const statements: Statement[] = policy.Properties.PolicyDocument.Statement;
    const st = statements.find((s) => s.Sid === 'AssistantDocumentsRead');
    expect(st).toBeDefined();
    expect(actions(st!)).toEqual(['s3:GetObject']);
    const resource = JSON.stringify(st!.Resource);
    expect(resource).toContain('RagDocumentsBucket');
    expect(resource).toContain('/assistants/*');
    expect(JSON.stringify(policy.Properties.Roles)).toContain('AgentCoreRuntimeExecutionRole');
  });

  it('gives the Runtime role no write, delete or list on rag-documents', () => {
    const statements = runtimeStatements();
    // Non-vacuous: the role's own policies are in the template.
    expect(statements.some((st) => st.Sid === 'UserFilesBucketAccess')).toBe(true);
    const onBucket = statements.filter(onRagDocuments);
    expect(onBucket.flatMap(actions)).toEqual(['s3:GetObject']);
  });
});
