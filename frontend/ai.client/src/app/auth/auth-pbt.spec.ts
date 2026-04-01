import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';

// Shared fast-check arbitraries for auth RBAC property-based tests
// Feature: auth-rbac-tests

const arbRoleName = fc.string({
  unit: fc.constantFrom(...'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-'.split('')),
  minLength: 1,
  maxLength: 30,
});

const arbRoleList = fc.array(arbRoleName, { maxLength: 10 });

describe('Auth RBAC PBT - Shared Arbitraries', () => {
  // Feature: auth-rbac-tests, Property smoke: arbRoleName generates valid role names
  it('arbRoleName generates non-empty strings with valid characters', () => {
    fc.assert(
      fc.property(arbRoleName, (role) => {
        expect(role.length).toBeGreaterThanOrEqual(1);
        expect(role.length).toBeLessThanOrEqual(30);
        expect(role).toMatch(/^[a-zA-Z0-9_-]+$/);
      }),
      { numRuns: 100 }
    );
  });

  // Feature: auth-rbac-tests, Property smoke: arbRoleList generates valid role arrays
  // Validates: Requirements 15.1
  it('arbRoleList generates arrays of valid role names', () => {
    fc.assert(
      fc.property(arbRoleList, (roles) => {
        expect(roles.length).toBeLessThanOrEqual(10);
        for (const role of roles) {
          expect(role.length).toBeGreaterThanOrEqual(1);
          expect(role.length).toBeLessThanOrEqual(30);
          expect(role).toMatch(/^[a-zA-Z0-9_-]+$/);
        }
      }),
      { numRuns: 100 }
    );
  });
});

export { arbRoleName, arbRoleList };
