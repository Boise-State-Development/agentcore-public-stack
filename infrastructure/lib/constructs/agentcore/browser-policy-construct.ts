import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import { Construct } from 'constructs';

import {
  AppConfig,
  getAutoDeleteObjects,
  getRemovalPolicy,
  getResourceName,
} from '../../config';

/**
 * The policy object's location, split so the deployment and the key the
 * backend is told about are derived from ONE source.
 *
 * They were separate constants once, and the BucketDeployment's
 * `destinationKeyPrefix` compounded with a source name that already carried
 * the prefix — the object landed at `policies/policies/managed-policies.json`
 * while `BROWSER_POLICY_S3` pointed at `policies/managed-policies.json`. The
 * deploy looked clean and the policy silently resolved to a missing key, which
 * is the worst shape for a security control to fail in. Keep these derived.
 */
const BROWSER_POLICY_PREFIX = 'policies';
const BROWSER_POLICY_FILENAME = 'managed-policies.json';

/** Full object key, as both the deployment and the backend see it. */
export const BROWSER_MANAGED_POLICY_KEY =
  `${BROWSER_POLICY_PREFIX}/${BROWSER_POLICY_FILENAME}`;

/**
 * Render the Chromium managed-policy document.
 *
 * Exported so the JSON is unit-testable directly, and so there is exactly one
 * place that decides what the browser is forbidden to reach.
 *
 * `URLBlocklist` is a flat list of Chromium URL-filter patterns. A bare host
 * blocks that host on every scheme, port and path. See
 * https://chromeenterprise.google/policies/#URLBlocklist
 */
export function buildBrowserManagedPolicy(
  urlBlocklist: string[],
): Record<string, unknown> {
  return { URLBlocklist: [...urlBlocklist] };
}

export interface AgentCoreBrowserPolicyConstructProps {
  config: AppConfig;
  /**
   * The browser's execution role. It is what reads the policy object from S3
   * when a session starts, so it needs `s3:GetObject` on the key.
   */
  browserExecutionRole: iam.IRole;
}

/**
 * AgentCoreBrowserPolicyConstruct — the S3-hosted Chromium managed policy that
 * every browser session is started with.
 *
 * Why this exists, and why it is not on the browser resource
 * ----------------------------------------------------------
 * A takeover hands a human a fully interactive Chromium inside our AWS
 * account. The only thing that can stop them navigating somewhere they should
 * not — the LMS, say, to have an agent submit coursework — is Chromium itself
 * refusing. A check in our own code cannot: it sees the page the takeover
 * *started* on, and the user drives from there.
 *
 * The obvious home for that policy is the browser resource, via
 * `CreateBrowser`'s `enterprisePolicies`. Three things rule it out:
 *
 *   1. There is no `UpdateBrowser` operation, and policy files are read from
 *      S3 "at the time of the API call" — so a policy attached at create time
 *      is frozen, and every edit replaces the browser resource.
 *   2. `CfnBrowserCustom` (aws-cdk-lib 2.251.0) does not expose
 *      `enterprisePolicies` at all, so CFN cannot set it without a custom
 *      resource that would inherit problem 1.
 *   3. `StartBrowserSession` accepts the same `enterprisePolicies` shape and
 *      reads it fresh on every session.
 *
 * So this construct owns only the *object and its IAM*; the backend passes the
 * reference on every `start()` (`agents/builtin_tools/browser/session_pool.py`).
 *
 * ⚠️ **Session-level policies are RECOMMENDED-only.** The API's `type` enum
 * accepts MANAGED, but the service rejects it — measured on dev 2026-09-19:
 * "Invalid value for parameter 'type'. MANAGED is not supported for
 * session-level policies." It does not degrade; `StartBrowserSession` fails and
 * every browser session dies.
 *
 * Chromium treats recommended policies as user-overridable defaults, so what
 * this deploys constrains the **agent** (which drives via CDP and never opens
 * settings) but NOT a **human** holding the browser during a takeover. The
 * un-overridable MANAGED policy needs `CreateBrowser`, which `CfnBrowserCustom`
 * does not expose — so it needs a custom resource, and `request_user_login`
 * must stay ungranted until that lands. See the spec's D6.
 *
 * The policy content is rendered from CDK config rather than being an object
 * an admin edits in place, so changing what the browser may reach is a
 * reviewed deploy. See `docs/specs/authenticated-web-assessment.md` D6.
 */
export class AgentCoreBrowserPolicyConstruct extends Construct {
  public readonly bucket: s3.Bucket;
  public readonly policyKey = BROWSER_MANAGED_POLICY_KEY;

  constructor(
    scope: Construct,
    id: string,
    props: AgentCoreBrowserPolicyConstructProps,
  ) {
    super(scope, id);

    const { config, browserExecutionRole } = props;

    // Must be in the same region as the browser (service requirement).
    this.bucket = new s3.Bucket(this, 'BrowserPolicyBucket', {
      bucketName: getResourceName(config, 'browser-policy', config.awsAccount),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      // Versioned on purpose: this is a security control, and being able to
      // see what the browser was allowed to reach on a given date is worth
      // more than the pennies of storage.
      versioned: true,
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: getAutoDeleteObjects(config),
    });

    new s3deploy.BucketDeployment(this, 'BrowserPolicyDeployment', {
      sources: [
        // Just the filename: `destinationKeyPrefix` below supplies the
        // prefix. Passing the full key here is what doubled it.
        s3deploy.Source.jsonData(
          BROWSER_POLICY_FILENAME,
          buildBrowserManagedPolicy(config.browser.urlBlocklist),
        ),
      ],
      destinationBucket: this.bucket,
      // Scoped prune: this deployment owns this prefix and nothing else.
      destinationKeyPrefix: BROWSER_POLICY_PREFIX,
      prune: true,
    });

    // The browser service reads the policy as the browser's execution role
    // when a session starts. `GetObjectVersion` as well as `GetObject`,
    // per the service's documented prerequisite, because the bucket is
    // versioned and a pinned `versionId` would otherwise be unreadable.
    browserExecutionRole.addToPrincipalPolicy(
      new iam.PolicyStatement({
        sid: 'BrowserEnterprisePolicyS3Access',
        effect: iam.Effect.ALLOW,
        actions: ['s3:GetObject', 's3:GetObjectVersion'],
        resources: [this.bucket.arnForObjects('policies/*')],
      }),
    );

    new cdk.CfnOutput(this, 'BrowserPolicyBucketName', {
      value: this.bucket.bucketName,
      description: 'S3 bucket holding the browser Chromium managed policy',
    });
  }
}
