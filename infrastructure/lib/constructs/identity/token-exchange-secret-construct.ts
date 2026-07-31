import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface TokenExchangeSecretConstructProps {
  config: AppConfig;
}

/**
 * TokenExchangeSecretConstruct — confidential-client secret this
 * deployment uses to authenticate to an external token service's RFC 8693
 * token-exchange endpoint.
 *
 * Why a secret of our own rather than reading the token service's copy: the
 * token service and this platform live in different AWS accounts, so the
 * runtime cannot read the secret the token service holds. Each side keeps its
 * own copy of the same shared secret.
 *
 * No value is supplied here, unlike the other secrets in this stack. The others
 * are values we generate and own; this one is a credential agreed with a system
 * we do not control, so CDK cannot invent it. CloudFormation still seeds a random
 * value on first create (it generates one whenever `GenerateSecretString` is
 * present, even empty), so the real credential is written over the top:
 *
 *   aws secretsmanager put-secret-value \
 *     --secret-id <this secret> \
 *     --secret-string '{"<client_id>":"<secret>"}'
 *
 * Stack updates do not regenerate it, so that written value survives subsequent
 * deploys. Same pattern as the OAuth and auth-provider client secrets.
 *
 * The JSON-map shape matches the token service's own secret so the same value
 * can be pasted on both sides, and so a deployment could hold credentials for
 * more than one exchange client later.
 *
 * Until it is populated the stored value is a random string that the token
 * service will not recognise, so exchanges are refused — the affected tools simply do
 * not load rather than failing mid-conversation.
 */
export class TokenExchangeSecretConstruct extends Construct {
  public readonly secret: secretsmanager.Secret;

  constructor(
    scope: Construct,
    id: string,
    props: TokenExchangeSecretConstructProps,
  ) {
    super(scope, id);

    const { config } = props;

    this.secret = new secretsmanager.Secret(this, 'TokenExchangeSecret', {
      secretName: getResourceName(config, 'token-exchange-client'),
      description:
        'client_id -> client_secret map used to authenticate to an external ' +
        'token service RFC 8693 exchange endpoint. Populated out of band; ' +
        'CDK cannot generate a credential shared with an external system.',
      removalPolicy: getRemovalPolicy(config),
    });
  }
}
