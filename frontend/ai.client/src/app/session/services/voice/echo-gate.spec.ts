import { describe, it, expect, beforeEach } from 'vitest';
import { EchoGate } from './echo-gate';
import { pcm16LevelDb } from './pcm-utils';

/** A 100 ms chunk of a constant sample, about `levelDb` dBFS loud. */
function chunkAt(levelDb: number): Int16Array {
  const amplitude = Math.round(Math.pow(10, levelDb / 20) * 0x8000);
  return new Int16Array(1600).fill(amplitude);
}

const isSilent = (pcm: Int16Array) => pcm.every(s => s === 0);

describe('pcm16LevelDb', () => {
  it('measures RMS in dBFS', () => {
    expect(pcm16LevelDb(chunkAt(-20))).toBeCloseTo(-20, 1);
    expect(pcm16LevelDb(chunkAt(-6))).toBeCloseTo(-6, 1);
  });

  it('reads digital silence and an empty chunk as -Infinity', () => {
    expect(pcm16LevelDb(new Int16Array(1600))).toBe(-Infinity);
    expect(pcm16LevelDb(new Int16Array(0))).toBe(-Infinity);
  });
});

describe('EchoGate (#2019)', () => {
  let gate: EchoGate;

  beforeEach(() => {
    gate = new EchoGate({ thresholdDb: -26, openChunks: 2, preRollChunks: 3, tailMs: 400 });
  });

  it('passes mic audio through while the agent is silent', () => {
    const pcm = chunkAt(-40);
    const result = gate.process(pcm, false, 0);
    expect(result.send).toEqual([pcm]);
    expect(result.bargeIn).toBe(false);
  });

  it('replaces quiet input with same-length silence while the agent plays', () => {
    const result = gate.process(chunkAt(-35), true, 0);
    expect(result.send).toHaveLength(1);
    expect(result.send[0]).toHaveLength(1600);
    expect(isSilent(result.send[0])).toBe(true);
    expect(result.bargeIn).toBe(false);
  });

  it('keeps gating for the tail after playback ends', () => {
    gate.process(chunkAt(-35), true, 0);
    expect(isSilent(gate.process(chunkAt(-35), false, 399).send[0])).toBe(true);

    const pcm = chunkAt(-35);
    expect(gate.process(pcm, false, 400).send).toEqual([pcm]);
  });

  it('does not open on one loud chunk', () => {
    const result = gate.process(chunkAt(-12), true, 0);
    expect(result.bargeIn).toBe(false);
    expect(isSilent(result.send[0])).toBe(true);
  });

  it('needs the loud chunks in a row', () => {
    gate.process(chunkAt(-12), true, 0);
    gate.process(chunkAt(-40), true, 100);
    expect(gate.process(chunkAt(-12), true, 200).bargeIn).toBe(false);
  });

  it('opens on sustained loud speech and releases the pre-roll in order', () => {
    const quiet = chunkAt(-40);
    const loud1 = chunkAt(-12);
    const loud2 = chunkAt(-11);
    gate.process(chunkAt(-45), true, 0); // falls out of the 3-chunk pre-roll
    gate.process(quiet, true, 100);
    gate.process(loud1, true, 200);
    const result = gate.process(loud2, true, 300);

    expect(result.bargeIn).toBe(true);
    expect(result.send).toEqual([quiet, loud1, loud2]);
  });

  it('passes audio straight through after a barge-in, without the tail', () => {
    gate.process(chunkAt(-12), true, 0);
    gate.process(chunkAt(-12), true, 100);

    // The caller has stopped playback; the user keeps talking.
    const pcm = chunkAt(-30);
    expect(gate.process(pcm, false, 200).send).toEqual([pcm]);
  });

  it('discards the pre-roll once the agent stops on its own', () => {
    gate.process(chunkAt(-12), true, 0);
    gate.process(chunkAt(-40), false, 1000);

    // A single loud chunk later must not combine with the stale one.
    expect(gate.process(chunkAt(-12), true, 1100).bargeIn).toBe(false);
  });

  it('uses a new threshold right away', () => {
    gate.setThresholdDb(-10);
    gate.process(chunkAt(-12), true, 0);
    expect(gate.process(chunkAt(-12), true, 100).bargeIn).toBe(false);

    gate.setThresholdDb(-30);
    gate.process(chunkAt(-12), true, 200);
    expect(gate.process(chunkAt(-12), true, 300).bargeIn).toBe(true);
  });

  it('reports each chunk level for the meter', () => {
    expect(gate.process(chunkAt(-20), false, 0).levelDb).toBeCloseTo(-20, 1);
  });

  it('forgets playback on reset', () => {
    gate.process(chunkAt(-35), true, 0);
    gate.reset();
    const pcm = chunkAt(-35);
    expect(gate.process(pcm, false, 100).send).toEqual([pcm]);
  });

  it('keeps at least the opening chunks as pre-roll', () => {
    const small = new EchoGate({ thresholdDb: -26, openChunks: 3, preRollChunks: 1, tailMs: 0 });
    small.process(chunkAt(-12), true, 0);
    small.process(chunkAt(-12), true, 100);
    expect(small.process(chunkAt(-12), true, 200).send).toHaveLength(3);
  });
});
