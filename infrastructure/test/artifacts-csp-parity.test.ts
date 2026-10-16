/**
 * Parity cover for the artifact origin's TWO hand-maintained CSP copies.
 *
 * Unlike `/api/*` — where the edge policy deliberately REPLACES a different
 * origin header (see api-security-headers.test.ts) — the artifact origin is
 * built so both layers emit the SAME policy:
 *
 *   - `ArtifactsDistributionConstruct` builds `cspDirectives` for the
 *     CloudFront response-headers policy (`override: true`).
 *   - `artifact_render/handler.py` builds its own via `_csp_header()`, from
 *     the `CSP_SCRIPT_SRC` / `FRAME_ANCESTOR_ORIGIN` env vars that
 *     `ArtifactRenderLambdaConstruct` passes it.
 *
 * Both files carry a comment saying the `script-src` allow-list "must stay
 * byte-identical" with the other, and until now nothing checked. Because the
 * distribution sets `override: true`, a drift is SILENT: CloudFront's copy
 * wins at the edge and the Lambda's copy is what a direct Function-URL reach
 * would return, so the two can disagree indefinitely without a failing test
 * or a visibly broken artifact.
 *
 * This asserts the values duplicated as literals in both languages: the
 * `script-src` source list, including its CDN allow-list, and the
 * `style-src` / `font-src` lists the Markdown wrapper's KaTeX depends on.
 */
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { loadConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import { mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

function seedRequiredContext(app: cdk.App): void {
  app.node.setContext('projectPrefix', 'test-project');
  app.node.setContext('awsRegion', MOCK_REGION);
  app.node.setContext('awsAccount', MOCK_ACCOUNT);
  app.node.setContext('vpcCidr', '10.0.0.0/16');
  app.node.setContext('corsOrigins', 'http://localhost:4200');
  app.node.setContext('production', false);
  app.node.setContext('retainDataOnDelete', false);
  app.node.setContext('frontend', { cloudFrontPriceClass: 'PriceClass_100' });
  app.node.setContext('appApi', { cpu: 256, memory: 512, desiredCount: 1, maxCapacity: 2 });
  app.node.setContext('inferenceApi', {});
  app.node.setContext('fineTuning', {});
  app.node.setContext('artifacts', { retentionDays: 90, extraFrameAncestors: [] });
  app.node.setContext('mcpSandbox', { extraFrameAncestors: [] });
  app.node.setContext('ragIngestion', {
    additionalCorsOrigins: '',
    lambdaMemorySize: 10240,
    lambdaTimeout: 900,
    embeddingModel: 'amazon.titan-embed-text-v2',
    vectorDimension: 1024,
    vectorDistanceMetric: 'cosine',
  });
}

function synth(): Template {
  const app = new cdk.App();
  seedRequiredContext(app);
  const config = loadConfig(app);
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'ArtifactsCspPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  return Template.fromStack(stack);
}

/** The artifact distribution's CSP string, as synthesized. */
function edgeCsp(template: Template): string {
  const policies = template.findResources('AWS::CloudFront::ResponseHeadersPolicy');
  const artifacts = Object.values(policies).find(
    (p) =>
      p.Properties?.ResponseHeadersPolicyConfig?.Name ===
      'test-project-artifacts-headers',
  );
  expect(artifacts).toBeDefined();
  return artifacts!.Properties.ResponseHeadersPolicyConfig.SecurityHeadersConfig
    .ContentSecurityPolicy.ContentSecurityPolicy;
}

/** The render Lambda's CSP_SCRIPT_SRC env var, as synthesized. */
function lambdaScriptSrc(template: Template): string {
  const functions = template.findResources('AWS::Lambda::Function');
  const render = Object.values(functions).find(
    (f) => f.Properties?.Environment?.Variables?.CSP_SCRIPT_SRC !== undefined,
  );
  expect(render).toBeDefined();
  return render!.Properties.Environment.Variables.CSP_SCRIPT_SRC;
}

/**
 * A directive the render Lambda hard-codes in `_csp_header()` (only
 * script-src and frame-ancestors come from env vars), read from the handler
 * source because no synthesized value carries it.
 */
function lambdaLiteralDirective(name: string): string {
  const handler = fs.readFileSync(
    path.join(__dirname, '../../backend/src/lambdas/artifact_render/handler.py'),
    'utf-8',
  );
  const found = handler.match(new RegExp(`"${name} ([^"]*)",`));
  expect(found).not.toBeNull();
  return found![1].trim();
}

/** Pull one directive's source list out of a `a; b; c` CSP string. */
function directive(csp: string, name: string): string {
  const found = csp
    .split(';')
    .map((d) => d.trim())
    .find((d) => d === name || d.startsWith(`${name} `));
  expect(found).toBeDefined();
  return found!.slice(name.length).trim();
}

describe('artifact origin CSP parity (CloudFront policy vs render Lambda)', () => {
  it('ships the same script-src allow-list to both layers', () => {
    const template = synth();

    // If this fails, one of the two literals was edited without the other:
    //   infrastructure/lib/constructs/artifacts/artifacts-distribution-construct.ts
    //   infrastructure/lib/constructs/artifacts/artifact-render-lambda-construct.ts
    // Fix by making them match, not by relaxing this assertion — a CDN added
    // to only one copy loads at the edge and is blocked on the other path.
    expect(directive(edgeCsp(template), 'script-src')).toBe(
      lambdaScriptSrc(template),
    );
  });

  it('ships the same style-src and font-src to both layers', () => {
    const csp = edgeCsp(synth());

    // The Markdown render wrapper loads KaTeX's stylesheet and fonts from
    // jsDelivr. Allowed at the edge but not by the Lambda (or the reverse),
    // math typesets on one path and degrades on the other.
    for (const name of ['style-src', 'font-src']) {
      expect(directive(csp, name)).toBe(lambdaLiteralDirective(name));
      expect(directive(csp, name)).toContain('https://cdn.jsdelivr.net');
    }
  });

  it('keeps the artifact sandbox directives that make artifact JS inert', () => {
    const csp = edgeCsp(synth());

    // `connect-src 'none'` is the line that stops artifact JS exfiltrating;
    // the other two block navigation-based escapes. All three are asserted
    // because artifacts are the one origin that intentionally runs
    // model-authored script.
    expect(directive(csp, 'connect-src')).toBe("'none'");
    expect(directive(csp, 'form-action')).toBe("'none'");
    expect(directive(csp, 'base-uri')).toBe("'none'");
  });
});
