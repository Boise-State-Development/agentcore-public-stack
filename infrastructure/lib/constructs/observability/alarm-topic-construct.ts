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
 * AlarmTopicConstruct — the one SNS topic every CloudWatch alarm in this stack
 * publishes to.
 *
 * ## Why the topic is created but never subscribed
 *
 * There are deliberately NO `sns.Subscription` resources here. Subscriptions
 * are managed out-of-band (console, CLI, or a separate operator-owned process)
 * for a specific reason: an institution running this platform has several teams
 * who each want to hear about failures, and their membership changes far more
 * often than the infrastructure does. Encoding subscribers in CDK would mean a
 * pull request, a review, and a CloudFormation deploy to add one person's email
 * — so in practice the list goes stale and people stop trusting it.
 *
 * The topic ARN is therefore published to SSM and as a CfnOutput, and adding a
 * recipient is a one-line `aws sns subscribe` that touches no code.
 *
 * ## Why a customer-managed KMS key
 *
 * This is the part that silently breaks. An SNS topic encrypted with the
 * AWS-managed `alias/aws/sns` key CANNOT receive messages from CloudWatch:
 * the alarm's publish call is made by the CloudWatch service principal, and an
 * AWS-managed key's policy cannot be edited to grant that principal
 * `kms:GenerateDataKey*`. The alarm transitions to ALARM, the console shows it
 * firing, and the notification is dropped — a monitoring system that looks
 * healthy precisely when it has stopped working.
 *
 * A customer-managed key whose policy grants `cloudwatch.amazonaws.com` both
 * `kms:GenerateDataKey*` and `kms:Decrypt` is the fix. Leaving the topic
 * unencrypted would also "work", but alarm bodies quote metric names, resource
 * names, and alarm descriptions, so the topic is worth encrypting.
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
      description:
        'Encrypts the platform alarm SNS topic. Customer-managed rather than '
        + 'alias/aws/sns because CloudWatch must be granted GenerateDataKey to '
        + 'publish, which is not possible on an AWS-managed key.',
      enableKeyRotation: true,
      // NOT getRemovalPolicy(config). This key protects no durable data — it
      // wraps in-flight notifications only — so retaining it on stack delete
      // would leave an orphaned billable key ($1/month each) behind with
      // nothing to decrypt. Alarm history lives in CloudWatch, not here.
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // The grant that makes alarm delivery actually work. `kms:Decrypt` alone is
    // not enough: SNS envelope encryption has the publisher generate the data
    // key, so CloudWatch needs GenerateDataKey* as well.
    //
    // Scoped with an SourceAccount condition so the grant cannot be leveraged
    // by CloudWatch acting for a different account.
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
      // Deny non-TLS publishes/subscribes. Cheap, and this topic's messages
      // name internal resources.
      enforceSSL: true,
    });

    // CloudWatch must also be allowed to Publish. CDK adds this automatically
    // when an SnsAction is attached to an alarm, but stating it here means the
    // topic is correct even for a publisher wired up later, and it documents
    // the second half of the permission pair alongside the KMS half above.
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

    // Published so an operator (or a subscription script) can find the topic
    // without reading the CloudFormation template. This is a WRITE from this
    // stack; nothing in this stack reads it back via valueForStringParameter,
    // which would be unsatisfiable on first deploy — in-stack consumers take
    // the typed `topic` reference instead.
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
