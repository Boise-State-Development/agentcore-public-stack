import * as cdk from 'aws-cdk-lib';
import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as cr from 'aws-cdk-lib/custom-resources';
import { Construct } from 'constructs';

import { AppConfig, getRemovalPolicy, getResourceName } from '../../config';

/**
 * AWS managed prefix list of CloudFront's origin-facing IPv4 addresses —
 * the source addresses CloudFront uses when it connects to an origin.
 */
export const CLOUDFRONT_ORIGIN_FACING_PREFIX_LIST = 'com.amazonaws.global.cloudfront.origin-facing';

/**
 * How long ALB access logs are kept. Long enough to investigate a pattern
 * across a couple of weeks of traffic, short enough that a high-volume log
 * of every request doesn't accumulate cost indefinitely.
 */
const ACCESS_LOG_RETENTION_DAYS = 30;

export interface AlbConstructProps {
  config: AppConfig;
  vpc: ec2.IVpc;
}

/**
 * AlbConstruct — internet-facing ALB + security group + primary listener.
 *
 * Provisions:
 *   - ALB security group admitting only CloudFront's origin-facing ranges
 *     on the port CloudFront dials (or :80 and :443 from anywhere when
 *     `albRestrictToCloudFront` is false)
 *   - Internet-facing ALB in the VPC's public subnets
 *   - Access-log bucket (30-day expiry) with delivery enabled, which is
 *     what makes a mid-stream SSE disconnect attributable to the client,
 *     the network, or the ALB's own idle timeout
 *   - Primary listener: HTTPS on :443 if `config.certificateArn` is set,
 *     plus a redirect-to-HTTPS HTTP listener on :80; otherwise HTTP on :80
 *     with a fixed 404 response
 *   - SSM publications for the ALB ARN, DNS name, security group ID,
 *     primary listener ARN, and (when HTTPS) the dedicated HTTPS listener
 *     ARN
 *
 * Default action on the primary listener is a fixed 404 — backend
 * services attach target groups + listener rules at deploy time and
 * the default response only fires when no rule matches.
 *
 * Logical IDs preserved from the original `infrastructure-stack.ts`.
 */
export class AlbConstruct extends Construct {
  public readonly alb: elbv2.ApplicationLoadBalancer;
  public readonly albListener: elbv2.ApplicationListener;
  public readonly albSecurityGroup: ec2.SecurityGroup;
  public readonly accessLogBucket: s3.Bucket;

  constructor(scope: Construct, id: string, props: AlbConstructProps) {
    super(scope, id);

    const { config, vpc } = props;

    // ALB Security Group.
    //
    // By default ingress is CloudFront-only. Every legitimate request
    // reaches app-api through the SPA distribution's `/api/*` behavior
    // (browser BFF and API-key callers alike), so an ALB that answers the
    // whole internet only serves callers who bypass CloudFront — and its
    // security headers, its access log, and any future edge controls. A
    // 30-day prod log audit found ~5% of ALB requests arriving that way,
    // almost all scanners.
    //
    // The peer is the AWS managed prefix list of CloudFront's
    // origin-facing addresses, which AWS keeps current. Its ID differs per
    // region and CloudFormation cannot look it up by name, so a custom
    // resource resolves it at deploy time (re-run on every update).
    //
    // One port only: the prefix list counts as ~55 rules against the
    // default 60-rule security-group quota, so it cannot be repeated. It
    // is the port CloudFront actually dials — 443 when the ALB has a
    // certificate (origin protocol HTTPS_ONLY), otherwise 80. With a
    // certificate the :80 redirect listener stays, unreachable, which is
    // harmless.
    //
    // This does not stop someone pointing *their own* distribution at the
    // ALB hostname; a CloudFront-to-ALB shared-secret header is the
    // control for that.
    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc,
      securityGroupName: getResourceName(config, 'alb-sg'),
      description: 'Security group for Application Load Balancer',
      allowAllOutbound: true,
    });

    if (config.albRestrictToCloudFront) {
      const lookup = new cr.AwsCustomResource(this, 'CloudFrontOriginFacingPrefixList', {
        onUpdate: {
          service: 'EC2',
          action: 'describeManagedPrefixLists',
          parameters: {
            Filters: [{ Name: 'prefix-list-name', Values: [CLOUDFRONT_ORIGIN_FACING_PREFIX_LIST] }],
          },
          physicalResourceId: cr.PhysicalResourceId.of(CLOUDFRONT_ORIGIN_FACING_PREFIX_LIST),
          outputPaths: ['PrefixLists.0.PrefixListId'],
        },
        // DescribeManagedPrefixLists does not support resource-level
        // permissions, so `*` is the only resource it accepts.
        policy: cr.AwsCustomResourcePolicy.fromStatements([
          new iam.PolicyStatement({
            actions: ['ec2:DescribeManagedPrefixLists'],
            resources: ['*'],
          }),
        ]),
        installLatestAwsSdk: false,
      });
      const cloudFrontPort = config.certificateArn ? 443 : 80;
      this.albSecurityGroup.addIngressRule(
        ec2.Peer.prefixList(lookup.getResponseField('PrefixLists.0.PrefixListId')),
        ec2.Port.tcp(cloudFrontPort),
        `Allow ${cloudFrontPort === 443 ? 'HTTPS' : 'HTTP'} from CloudFront origin-facing ranges only`,
      );
    } else {
      this.albSecurityGroup.addIngressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(80),
        'Allow HTTP traffic from internet',
      );

      this.albSecurityGroup.addIngressRule(
        ec2.Peer.anyIpv4(),
        ec2.Port.tcp(443),
        'Allow HTTPS traffic from internet',
      );
    }

    // Export ALB Security Group ID to SSM

    // Application Load Balancer
    this.alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: true,
      loadBalancerName: getResourceName(config, 'alb'),
      securityGroup: this.albSecurityGroup,
      vpcSubnets: {
        subnetType: ec2.SubnetType.PUBLIC,
      },
    });

    // Access logs: the only record of who ENDED a connection.
    //
    // The chat path is SSE, and a stream that dies mid-turn reaches the
    // backend as an indistinguishable cancellation — a client going away, a
    // dropped socket, and the ALB's own 60s idle timeout all look identical
    // from inside the container. The access log is the one place that
    // separates them: `elb_status_code`, the `-`/`connection` termination
    // fields, and `request_processing_time` name the terminator and how long
    // the request had run. Without it, that attribution is guesswork.
    //
    // SSE-S3 rather than KMS deliberately — the ALB log delivery service
    // only supports SSE-S3 (or none) and silently fails to deliver against
    // an SSE-KMS bucket. `logAccessLogs` writes the bucket policy that
    // grants the regional ELB log-delivery principal.
    this.accessLogBucket = new s3.Bucket(this, 'AlbAccessLogBucket', {
      bucketName: getResourceName(config, 'alb-access-logs'),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      lifecycleRules: [
        {
          id: 'expire-access-logs',
          expiration: cdk.Duration.days(ACCESS_LOG_RETENTION_DAYS),
          abortIncompleteMultipartUploadAfter: cdk.Duration.days(7),
        },
      ],
      removalPolicy: getRemovalPolicy(config),
      autoDeleteObjects: !config.retainDataOnDelete,
    });

    this.alb.logAccessLogs(this.accessLogBucket);



    // `open` defaults to true on an ALB listener, and an open listener has
    // CDK add its own 0.0.0.0/0 ingress rule for the listener port — which
    // would silently undo the CloudFront-only rule above. Leave it on only
    // when the ALB is deliberately public, so that template is unchanged.
    const openToInternet = !config.albRestrictToCloudFront;

    // ALB Listeners (HTTP and optional HTTPS)
    if (config.certificateArn) {
      const certificate = acm.Certificate.fromCertificateArn(
        this,
        'Certificate',
        config.certificateArn,
      );

      // Create HTTPS listener - this is where backend services attach
      this.albListener = this.alb.addListener('HttpsListener', {
        port: 443,
        open: openToInternet,
        protocol: elbv2.ApplicationProtocol.HTTPS,
        certificates: [certificate],
        // Pin to the 2021 TLS-1.3 policy: TLS 1.2 minimum, all CBC
        // cipher suites removed, modern AEAD ciphers only. The default
        // (ELBSecurityPolicy-2016-08) still allows TLS 1.0 + CBC,
        // which is the BEAST exposure path.
        sslPolicy: elbv2.SslPolicy.TLS13_RES,
        defaultAction: elbv2.ListenerAction.fixedResponse(404, {
          contentType: 'text/plain',
          messageBody: 'Not Found - No matching route',
        }),
      });


      // HTTP listener only redirects to HTTPS (no target groups here)
      this.alb.addListener('HttpListener', {
        port: 80,
        open: openToInternet,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.redirect({
          protocol: 'HTTPS',
          port: '443',
          permanent: true,
        }),
      });
    } else {
      // No certificate — single HTTP listener serves as the primary.
      this.albListener = this.alb.addListener('HttpListener', {
        port: 80,
        open: openToInternet,
        protocol: elbv2.ApplicationProtocol.HTTP,
        defaultAction: elbv2.ListenerAction.fixedResponse(404, {
          contentType: 'text/plain',
          messageBody: 'Not Found - No matching route',
        }),
      });
    }

    // Export the primary listener ARN — backend services use this to
    // attach their target group rules.
  }
}
