import { buildAppApiEnvironment, AppApiSsmParams } from '../lib/constructs/app-api/app-api-environment';
import { createMockConfig } from './helpers/mock-config';

/**
 * Guards the Memory Spaces env wiring on app-api. The service reads the table
 * and bucket names from `DYNAMODB_MEMORY_SPACES_TABLE_NAME` /
 * `S3_MEMORY_SPACES_BUCKET_NAME`; without them app-api falls back to the
 * default "memory-spaces" name and every read 502s (ResourceNotFoundException).
 * inference-api already sets the identical trio — app-api owns the CRUD
 * surface, so it must too.
 */
function stubParams(overrides: Partial<AppApiSsmParams> = {}): AppApiSsmParams {
  // Only the fields the env builder reads need real values; the rest are
  // filled with placeholders so the type is satisfied.
  return {
    memorySpacesTableName: 'test-project-memory-spaces',
    memorySpacesBucketName: 'test-project-memory-spaces',
    ...overrides,
  } as AppApiSsmParams;
}

describe('buildAppApiEnvironment — Memory Spaces', () => {  it('wires the table and bucket names the service reads', () => {
    const env = buildAppApiEnvironment(createMockConfig(), stubParams());

    expect(env.DYNAMODB_MEMORY_SPACES_TABLE_NAME).toBe('test-project-memory-spaces');
    expect(env.S3_MEMORY_SPACES_BUCKET_NAME).toBe('test-project-memory-spaces');
  });

  it('gates only feature existence on the flag, always wiring the names', () => {
    const off = buildAppApiEnvironment(createMockConfig(), stubParams());
    expect(off.MEMORY_SPACES_ENABLED).toBe('false');
    // Names are present even with the kill switch off — the service reads them
    // lazily, so a later flip to enabled needs no env change.
    expect(off.DYNAMODB_MEMORY_SPACES_TABLE_NAME).toBe('test-project-memory-spaces');

    const on = buildAppApiEnvironment(
      createMockConfig({ memorySpaces: { enabled: true } }),
      stubParams(),
    );
    expect(on.MEMORY_SPACES_ENABLED).toBe('true');
  });

  it('threads config.agents.enabled into the AGENTS_API_ENABLED env var', () => {
    // The mock config sets agents.enabled=false explicitly; the loadConfig default
    // is ON with a kill switch (covered in config.test.ts).
    const off = buildAppApiEnvironment(createMockConfig(), stubParams());
    expect(off.AGENTS_API_ENABLED).toBe('false');

    const on = buildAppApiEnvironment(
      createMockConfig({ agents: { enabled: true } }),
      stubParams(),
    );
    expect(on.AGENTS_API_ENABLED).toBe('true');
  });
});

/**
 * Guards the owner-facing upgrade offer's flag wiring.
 *
 * `apis/app_api/kb_upgrade/service.py` reads `MANAGED_KB_MIGRATION_ENABLED` from
 * THIS task's environment to decide whether to offer the upgrade at all. The
 * migration Lambdas get their own copy from `kb-migration-construct.ts`; app-api
 * was originally missed, and the failure mode is the reason this test exists:
 * the card renders `phase: "none"` for every user, forever, no matter what the
 * environment's flag is set to. It deploys clean, logs nothing, and the feature
 * is simply unreachable — the same shape as the unregistered-backend and
 * no-enrolment-surface defects before it.
 */
describe('buildAppApiEnvironment — managed KB upgrade offer', () => {
  /** The helper's own documented pattern: start from off, opt in explicitly. */
  const managedKbDefaults = () => createMockConfig().managedKb;

  it('threads config.managedKb.migrationEnabled into the env the service reads', () => {
    const on = buildAppApiEnvironment(
      createMockConfig({ managedKb: { ...managedKbDefaults(), migrationEnabled: true } }),
      stubParams(),
    );
    expect(on.MANAGED_KB_MIGRATION_ENABLED).toBe('true');
  });

  it("ships 'false' explicitly rather than omitting the variable", () => {
    // Absent and 'false' behave identically in the service (it uses an
    // allow-list of affirmative spellings), but an explicit value makes the
    // shipped state readable in the task definition rather than inferred from
    // an absence that could equally mean "someone forgot".
    const off = buildAppApiEnvironment(createMockConfig(), stubParams());
    expect(off.MANAGED_KB_MIGRATION_ENABLED).toBe('false');
    expect(Object.keys(off)).toContain('MANAGED_KB_MIGRATION_ENABLED');
  });

  it('uses the same flag as the migration worker, not a second one', () => {
    // Two independent flags would let the offer and the capability disagree —
    // a user could enrol a knowledge base that nothing will ever migrate.
    const env = buildAppApiEnvironment(
      createMockConfig({ managedKb: { ...managedKbDefaults(), migrationEnabled: true } }),
      stubParams(),
    );
    const strays = Object.keys(env).filter(
      (key) => key.startsWith('MANAGED_KB_') && key.includes('OFFER'),
    );
    expect(strays).toEqual([]);
  });
});
