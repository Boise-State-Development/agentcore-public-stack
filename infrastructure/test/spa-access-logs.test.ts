/**
 * Regression cover for the SPA distribution's access logs.
 *
 * On 2026-09-24 a user-reported 404 on boisestate.ai could not be traced:
 * app-api and the ALB had no matching request, CloudFront's 4xx metric
 * showed errors in the window, and with logging disabled on the
 * distribution nothing could name the URI or the client. These pin the
 * pieces that make the logs both deliverable and safe:
 *
 *   - Logging is on, into the dedicated bucket, cookies excluded (the SPA's
 *     session is an httpOnly cookie — a logged one is a replayable session).
 *   - The bucket has ACLs enabled. Legacy CloudFront logging writes through
 *     the bucket ACL; S3's default BucketOwnerEnforced makes the distribution
 *     update fail.
 *   - The bucket expires its logs, is SSE-S3, and blocks public access.
 *   - The kill switch drops the Logging block but keeps the bucket, so a
 *     disable/re-enable cycle never collides with a RETAINed bucket name.
 */
import * as cdk from 'aws-cdk-lib';
import { Match, Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { AppConfig } from '../lib/config';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

const LOG_BUCKET_NAME = `test-project-frontend-access-logs-${MOCK_ACCOUNT}`;

function synth(frontend: AppConfig['frontend']): Template {
  const config = createMockConfig({ frontend });
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'SpaAccessLogsPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  return Template.fromStack(stack);
}

/** Logical id of the access-log bucket, found by its physical name. */
function logBucketLogicalId(template: Template): string {
  const matches = template.findResources('AWS::S3::Bucket', {
    Properties: { BucketName: LOG_BUCKET_NAME },
  });
  const ids = Object.keys(matches);
  expect(ids).toHaveLength(1);
  return ids[0];
}

describe('SPA distribution access logs', () => {
  describe('default (enabled)', () => {
    let template: Template;

    beforeAll(() => {
      template = synth({ cloudFrontPriceClass: 'PriceClass_100' });
    });

    it('logs to the dedicated bucket under spa/ without cookies', () => {
      const bucketId = logBucketLogicalId(template);

      template.hasResourceProperties('AWS::CloudFront::Distribution', {
        DistributionConfig: {
          Comment: 'test-project Frontend Distribution',
          Logging: {
            Bucket: { 'Fn::GetAtt': [bucketId, 'RegionalDomainName'] },
            IncludeCookies: false,
            Prefix: 'spa/',
          },
        },
      });
    });

    it('enables ACLs on the bucket so legacy log delivery can write', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketName: LOG_BUCKET_NAME,
        OwnershipControls: {
          Rules: [{ ObjectOwnership: 'BucketOwnerPreferred' }],
        },
      });
    });

    it('expires logs, encrypts with SSE-S3, and blocks public access', () => {
      template.hasResourceProperties('AWS::S3::Bucket', {
        BucketName: LOG_BUCKET_NAME,
        LifecycleConfiguration: {
          Rules: Match.arrayWith([
            Match.objectLike({
              Id: 'expire-access-logs',
              Status: 'Enabled',
              ExpirationInDays: 90,
            }),
          ]),
        },
        BucketEncryption: {
          ServerSideEncryptionConfiguration: [
            { ServerSideEncryptionByDefault: { SSEAlgorithm: 'AES256' } },
          ],
        },
        PublicAccessBlockConfiguration: {
          BlockPublicAcls: true,
          BlockPublicPolicy: true,
          IgnorePublicAcls: true,
          RestrictPublicBuckets: true,
        },
      });
    });
  });

  describe('kill switch (accessLogsEnabled=false)', () => {
    let template: Template;

    beforeAll(() => {
      template = synth({
        cloudFrontPriceClass: 'PriceClass_100',
        accessLogsEnabled: false,
      });
    });

    it('drops the Logging block from the SPA distribution', () => {
      template.hasResourceProperties('AWS::CloudFront::Distribution', {
        DistributionConfig: {
          Comment: 'test-project Frontend Distribution',
          Logging: Match.absent(),
        },
      });
    });

    it('keeps the bucket so re-enabling never collides with a retained name', () => {
      logBucketLogicalId(template);
    });
  });
});
