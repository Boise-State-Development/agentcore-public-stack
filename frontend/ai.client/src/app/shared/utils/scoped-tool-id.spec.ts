import { describe, it, expect } from 'vitest';
import {
  SCOPE_DELIMITER,
  baseToolId,
  isScopedToolId,
  makeScopedToolId,
  parseScopedToolId,
} from './scoped-tool-id';

describe('scoped-tool-id', () => {
  it('detects scoped ids', () => {
    expect(isScopedToolId('gmail::send')).toBe(true);
    expect(isScopedToolId('gmail')).toBe(false);
  });

  it('round-trips make/parse', () => {
    const id = makeScopedToolId('gateway_class_search', 'search');
    expect(id).toBe(`gateway_class_search${SCOPE_DELIMITER}search`);
    expect(parseScopedToolId(id)).toEqual({ base: 'gateway_class_search', name: 'search' });
  });

  it('parses a bare id', () => {
    expect(parseScopedToolId('fetch_url_content')).toEqual({
      base: 'fetch_url_content',
      name: null,
    });
  });

  it('treats an empty name as a bare id', () => {
    expect(parseScopedToolId('server::  ')).toEqual({ base: 'server', name: null });
  });

  it('only splits on the first delimiter', () => {
    expect(parseScopedToolId('server::a::b')).toEqual({ base: 'server', name: 'a::b' });
  });

  it('returns the base id', () => {
    expect(baseToolId('server::tool')).toBe('server');
    expect(baseToolId('server')).toBe('server');
  });
});
