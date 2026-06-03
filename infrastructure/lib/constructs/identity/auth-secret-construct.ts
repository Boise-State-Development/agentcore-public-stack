import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface AuthSecretConstructProps {
  config: AppConfig;
}

/**
 * AuthSecretConstruct — JWT signing / session encryption secret.
 *
 * 64-char alphanumeric secret, generated once at stack create. Read by
 * app-api at startup for JWT signing and session encryption. Rotated by
 * replacing the secret value.
 *
 * SSM publications:
 *   /{prefix}/auth/secret-arn
 *   /{prefix}/auth/secret-name
 */
export class AuthSecretConstruct extends Construct {
  public readonly authSecret: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: AuthSecretConstructProps) {
    super(scope, id);

    const { config } = props;

    this.authSecret = new secretsmanager.Secret(this, 'AuthenticationSecret', {
      secretName: getResourceName(config, 'auth-secret'),
      description:
        'Authentication secret for JWT signing, session encryption, ' +
        'and other auth operations',
      generateSecretString: {
        secretStringTemplate: JSON.stringify({
          description: 'Authentication Secret',
        }),
        generateStringKey: 'secret',
        excludePunctuation: true,
        includeSpace: false,
        passwordLength: 64,
      },
      removalPolicy: getRemovalPolicy(config),
    });


  }
}
