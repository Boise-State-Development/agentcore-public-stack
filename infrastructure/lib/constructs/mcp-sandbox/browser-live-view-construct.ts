import * as cdk from 'aws-cdk-lib';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import * as s3 from 'aws-cdk-lib/aws-s3';
import * as s3deploy from 'aws-cdk-lib/aws-s3-deployment';
import * as fs from 'fs';
import * as path from 'path';
import { Construct } from 'constructs';

import { AppConfig, getRemovalPolicy, getResourceName } from '../../config';

/** Where the viewer's static files live in the repo. */
export const BROWSER_LIVE_VIEW_ASSET_DIR = path.resolve(
  __dirname,
  '..',
  '..',
  '..',
  'assets',
  'browser-live-view',
);

export interface BrowserLiveViewConstructProps {
  config: AppConfig;
  /**
   * The mcp-sandbox distribution's dynamic-CSP function. Shared rather than
   * copied: it is the one place `frame-ancestors` is locked to the SPA, and it
   * composes the viewer's `connect-src` from the `?csp=` query the SPA sends
   * (the minted stream's own origin, nothing wider). A second copy would be a
   * second `frame-ancestors` that could drift.
   */
  cspFunction: cloudfront.IFunction;
  /** HSTS + `no-referrer` + nosniff, shared with the mcp-sandbox origin. */
  responseHeadersPolicy: cloudfront.IResponseHeadersPolicy;
  /** Short-TTL policy, shared with the mcp-sandbox origin. */
  cachePolicy: cloudfront.ICachePolicy;
}

/**
 * BrowserLiveViewConstruct — the browser sign-in viewer's own origin.
 *
 * `live-view.html` is where a user types a password into a browser running in
 * our AWS account (docs/specs/authenticated-web-assessment.md D3). It used to
 * be served from the mcp-sandbox origin, which also runs untrusted MCP App
 * HTML. Two frames of one origin on one page are one security principal: an
 * App framed beside the viewer could reach it through `window.top.frames` and
 * read its document — the signed live-view URL the SPA posts in, and what the
 * user types. So the viewer gets an origin no MCP App ever runs on.
 *
 * Why a second distribution on its CloudFront default domain
 * ----------------------------------------------------------
 * - **Origins are per host**, so isolation needs a different host. A second
 *   alias on the mcp-sandbox distribution would serve both pages on both
 *   hosts; a second distribution serves only this.
 * - **No certificate, no DNS.** `dxxxx.cloudfront.net` carries CloudFront's
 *   own certificate, so an operator adds nothing: no ACM SAN, no Route53
 *   record, no `CDK_*_CERTIFICATE_ARN` variable, and no synth that throws for
 *   a domain configured ahead of its cert. The hostname is never shown to the
 *   user; it is an iframe's origin.
 * - **Its own bucket.** The mcp-sandbox distribution's OAC cannot read a
 *   single viewer file, and this one cannot read `proxy.html`. A shared bucket
 *   with a key prefix would leave a copy of the viewer reachable on the App
 *   origin, and the mcp-sandbox deployment's `prune` would fight the prefix.
 * - **Sandboxing the iframe without `allow-same-origin` was not chosen.** An
 *   opaque origin loses storage and sends `Origin: null` on the WebSocket
 *   upgrade, and the DCV SDK's behaviour under either is undocumented; it would
 *   also force the SPA to post the signed URL to `'*'`, since an opaque frame
 *   has no origin to target. A real origin keeps the exact-origin postMessage
 *   handshake both sides already enforce.
 *
 * The resolved origin is surfaced as `origin` and reaches the SPA on app-api's
 * live-view mint response (`viewerOrigin`), beside the credential it is the
 * only place to send. The AgentCore Runtime never carries it, so the V2
 * environment budget is untouched.
 */
export class BrowserLiveViewConstruct extends Construct {
  public readonly bucket: s3.Bucket;
  public readonly distribution: cloudfront.Distribution;
  /** `https://dxxxx.cloudfront.net` — never the mcp-sandbox proxy origin. */
  public readonly origin: string;

  constructor(scope: Construct, id: string, props: BrowserLiveViewConstructProps) {
    super(scope, id);

    const { config, cspFunction, responseHeadersPolicy, cachePolicy } = props;

    this.bucket = new s3.Bucket(this, 'Bucket', {
      bucketName: getResourceName(config, 'browser-live-view', config.awsAccount),
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: getRemovalPolicy(config),
    });

    this.distribution = new cloudfront.Distribution(this, 'Distribution', {
      comment: getResourceName(config, 'browser-live-view-cdn'),
      defaultRootObject: 'live-view.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(this.bucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy,
        responseHeadersPolicy,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_GET_HEAD,
        cachedMethods: cloudfront.CachedMethods.CACHE_GET_HEAD,
        compress: true,
        functionAssociations: [
          {
            function: cspFunction,
            eventType: cloudfront.FunctionEventType.VIEWER_RESPONSE,
          },
        ],
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      enabled: true,
    });

    this.origin = `https://${this.distribution.distributionDomainName}`;

    // The viewer needs the Amazon DCV Web Client SDK served beside it. The SDK
    // is a EULA-licensed AWS download and this repo is public, so it is fetched
    // at build time by `scripts/build/fetch-dcv-sdk.sh` and is gitignored —
    // which means a synth that skipped that step would deploy a viewer with no
    // client.
    //
    // A warning rather than an error on purpose: every CDK unit test synths
    // this stack, and none of them should have to download a 1.6MB archive to
    // do it. The viewer also fails visibly rather than silently ("The viewer
    // failed to load"), so this is a nudge, not the only line of defence.
    if (!fs.existsSync(path.join(BROWSER_LIVE_VIEW_ASSET_DIR, 'dcvjs', 'dcv.js'))) {
      cdk.Annotations.of(this).addWarning(
        'Amazon DCV Web Client SDK not found at assets/browser-live-view/dcvjs/. ' +
          'The browser sign-in viewer will deploy without its client and fail ' +
          'to connect. Run scripts/build/fetch-dcv-sdk.sh before deploying.',
      );
    }

    new s3deploy.BucketDeployment(this, 'Deployment', {
      sources: [s3deploy.Source.asset(BROWSER_LIVE_VIEW_ASSET_DIR)],
      destinationBucket: this.bucket,
      distribution: this.distribution,
      distributionPaths: ['/*'],
      prune: true,
    });
  }
}
