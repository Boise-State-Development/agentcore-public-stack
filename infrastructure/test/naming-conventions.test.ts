/**
 * Final batch — table naming convention tests + misc assertions.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { createMockConfig, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

import { AuthTablesConstruct } from '../lib/constructs/data/auth-tables-construct';
import { QuotaTablesConstruct } from '../lib/constructs/data/quota-tables-construct';
import { CostTrackingTablesConstruct } from '../lib/constructs/data/cost-tracking-tables-construct';
import { AdminTablesConstruct } from '../lib/constructs/data/admin-tables-construct';
import { FineTuningDataConstruct } from '../lib/constructs/fine-tuning/fine-tuning-data-construct';
import { RagDataConstruct } from '../lib/constructs/rag/rag-data-construct';
import { ArtifactsDataConstruct } from '../lib/constructs/artifacts/artifacts-data-construct';
import { SharedConversationsConstruct } from '../lib/constructs/data/shared-conversations-construct';
import { FileUploadConstruct } from '../lib/constructs/data/file-upload-construct';
import { VoiceTicketConstruct } from '../lib/constructs/identity/voice-ticket-construct';

function testStack(): cdk.Stack {
  return new cdk.Stack(new cdk.App(), 'TestStack', {
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
}

const config = createMockConfig();

describe('Table naming convention — all tables use {prefix}-{name}', () => {
  it('auth tables follow naming convention', () => {
    const stack = testStack();
    new AuthTablesConstruct(stack, 'AT', { config });
    const t = Template.fromStack(stack);
    const tables = t.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.TableName).toMatch(/^test-project-/);
    }
  });

  it('quota tables follow naming convention', () => {
    const stack = testStack();
    new QuotaTablesConstruct(stack, 'QT', { config });
    const t = Template.fromStack(stack);
    const tables = t.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.TableName).toMatch(/^test-project-/);
    }
  });

  it('cost-tracking tables follow naming convention', () => {
    const stack = testStack();
    new CostTrackingTablesConstruct(stack, 'CT', { config });
    const t = Template.fromStack(stack);
    const tables = t.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.TableName).toMatch(/^test-project-/);
    }
  });

  it('admin tables follow naming convention', () => {
    const stack = testStack();
    new AdminTablesConstruct(stack, 'Admin', { config });
    const t = Template.fromStack(stack);
    const tables = t.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.TableName).toMatch(/^test-project-/);
    }
  });

  it('fine-tuning tables follow naming convention', () => {
    const stack = testStack();
    new FineTuningDataConstruct(stack, 'FT', { config });
    const t = Template.fromStack(stack);
    const tables = t.findResources('AWS::DynamoDB::Table');
    for (const [, resource] of Object.entries(tables)) {
      expect((resource as any).Properties.TableName).toMatch(/^test-project-/);
    }
  });

  it('RAG assistants table follows naming convention', () => {
    const stack = testStack();
    new RagDataConstruct(stack, 'Rag', { config });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::DynamoDB::Table', { TableName: 'test-project-rag-assistants' });
  });

  it('artifacts table follows naming convention', () => {
    const stack = testStack();
    new ArtifactsDataConstruct(stack, 'Art', { config });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::DynamoDB::Table', { TableName: 'test-project-user-artifacts' });
  });

  it('shared-conversations table follows naming convention', () => {
    const stack = testStack();
    new SharedConversationsConstruct(stack, 'SC', { config });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::DynamoDB::Table', { TableName: 'test-project-shared-conversations' });
  });

  it('file-uploads table follows naming convention', () => {
    const stack = testStack();
    new FileUploadConstruct(stack, 'FU', { config });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::DynamoDB::Table', { TableName: 'test-project-user-file-uploads' });
  });

  it('voice-ticket-replay table follows naming convention', () => {
    const stack = testStack();
    new VoiceTicketConstruct(stack, 'Voice', { config });
    const t = Template.fromStack(stack);
    t.hasResourceProperties('AWS::DynamoDB::Table', { TableName: 'test-project-voice-ticket-replay' });
  });
});

describe('All tables use PAY_PER_REQUEST billing', () => {
  const constructFactories = [
    { name: 'AuthTables', factory: () => new AuthTablesConstruct(testStack(), 'X', { config }) },
    { name: 'QuotaTables', factory: () => new QuotaTablesConstruct(testStack(), 'X', { config }) },
    { name: 'CostTracking', factory: () => new CostTrackingTablesConstruct(testStack(), 'X', { config }) },
    { name: 'Admin', factory: () => new AdminTablesConstruct(testStack(), 'X', { config }) },
    { name: 'FineTuning', factory: () => new FineTuningDataConstruct(testStack(), 'X', { config }) },
    { name: 'RAG', factory: () => new RagDataConstruct(testStack(), 'X', { config }) },
    { name: 'Artifacts', factory: () => new ArtifactsDataConstruct(testStack(), 'X', { config }) },
  ];

  for (const { name, factory } of constructFactories) {
    it(`${name} tables use PAY_PER_REQUEST`, () => {
      const stack = testStack();
      // Need to create in a fresh stack for Template
      const s = testStack();
      if (name === 'AuthTables') new AuthTablesConstruct(s, 'X', { config });
      else if (name === 'QuotaTables') new QuotaTablesConstruct(s, 'X', { config });
      else if (name === 'CostTracking') new CostTrackingTablesConstruct(s, 'X', { config });
      else if (name === 'Admin') new AdminTablesConstruct(s, 'X', { config });
      else if (name === 'FineTuning') new FineTuningDataConstruct(s, 'X', { config });
      else if (name === 'RAG') new RagDataConstruct(s, 'X', { config });
      else if (name === 'Artifacts') new ArtifactsDataConstruct(s, 'X', { config });
      const t = Template.fromStack(s);
      const tables = t.findResources('AWS::DynamoDB::Table');
      for (const [, resource] of Object.entries(tables)) {
        expect((resource as any).Properties.BillingMode).toBe('PAY_PER_REQUEST');
      }
    });
  }
});
