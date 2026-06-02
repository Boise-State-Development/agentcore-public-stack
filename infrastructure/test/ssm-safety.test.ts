/**
 * Same-stack SSM safety: assert no AWS::SSM::Parameter::Value
 * template parameter references an SSM path that this stack creates.
 *
 * Background: `ssm.StringParameter.valueForStringParameter()`
 * synthesizes an `AWS::SSM::Parameter::Value<String>` CFN template
 * parameter with the SSM path baked in as the `Default`. CFN
 * resolves all template parameters BEFORE creating any resources,
 * so if the same template ALSO creates that SSM parameter via an
 * `AWS::SSM::Parameter` resource, the first-deploy is unsatisfiable
 * — CFN errors with "Unable to fetch parameters from parameter store".
 *
 * This test walks every template parameter of that type, extracts
 * the referenced SSM path, and asserts the path is NOT also created
 * by an `AWS::SSM::Parameter` resource in the same template (or is
 * in a small allowlist of legitimately-external params).
 *
 * Would have caught the pre-Phase-7 deadlock that motivated the
 * platform-compute-refs.ts refactor at synth time, before any
 * deploy.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

// SSM paths that are legitimately external (created by build
// scripts or seeded out-of-band, not by CDK in the same stack).
// These are safe to read via valueForStringParameter because they
// exist before the stack ever runs CFN parameter resolution.
const EXTERNALLY_SEEDED_SSM_PATHS: ReadonlyArray<RegExp> = [
  // CDK's own bootstrap version. Seeded by `cdk bootstrap` into the
  // CDKToolkit stack before any application stack ever runs.
  /^\/cdk-bootstrap\/[\w-]+\/version$/,
  // Image tags written by content-hash build pipeline before any
  // CFN run.
  /\/[\w-]+\/app-api\/image-tag$/,
  /\/[\w-]+\/inference-api\/image-tag$/,
  /\/[\w-]+\/rag-ingestion\/image-tag$/,
  // Code hashes written by build pipeline.
  /\/[\w-]+\/artifacts\/render-code-hash$/,
];

describe('Same-stack SSM safety', () => {
  let template: Template;

  beforeAll(() => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: {},
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  function ssmParameterValueParameters(): Array<{ name: string; ssmPath: string }> {
    const params = template.toJSON().Parameters ?? {};
    const out: Array<{ name: string; ssmPath: string }> = [];
    for (const [name, param] of Object.entries(params) as [string, Record<string, unknown>][]) {
      const ptype = param.Type as string | undefined;
      if (typeof ptype === 'string' && ptype.startsWith('AWS::SSM::Parameter::Value')) {
        const ssmPath = (param.Default as string | undefined) ?? '';
        out.push({ name, ssmPath });
      }
    }
    return out;
  }

  function ssmParameterResourcePaths(): Set<string> {
    const resources = template.findResources('AWS::SSM::Parameter');
    const paths = new Set<string>();
    for (const r of Object.values(resources)) {
      const props = r.Properties as { Name?: string } | undefined;
      if (props?.Name) paths.add(props.Name);
    }
    return paths;
  }

  function isExternallySeeded(ssmPath: string): boolean {
    return EXTERNALLY_SEEDED_SSM_PATHS.some((re) => re.test(ssmPath));
  }

  it('no AWS::SSM::Parameter::Value template parameter references a path this stack creates', () => {
    const valueParams = ssmParameterValueParameters();
    const createdPaths = ssmParameterResourcePaths();

    const violations: string[] = [];
    for (const { name, ssmPath } of valueParams) {
      if (createdPaths.has(ssmPath)) {
        violations.push(
          `  template Parameter "${name}" reads SSM path "${ssmPath}" — but this same stack creates that path. ` +
            `CFN resolves Parameters before resources, so first deploy will fail with "Unable to fetch parameters from parameter store".`,
        );
      }
    }

    if (violations.length > 0) {
      throw new Error(
        `Same-stack SSM publish-then-read deadlock detected (${violations.length}):\n` +
          violations.join('\n') +
          `\n\nFix by passing the value via a typed construct ref (PlatformComputeRefs) instead of valueForStringParameter.`,
      );
    }
  });

  it('any remaining AWS::SSM::Parameter::Value parameter is in the externally-seeded allowlist', () => {
    const valueParams = ssmParameterValueParameters();
    const orphans = valueParams.filter(
      ({ ssmPath }) => !isExternallySeeded(ssmPath),
    );

    if (orphans.length > 0) {
      throw new Error(
        `Found ${orphans.length} AWS::SSM::Parameter::Value template parameter(s) that are not in the externally-seeded allowlist:\n` +
          orphans.map((o) => `  ${o.name} → ${o.ssmPath}`).join('\n') +
          `\n\nIf these are legitimately external (written by build scripts before deploy), add the path pattern to EXTERNALLY_SEEDED_SSM_PATHS.\n` +
          `Otherwise, replace the valueForStringParameter call with a typed construct ref.`,
      );
    }
  });
});
