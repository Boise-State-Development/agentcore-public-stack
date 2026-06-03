import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { MessageMetadataBadgesComponent } from './message-metadata-badges.component';
import { LocalSettingsService } from '../../../../services/local-settings.service';

const BREAKDOWN = {
  total: 705,
  partitions: [
    { key: 'system', label: 'System prompt', tokens: 16 },
    { key: 'tools', label: 'Tools', tokens: 655 },
    { key: 'messages', label: 'Messages', tokens: 34 },
  ],
};

describe('MessageMetadataBadgesComponent — context breakdown', () => {
  let fixture: ComponentFixture<MessageMetadataBadgesComponent>;
  let settings: LocalSettingsService;

  function text(): string {
    return fixture.nativeElement.textContent ?? '';
  }

  function badge(): HTMLElement | null {
    return fixture.nativeElement.querySelector('[title]');
  }

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [MessageMetadataBadgesComponent],
    }).compileComponents();

    settings = TestBed.inject(LocalSettingsService);
    settings.showTokenCount.set(true);

    fixture = TestBed.createComponent(MessageMetadataBadgesComponent);
  });

  it('renders the total and every partition when token count is enabled', () => {
    fixture.componentRef.setInput('metadata', { contextBreakdown: BREAKDOWN });
    fixture.detectChanges();

    const t = text();
    expect(t).toContain('Context: 705');
    expect(t).toContain('System prompt');
    expect(t).toContain('16');
    expect(t).toContain('Tools');
    expect(t).toContain('655');
    expect(t).toContain('Messages');
    expect(t).toContain('34');
  });

  it('exposes the breakdown as an accessible title summary', () => {
    fixture.componentRef.setInput('metadata', { contextBreakdown: BREAKDOWN });
    fixture.detectChanges();

    expect(badge()?.getAttribute('title')).toBe(
      'System prompt: 16 · Tools: 655 · Messages: 34',
    );
  });

  it('renders whatever partitions arrive (open-ended contract)', () => {
    fixture.componentRef.setInput('metadata', {
      contextBreakdown: {
        total: 100,
        partitions: [{ key: 'skills', label: 'Skills', tokens: 100 }],
      },
    });
    fixture.detectChanges();

    expect(text()).toContain('Skills');
    expect(text()).toContain('Context: 100');
  });

  it('hides the breakdown when token count is disabled', () => {
    settings.showTokenCount.set(false);
    fixture.componentRef.setInput('metadata', { contextBreakdown: BREAKDOWN });
    fixture.detectChanges();

    expect(text()).not.toContain('Context: 705');
  });

  it('renders nothing for an empty partition list', () => {
    fixture.componentRef.setInput('metadata', {
      contextBreakdown: { total: 0, partitions: [] },
    });
    fixture.detectChanges();

    expect(text()).not.toContain('Context:');
  });

  it('is absent when no metadata is provided', () => {
    fixture.componentRef.setInput('metadata', null);
    fixture.detectChanges();

    expect(text()).not.toContain('Context:');
  });
});
