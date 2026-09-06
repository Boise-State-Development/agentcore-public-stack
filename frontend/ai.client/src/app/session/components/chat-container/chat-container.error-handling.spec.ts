import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, input, output } from '@angular/core';

import { ChatContainerComponent } from './chat-container.component';
import { ChatInputComponent } from '../chat-input/chat-input.component';

/**
 * Feature: branding-customization
 *
 * Covers the two example/edge-case scenarios called out in the design's
 * "Testing Strategy" and "Error Handling" sections for the Chat_Greeting_Block
 * branding logo:
 *
 *  1. Theme swap happens via CSS only (both `<img>` elements are always
 *     present in the DOM at once, differentiated only by `dark:` classes) —
 *     no navigation/reload and no JS branch on theme state for the
 *     light/dark choice itself (Requirements 2.4, 2.5, 2.6, 7.3).
 *  2. The `(error)` handler reveals a same-dimension placeholder and keeps
 *     surrounding content (the greeting text, `app-animated-text`) rendered
 *     (Requirement 2.8).
 *
 * `ChatInputComponent` is stubbed — this spec is about the empty-state
 * greeting logo/error-handling markup, not the composer's own (much
 * heavier) dependency graph.
 */
describe('ChatContainerComponent — branding logo theme swap and error handling', () => {
  @Component({ selector: 'app-chat-input', template: '' })
  class ChatInputStub {
    readonly sessionId = input<string | null>(null);
    readonly isChatLoading = input<boolean>(false);
    readonly showFileControls = input<boolean>(true);
    readonly showVoiceControl = input<boolean>(true);
    readonly showSettingsControl = input<boolean>(true);
    readonly showAnnouncements = input<boolean>(true);
    readonly announcementPlacement = input<'above' | 'below'>('above');
    readonly messageSubmitted = output<{ content: string; timestamp: Date; fileUploadIds?: string[]; mentionAgentId?: string }>();
    readonly messageCancelled = output<void>();
    readonly fileAttached = output<File>();
    readonly settingsToggled = output<void>();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function renderEmptyStateGreeting() {
    TestBed.overrideComponent(ChatContainerComponent, {
      remove: { imports: [ChatInputComponent] },
      add: { imports: [ChatInputStub] },
    });

    const fixture = TestBed.createComponent(ChatContainerComponent);
    // Required input; empty + no assistant + not loading renders the
    // "no assistant: show greeting with inline input" full-page branch,
    // which contains the branding logo.
    fixture.componentRef.setInput('messages', []);
    fixture.detectChanges();
    return fixture;
  }

  it('renders both light and dark logo <img> elements simultaneously, swapped only via dark: CSS classes', () => {
    const fixture = renderEmptyStateGreeting();
    const html = fixture.nativeElement as HTMLElement;

    const images = html.querySelectorAll('img');
    expect(images.length).toBe(2);

    const lightImg = Array.from(images).find((img) => img.className.includes('dark:hidden'));
    const darkImg = Array.from(images).find((img) => img.className.includes('hidden') && img.className.includes('dark:block'));

    // Both images are present in the DOM at once — the swap is CSS-driven
    // (via class presence), not JS-driven (no *ngIf/@if branching on a
    // theme signal for the light/dark choice itself).
    expect(lightImg).toBeDefined();
    expect(darkImg).toBeDefined();
    expect(lightImg).not.toBe(darkImg);
  });

  it('reveals a same-dimension placeholder on (error) and keeps the greeting text rendered', () => {
    const fixture = renderEmptyStateGreeting();
    const html = fixture.nativeElement as HTMLElement;
    const component = fixture.componentInstance;

    // Precondition: images present, placeholder absent, failure flag false.
    expect(html.querySelectorAll('img').length).toBe(2);
    expect(component['logoLoadFailed']()).toBe(false);
    expect(html.querySelector('[role="img"][aria-label*="logo failed to load"]')).toBeNull();
    expect(html.querySelector('app-animated-text')).not.toBeNull();

    const lightImg = html.querySelectorAll('img')[0];
    lightImg.dispatchEvent(new Event('error'));

    expect(component['logoLoadFailed']()).toBe(true);

    fixture.detectChanges();

    // Placeholder now present, identified by its aria-label.
    const placeholder = html.querySelector('[role="img"][aria-label*="logo failed to load"]');
    expect(placeholder).not.toBeNull();

    // The original <img> elements are swapped out.
    expect(html.querySelectorAll('img').length).toBe(0);

    // Surrounding content — the greeting text — still renders normally.
    expect(html.querySelector('app-animated-text')).not.toBeNull();
    expect(html.querySelector('app-chat-input')).not.toBeNull();
  });
});
