/**
 * Display helpers for audit records.
 *
 * These are small, but they are the difference between a readable trail and a
 * wall of raw ids — and the fallbacks matter: a record whose action or field the
 * client does not recognize must still render, because the server's closed set
 * can gain an entry before the SPA ships.
 */
import { describe, it, expect } from 'vitest';

import { actionLabel, fieldLabel, formatValue } from './audit.model';

describe('actionLabel', () => {
  it('names the known actions', () => {
    expect(actionLabel('app_role.created')).toBe('Role created');
    expect(actionLabel('app_role.mutation_denied')).toBe('Change denied');
  });

  it('falls back to the raw id for an action the client has not shipped yet', () => {
    expect(actionLabel('app_role.something_new')).toBe('app_role.something_new');
  });
});

describe('fieldLabel', () => {
  it('uses the wording the role form uses', () => {
    expect(fieldLabel('granted_admin_scopes')).toBe('Admin access');
    expect(fieldLabel('granted_tools')).toBe('Tools');
  });

  it('falls back to the raw field name', () => {
    expect(fieldLabel('some_new_field')).toBe('some_new_field');
  });
});

describe('formatValue', () => {
  it('renders an empty list as (none) rather than blank', () => {
    // A blank cell next to an arrow reads as "unknown"; the point of the diff
    // is that the admin can see a grant list went from nothing to something.
    expect(formatValue([])).toBe('(none)');
  });

  it('joins a populated list', () => {
    expect(formatValue(['admin.costs', 'admin.users'])).toBe(
      'admin.costs, admin.users'
    );
  });

  it('renders booleans as words', () => {
    expect(formatValue(true)).toBe('Yes');
    expect(formatValue(false)).toBe('No');
  });

  it('distinguishes an empty string from a missing value', () => {
    expect(formatValue('')).toBe('(empty)');
    expect(formatValue(null)).toBe('—');
    expect(formatValue(undefined)).toBe('—');
  });

  it('passes numbers and strings through', () => {
    expect(formatValue(5)).toBe('5');
    expect(formatValue('Analyst')).toBe('Analyst');
  });
});
