import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';

import { VoiceChatService } from './voice-chat.service';
import { AudioRecorderService } from './audio-recorder.service';
import { AudioPlayerService } from './audio-player.service';
import { VoiceTicketService } from './voice-ticket.service';
import { ConfigService } from '../../../services/config.service';
import { UserSettingsService } from '../../../services/user-settings.service';
import { IncognitoChatService } from '../incognito';
import { ToastService } from '../../../services/toast/toast.service';

/** A WebSocket that opens on the next tick and records what was sent. */
class FakeWebSocket {
  static readonly OPEN = 1;
  static last: FakeWebSocket | null = null;
  readyState = 0;
  sent: Record<string, unknown>[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: MessageEvent) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: ((e: CloseEvent) => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.last = this;
    setTimeout(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    }, 0);
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data));
  }

  close(): void {
    this.readyState = 3;
  }

  /** Deliver a server frame. */
  receive(frame: Record<string, unknown>): void {
    this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent);
  }
}

describe('VoiceChatService (#625)', () => {
  let service: VoiceChatService;
  const toast = { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() };
  const recorder: {
    start: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
    onAudioChunk: ((pcm: Int16Array, sampleRate: number) => void) | null;
  } = {
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn().mockResolvedValue(undefined),
    onAudioChunk: null,
  };
  const playing = signal(false);
  const player = {
    isPlaying: playing.asReadonly(),
    play: vi.fn(() => playing.set(true)),
    clear: vi.fn(() => playing.set(false)),
  };
  const originalWebSocket = globalThis.WebSocket;

  beforeEach(() => {
    vi.clearAllMocks();
    playing.set(false);
    localStorage.removeItem('voice-interrupt-level-db');
    FakeWebSocket.last = null;
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AudioRecorderService, useValue: recorder },
        { provide: AudioPlayerService, useValue: player },
        {
          provide: VoiceTicketService,
          useValue: { issue: vi.fn().mockResolvedValue({ ticket: 't' }) },
        },
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        {
          provide: UserSettingsService,
          useValue: { settingsResource: { value: signal(undefined) }, updateSettings: vi.fn() },
        },
        { provide: IncognitoChatService, useValue: { claim: () => false } },
        { provide: ToastService, useValue: toast },
      ],
    });
    service = TestBed.inject(VoiceChatService);
  });

  afterEach(() => {
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = originalWebSocket;
    TestBed.resetTestingModule();
  });

  it("sends the conversation's agent in the config frame", async () => {
    await service.connect('sess-1', 'ast-1');
    expect(FakeWebSocket.last!.sent[0]).toEqual(
      expect.objectContaining({ type: 'config', session_id: 'sess-1', agent_id: 'ast-1' }),
    );
  });

  it('leaves the agent out of a plain voice chat', async () => {
    await service.connect('sess-1');
    expect(FakeWebSocket.last!.sent[0]).not.toHaveProperty('agent_id');
  });

  it('leaves voice mode and says why when voice is at capacity', async () => {
    await service.connect('sess-1');
    expect(service.isVoiceActive()).toBe(true);

    FakeWebSocket.last!.receive({
      type: 'bidi_error',
      code: 'voice_capacity',
      message: "Voice mode isn't available right now because too many people are using it.",
    });

    expect(toast.error).toHaveBeenCalledWith(
      'Voice mode is busy',
      "Voice mode isn't available right now because too many people are using it.",
    );
    expect(service.isVoiceActive()).toBe(false);
    expect(service.isConnected()).toBe(false);
  });

  it('shows the message when the agent cannot run in voice', async () => {
    await service.connect('sess-1', 'ast-1');
    FakeWebSocket.last!.receive({
      type: 'bidi_error',
      code: 'agent_voice_unavailable',
      message: "This agent isn't available in voice mode.",
    });
    expect(toast.error).toHaveBeenCalledWith(
      'Voice mode unavailable',
      "This agent isn't available in voice mode.",
    );
    expect(service.isVoiceActive()).toBe(false);
  });

  it('keeps an uncoded error in the console only', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    await service.connect('sess-1');
    FakeWebSocket.last!.receive({ type: 'bidi_error', message: 'transient' });
    expect(toast.error).not.toHaveBeenCalled();
    expect(service.isVoiceActive()).toBe(true);
    consoleError.mockRestore();
  });
});

/** A 100 ms chunk of a constant sample, about `levelDb` dBFS loud. */
function chunkAt(levelDb: number): Int16Array {
  const amplitude = Math.round(Math.pow(10, levelDb / 20) * 0x8000);
  return new Int16Array(1600).fill(amplitude);
}

/** Decode the PCM of a `bidi_audio_input` frame. */
function sentPcm(frame: Record<string, unknown>): Int16Array {
  const binary = atob(frame['audio'] as string);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Int16Array(bytes.buffer);
}

describe('VoiceChatService echo gate (#2019)', () => {
  let service: VoiceChatService;
  const recorder: {
    start: ReturnType<typeof vi.fn>;
    stop: ReturnType<typeof vi.fn>;
    onAudioChunk: ((pcm: Int16Array, sampleRate: number) => void) | null;
  } = {
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn().mockResolvedValue(undefined),
    onAudioChunk: null,
  };
  const playing = signal(false);
  const player = {
    isPlaying: playing.asReadonly(),
    play: vi.fn(() => playing.set(true)),
    clear: vi.fn(() => playing.set(false)),
  };
  const originalWebSocket = globalThis.WebSocket;

  function audioFrames(): Record<string, unknown>[] {
    return FakeWebSocket.last!.sent.filter(f => f['type'] === 'bidi_audio_input');
  }

  function setup(): void {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AudioRecorderService, useValue: recorder },
        { provide: AudioPlayerService, useValue: player },
        {
          provide: VoiceTicketService,
          useValue: { issue: vi.fn().mockResolvedValue({ ticket: 't' }) },
        },
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
        {
          provide: UserSettingsService,
          useValue: { settingsResource: { value: signal(undefined) }, updateSettings: vi.fn() },
        },
        { provide: IncognitoChatService, useValue: { claim: () => false } },
        {
          provide: ToastService,
          useValue: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
        },
      ],
    });
    service = TestBed.inject(VoiceChatService);
  }

  beforeEach(() => {
    vi.clearAllMocks();
    playing.set(false);
    recorder.onAudioChunk = null;
    localStorage.removeItem('voice-interrupt-level-db');
    FakeWebSocket.last = null;
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket;
    setup();
  });

  afterEach(() => {
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = originalWebSocket;
    TestBed.resetTestingModule();
  });

  it('sends mic audio unchanged while the agent is silent', async () => {
    await service.connect('sess-1');
    recorder.onAudioChunk!(chunkAt(-40), 16000);

    expect(audioFrames()).toHaveLength(1);
    expect(sentPcm(audioFrames()[0])).toEqual(chunkAt(-40));
  });

  it('sends silence instead of echo while the agent is speaking', async () => {
    await service.connect('sess-1');
    FakeWebSocket.last!.receive({ type: 'bidi_audio_stream', audio: 'AAAA', sample_rate: 16000 });
    recorder.onAudioChunk!(chunkAt(-40), 16000);

    expect(audioFrames()).toHaveLength(1);
    expect(sentPcm(audioFrames()[0]).every(s => s === 0)).toBe(true);
    expect(player.clear).not.toHaveBeenCalled();
  });

  it('stops playback locally and sends the speech when the user talks over the agent', async () => {
    await service.connect('sess-1');
    FakeWebSocket.last!.receive({ type: 'bidi_audio_stream', audio: 'AAAA', sample_rate: 16000 });
    recorder.onAudioChunk!(chunkAt(-12), 16000);
    recorder.onAudioChunk!(chunkAt(-12), 16000);

    expect(player.clear).toHaveBeenCalled();
    expect(service.status()).toBe('listening');
    // One silent stand-in, then the pre-roll with both loud chunks.
    const frames = audioFrames();
    expect(frames).toHaveLength(3);
    expect(sentPcm(frames[1])).toEqual(chunkAt(-12));
    expect(sentPcm(frames[2])).toEqual(chunkAt(-12));
  });

  it('drops the rest of an interrupted reply until the next one starts', async () => {
    await service.connect('sess-1');
    FakeWebSocket.last!.receive({ type: 'bidi_audio_stream', audio: 'AAAA', sample_rate: 16000 });
    recorder.onAudioChunk!(chunkAt(-12), 16000);
    recorder.onAudioChunk!(chunkAt(-12), 16000);
    player.play.mockClear();

    FakeWebSocket.last!.receive({ type: 'bidi_audio_stream', audio: 'AAAA', sample_rate: 16000 });
    expect(player.play).not.toHaveBeenCalled();

    FakeWebSocket.last!.receive({ type: 'bidi_response_start', response_id: 'r2' });
    FakeWebSocket.last!.receive({ type: 'bidi_audio_stream', audio: 'AAAA', sample_rate: 16000 });
    expect(player.play).toHaveBeenCalledTimes(1);
  });

  it('applies a new interrupt level to a live session', async () => {
    await service.connect('sess-1');
    service.setInterruptLevel(-10);
    FakeWebSocket.last!.receive({ type: 'bidi_audio_stream', audio: 'AAAA', sample_rate: 16000 });
    recorder.onAudioChunk!(chunkAt(-12), 16000);
    recorder.onAudioChunk!(chunkAt(-12), 16000);

    expect(player.clear).not.toHaveBeenCalled();
  });

  it('reports the mic level for the meter', async () => {
    await service.connect('sess-1');
    recorder.onAudioChunk!(chunkAt(-20), 16000);
    expect(service.micLevelDb()).toBeCloseTo(-20, 0);
  });

  it('remembers the interrupt level in this browser, clamped to the slider range', () => {
    service.setInterruptLevel(-18);
    setup();
    expect(service.interruptLevelDb()).toBe(-18);

    service.setInterruptLevel(10);
    expect(service.interruptLevelDb()).toBe(-5);
  });

  it('ignores a stored level outside the slider range', () => {
    localStorage.setItem('voice-interrupt-level-db', '-200');
    setup();
    expect(service.interruptLevelDb()).toBe(-26);
  });
});
