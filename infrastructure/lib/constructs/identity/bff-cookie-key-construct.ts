import * as kms from 'aws-cdk-lib/aws-kms';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface BffCookieKeyConstructProps {
  config: AppConfig;
}

/**
 * BffCookieKeyConstruct — KMS CMK + high-entropy data-key secret used
 * to derive the AES-256 BFF cookie sealing key.
 *
 * The KMS key encrypts the Secrets Manager secret at rest. The secret's
 * 44-char alphanumeric value (~261 bits of entropy) is hashed with
 * SHA-256 at app-api startup to produce the 32-byte AES-256 key.
 *
 * Generating the data key once at stack create — rather than per-task
 * via `kms:GenerateDataKey` — guarantees that every app-api task across
 * `desiredCount > 1` and across rolling deploys derives the same
 * plaintext key. Without this, each task's CookieCodec singleton would
 * mint its own random AES key and any cookie sealed by Task A would
 * fail `bad seal` on Task B (a 401 storm under the desiredCount: 2
 * deployment shape).
 *
 * Access requires both `secretsmanager:GetSecretValue` on the secret
 * AND `kms:Decrypt` on the CMK (Secrets Manager invokes Decrypt on the
 * caller's behalf using the secret-ARN encryption context).
 */
export class BffCookieKeyConstruct extends Construct {
  public readonly signingKey: kms.Key;
  public readonly dataKeySecret: secretsmanager.Secret;

  constructor(
    scope: Construct,
    id: string,
    props: BffCookieKeyConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.signingKey = new kms.Key(this, 'BFFCookieSigningKey', {
      alias: getResourceName(config, 'bff-cookie-signing-key'),
      description:
        'KMS key for sealing BFF session cookies (data-key wrapping)',
      enableKeyRotation: true,
      removalPolicy: getRemovalPolicy(config),
    });


    this.dataKeySecret = new secretsmanager.Secret(
      this,
      'BFFCookieDataKeySecret',
      {
        secretName: getResourceName(config, 'bff-cookie-data-key'),
        description:
          'High-entropy random secret used to derive the AES-256 BFF ' +
          'cookie sealing key (SHA-256). Generated once at deploy time; ' +
          'rotation invalidates active cookies (no kid versioning yet).',
        encryptionKey: this.signingKey,
        generateSecretString: {
          // 44 chars from the 62-char alphanumeric alphabet ≈ 261 bits
          // of entropy — comfortably above the 256-bit AES-256 target
          // after SHA-256 derivation.
          passwordLength: 44,
          excludePunctuation: true,
          includeSpace: false,
        },
        removalPolicy: getRemovalPolicy(config),
      },
    );

  }
}
