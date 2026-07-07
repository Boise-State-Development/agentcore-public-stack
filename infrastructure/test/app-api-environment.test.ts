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

describe('buildAppApiEnvironment — Memory Spaces', () => {
  it('wires the table and bucket names the service reads', () => {
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
});
