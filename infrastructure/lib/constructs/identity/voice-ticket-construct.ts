import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName, getRemovalPolicy } from '../../config';

export interface VoiceTicketConstructProps {
  config: AppConfig;
}

/**
 * VoiceTicketConstruct — single-use jti tracking + HMAC signing key for
 * the WebSocket-upgrade ticket exchanged between SPA and app-api before
 * proxying voice traffic upstream to the AgentCore Runtime.
 *
 * The ticket lives entirely within app-api (issuer == verifier), so
 * these resources are owned by app-api alone. jti is the partition key;
 * TTL on `ttl` reaps consumed rows ~1 minute after expiry. Conditional
 * puts on the jti enforce single-use.
 *
 * The signing secret is HMAC-SHA256, sourced once at app-api startup
 * and cached in process memory.
 */
export class VoiceTicketConstruct extends Construct {
  public readonly replayTable: dynamodb.Table;
  public readonly signingSecret: secretsmanager.Secret;

  constructor(scope: Construct, id: string, props: VoiceTicketConstructProps) {
    super(scope, id);

    const { config } = props;

    this.replayTable = new dynamodb.Table(this, 'VoiceTicketReplayTable', {
      tableName: getResourceName(config, 'voice-ticket-replay'),
      partitionKey: { name: 'jti', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      timeToLiveAttribute: 'ttl',
      removalPolicy: getRemovalPolicy(config),
      encryption: dynamodb.TableEncryption.AWS_MANAGED,
    });



    this.signingSecret = new secretsmanager.Secret(
      this,
      'VoiceTicketSigningSecret',
      {
        secretName: getResourceName(config, 'voice-ticket-signing-key'),
        description: 'HMAC signing key for voice WebSocket-upgrade tickets',
        generateSecretString: {
          secretStringTemplate: JSON.stringify({
            description: 'Voice ticket signing key',
          }),
          generateStringKey: 'secret',
          excludePunctuation: true,
          includeSpace: false,
          passwordLength: 64,
        },
        removalPolicy: getRemovalPolicy(config),
      },
    );

  }
}
