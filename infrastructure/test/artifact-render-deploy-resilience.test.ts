/**
 * Guard tests for the artifact-render zip-Lambda code deploy.
 *
 * Regression cover for the stranded-bootstrap-stub bug: the
 * out-of-band code deploy (`deploy-lambda-code-if-changed.sh`) skipped
 * `update-function-code` whenever the SOURCE hash was unchanged. But
 * the SSM source hash is decoupled from the function's ACTUAL live
 * code — a CFN/Platform deploy that replaces the Lambda (logical-id
 * change, e.g. hoisting it into PlatformStack) reverts it to the
 * bootstrap 503 stub WITHOUT changing the source. So the script
 * skipped forever and the placeholder stayed live in production
 * (artifacts.{domain} served "Artifact service is updating").
 *
 * The fix adds a liveness guard: the script also records the deployed
 * CodeSha256 and compares it against the function's live CodeSha256
 * each run, re-deploying when they diverge regardless of the source
 * hash. These tests pin that the guard wiring stays in place — they
 * mirror the file-shape style of the "Build scripts" suite in
 * network-and-scripts.test.ts (the build scripts are intentionally
 * not executed in unit tests so they don't depend on aws/zip/sha256sum
 * being present locally).
 */
import * as fs from 'fs';
import * as path from 'path';

const BUILD = path.resolve(__dirname, '..', '..', 'scripts', 'build');
const IF_CHANGED = path.join(BUILD, 'deploy-lambda-code-if-changed.sh');
const ONE = path.join(BUILD, 'deploy-zip-lambda-one.sh');

const read = (p: string): string => fs.readFileSync(p, 'utf-8');

describe('deploy-lambda-code-if-changed.sh — liveness guard', () => {
  let content: string;
  beforeAll(() => {
    content = read(IF_CHANGED);
  });

  it('exists and is executable', () => {
    const stat = fs.statSync(IF_CHANGED);
    expect(stat.isFile()).toBe(true);
    expect(stat.mode & 0o111).toBeGreaterThan(0);
  });

  it('accepts a --code-sha256-ssm argument', () => {
    expect(content).toContain('--code-sha256-ssm');
    expect(content).toContain('CODE_SHA256_SSM');
  });

  it('requires --code-sha256-ssm (fails loudly if omitted)', () => {
    expect(content).toMatch(/missing --code-sha256-ssm/);
  });

  it('reads the function’s live CodeSha256', () => {
    expect(content).toContain('get-function-configuration');
    expect(content).toContain("--query 'CodeSha256'");
    expect(content).toContain('LIVE_SHA');
  });

  it('only skips when the source hash AND the live CodeSha256 both match the last deploy', () => {
    // The skip condition must AND all three signals together; a
    // source-hash-only skip is exactly the regression we are guarding.
    expect(content).toMatch(
      /if\s*\[\[\s*"\$HASH"\s*==\s*"\$PUBLISHED_HASH"\s*&&\s*-n\s*"\$RECORDED_SHA"\s*&&\s*"\$LIVE_SHA"\s*==\s*"\$RECORDED_SHA"\s*\]\]/,
    );
  });

  it('re-deploys (does not skip) when the live code drifted from what we shipped', () => {
    // The drift branch is what un-sticks a function that CFN reset to
    // the bootstrap stub.
    expect(content).toContain('LIVE DRIFT');
  });

  it('records the settled CodeSha256 to SSM after a successful deploy', () => {
    // After update-function-code we must persist the new baseline so
    // the next run can compare against it.
    expect(content).toContain('NEW_SHA');
    expect(content).toMatch(/put-parameter[\s\S]*?--name "\$CODE_SHA256_SSM"/);
  });
});

describe('deploy-zip-lambda-one.sh — artifact-render wiring', () => {
  let content: string;
  beforeAll(() => {
    content = read(ONE);
  });

  it('exists and is executable', () => {
    const stat = fs.statSync(ONE);
    expect(stat.isFile()).toBe(true);
    expect(stat.mode & 0o111).toBeGreaterThan(0);
  });

  it('defines the render-code-sha256 SSM path for artifact-render', () => {
    expect(content).toContain('/artifacts/render-code-sha256');
    expect(content).toContain('CODE_SHA256_SSM=');
  });

  it('passes --code-sha256-ssm through to deploy-lambda-code-if-changed.sh', () => {
    expect(content).toMatch(/--code-sha256-ssm "\$CODE_SHA256_SSM"/);
  });
});
