import * as cdk from 'aws-cdk-lib';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as route53 from 'aws-cdk-lib/aws-route53';
import * as route53Targets from 'aws-cdk-lib/aws-route53-targets';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig } from '../../config';

export interface AlbDnsConstructProps {
  config: AppConfig;
  /** The Platform-owned ALB; an A-record points here when configured. */
  alb: elbv2.IApplicationLoadBalancer;
}

/**
 * AlbDnsConstruct — Route53 hosted zone lookup + ALB A record + ALB
 * URL export.
 *
 * The hosted zone and certificates are created outside the stack
 * (manually or via a separate DNS stack). When
 * `config.infrastructureHostedZoneDomain` is set, this construct looks
 * up the existing zone, optionally creates an A record at
 * `{albSubdomain}.{infrastructureHostedZoneDomain}` aliasing the ALB,
 * and publishes the resolved URL to SSM (consumed by the SPA at
 * runtime).
 *
 * When no hosted zone is configured the construct falls back to
 * exporting the bare ALB DNS name as the URL.
 */
export class AlbDnsConstruct extends Construct {
  /** The resolved ALB URL, with protocol. */
  public readonly albUrl: string;

  constructor(scope: Construct, id: string, props: AlbDnsConstructProps) {
    super(scope, id);

    const { config, alb } = props;

    if (
      config.infrastructureHostedZoneDomain &&
      config.infrastructureHostedZoneDomain.trim() !== ''
    ) {
      const hostedZone = route53.HostedZone.fromLookup(this, 'HostedZone', {
        domainName: config.infrastructureHostedZoneDomain,
      });



      if (config.albSubdomain) {
        const albRecordName = `${config.albSubdomain}.${config.infrastructureHostedZoneDomain}`;

        new route53.ARecord(this, 'AlbARecord', {
          zone: hostedZone,
          recordName: config.albSubdomain,
          target: route53.RecordTarget.fromAlias(
            new route53Targets.LoadBalancerTarget(alb),
          ),
          comment: `A record for ALB - points ${albRecordName} to load balancer`,
        });

        if (config.certificateArn) {
          new cdk.CfnOutput(this, 'AlbUrlHttps', {
            value: `https://${albRecordName}`,
            description:
              'Application Load Balancer HTTPS URL (HTTP redirects here)',
            exportName: `${config.projectPrefix}-alb-url-https`,
          });
        }
      }
    }

    // Determine the ALB URL to export.
    // Priority: custom domain (if configured) > ALB DNS name.
    let albUrlDescription: string;
    if (config.infrastructureHostedZoneDomain && config.albSubdomain) {
      const albRecordName = `${config.albSubdomain}.${config.infrastructureHostedZoneDomain}`;
      const protocol = config.certificateArn ? 'https' : 'http';
      this.albUrl = `${protocol}://${albRecordName}`;
      albUrlDescription = 'Application Load Balancer Custom Domain URL';
    } else {
      const protocol = config.certificateArn ? 'https' : 'http';
      this.albUrl = `${protocol}://${alb.loadBalancerDnsName}`;
      albUrlDescription = 'Application Load Balancer URL (DNS name)';
    }

    new ssm.StringParameter(this, 'AlbUrlParameter', {
      parameterName: `/${config.projectPrefix}/network/alb-url`,
      stringValue: this.albUrl,
      description: albUrlDescription,
      tier: ssm.ParameterTier.STANDARD,
    });

    new cdk.CfnOutput(this, 'AlbUrl', {
      value: this.albUrl,
      description: albUrlDescription,
      exportName: `${config.projectPrefix}-alb-url`,
    });
  }
}
