import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';

export const MOCK_USER_POOL_ID = 'us-west-2_testpool1';
export const MOCK_BFF_CLIENT_ID = 'testbffclientid123';

/**
 * Cognito refs for constructs that need a user pool / app client (e.g. the
 * Gateway's CUSTOM_JWT inbound authorizer).
 *
 * Uses `from*` imports rather than creating a real `UserPool`, so the returned
 * refs add **zero resources** to the test stack. That keeps existing
 * `resourceCountIs` assertions (IAM roles, etc.) meaningful in construct
 * isolation tests.
 */
export function mockCognitoRefs(scope: cdk.Stack, idPrefix = 'MockCognito') {
  return {
    userPool: cognito.UserPool.fromUserPoolId(
      scope,
      `${idPrefix}UserPool`,
      MOCK_USER_POOL_ID,
    ),
    bffAppClient: cognito.UserPoolClient.fromUserPoolClientId(
      scope,
      `${idPrefix}BffClient`,
      MOCK_BFF_CLIENT_ID,
    ),
  };
}
