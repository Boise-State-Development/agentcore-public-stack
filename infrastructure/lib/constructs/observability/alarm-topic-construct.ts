import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as kms from 'aws-cdk-lib/aws-kms';
import * as sns from 'aws-cdk-lib/aws-sns';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface AlarmTopicConstructProps {
  config: AppConfig;
}

/**
 * The SNS topic every alarm publishes to.
 *
 * Subscriptions are intentionally absent — teams subscribe out-of-band so adding
 * a recipient needs no deploy. The ARN is published to SSM and as a CfnOutput.
 *
 * The key must be customer-managed: CloudWatch cannot publish to a topic
 * encrypted with alias/aws/sns, because that key's policy cannot be edited to
 * grant the service principal kms:GenerateDataKey*. The failure is silent — the
 * alarm fires and the notification is dropped.
 */
export class AlarmTopicConstruct extends Construct {
  /** The topic every alarm action targets. */
  public readonly topic: sns.Topic;

  /** CMK encrypting the topic. Exposed for tests and for any future publisher
   *  that needs an explicit grant. */
  public readonly key: kms.Key;

  constructor(scope: Construct, id: string, props: AlarmTopicConstructProps) {
    super(scope, id);

    const { config } = props;

    // ============================================================
    // CMK
    // ============================================================

    this.key = new kms.Key(this, 'AlarmTopicKey', {
      alias: getResourceName(config, 'alarm-topic-key'),
      description: 'Encrypts the platform alarm SNS topic.',
      enableKeyRotation: true,
      // Not getRemovalPolicy(config): this wraps in-flight notifications only,
      // so retaining it would strand a billable key with nothing to decrypt.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // Decrypt alone is not enough: SNS envelope encryption has the publisher
    // generate the data key, so GenerateDataKey* is required too.
    this.key.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowCloudWatchAlarmsToPublishToEncryptedTopic',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['kms:GenerateDataKey*', 'kms:Decrypt'],
        resources: ['*'],
        conditions: {
          StringEquals: { 'aws:SourceAccount': config.awsAccount },
        },
      }),
    );

    // ============================================================
    // Topic
    // ============================================================

    this.topic = new sns.Topic(this, 'AlarmTopic', {
      topicName: getResourceName(config, 'alarms'),
      displayName: `${config.projectPrefix} platform alarms`,
      masterKey: this.key,
      enforceSSL: true,
    });

    this.topic.addToResourcePolicy(
      new iam.PolicyStatement({
        sid: 'AllowCloudWatchAlarmsToPublish',
        effect: iam.Effect.ALLOW,
        principals: [new iam.ServicePrincipal('cloudwatch.amazonaws.com')],
        actions: ['sns:Publish'],
        resources: [this.topic.topicArn],
        conditions: {
          StringEquals: { 'aws:SourceAccount': config.awsAccount },
        },
      }),
    );

    // ============================================================
    // Discovery
    // ============================================================

    new ssm.StringParameter(this, 'AlarmTopicArnParam', {
      parameterName: `/${config.projectPrefix}/observability/alarm-topic-arn`,
      stringValue: this.topic.topicArn,
      description:
        'SNS topic ARN for all platform CloudWatch alarms. Subscribe teams to '
        + 'this topic out-of-band: aws sns subscribe --topic-arn <this> '
        + '--protocol email --notification-endpoint you@example.edu',
    });

    new cdk.CfnOutput(this, 'AlarmTopicArn', {
      value: this.topic.topicArn,
      description:
        'SNS topic for platform alarms. Subscriptions are intentionally not '
        + 'managed by CDK — subscribe with `aws sns subscribe`.',
      exportName: `${config.projectPrefix}-AlarmTopicArn`,
    });
  }
}
