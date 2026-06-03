import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface CognitoConstructProps {
  config: AppConfig;
}

/**
 * CognitoConstruct — Cognito user pool, BFF app client, hosted UI domain.
 *
 * Central identity broker for all authentication. Federates to external
 * IdPs (Entra ID, Okta, Google) and issues its own JWTs. Self-signup is
 * enabled initially for first-boot; the App API disables it after the
 * first admin user is created.
 *
 * The BFF App Client is a confidential client used by app-api for the
 * server-side OAuth token exchange in the Token Handler BFF flow. The
 * client secret authenticates the /oauth2/token call from app-api; the
 * secret never leaves the server. The public PKCE SPA client that
 * previously sat alongside this one was retired in Phase 7 when the SPA
 * cut over fully to cookie auth.
 *
 * Federated IdPs are configured on the user pool out-of-band today and
 * listed here by ProviderName via `config.cognito.supportedIdentityProviders`.
 * COGNITO is always included so username/password sign-in keeps working
 * alongside SSO.
 */
export class CognitoConstruct extends Construct {
  public readonly userPool: cognito.UserPool;
  public readonly bffAppClient: cognito.UserPoolClient;
  public readonly bffAppClientSecret: secretsmanager.Secret;
  public readonly cognitoDomain: cognito.UserPoolDomain;

  constructor(scope: Construct, id: string, props: CognitoConstructProps) {
    super(scope, id);

    const { config } = props;

    this.userPool = new cognito.UserPool(this, 'CognitoUserPool', {
      userPoolName: getResourceName(config, 'user-pool'),
      selfSignUpEnabled: true,
      signInAliases: { username: true, email: true },
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
        givenName: { mutable: true },
        familyName: { mutable: true },
      },
      customAttributes: {
        provider_sub: new cognito.StringAttribute({ mutable: true }),
        roles: new cognito.StringAttribute({ mutable: true }),
      },
      passwordPolicy: {
        minLength: config.cognito.passwordMinLength || 8,
        requireUppercase: true,
        requireLowercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: getRemovalPolicy(config),
    });

    const bffCallbackUrls = config.domainName
      ? [`https://${config.domainName}/api/auth/callback`]
      : ['http://localhost:8000/auth/callback'];
    const bffLogoutUrls = config.domainName
      ? [`https://${config.domainName}`]
      : ['http://localhost:4200'];

    if (config.cognito.callbackUrls) {
      bffCallbackUrls.push(...config.cognito.callbackUrls);
    }
    if (config.cognito.logoutUrls) {
      bffLogoutUrls.push(...config.cognito.logoutUrls);
    }

    const bffSupportedIdentityProviders: cognito.UserPoolClientIdentityProvider[] =
      [
        cognito.UserPoolClientIdentityProvider.COGNITO,
        ...(config.cognito.supportedIdentityProviders ?? [])
          .filter((name) => name !== 'COGNITO')
          .map((name) =>
            cognito.UserPoolClientIdentityProvider.custom(name),
          ),
      ];

    this.bffAppClient = this.userPool.addClient('CognitoBFFAppClient', {
      userPoolClientName: getResourceName(config, 'bff-app-client'),
      generateSecret: true,
      authFlows: { userSrp: false, custom: false },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.PROFILE,
          cognito.OAuthScope.EMAIL,
        ],
        callbackUrls: bffCallbackUrls,
        logoutUrls: bffLogoutUrls,
      },
      preventUserExistenceErrors: true,
      supportedIdentityProviders: bffSupportedIdentityProviders,
    });

    this.bffAppClientSecret = new secretsmanager.Secret(
      this,
      'CognitoBFFAppClientSecret',
      {
        secretName: getResourceName(config, 'cognito-bff-app-client-secret'),
        description:
          'Client secret for the Cognito BFF app client (Token Handler flow)',
        secretStringValue: this.bffAppClient.userPoolClientSecret,
        removalPolicy: getRemovalPolicy(config),
      },
    );

    this.cognitoDomain = this.userPool.addDomain('CognitoDomain', {
      cognitoDomain: {
        domainPrefix: config.cognito.domainPrefix || config.projectPrefix,
      },
    });

    // SSM publications
    new ssm.StringParameter(this, 'CognitoUserPoolIdParameter', {
      parameterName: `/${config.projectPrefix}/auth/cognito/user-pool-id`,
      stringValue: this.userPool.userPoolId,
      description: 'Cognito User Pool ID',
      tier: ssm.ParameterTier.STANDARD,
    });




    new ssm.StringParameter(this, 'CognitoBFFAppClientIdParameter', {
      parameterName: `/${config.projectPrefix}/auth/cognito/bff-app-client-id`,
      stringValue: this.bffAppClient.userPoolClientId,
      description: 'Cognito BFF (confidential) app client ID',
      tier: ssm.ParameterTier.STANDARD,
    });

  }
}
