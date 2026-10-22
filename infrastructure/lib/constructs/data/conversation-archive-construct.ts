import * as cdk from 'aws-cdk-lib';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

export interface ConversationArchiveConstructProps {
  config: AppConfig;
}

/** Key prefix every archived turn lives under; the lifecycle rule targets it. */
export const CONVERSATION_ARCHIVE_PREFIX = 'conversations/';

/**
 * ConversationArchiveConstruct — the durable per-turn transcript copy that
 * feeds conversation search (docs/specs/conversation-search.md §3, §4).
 *
 * One object per turn, `conversations/{userId}/{sessionId}/{messageIndex}.json`,
 * holding the user's text and the assistant's text for that turn (tool results
 * excluded). Written fire-and-forget by the runtime after `done`, and by
 * app-api when a shared conversation is forked; deleted by app-api with the
 * session. The search index is built from these objects, so it can always be
 * rebuilt from them.
 *
 * Retention follows `conversationRetentionDays`, the same number that sets
 * AgentCore Memory's event expiry, so the two copies age out together. The
 * rule counts from each object's write, so a long conversation loses its
 * oldest turns first, as Memory does. Unlike Memory the archive is not capped
 * at 365, which is why the raw value is used here.
 *
 * Unversioned on purpose: a lifecycle expiry or a delete must remove the
 * bytes, not leave a noncurrent version holding text the user asked to lose.
 */
export class ConversationArchiveConstruct extends Construct {
  public readonly bucket: s3.Bucket;

  constructor(
    scope: Construct,
    id: string,
    props: ConversationArchiveConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.bucket = new s3.Bucket(this, 'ConversationArchiveBucket', {
      bucketName: getResourceName(config, 'conversation-archive'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: false,
      lifecycleRules: [
        {
          id: 'conversation-retention',
          prefix: CONVERSATION_ARCHIVE_PREFIX,
          expiration: cdk.Duration.days(config.conversationRetentionDays),
        },
        {
          id: 'abort-stale-multipart',
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
      ],
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

    // The runtime resolves the bucket from here rather than from an env var:
    // its 50-variable budget is nearly spent (runtime-env-var-limit.test.ts),
    // and it already holds ssm:GetParameter on `/{prefix}/*`.
    new ssm.StringParameter(this, 'ConversationArchiveBucketNameParameter', {
      parameterName: `/${config.projectPrefix}/conversations/archive-bucket-name`,
      stringValue: this.bucket.bucketName,
      description: 'Conversation archive (per-turn transcript) S3 bucket name',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
