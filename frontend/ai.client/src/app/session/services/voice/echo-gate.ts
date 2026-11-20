import { pcm16LevelDb } from './pcm-utils';

export interface EchoGateOptions {
  /** Level in dBFS a chunk must reach to count toward an interruption. */
  thresholdDb: number;
  /** Consecutive loud chunks needed to open the gate. */
  openChunks: number;
  /** Chunks kept while gated and sent when the gate opens (at least `openChunks`). */
  preRollChunks: number;
  /** How long the agent still counts as audible after playback ends. */
  tailMs: number;
}

export interface EchoGateResult {
  /** Chunks to send to the server, in order. */
  send: Int16Array[];
  /** True when this chunk opened the gate: the user is talking over the agent. */
  bargeIn: boolean;
  /** Level of this chunk in dBFS, for the mic meter. */
  levelDb: number;
}

/**
 * Keeps the agent's own voice, picked up by the microphone, from reaching
 * Nova as user speech (#2019).
 *
 * While the agent is audible (playing, or within `tailMs` after), each mic
 * chunk is replaced by silence of the same length, so Nova's input stream
 * stays continuous. Recent chunks are kept as pre-roll. When `openChunks`
 * chunks in a row reach `thresholdDb`, the gate reports a barge-in and
 * releases the pre-roll, so the start of the interruption is not lost.
 *
 * Pure apart from its own state: the caller passes in whether the agent is
 * playing and the current time, which keeps it testable without Web Audio.
 */
export class EchoGate {
  private thresholdDb: number;
  private readonly openChunks: number;
  private readonly preRollChunks: number;
  private readonly tailMs: number;

  private lastAudibleAt = -Infinity;
  private loudChunks = 0;
  private preRoll: Int16Array[] = [];

  constructor(options: EchoGateOptions) {
    this.thresholdDb = options.thresholdDb;
    this.openChunks = Math.max(1, options.openChunks);
    this.preRollChunks = Math.max(this.openChunks, options.preRollChunks);
    this.tailMs = Math.max(0, options.tailMs);
  }

  setThresholdDb(thresholdDb: number): void {
    this.thresholdDb = thresholdDb;
  }

  /** Forget playback history and buffered audio (new session). */
  reset(): void {
    this.lastAudibleAt = -Infinity;
    this.loudChunks = 0;
    this.preRoll = [];
  }

  process(pcm: Int16Array, agentPlaying: boolean, nowMs: number): EchoGateResult {
    const levelDb = pcm16LevelDb(pcm);
    if (agentPlaying) this.lastAudibleAt = nowMs;

    const agentAudible = agentPlaying || nowMs - this.lastAudibleAt < this.tailMs;
    if (!agentAudible) {
      // Anything buffered was recorded over the agent's voice: drop it.
      this.loudChunks = 0;
      this.preRoll = [];
      return { send: [pcm], bargeIn: false, levelDb };
    }

    this.preRoll.push(pcm);
    if (this.preRoll.length > this.preRollChunks) this.preRoll.shift();

    this.loudChunks = levelDb >= this.thresholdDb ? this.loudChunks + 1 : 0;
    if (this.loudChunks >= this.openChunks) {
      const send = this.preRoll;
      this.preRoll = [];
      this.loudChunks = 0;
      // The caller stops playback now; the tail must not close the gate again.
      this.lastAudibleAt = -Infinity;
      return { send, bargeIn: true, levelDb };
    }

    return { send: [new Int16Array(pcm.length)], bargeIn: false, levelDb };
  }
}
