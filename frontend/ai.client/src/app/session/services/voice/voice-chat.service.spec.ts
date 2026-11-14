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
  const recorder = {
    start: vi.fn().mockResolvedValue(undefined),
    stop: vi.fn().mockResolvedValue(undefined),
    onAudioChunk: null,
  };
  const originalWebSocket = globalThis.WebSocket;

  beforeEach(() => {
    vi.clearAllMocks();
    FakeWebSocket.last = null;
    (globalThis as unknown as { WebSocket: unknown }).WebSocket = FakeWebSocket;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: AudioRecorderService, useValue: recorder },
        { provide: AudioPlayerService, useValue: { play: vi.fn(), clear: vi.fn() } },
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
