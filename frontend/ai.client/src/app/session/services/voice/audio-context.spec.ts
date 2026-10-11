import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAudioContext, describeAudioError } from './audio-context';

describe('createAudioContext', () => {
  const original = window.AudioContext;

  afterEach(() => {
    window.AudioContext = original;
  });

  function stubAudioContext(refuseRate: (options?: AudioContextOptions) => Error | null) {
    const calls: (AudioContextOptions | undefined)[] = [];
    window.AudioContext = vi.fn(function (this: object, options?: AudioContextOptions) {
      calls.push(options);
      const err = refuseRate(options);
      if (err) throw err;
      Object.assign(this, { sampleRate: options?.sampleRate ?? 48000 });
    }) as unknown as typeof AudioContext;
    return calls;
  }

  it('asks for the requested rate first', () => {
    const calls = stubAudioContext(() => null);

    const ctx = createAudioContext(16000);

    expect(calls).toEqual([{ sampleRate: 16000 }]);
    expect(ctx.sampleRate).toBe(16000);
  });

  it('falls back to the native rate when the browser refuses the requested one', () => {
    const calls = stubAudioContext((options) =>
      options?.sampleRate ? new DOMException('rate not supported', 'NotSupportedError') : null,
    );

    const ctx = createAudioContext(16000);

    expect(calls).toEqual([{ sampleRate: 16000 }, undefined]);
    expect(ctx.sampleRate).toBe(48000);
  });

  it('does not swallow other failures', () => {
    stubAudioContext(() => new DOMException('blocked', 'NotAllowedError'));

    expect(() => createAudioContext(16000)).toThrow('blocked');
  });
});

describe('describeAudioError', () => {
  it('leads with the DOMException name', () => {
    const err = new DOMException('Requested device not found', 'OverconstrainedError');

    expect(describeAudioError(err, 'fallback')).toBe('OverconstrainedError: Requested device not found');
  });

  it('uses the name alone when the browser gives no message', () => {
    expect(describeAudioError(new DOMException('', 'NotReadableError'), 'fallback')).toBe('NotReadableError');
  });

  it('uses a plain error message', () => {
    expect(describeAudioError(new Error('Voice ticket expired'), 'fallback')).toBe('Voice ticket expired');
  });

  it('falls back for anything else', () => {
    expect(describeAudioError('nope', 'Failed to start voice')).toBe('Failed to start voice');
  });
});
