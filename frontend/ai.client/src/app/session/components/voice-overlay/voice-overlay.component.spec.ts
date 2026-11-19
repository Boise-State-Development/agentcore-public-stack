import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { signal } from '@angular/core';

import { VoiceOverlayComponent } from './voice-overlay.component';
import { VoiceChatService, type VoiceStatus } from '../../services/voice';

describe('VoiceOverlayComponent interrupt level (#2019)', () => {
  let fixture: ComponentFixture<VoiceOverlayComponent>;
  const interruptLevelDb = signal(-26);
  const micLevelDb = signal(-Infinity);
  const voiceChat = {
    status: signal<VoiceStatus>('listening'),
    interruptLevelDb: interruptLevelDb.asReadonly(),
    micLevelDb: micLevelDb.asReadonly(),
    setInterruptLevel: vi.fn((db: number) => interruptLevelDb.set(db)),
    disconnect: vi.fn(),
  };

  beforeEach(() => {
    vi.clearAllMocks();
    interruptLevelDb.set(-26);
    micLevelDb.set(-Infinity);
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [VoiceOverlayComponent],
      providers: [{ provide: VoiceChatService, useValue: voiceChat }],
    });
    fixture = TestBed.createComponent(VoiceOverlayComponent);
    fixture.detectChanges();
  });

  const slider = () =>
    fixture.nativeElement.querySelector('#voice-interrupt-level') as HTMLInputElement;

  it('shows a labelled slider set to the current level', () => {
    const label = fixture.nativeElement.querySelector('label[for="voice-interrupt-level"]');
    expect(label?.textContent).toContain('Interrupt level');
    expect(slider().min).toBe('-50');
    expect(slider().max).toBe('-5');
    expect(slider().value).toBe('-26');
    expect(slider().getAttribute('aria-valuetext')).toBe('-26 decibels');
  });

  it('sets the interrupt level when the slider moves', () => {
    slider().value = '-15';
    slider().dispatchEvent(new Event('input'));
    expect(voiceChat.setInterruptLevel).toHaveBeenCalledWith(-15);
  });

  it('places the meter fill and the marker on the -60 to 0 dB scale', () => {
    micLevelDb.set(-30);
    fixture.detectChanges();
    expect(fixture.componentInstance.micLevelPercent()).toBe(50);
    expect(fixture.componentInstance.interruptLevelPercent()).toBeCloseTo(56.67, 1);

    micLevelDb.set(-Infinity);
    expect(fixture.componentInstance.micLevelPercent()).toBe(0);
  });
});
