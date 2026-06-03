import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface ArtifactRenderTokenSecretConstructProps {
  config: AppConfig;
}

/**
 * ArtifactRenderTokenSecretConstruct — HMAC-SHA256 key shared between
 * app-api (minter) and the artifact render Lambda (verifier).
 *
 * The app-api hands the SPA a short-lived JWT scoped to one
 * (artifact_id, version); the SPA embeds it as `?t=...` on the iframe
 * src; the render Lambda validates the JWT, fetches content from
 * S3/DDB, and returns HTML with a strict CSP.
 *
 * Lives in PlatformStack (foundational identity) so app-api and the
 * render Lambda both read it symmetrically from a foundation neither
 * owns. If this lived in artifacts-specific construct, app-api would
 * gain a deploy-order dependency on it.
 */
export class ArtifactRenderTokenSecretConstruct extends Construct {
  public readonly secret: secretsmanager.Secret;

  constructor(
    scope: Construct,
    id: string,
    props: ArtifactRenderTokenSecretConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.secret = new secretsmanager.Secret(this, 'ArtifactRenderTokenSecret', {
      secretName: getResourceName(config, 'artifact-render-token-key'),
      description:
        'HMAC-SHA256 key for signing artifact iframe render tokens. ' +
        'Used by app-api to mint short-lived JWTs and by the artifact ' +
        'render Lambda to verify them.',
      generateSecretString: {
        // 44 chars from the 62-char alphanumeric alphabet ≈ 261 bits
        // of entropy — same shape as the BFF cookie data key.
        passwordLength: 44,
        excludePunctuation: true,
        includeSpace: false,
      },
      removalPolicy: getRemovalPolicy(config),
    });

  }
}
