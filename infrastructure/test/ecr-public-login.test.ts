/**
 * Authenticated ECR Public pulls for the image builds.
 *
 * On 2026-10-17 the 1.28.2 prod Backend Deploy failed "Build app-api image"
 * twice with `429 Too Many Requests — toomanyrequests: Data limit exceeded`
 * from public.ecr.aws: ECR Public caps ANONYMOUS pulls by data per source IP,
 * and the shared x86 runner pool had spent it. These tests drive the real
 * scripts against stub `aws` / `docker` binaries on PATH, so they pin the
 * behaviour (login before build, best-effort fallback, a clean stdout, one
 * bounded retry on a 429) rather than the wording.
 */
import { spawnSync, SpawnSyncReturns } from 'child_process';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const REPO_ROOT = path.resolve(__dirname, '..', '..');
const SCRIPTS = path.join(REPO_ROOT, 'scripts', 'build');
const LOGIN_SCRIPT = path.join(SCRIPTS, 'ecr-public-login.sh');
const BUILD_SCRIPT = path.join(SCRIPTS, 'build-and-push-if-changed.sh');

const AWS_STUB = `#!/usr/bin/env bash
echo "aws $*" >> "$STUB_LOG"
case "$1 $2" in
  "ecr-public get-login-password")
    if [[ "\${STUB_PUBLIC_LOGIN_FAILS:-0}" == "1" ]]; then
      echo "AccessDeniedException: not authorized" >&2; exit 254
    fi
    echo "public-token" ;;
  "ecr get-login-password") echo "private-token" ;;
  "ecr describe-images") exit 254 ;;
esac
exit 0
`;

const DOCKER_STUB = `#!/usr/bin/env bash
echo "docker $*" >> "$STUB_LOG"
case "$1" in
  login)
    echo "docker login-password $(cat)" >> "$STUB_LOG"
    echo "Login Succeeded" ;;
  build)
    n=$(( $(cat "$STUB_DIR/builds" 2>/dev/null || echo 0) + 1 ))
    echo "$n" > "$STUB_DIR/builds"
    if (( n <= \${STUB_BUILD_FAILURES:-0} )); then
      echo "$STUB_BUILD_ERROR"; exit 1
    fi
    echo "#1 built" ;;
esac
exit 0
`;

const RATE_LIMITED =
  'ERROR: failed to resolve source metadata for public.ecr.aws/docker/library/python:3.13-slim: ' +
  '429 Too Many Requests - Server message: toomanyrequests: Data limit exceeded';

interface StubRun {
  result: SpawnSyncReturns<string>;
  log: string[];
}

function runWithStubs(script: string, args: string[], env: Record<string, string> = {}): StubRun {
  const stubDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ecr-public-stubs-'));
  const stubLog = path.join(stubDir, 'calls.log');
  fs.writeFileSync(path.join(stubDir, 'aws'), AWS_STUB, { mode: 0o755 });
  fs.writeFileSync(path.join(stubDir, 'docker'), DOCKER_STUB, { mode: 0o755 });
  fs.writeFileSync(stubLog, '');
  try {
    const result = spawnSync('bash', [script, ...args], {
      cwd: REPO_ROOT,
      encoding: 'utf-8',
      env: {
        ...process.env,
        PATH: `${stubDir}:${process.env.PATH}`,
        STUB_DIR: stubDir,
        STUB_LOG: stubLog,
        AWS_REGION: 'us-west-2',
        BASE_IMAGE_RETRY_DELAY_SECONDS: '0',
        ...env,
      },
    });
    const log = fs.readFileSync(stubLog, 'utf-8').split('\n').filter(Boolean);
    return { result, log };
  } finally {
    fs.rmSync(stubDir, { recursive: true, force: true });
  }
}

const BUILD_ARGS = [
  '--service', 'app-api',
  '--dockerfile', 'backend/Dockerfile.app-api',
  '--source-dir', 'scripts/build',
  '--manifest', 'backend/pyproject.toml',
  '--ecr-repository', '123456789012.dkr.ecr.us-west-2.amazonaws.com/test-app-api',
];

describe('ecr-public-login.sh', () => {
  it('is executable', () => {
    expect(fs.statSync(LOGIN_SCRIPT).mode & 0o111).toBeGreaterThan(0);
  });

  it('logs docker in to public.ecr.aws with a us-east-1 token, keeping stdout empty', () => {
    const { result, log } = runWithStubs(LOGIN_SCRIPT, []);

    expect(result.status).toBe(0);
    expect(log).toContain('aws ecr-public get-login-password --region us-east-1');
    expect(log).toContain('docker login --username AWS --password-stdin public.ecr.aws');
    expect(log).toContain('docker login-password public-token');
    // build-and-push-if-changed.sh's stdout is the image tag its caller captures.
    expect(result.stdout).toBe('');
  });

  it('falls back to anonymous pulls, without failing, when the token is refused', () => {
    const { result, log } = runWithStubs(LOGIN_SCRIPT, [], { STUB_PUBLIC_LOGIN_FAILS: '1' });

    expect(result.status).toBe(0);
    expect(log.some((l) => l.startsWith('docker login'))).toBe(false);
    expect(result.stderr).toContain('WARNING');
    expect(result.stderr).toContain('ecr-public:GetAuthorizationToken');
    expect(result.stdout).toBe('');
  });
});

describe('build-and-push-if-changed.sh base-image pulls', () => {
  it('logs in to ECR Public before the build, and prints only the tag', () => {
    const { result, log } = runWithStubs(BUILD_SCRIPT, BUILD_ARGS);

    expect(result.status).toBe(0);
    const publicLogin = log.indexOf('docker login --username AWS --password-stdin public.ecr.aws');
    const firstBuild = log.findIndex((l) => l.startsWith('docker build'));
    expect(publicLogin).toBeGreaterThanOrEqual(0);
    expect(firstBuild).toBeGreaterThan(publicLogin);
    expect(result.stdout.trim()).toMatch(/^[0-9a-f]{16}$/);
  });

  it('still builds when the ECR Public login is refused', () => {
    const { result, log } = runWithStubs(BUILD_SCRIPT, BUILD_ARGS, { STUB_PUBLIC_LOGIN_FAILS: '1' });

    expect(result.status).toBe(0);
    expect(log.filter((l) => l.startsWith('docker push'))).toHaveLength(1);
  });

  it('retries the build once after a registry 429, then pushes', () => {
    const { result, log } = runWithStubs(BUILD_SCRIPT, BUILD_ARGS, {
      STUB_BUILD_FAILURES: '1',
      STUB_BUILD_ERROR: RATE_LIMITED,
    });

    expect(result.status).toBe(0);
    expect(log.filter((l) => l.startsWith('docker build'))).toHaveLength(2);
    expect(log.filter((l) => l.startsWith('docker push'))).toHaveLength(1);
    expect(result.stdout.trim()).toMatch(/^[0-9a-f]{16}$/);
  });

  it('gives up after one retry when the 429 persists', () => {
    const { result, log } = runWithStubs(BUILD_SCRIPT, BUILD_ARGS, {
      STUB_BUILD_FAILURES: '2',
      STUB_BUILD_ERROR: RATE_LIMITED,
    });

    expect(result.status).not.toBe(0);
    expect(log.filter((l) => l.startsWith('docker build'))).toHaveLength(2);
    expect(log.some((l) => l.startsWith('docker push'))).toBe(false);
  });

  it('does not retry a build failure that is not a 429', () => {
    const { result, log } = runWithStubs(BUILD_SCRIPT, BUILD_ARGS, {
      STUB_BUILD_FAILURES: '1',
      STUB_BUILD_ERROR: 'ERROR: failed to solve: process "/bin/sh -c uv sync" did not complete successfully',
    });

    expect(result.status).not.toBe(0);
    expect(log.filter((l) => l.startsWith('docker build'))).toHaveLength(1);
    expect(log.some((l) => l.startsWith('docker push'))).toBe(false);
  });
});

describe('platform.yml', () => {
  it('logs in to ECR Public before CDK builds the bootstrap-asset images', () => {
    const workflow = fs.readFileSync(
      path.join(REPO_ROOT, '.github', 'workflows', 'platform.yml'),
      'utf-8',
    );
    const login = workflow.indexOf('run: bash scripts/build/ecr-public-login.sh');
    const deploy = workflow.indexOf('run: bash scripts/platform/deploy.sh');
    expect(login).toBeGreaterThan(0);
    expect(deploy).toBeGreaterThan(login);
  });
});
