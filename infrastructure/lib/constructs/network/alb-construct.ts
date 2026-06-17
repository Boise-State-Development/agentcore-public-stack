import * as acm from 'aws-cdk-lib/aws-certificatemanager';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface AlbConstructProps {
  config: AppConfig;
  vpc: ec2.IVpc;
}

/**
 * AlbConstruct — internet-facing ALB + security group + primary listener.
 *
 * Provisions:
 *   - ALB security group permitting :80 and :443 from anywhere
 *   - Internet-facing ALB in the VPC's public subnets
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

  constructor(scope: Construct, id: string, props: AlbConstructProps) {
    super(scope, id);

    const { config, vpc } = props;

    // ALB Security Group - Allow HTTP/HTTPS from internet
    this.albSecurityGroup = new ec2.SecurityGroup(this, 'AlbSecurityGroup', {
      vpc,
      securityGroupName: getResourceName(config, 'alb-sg'),
      description: 'Security group for Application Load Balancer',
      allowAllOutbound: true,
    });

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
