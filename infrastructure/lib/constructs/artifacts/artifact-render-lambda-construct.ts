import * as cdk from 'aws-cdk-lib';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';

export interface ArtifactRenderLambdaConstructProps {
  config: AppConfig;
  /** Artifact metadata table — granted read access. */
  artifactsTable: dynamodb.ITable;
  /** Artifact content bucket — granted read access. */
  artifactsBucket: s3.IBucket;
  /**
   * The HMAC-SHA256 signing key for render JWTs. Same-stack typed
   * ref (PlatformStack creates the secret via
   * `ArtifactRenderTokenSecretConstruct`); no SSM round-trip needed.
   */
  renderTokenSecret: secretsmanager.ISecret;
  /** CSP `frame-ancestors` source list (space-separated). */
  frameAncestors: string;
}

/**
 * ArtifactRenderLambdaConstruct — JWT-validating, S3-fetching Lambda
 * that returns rendered artifact HTML with a strict CSP.
 *
 * (and its paired CloudFront distribution) from a sibling construct to
 * PlatformStack. The Lambda's *configuration* (runtime, arch, IAM
 * role, env vars, function URL, CloudFront origin wiring) is owned
 * by CDK; its *handler code* is shipped independently by the backend
 * workflow's `scripts/build/deploy-artifact-render-code.sh` step,
 * which calls `aws lambda update-function-code` directly.
 *
 * The model: same as how the SPA works (Platform owns the bucket +
 * CloudFront; the workflow does `aws s3 sync` + invalidation).
 *
 * `lambda.Code.fromAsset` here points at the bootstrap zip dir, NOT
 * at backend/src/lambdas/artifact_render. The bootstrap dir contents
 * are byte-stable, so its content-hash is stable, so CFN sees no
 * change to the `Code` property on subsequent Platform deploys and
 * leaves the out-of-band-deployed real handler untouched. (CFN tracks
 * desired-vs-known-state from its own model, not by querying live
 * Lambda config — drift detection only fires on a manual scan.)
 *
 * Function URL is `AWS_IAM`-authed — the URL is invoked by CloudFront
 * over Origin Access Control (configured in
 * `ArtifactsDistributionConstruct`). AWS_IAM blocks direct invocation
 * at the `lambdaUrl.amazonaws.com` hostname; CloudFront signs each
 * origin request with SigV4.
 *
 * The CSP `script-src` allow-list (`CSP_SCRIPT_SRC` env var) is kept
 * byte-identical with the CloudFront response-headers-policy in the
 * paired distribution construct (defense in depth — the Lambda emits
 * its own CSP and CloudFront adds another, both must list the same
 * trusted CDNs).
 *
 * ARM64 for cost; Python 3.12 to match the rest of the backend
 * toolchain.
 */
export class ArtifactRenderLambdaConstruct extends Construct {
  public readonly renderFunction: lambda.Function;
  public readonly functionUrl: lambda.FunctionUrl;

  constructor(
    scope: Construct,
    id: string,
    props: ArtifactRenderLambdaConstructProps,
  ) {
    super(scope, id);

    const { config, artifactsTable, artifactsBucket, renderTokenSecret, frameAncestors } = props;

    // Auto-generated log group name (no `logGroupName`) so a
    // failed-deploy orphan can't collide with a redeploy.
    const renderLogGroup = new logs.LogGroup(this, 'RenderFunctionLogGroup', {
      retention: logs.RetentionDays.ONE_WEEK,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    this.renderFunction = new lambda.Function(this, 'RenderFunction', {
      // Intentionally no `functionName` — let CDK auto-generate.
      // The deploy script resolves the function via SSM at
      // `/{prefix}/artifacts/render-function-name` (published below).
      runtime: lambda.Runtime.PYTHON_3_12,
      architecture: lambda.Architecture.ARM_64,
      handler: 'handler.handler',
      logGroup: renderLogGroup,
      // Stable bootstrap (returns 503). Real code is deployed by the
      // backend workflow via update-function-code. Do NOT point this
      // at backend/src/lambdas/artifact_render/ — that would re-couple
      // Lambda code changes to Platform deploys.
      code: lambda.Code.fromAsset(
        path.resolve(
          __dirname,
          '..',
          '..',
          '..',
          'bootstrap-assets',
          'artifact-render',
        ),
      ),
      memorySize: 512,
      timeout: cdk.Duration.seconds(5),
      environment: {
        ARTIFACTS_BUCKET: artifactsBucket.bucketName,
        ARTIFACTS_TABLE: artifactsTable.tableName,
        RENDER_TOKEN_SECRET_ARN: renderTokenSecret.secretArn,
        FRAME_ANCESTOR_ORIGIN: frameAncestors,
        // Pinned CSP allow-list. Must stay byte-identical with the
        // `script-src` line in the paired distribution construct's
        // response-headers-policy CSP (defense in depth).
        CSP_SCRIPT_SRC:
          "'self' 'unsafe-inline' https://cdn.tailwindcss.com https://esm.sh https://cdn.jsdelivr.net https://unpkg.com",
      },
    });

    artifactsBucket.grantRead(this.renderFunction);
    artifactsTable.grantReadData(this.renderFunction);
    renderTokenSecret.grantRead(this.renderFunction);

    this.functionUrl = this.renderFunction.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.AWS_IAM,
    });

    // Publish the auto-generated function name so the backend
    // workflow's code-deploy step can resolve which function to
    // call `aws lambda update-function-code` against.
    new ssm.StringParameter(this, 'RenderFunctionNameParameter', {
      parameterName: `/${config.projectPrefix}/artifacts/render-function-name`,
      stringValue: this.renderFunction.functionName,
      description: 'Artifact render Lambda function name (CDK-auto-generated; consumed by backend workflow code-deploy step)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
