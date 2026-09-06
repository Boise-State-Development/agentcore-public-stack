// chat-container.component.branding.spec.ts
//
// Property-based test for Property 9 (Chat_Greeting_Block half). See
// design.md "Correctness Properties" for the authoritative property text
// and requirements.md 3.2/3.3/3.4/3.5 for the acceptance criteria validated
// here. The Sidenav_Component half of this property lives in
// `../../../components/sidenav/sidenav.branding.spec.ts`.
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Component, input, output } from '@angular/core';
import { signal } from '@angular/core';
import fc from 'fast-check';

import { ChatContainerComponent } from './chat-container.component';
import { SidenavService } from '../../../services/sidenav/sidenav.service';
import { ArtifactStateService } from '../../services/artifacts/artifact-state.service';
import { VoiceChatService } from '../../services/voice';
import { BrandingService } from '../../../../branding/branding.service';

/**
 * Stand-in for `ChatInputComponent`, the one real child unconditionally
 * rendered by the "no assistant" empty-state branch this spec exercises
 * (the greeting logo lives right next to it in the template). This spec is
 * about the branding `<img>` markup only; the real chat input pulls in
 * `FileUploadService`, `ToolService`, `VoiceChatService`,
 * `SystemPromptsService`, `AgentMentionService` and `Router` — none of
 * which this property cares about.
 */
@Component({ selector: 'app-chat-input', template: '' })
class ChatInputStub {
  readonly sessionId = input<string | null>(null);
  readonly isChatLoading = input<boolean>(false);
  readonly showFileControls = input<boolean>(true);
  readonly showVoiceControl = input<boolean>(true);
  readonly showSettingsControl = input<boolean>(true);
  readonly showAnnouncements = input<boolean>(true);
  readonly messageSubmitted = output<unknown>();
  readonly messageCancelled = output<void>();
  readonly fileAttached = output<File>();
  readonly settingsToggled = output<void>();
}

/**
 * Arbitrary for an already-normalized app name, as `BrandingService`
 * would expose it to a consuming component: BrandingService's own
 * normalization (Property 5, task 4.3) guarantees the exposed `appName`
 * is never absent/empty/whitespace-only — it is either the valid
 * configured `App_Name` or the fixed `DEFAULT_ALT_LABEL`. This test does
 * not re-test that normalization; it generates any string meeting that
 * same guarantee (1-100 chars, >=1 non-whitespace character, matching the
 * App_Name bounds) and asserts the component wiring stays consistent for
 * every such value.
 */
const arbNormalizedAppName = fc
  .string({ minLength: 1, maxLength: 100 })
  .filter((s) => /\S/.test(s));

interface MockBrandingService {
  logo: { light: string; dark: string };
  appName: string;
  greetingTemplates: readonly string[];
  fallbackGreetings: readonly string[];
  configErrors: readonly unknown[];
}

describe('ChatContainerComponent — Property 9: Logo alt text equals normalized app name', () => {
  let mockBranding: MockBrandingService;

  beforeEach(() => {
    TestBed.resetTestingModule();

    mockBranding = {
      logo: { light: 'img/logo-light.png', dark: 'img/logo-dark.png' },
      appName: 'Initial App Name',
      greetingTemplates: [],
      fallbackGreetings: [],
      configErrors: [],
    };

    TestBed.configureTestingModule({
      providers: [
        {
          provide: SidenavService,
          useValue: { isCollapsed: signal(false), close: vi.fn(), toggleCollapsed: vi.fn() },
        },
        {
          provide: ArtifactStateService,
          useValue: { openArtifact: signal(null) },
        },
        {
          provide: VoiceChatService,
          useValue: { isVoiceActive: signal(false) },
        },
        { provide: BrandingService, useValue: mockBranding },
      ],
    });
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /**
   * One-time async setup that resolves the lazy `ChatInputComponent` chunk
   * and swaps in the stub. Kept separate from fixture creation so the
   * property below can create a fresh fixture per iteration synchronously
   * (see the comment at the call site for why a fresh fixture per
   * iteration is required here).
   */
  async function prepareChatContainerModule() {
    const { ChatInputComponent } = await import('../chat-input/chat-input.component');

    TestBed.overrideComponent(ChatContainerComponent, {
      remove: { imports: [ChatInputComponent] },
      add: { imports: [ChatInputStub] },
    });
  }

  function createChatContainerFixture() {
    const fixture = TestBed.createComponent(ChatContainerComponent);
    // Default inputs land in the "no assistant, not loading, empty state"
    // full-page branch, which renders the greeting logo unconditionally.
    fixture.componentRef.setInput('messages', []);
    return fixture;
  }

  // Feature: branding-customization, Property 9: Logo alt text equals normalized app name
  // Validates: Requirements 3.2, 3.3, 3.4, 3.5
  it('every branding logo <img> has alt text identical to the normalized app name, for any app name', async () => {
    // Resolve the lazy `ChatInputComponent` chunk and stub override once,
    // up front, so each of the 100 property iterations below can create a
    // *fresh* component fixture synchronously. A fresh fixture per
    // iteration — with `appName` set on the shared mock BEFORE
    // `TestBed.createComponent` — avoids Angular's dev-mode `NG0100`
    // re-check: reusing one fixture and mutating the mock's `appName`
    // between `detectChanges()` calls trips the "Expression has changed
    // after it was checked" guard, because the previously-checked binding
    // value and the live mock value briefly disagree across the two
    // internal check passes `detectChanges()` performs.
    await prepareChatContainerModule();

    fc.assert(
      fc.property(arbNormalizedAppName, (appName) => {
        mockBranding.appName = appName;
        const fixture = createChatContainerFixture();
        fixture.detectChanges();

        try {
          const imgs: HTMLImageElement[] = Array.from(
            fixture.nativeElement.querySelectorAll('img'),
          );

          // Sanity: the logo markup (light + dark variants) is actually present.
          expect(imgs.length).toBeGreaterThan(0);

          for (const img of imgs) {
            expect(img.alt).toBe(appName);
          }
        } finally {
          // Undestroyed fixtures from earlier iterations stay attached to
          // the same TestBed-managed injector tree; leaving them around
          // means a later iteration's `detectChanges()` sweeps them back
          // in for a no-changes re-check against the (by-then-mutated)
          // shared mock, tripping NG0100 spuriously. Destroy after each
          // iteration so only the current fixture is ever live.
          fixture.destroy();
        }
      }),
      { numRuns: 100 },
    );
  }, 30_000);
});
