import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface SkillResourcesConstructProps {
  config: AppConfig;
}

/**
 * SkillResourcesConstruct — S3 content bucket for admin-managed Skills'
 * supporting reference files (PR-4 of admin-managed Skills).
 *
 * A skill's reference files (read-only markdown/resources for deep
 * progressive disclosure) are too large to inline in the skill's DynamoDB
 * row (400 KB item limit), so the bytes live here and the row carries only
 * a lightweight `resources` manifest. Mirrors the artifacts content bucket.
 *
 * S3 layout (content-addressed, so identical content within a skill dedupes
 * to one object — see `apis/shared/skills/resource_store.py`):
 *   skills/{skill_id}/{content_hash}
 *
 * Private — bytes are only ever fetched server-side by app-api (admin
 * authoring) and, in PR-6, the inference-api runtime at dispatch time. No
 * CORS; never loaded cross-origin by a browser.
 *
 * Lifecycle:
 *   - Failed multipart uploads aborted after 7 days.
 *   (No expiration rule: reference files persist for the life of the skill
 *   and are removed explicitly by the delete-resource path.)
 *
 * The bucket name is threaded to the compute roles via
 * `PlatformComputeRefs.skillResourcesBucket` (typed ref, not SSM) — see
 * the CLAUDE.md File Creation Rules.
 */
export class SkillResourcesConstruct extends Construct {
  public readonly bucket: s3.Bucket;

  constructor(
    scope: Construct,
    id: string,
    props: SkillResourcesConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.bucket = new s3.Bucket(this, 'SkillResourcesBucket', {
      bucketName: getResourceName(config, 'skill-resources'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: 'abort-stale-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
      ],
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });
  }
}
