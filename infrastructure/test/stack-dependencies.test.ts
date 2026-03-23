/**
 * Stack Dependency Order Tests
 *
 * These tests statically analyse the CDK stack source files to extract
 * SSM parameter reads and writes, build a dependency graph, and verify:
 *
 *   1. The graph is a valid DAG (no circular dependencies).
 *   2. No stack reads a param written by a stack in the same or a later
 *      deployment tier — which would break fresh deployments.
 *   3. Every SSM param that is read is written by some stack (or is a
 *      known externally-provided param like image tags).
 *
 * WHY source-code scanning instead of CloudFormation template inspection?
 * ─────────────────────────────────────────────────────────────────────────
 * The stacks use `StringParameter.valueFromLookup()` which resolves at
 * **synthesis time**.  The resolved value is baked into the CF template as
 * a literal string, so the SSM dependency is invisible in the generated
 * CloudFormation output.  Scanning the TypeScript source is the only
 * reliable way to detect these dependencies.
 */

import * as fs from 'fs';
import * as path from 'path';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const LIB_DIR = path.resolve(__dirname, '..', 'lib');

/** Canonical stack file names → human-readable labels. */
const STACK_FILES: Record<string, string> = {
  'infrastructure-stack.ts': 'InfrastructureStack',
  'rag-ingestion-stack.ts': 'RagIngestionStack',
  'gateway-stack.ts': 'GatewayStack',
  'sagemaker-fine-tuning-stack.ts': 'SageMakerFineTuningStack',
  'inference-api-stack.ts': 'InferenceApiStack',
  'app-api-stack.ts': 'AppApiStack',
  'frontend-stack.ts': 'FrontendStack',
};

/**
 * Deployment tier assignments.
 *
 * A stack in tier N may only read SSM parameters written by stacks in
 * tiers 0 … N-1.  Stacks within the same tier MUST NOT depend on each
 * other via SSM.
 *
 * Tier 0 — foundation (no SSM reads from other stacks)
 * Tier 1 — reads only from Tier 0
 * Tier 2 — reads from Tier 0 + 1
 * Tier 3 — reads from Tier 0 + 1 + 2
 * Tier 4 — reads from Tier 0 + 1 + 2 + 3
 */
const DEPLOYMENT_TIERS: Record<string, number> = {
  InfrastructureStack: 0,
  RagIngestionStack: 1,
  GatewayStack: 1,
  SageMakerFineTuningStack: 1,
  InferenceApiStack: 2,
  AppApiStack: 3,
  FrontendStack: 4,
};

/**
 * SSM parameter suffixes that are NOT written by any CDK stack.
 * These are set externally (e.g. by CI/CD pipelines) before deployment.
 */
const EXTERNAL_PARAMS = new Set([
  'app-api/image-tag',
  'inference-api/image-tag',
  'rag-ingestion/image-tag',
]);

// ---------------------------------------------------------------------------
// Extraction helpers
// ---------------------------------------------------------------------------

/**
 * Extract SSM parameter path suffixes that a stack WRITES.
 *
 * Looks for patterns like:
 *   parameterName: `/${config.projectPrefix}/some/path`
 *   parameterName: `/${projectPrefix}/some/path`
 */
function extractSsmWrites(source: string): Set<string> {
  const results = new Set<string>();
  // Match parameterName with template literal containing projectPrefix
  const re = /parameterName:\s*`\/\$\{[^}]*(?:projectPrefix|config\.projectPrefix)[^}]*\}\/([\w\-/]+)`/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(source)) !== null) {
    results.add(match[1]);
  }
  return results;
}

/**
 * Extract SSM parameter path suffixes that a stack READS.
 *
 * Looks for patterns like:
 *   ssm.StringParameter.valueFromLookup(this, `/${config.projectPrefix}/some/path`)
 *   ssm.StringParameter.valueForStringParameter(this, `/${config.projectPrefix}/some/path`)
 *   ssm.StringParameter.valueFromLookup(this, `/${projectPrefix}/some/path`)
 *
 * Also catches multi-line variants where the path is on the next line.
 */
function extractSsmReads(source: string): Set<string> {
  const results = new Set<string>();
  // Pattern 1: valueFromLookup or valueForStringParameter with template literal
  const re1 = /(?:valueFromLookup|valueForStringParameter)\s*\(\s*(?:this|scope)\s*,\s*`\/\$\{[^}]*(?:projectPrefix|config\.projectPrefix)[^}]*\}\/([\w\-/]+)`/g;
  let match: RegExpExecArray | null;
  while ((match = re1.exec(source)) !== null) {
    results.add(match[1]);
  }

  // Pattern 2: parameterName passed to fromStringParameterName/fromStringParameterAttributes
  const re2 = /(?:fromStringParameterName|fromStringParameterAttributes)\s*\([^,]+,\s*[^,]*`\/\$\{[^}]*(?:projectPrefix|config\.projectPrefix)[^}]*\}\/([\w\-/]+)`/g;
  while ((match = re2.exec(source)) !== null) {
    results.add(match[1]);
  }

  // Pattern 3: SSM parameter paths used in SSM read calls via variable (secondary param arg as template literal)
  const re3 = /(?:valueFromLookup|valueForStringParameter)\s*\(\s*(?:this|scope)\s*,[\s\n]*`\/\$\{[^}]*(?:projectPrefix|config\.projectPrefix)[^}]*\}\/([\w\-/]+)`/gm;
  while ((match = re3.exec(source)) !== null) {
    results.add(match[1]);
  }

  return results;
}

/**
 * Read all stack source files and return maps of reads and writes.
 */
function analyseStacks(): {
  reads: Map<string, Set<string>>;
  writes: Map<string, Set<string>>;
} {
  const reads = new Map<string, Set<string>>();
  const writes = new Map<string, Set<string>>();

  for (const [file, label] of Object.entries(STACK_FILES)) {
    const filePath = path.join(LIB_DIR, file);
    const source = fs.readFileSync(filePath, 'utf-8');
    reads.set(label, extractSsmReads(source));
    writes.set(label, extractSsmWrites(source));
  }

  return { reads, writes };
}

/**
 * Build a dependency graph: an edge from A → B means stack A reads a
 * parameter that stack B writes.
 */
function buildDependencyGraph(
  reads: Map<string, Set<string>>,
  writes: Map<string, Set<string>>,
): Map<string, Set<string>> {
  const graph = new Map<string, Set<string>>();
  for (const stack of reads.keys()) {
    graph.set(stack, new Set());
  }

  // Build a reverse index: paramSuffix → writer stack
  const writerOf = new Map<string, string>();
  for (const [stack, params] of writes) {
    for (const p of params) {
      writerOf.set(p, stack);
    }
  }

  for (const [reader, params] of reads) {
    for (const p of params) {
      if (EXTERNAL_PARAMS.has(p)) continue;
      const writer = writerOf.get(p);
      if (writer && writer !== reader) {
        graph.get(reader)!.add(writer);
      }
    }
  }

  return graph;
}

/**
 * Detect cycles in a directed graph using iterative DFS.
 * Returns the first cycle found as an array of stack names, or null.
 */
function detectCycle(graph: Map<string, Set<string>>): string[] | null {
  const WHITE = 0, GRAY = 1, BLACK = 2;
  const color = new Map<string, number>();
  const parent = new Map<string, string | null>();

  for (const node of graph.keys()) {
    color.set(node, WHITE);
  }

  for (const startNode of graph.keys()) {
    if (color.get(startNode) !== WHITE) continue;

    const stack: string[] = [startNode];
    color.set(startNode, GRAY);
    parent.set(startNode, null);

    while (stack.length > 0) {
      const node = stack[stack.length - 1];
      const neighbors = Array.from(graph.get(node) ?? []);
      let pushed = false;

      for (const neighbor of neighbors) {
        if (color.get(neighbor) === GRAY) {
          // Found a cycle — reconstruct it
          const cycle = [neighbor, node];
          let cur = node;
          while (parent.get(cur) !== null && parent.get(cur) !== neighbor) {
            cur = parent.get(cur)!;
            cycle.push(cur);
          }
          cycle.push(neighbor);
          return cycle.reverse();
        }
        if (color.get(neighbor) === WHITE) {
          color.set(neighbor, GRAY);
          parent.set(neighbor, node);
          stack.push(neighbor);
          pushed = true;
          break;
        }
      }

      if (!pushed) {
        color.set(node, BLACK);
        stack.pop();
      }
    }
  }

  return null;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('Stack Dependency Order', () => {
  let reads: Map<string, Set<string>>;
  let writes: Map<string, Set<string>>;
  let graph: Map<string, Set<string>>;

  beforeAll(() => {
    const analysis = analyseStacks();
    reads = analysis.reads;
    writes = analysis.writes;
    graph = buildDependencyGraph(reads, writes);
  });

  // ── Cycle Detection ─────────────────────────────────────────────────

  test('dependency graph has no circular dependencies', () => {
    const cycle = detectCycle(graph);
    if (cycle) {
      const cycleStr = cycle.join(' → ');
      throw new Error(
        `Circular SSM dependency detected!\n\n` +
        `  ${cycleStr}\n\n` +
        `This means these stacks depend on each other via SSM parameters, ` +
        `making it impossible to deploy them in sequence on a fresh environment.\n` +
        `Review the SSM reads/writes in the involved stacks and remove the ` +
        `circular reference.`,
      );
    }
  });

  // ── Tier Validation ─────────────────────────────────────────────────

  test('no stack reads a parameter written by a same-tier or later-tier stack', () => {
    const violations: string[] = [];

    for (const [reader, dependencies] of graph) {
      const readerTier = DEPLOYMENT_TIERS[reader];
      if (readerTier === undefined) {
        violations.push(`Stack "${reader}" has no tier assignment in DEPLOYMENT_TIERS`);
        continue;
      }

      for (const writer of dependencies) {
        const writerTier = DEPLOYMENT_TIERS[writer];
        if (writerTier === undefined) {
          violations.push(`Stack "${writer}" (dependency of ${reader}) has no tier assignment`);
          continue;
        }

        if (writerTier >= readerTier) {
          // Find the specific params causing the violation
          const writerParams = writes.get(writer)!;
          const readerParams = reads.get(reader)!;
          const offending = [...readerParams].filter(
            (p) => writerParams.has(p) && !EXTERNAL_PARAMS.has(p),
          );

          violations.push(
            `${reader} (tier ${readerTier}) reads SSM params from ` +
            `${writer} (tier ${writerTier}): [${offending.join(', ')}]`,
          );
        }
      }
    }

    if (violations.length > 0) {
      throw new Error(
        `Deployment tier violations detected!\n\n` +
        violations.map((v) => `  • ${v}`).join('\n') +
        `\n\n` +
        `A stack may only read SSM parameters written by stacks in earlier tiers.\n` +
        `Current tier assignments:\n` +
        Object.entries(DEPLOYMENT_TIERS)
          .sort(([, a], [, b]) => a - b)
          .map(([s, t]) => `  Tier ${t}: ${s}`)
          .join('\n'),
      );
    }
  });

  // ── All Reads Satisfied ─────────────────────────────────────────────

  test('every SSM read is satisfied by a writer stack or is externally provided', () => {
    // Build a set of all params written by any stack
    const allWritten = new Set<string>();
    for (const params of writes.values()) {
      for (const p of params) {
        allWritten.add(p);
      }
    }

    const unsatisfied: string[] = [];

    for (const [stack, params] of reads) {
      for (const p of params) {
        if (EXTERNAL_PARAMS.has(p)) continue;
        if (!allWritten.has(p)) {
          unsatisfied.push(`${stack} reads "${p}" but no stack writes it`);
        }
      }
    }

    if (unsatisfied.length > 0) {
      throw new Error(
        `Unsatisfied SSM parameter reads:\n\n` +
        unsatisfied.map((u) => `  • ${u}`).join('\n') +
        `\n\nEither add the parameter to the appropriate stack, add it to ` +
        `EXTERNAL_PARAMS, or remove the read.`,
      );
    }
  });

  // ── Tier Completeness ───────────────────────────────────────────────

  test('all stacks have a deployment tier assignment', () => {
    const missing = Object.values(STACK_FILES).filter(
      (label) => DEPLOYMENT_TIERS[label] === undefined,
    );
    expect(missing).toEqual([]);
  });

  test('all stack files in lib/ are accounted for', () => {
    const libFiles = fs.readdirSync(LIB_DIR)
      .filter((f) => f.endsWith('-stack.ts'));
    const tracked = new Set(Object.keys(STACK_FILES));
    const untracked = libFiles.filter((f) => !tracked.has(f));

    if (untracked.length > 0) {
      throw new Error(
        `New stack files found that are not tracked in STACK_FILES:\n` +
        untracked.map((f) => `  • ${f}`).join('\n') +
        `\nAdd them to STACK_FILES and DEPLOYMENT_TIERS in stack-dependencies.test.ts`,
      );
    }
  });

  // ── Extraction Sanity Checks ────────────────────────────────────────

  test('InfrastructureStack writes at least 44 SSM params', () => {
    const infraWrites = writes.get('InfrastructureStack')!;
    expect(infraWrites.size).toBeGreaterThanOrEqual(44);
  });

  test('InfrastructureStack reads zero SSM params from other stacks', () => {
    const infraReads = reads.get('InfrastructureStack')!;
    expect(infraReads.size).toBe(0);
  });

  test('AppApiStack reads from InferenceApiStack', () => {
    const appReads = reads.get('AppApiStack')!;
    expect(appReads.has('inference-api/memory-id')).toBe(true);
  });

  test('AppApiStack reads from RagIngestionStack', () => {
    const appReads = reads.get('AppApiStack')!;
    expect(appReads.has('rag/documents-bucket-name')).toBe(true);
    expect(appReads.has('rag/assistants-table-name')).toBe(true);
  });

  test('InferenceApiStack reads from RagIngestionStack', () => {
    const inferenceReads = reads.get('InferenceApiStack')!;
    expect(inferenceReads.has('rag/assistants-table-arn')).toBe(true);
    expect(inferenceReads.has('rag/vector-bucket-name')).toBe(true);
  });

  test('InferenceApiStack reads file-upload params from InfrastructureStack', () => {
    const inferenceReads = reads.get('InferenceApiStack')!;
    expect(inferenceReads.has('user-file-uploads/table-arn')).toBe(true);
    expect(inferenceReads.has('user-file-uploads/bucket-arn')).toBe(true);
  });

  test('AppApiStack reads file-upload params from InfrastructureStack', () => {
    const appReads = reads.get('AppApiStack')!;
    expect(appReads.has('user-file-uploads/bucket-name')).toBe(true);
    expect(appReads.has('user-file-uploads/bucket-arn')).toBe(true);
    expect(appReads.has('user-file-uploads/table-name')).toBe(true);
    expect(appReads.has('user-file-uploads/table-arn')).toBe(true);
  });

  test('GatewayStack reads zero SSM params', () => {
    const gatewayReads = reads.get('GatewayStack')!;
    expect(gatewayReads.size).toBe(0);
  });

  test('SageMakerFineTuningStack reads only from InfrastructureStack (network params)', () => {
    const ftReads = reads.get('SageMakerFineTuningStack')!;
    for (const param of ftReads) {
      expect(param).toMatch(/^network\//);
    }
  });

  test('SageMakerFineTuningStack writes fine-tuning SSM params', () => {
    const ftWrites = writes.get('SageMakerFineTuningStack')!;
    expect(ftWrites.size).toBeGreaterThanOrEqual(8);
    expect(ftWrites.has('fine-tuning/jobs-table-name')).toBe(true);
    expect(ftWrites.has('fine-tuning/jobs-table-arn')).toBe(true);
    expect(ftWrites.has('fine-tuning/access-table-name')).toBe(true);
    expect(ftWrites.has('fine-tuning/access-table-arn')).toBe(true);
    expect(ftWrites.has('fine-tuning/data-bucket-name')).toBe(true);
    expect(ftWrites.has('fine-tuning/data-bucket-arn')).toBe(true);
    expect(ftWrites.has('fine-tuning/sagemaker-execution-role-arn')).toBe(true);
    expect(ftWrites.has('fine-tuning/sagemaker-security-group-id')).toBe(true);
    expect(ftWrites.has('fine-tuning/private-subnet-ids')).toBe(true);
  });

  test('AppApiStack reads fine-tuning SSM params from SageMakerFineTuningStack', () => {
    const appReads = reads.get('AppApiStack')!;
    expect(appReads.has('fine-tuning/jobs-table-name')).toBe(true);
    expect(appReads.has('fine-tuning/sagemaker-execution-role-arn')).toBe(true);
    expect(appReads.has('fine-tuning/data-bucket-name')).toBe(true);
  });

  // ── Diagnostic: Print the dependency graph ──────────────────────────

  test('prints dependency graph for debugging', () => {
    const lines: string[] = ['Dependency graph (reader → [writers]):'];
    for (const [reader, writers] of graph) {
      const tier = DEPLOYMENT_TIERS[reader];
      if (writers.size === 0) {
        lines.push(`  Tier ${tier} | ${reader} → (none)`);
      } else {
        lines.push(`  Tier ${tier} | ${reader} → ${[...writers].join(', ')}`);
      }
    }
    console.log(lines.join('\n'));
    // This test always passes — it's purely informational
    expect(true).toBe(true);
  });
});
