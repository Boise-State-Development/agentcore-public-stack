import { describe, expect, it } from 'vitest';
import { isChunkLoadError } from './chunk-load-error';

const CHUNK_URL = 'https://boisestate.ai/chunk-4DOJ3VBG.js';

describe('isChunkLoadError', () => {
  describe('browser wording', () => {
    it.each([
      ['Chromium', new TypeError(`Failed to fetch dynamically imported module: ${CHUNK_URL}`)],
      ['Safari', new TypeError('Importing a module script failed.')],
      ['Firefox', new TypeError(`error loading dynamically imported module: ${CHUNK_URL}`)],
      ['webpack JS chunk', new Error('Loading chunk 123 failed.\n(error: /chunk-123.js)')],
      ['webpack named chunk', new Error('Loading chunk settings-page failed.')],
      ['webpack CSS chunk', new Error('Loading CSS chunk 7 failed.')],
      [
        'HTML served for a module',
        new TypeError(
          'Failed to load module script: Expected a JavaScript module script but the server responded with a MIME type of "text/html".',
        ),
      ],
      [
        'HTML served for a module (wasm wording)',
        new TypeError(
          'Failed to load module script: Expected a JavaScript-or-Wasm module script but the server responded with a MIME type of "text/html".',
        ),
      ],
    ])('matches %s', (_engine, error) => {
      expect(isChunkLoadError(error)).toBe(true);
    });

    it('matches by name alone for a ChunkLoadError', () => {
      const error = new Error('something unrelated');
      error.name = 'ChunkLoadError';
      expect(isChunkLoadError(error)).toBe(true);
    });

    it('is case-insensitive', () => {
      expect(isChunkLoadError(new Error('FAILED TO FETCH DYNAMICALLY IMPORTED MODULE'))).toBe(true);
    });

    it('matches a bare message string', () => {
      expect(isChunkLoadError('Importing a module script failed.')).toBe(true);
    });
  });

  describe('wrapped errors', () => {
    const inner = new TypeError('Importing a module script failed.');

    it('follows `cause`', () => {
      expect(isChunkLoadError(new Error('Navigation failed', { cause: inner }))).toBe(true);
    });

    it("follows zone.js's `rejection`", () => {
      expect(isChunkLoadError({ message: 'Uncaught (in promise)', rejection: inner })).toBe(true);
    });

    it('follows `error`', () => {
      expect(isChunkLoadError({ error: inner })).toBe(true);
    });

    it('stops on a self-referencing wrapper', () => {
      const loop: { cause?: unknown } = {};
      loop.cause = loop;
      expect(isChunkLoadError(loop)).toBe(false);
    });
  });

  describe('everything else', () => {
    it.each([
      ['a plain network failure', new TypeError('Failed to fetch')],
      ['an HTTP 404 from app-api', new Error('Http failure response for /api/auth/api-keys: 404 Not Found')],
      ['a render error', new TypeError("Cannot read properties of undefined (reading 'id')")],
      ['a syntax error in a loaded module', new SyntaxError('Unexpected token <')],
      ['null', null],
      ['undefined', undefined],
      ['a number', 404],
      ['an empty object', {}],
    ])('does not match %s', (_label, error) => {
      expect(isChunkLoadError(error)).toBe(false);
    });
  });
});
