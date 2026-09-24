import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DictationChimeService } from './dictation-chime.service';

/** Just enough of an AudioContext to record which notes were scheduled. */
class FakeAudioContext {
  static instances = 0;
  state: AudioContextState = 'suspended';
  currentTime = 0;
  readonly destination = {};
  readonly frequencies: number[] = [];
  readonly resume = vi.fn(async () => {
    this.state = 'running';
  });

  constructor() {
    FakeAudioContext.instances++;
  }

  createOscillator() {
    const oscillator = {
      type: 'sine',
      frequency: { value: 0 },
      connect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
    };
    // Read the frequency at start(), after the service has set it.
    oscillator.start = vi.fn(() => this.frequencies.push(oscillator.frequency.value));
    return oscillator;
  }

  createGain() {
    return {
      gain: { setValueAtTime: vi.fn(), exponentialRampToValueAtTime: vi.fn() },
      connect: vi.fn(),
    };
  }
}

describe('DictationChimeService', () => {
  const original = (window as { AudioContext?: unknown }).AudioContext;

  beforeEach(() => {
    FakeAudioContext.instances = 0;
    TestBed.configureTestingModule({});
  });

  afterEach(() => {
    (window as { AudioContext?: unknown }).AudioContext = original;
  });

  it('is a silent no-op where there is no Web Audio', () => {
    (window as { AudioContext?: unknown }).AudioContext = undefined;
    const service = TestBed.inject(DictationChimeService);
    expect(() => {
      service.prime();
      service.play('start');
    }).not.toThrow();
  });

  it('creates one context on prime and resumes it, then reuses it', () => {
    (window as { AudioContext?: unknown }).AudioContext = FakeAudioContext;
    const service = TestBed.inject(DictationChimeService);
    service.prime();
    service.play('start');
    service.play('stop');
    expect(FakeAudioContext.instances).toBe(1);
  });

  it('rises for start and falls for stop', () => {
    let context: FakeAudioContext | null = null;
    (window as { AudioContext?: unknown }).AudioContext = class extends FakeAudioContext {
      constructor() {
        super();
        context = this;
      }
    };
    const service = TestBed.inject(DictationChimeService);

    service.play('start');
    const fundamentals = (notes: number[]) => notes.filter((_, i) => i % 2 === 0);
    const [firstUp, secondUp] = fundamentals(context!.frequencies);
    expect(secondUp).toBeGreaterThan(firstUp);

    context!.frequencies.length = 0;
    service.play('stop');
    const [firstDown, secondDown] = fundamentals(context!.frequencies);
    expect(secondDown).toBeLessThan(firstDown);
  });
});
