/**
 * Regression cover for the RAG documents bucket's lifecycle rule.
 *
 * The bucket is versioned, so every delete the app makes in it (document
 * cleanup, icon replace/remove, agent-delete icon cleanup, the orphaned-row
 * cleanup script) only writes a delete marker. With no lifecycle rule the
 * bytes stayed forever as noncurrent versions. These pin:
 *
 *   - versioning stays on (it is the accidental-delete recovery net),
 *   - noncurrent versions expire after 35 days — the assistants table's
 *     PITR window, so a PITR-restorable DOC# row still has its bytes,
 *   - orphaned delete markers and stalled multipart uploads are cleaned up.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

describe('RAG documents bucket lifecycle', () => {
  let template: Template;

  beforeAll(() => {
    const config = createMockConfig();
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'RagDocumentsLifecyclePlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    template = Template.fromStack(stack);
  });

  function documentsBucket(): Record<string, any> {
    const buckets = template.findResources('AWS::S3::Bucket');
    const ids = Object.keys(buckets).filter((id) => id.includes('RagDocumentsBucket'));
    expect(ids).toHaveLength(1);
    return buckets[ids[0]].Properties;
  }

  it('stays versioned', () => {
    expect(documentsBucket().VersioningConfiguration).toEqual({ Status: 'Enabled' });
  });

  it('expires noncurrent versions after 35 days and cleans up markers and uploads', () => {
    expect(documentsBucket().LifecycleConfiguration).toEqual({
      Rules: [
        {
          Id: 'ExpireNoncurrentVersions',
          Status: 'Enabled',
          NoncurrentVersionExpiration: { NoncurrentDays: 35 },
          ExpiredObjectDeleteMarker: true,
          AbortIncompleteMultipartUpload: { DaysAfterInitiation: 7 },
        },
      ],
    });
  });

  it('never expires current objects', () => {
    const [rule] = documentsBucket().LifecycleConfiguration.Rules;
    expect(rule.ExpirationInDays).toBeUndefined();
    expect(rule.ExpirationDate).toBeUndefined();
    expect(rule.Transitions).toBeUndefined();
  });
});
