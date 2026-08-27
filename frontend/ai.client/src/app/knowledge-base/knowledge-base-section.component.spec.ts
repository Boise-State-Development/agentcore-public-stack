import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { KnowledgeBaseSectionComponent } from './knowledge-base-section.component';
import { KbUpgradeService, UpgradeStatus, DocumentNotCarried } from './kb-upgrade.service';
import { ConfigService } from '../services/config.service';
import { ToastService } from '../services/toast/toast.service';
import { DocumentService } from '../assistants/services/document.service';
import { FileSourceService } from '../assistants/services/file-source.service';
import { WebSourceService } from '../assistants/services/web-source.service';
import { SyncPolicyService } from '../assistants/services/sync-policy.service';
import { UserConnectorsService } from '../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../services/oauth-consent/oauth-consent.service';

/**
 * The upgrade card's five states (Requirement 23) and the stranded-document
 * disclosure (Requirement 21).
 *
 * The first test is the most important one in the file: a legacy knowledge base
 * that needs no action must render **nothing at all**. Every other state is a
 * deliberate interruption of someone's work and has to earn its place.
 *
 * Every collaborating service is stubbed rather than left to the HTTP testing
 * backend. The section loads documents, crawls, sync policies and connectors on
 * hydration; left unstubbed those requests stay pending and `whenStable()` never
 * settles, so the whole file times out without telling you why.
 */
function status(overrides: Partial<UpgradeStatus> = {}): UpgradeStatus {
  return {
    phase: 'none',
    canUpgrade: false,
    progress: null,
    reason: null,
    noticePending: false,
    documentsNotCarried: [],
    ...overrides,
  };
}

function strandedDoc(overrides: Partial<DocumentNotCarried> = {}): DocumentNotCarried {
  return {
    documentId: 'doc-1',
    filename: 'quarterly-report.pdf',
    status: 'failed',
    kind: 'processing_failure',
    message: 'This document could not be processed.',
    retryable: true,
    ...overrides,
  };
}

/** Collaborators the card does not exercise, quiet and HTTP-free. */
function quietCollaborators() {
  return [
    {
      provide: DocumentService,
      useValue: {
        listDocuments: vi.fn().mockResolvedValue({ documents: [], nextToken: null }),
        deleteDocument: vi.fn().mockResolvedValue(undefined),
        getDownloadUrl: vi.fn(),
        pollDocumentStatus: vi.fn(),
        reportUploadFailure: vi.fn(),
        requestUploadUrl: vi.fn(),
        uploadToS3: vi.fn(),
      },
    },
    {
      provide: FileSourceService,
      useValue: { listFileSources: vi.fn().mockResolvedValue([]) },
    },
    {
      provide: WebSourceService,
      useValue: {
        listCrawls: vi.fn().mockResolvedValue([]),
        listActiveCrawls: vi.fn().mockResolvedValue([]),
        deleteCrawl: vi.fn(),
      },
    },
    {
      provide: SyncPolicyService,
      useValue: {
        listPolicies: vi.fn().mockResolvedValue([]),
        createPolicy: vi.fn(),
        updatePolicy: vi.fn(),
        deletePolicy: vi.fn(),
        runNow: vi.fn(),
      },
    },
    {
      provide: UserConnectorsService,
      useValue: { initiateConsent: vi.fn() },
    },
    {
      provide: OAuthConsentService,
      useValue: {
        completion: signal(null),
        inFlightProviders: signal(new Set<string>()),
        acknowledgeCompletion: vi.fn(),
        openConsentPopup: vi.fn(),
        requestConsent: vi.fn(),
      },
    },
  ];
}

describe('KnowledgeBaseSectionComponent — upgrade card', () => {
  let fixture: ComponentFixture<KnowledgeBaseSectionComponent>;
  let upgrade: {
    getStatus: ReturnType<typeof vi.fn>;
    start: ReturnType<typeof vi.fn>;
    retry: ReturnType<typeof vi.fn>;
    dismissNotice: ReturnType<typeof vi.fn>;
  };
  let toasts: { success: string[]; error: string[] };

  beforeEach(async () => {
    TestBed.resetTestingModule();
    upgrade = {
      getStatus: vi.fn().mockResolvedValue(status()),
      start: vi
        .fn()
        .mockResolvedValue({ phase: 'in_progress', started: true, message: 'Upgrade started.' }),
      retry: vi
        .fn()
        .mockResolvedValue({ phase: 'in_progress', started: true, message: 'Upgrade restarted.' }),
      dismissNotice: vi.fn().mockResolvedValue(undefined),
    };
    toasts = { success: [], error: [] };

    await TestBed.configureTestingModule({
      imports: [KnowledgeBaseSectionComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ...quietCollaborators(),
        { provide: ConfigService, useValue: { appApiUrl: () => 'http://api.test' } },
        { provide: KbUpgradeService, useValue: upgrade },
        {
          provide: ToastService,
          useValue: {
            success: (m: string) => toasts.success.push(m),
            error: (m: string) => toasts.error.push(m),
            info: () => undefined,
            warning: () => undefined,
          },
        },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(KnowledgeBaseSectionComponent);
    fixture.componentRef.setInput('createDraft', async () => 'ast-1');
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  /** Settle the hydration effect plus its awaited status read. */
  async function render(current: UpgradeStatus, entityId: string | null = 'ast-1') {
    upgrade.getStatus.mockResolvedValue(current);
    fixture.componentRef.setInput('entityId', entityId);
    fixture.componentRef.setInput('userPermission', 'owner');
    fixture.componentRef.setInput('permissionResolved', true);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function text(): string {
    return (fixture.nativeElement.textContent as string) ?? '';
  }

  function buttonWith(fragment: string): HTMLButtonElement | undefined {
    return Array.from(
      fixture.nativeElement.querySelectorAll('button'),
    ).find((b) =>
      ((b as HTMLButtonElement).textContent ?? '').toLowerCase().includes(fragment.toLowerCase()),
    ) as HTMLButtonElement | undefined;
  }

  describe('phase none (Requirement 23.1)', () => {
    it('renders no badge, banner or prompt for a legacy knowledge base', async () => {
      await render(status({ phase: 'none' }));
      expect(text()).not.toContain('Upgrade');
      expect(text()).not.toContain('faster');
      expect(buttonWith('Upgrade knowledge base')).toBeUndefined();
    });

    it('asks for nothing at all in create mode, where there is no record', async () => {
      await render(status({ phase: 'available', canUpgrade: true }), null);
      expect(upgrade.getStatus).not.toHaveBeenCalled();
      expect(buttonWith('Upgrade knowledge base')).toBeUndefined();
    });

    it('does not ask until the permission is resolved', async () => {
      // Asking with the default 'owner' guess would offer a control the user may
      // not have, and would be a 403 for a viewer.
      fixture.componentRef.setInput('entityId', 'ast-1');
      fixture.componentRef.setInput('permissionResolved', false);
      fixture.detectChanges();
      await fixture.whenStable();
      expect(upgrade.getStatus).not.toHaveBeenCalled();
    });
  });

  describe('phase available (Requirement 23.2)', () => {
    it('offers an inline opt-in control that promises continued service', async () => {
      await render(status({ phase: 'available', canUpgrade: true }));
      expect(text()).toContain('A faster knowledge base is available');
      expect(text()).toContain('keeps working');
      expect(buttonWith('Upgrade knowledge base')).toBeDefined();
    });

    it('hides the control from a viewer (Requirement 23.7)', async () => {
      // The server decides; canUpgrade false is how it says no.
      await render(status({ phase: 'available', canUpgrade: false }));
      expect(buttonWith('Upgrade knowledge base')).toBeUndefined();
    });

    it('starts the upgrade and reports the outcome', async () => {
      await render(status({ phase: 'available', canUpgrade: true }));
      buttonWith('Upgrade knowledge base')!.click();
      await fixture.whenStable();
      expect(upgrade.start).toHaveBeenCalledWith('ast-1');
      expect(toasts.success).toContain('Upgrade started.');
    });

    it('surfaces a refusal without claiming anything changed', async () => {
      upgrade.start.mockRejectedValue(new Error('Upgrades are not being accepted.'));
      await render(status({ phase: 'available', canUpgrade: true }));
      buttonWith('Upgrade knowledge base')!.click();
      await fixture.whenStable();
      expect(toasts.error).toContain('Upgrades are not being accepted.');
    });

    it('re-reads the status after a refusal rather than guessing', async () => {
      // The refusal may have been "already running", in which case the honest
      // phase is not the one the click started from.
      upgrade.start.mockRejectedValue(new Error('nope'));
      await render(status({ phase: 'available', canUpgrade: true }));
      const before = upgrade.getStatus.mock.calls.length;
      buttonWith('Upgrade knowledge base')!.click();
      await fixture.whenStable();
      expect(upgrade.getStatus.mock.calls.length).toBeGreaterThan(before);
    });
  });

  describe('phase in_progress (Requirement 23.3)', () => {
    it('shows non-blocking progress and says it is safe to leave', async () => {
      await render(
        status({ phase: 'in_progress', progress: { completed: 12, total: 40, skipped: 0 } }),
      );
      expect(text()).toContain('Upgrading — 12 of 40 documents');
      expect(text()).toContain('safely leave this page');
    });

    it('announces progress politely rather than seizing focus', async () => {
      await render(status({ phase: 'in_progress' }));
      const live = fixture.nativeElement.querySelector('[aria-live="polite"]');
      expect(live).not.toBeNull();
    });

    it('offers no second upgrade while one is running', async () => {
      await render(status({ phase: 'in_progress' }));
      expect(buttonWith('Upgrade knowledge base')).toBeUndefined();
    });

    it('stays honest when counts are missing instead of showing 0 of 0', async () => {
      await render(status({ phase: 'in_progress', progress: null }));
      expect(text()).toContain('Upgrading your knowledge base');
      expect(text()).not.toContain('0 of 0');
    });

    it('clamps a nonsense count instead of overflowing the bar', async () => {
      await render(
        status({ phase: 'in_progress', progress: { completed: 99, total: 10, skipped: 0 } }),
      );
      expect(fixture.componentInstance.upgradeProgressPercent()).toBe(100);
    });
  });

  describe('phase succeeded (Requirement 23.4)', () => {
    it('shows a one-time notice, not a permanent badge', async () => {
      await render(status({ phase: 'succeeded', noticePending: true }));
      expect(text()).toContain('Upgrade complete');
    });

    it('shows nothing once the notice has been dismissed server-side', async () => {
      await render(status({ phase: 'succeeded', noticePending: false }));
      expect(text()).not.toContain('Upgrade complete');
    });

    it('hides on click, before the server confirms', async () => {
      await render(status({ phase: 'succeeded', noticePending: true }));
      const dismiss = fixture.nativeElement.querySelector(
        '[aria-label="Dismiss upgrade confirmation"]',
      ) as HTMLButtonElement;
      dismiss.click();
      fixture.detectChanges();
      expect(fixture.componentInstance.showUpgradeNotice()).toBe(false);
      expect(upgrade.dismissNotice).toHaveBeenCalledWith('ast-1');
    });
  });

  describe('phase failed (Requirement 23.5)', () => {
    it('gives a plain-language reason, a retry, and reassurance', async () => {
      await render(
        status({
          phase: 'failed',
          canUpgrade: true,
          reason: 'Your knowledge base is larger than the current upgrade size limit.',
        }),
      );
      expect(text()).toContain('The upgrade did not finish');
      expect(text()).toContain('larger than the current upgrade size limit');
      expect(text()).toContain('working normally');
      expect(buttonWith('Try the upgrade again')).toBeDefined();
    });

    it('restarts on retry', async () => {
      await render(status({ phase: 'failed', canUpgrade: true, reason: 'It broke.' }));
      buttonWith('Try the upgrade again')!.click();
      await fixture.whenStable();
      expect(upgrade.retry).toHaveBeenCalledWith('ast-1');
      expect(toasts.success).toContain('Upgrade restarted.');
    });

    it('shows a viewer the failure but no retry control', async () => {
      await render(status({ phase: 'failed', canUpgrade: false, reason: 'It broke.' }));
      expect(text()).toContain('The upgrade did not finish');
      expect(buttonWith('Try the upgrade again')).toBeUndefined();
    });
  });

  describe('stranded documents (Requirement 21)', () => {
    it('warns before the user commits, collapsed by default', async () => {
      await render(
        status({
          phase: 'available',
          canUpgrade: true,
          documentsNotCarried: [strandedDoc(), strandedDoc({ documentId: 'doc-2' })],
        }),
      );
      expect(text()).toContain('2 documents will not be carried across');
      expect(text()).toContain('Show details');
      expect(text()).not.toContain('quarterly-report.pdf');
    });

    it('uses the singular for one document', async () => {
      await render(
        status({ phase: 'available', canUpgrade: true, documentsNotCarried: [strandedDoc()] }),
      );
      expect(text()).toContain('1 document will not be carried across');
    });

    it('reveals the server-authored explanation on expand', async () => {
      await render(
        status({ phase: 'available', canUpgrade: true, documentsNotCarried: [strandedDoc()] }),
      );
      fixture.componentInstance.toggleStrandedDocuments();
      fixture.detectChanges();
      expect(text()).toContain('quarterly-report.pdf');
      expect(text()).toContain('This document could not be processed.');
    });

    it('distinguishes an unsupported format from a processing failure', async () => {
      // Requirement 21.4 — the two need different actions, so they get
      // different headings and only one offers a re-upload.
      await render(
        status({
          phase: 'available',
          canUpgrade: true,
          documentsNotCarried: [
            strandedDoc({
              documentId: 'doc-a',
              filename: 'deck.pages',
              kind: 'unsupported_format',
              message: 'This platform cannot read .pages files.',
              retryable: false,
            }),
          ],
        }),
      );
      fixture.componentInstance.toggleStrandedDocuments();
      fixture.detectChanges();
      expect(text()).toContain('Cannot be read by this platform');
      expect(text()).not.toContain('upload the file again');
    });

    it('offers a re-upload for a document that could succeed as-is', async () => {
      await render(
        status({ phase: 'available', canUpgrade: true, documentsNotCarried: [strandedDoc()] }),
      );
      fixture.componentInstance.toggleStrandedDocuments();
      fixture.detectChanges();
      expect(text()).toContain('upload the file again');
    });

    it('names the kinds distinctly so no two read the same', () => {
      const headings = (
        ['unsupported_format', 'processing_failure', 'being_removed', 'still_processing'] as const
      ).map((kind) => fixture.componentInstance.strandedHeading(kind));
      expect(new Set(headings).size).toBe(headings.length);
    });

    it('stays quiet mid-upgrade, when the user can do nothing about it', async () => {
      await render(
        status({ phase: 'in_progress', documentsNotCarried: [strandedDoc()] }),
      );
      expect(text()).not.toContain('will not be carried across');
    });

    it('marks the disclosure control as expandable for assistive tech', async () => {
      await render(
        status({ phase: 'available', canUpgrade: true, documentsNotCarried: [strandedDoc()] }),
      );
      const toggle = fixture.nativeElement.querySelector('[aria-controls="stranded-documents"]');
      expect(toggle?.getAttribute('aria-expanded')).toBe('false');
    });
  });

  describe('copy (Requirement 23.6)', () => {
    it('never uses the word "vector" in any rendered state', async () => {
      const states: UpgradeStatus[] = [
        status({ phase: 'available', canUpgrade: true, documentsNotCarried: [strandedDoc()] }),
        status({ phase: 'in_progress', progress: { completed: 1, total: 2, skipped: 0 } }),
        status({ phase: 'succeeded', noticePending: true }),
        status({ phase: 'failed', canUpgrade: true, reason: 'It stopped.' }),
      ];
      for (const state of states) {
        TestBed.resetTestingModule();
        await TestBed.configureTestingModule({
          imports: [KnowledgeBaseSectionComponent],
          providers: [
            provideHttpClient(),
            provideHttpClientTesting(),
            ...quietCollaborators(),
            { provide: ConfigService, useValue: { appApiUrl: () => 'http://api.test' } },
            {
              provide: KbUpgradeService,
              useValue: { ...upgrade, getStatus: vi.fn().mockResolvedValue(state) },
            },
            {
              provide: ToastService,
              useValue: {
                success: () => undefined,
                error: () => undefined,
                info: () => undefined,
                warning: () => undefined,
              },
            },
          ],
        }).compileComponents();
        fixture = TestBed.createComponent(KnowledgeBaseSectionComponent);
        fixture.componentRef.setInput('createDraft', async () => 'ast-1');
        fixture.componentRef.setInput('entityId', 'ast-1');
        fixture.componentRef.setInput('permissionResolved', true);
        fixture.detectChanges();
        await fixture.whenStable();
        fixture.detectChanges();
        fixture.componentInstance.strandedExpanded.set(true);
        fixture.detectChanges();
        expect(text().toLowerCase()).not.toContain('vector');
      }
    });
  });

  describe('polling', () => {
    it('polls only while an upgrade is running', async () => {
      vi.useFakeTimers();
      try {
        await render(status({ phase: 'in_progress' }));
        const before = upgrade.getStatus.mock.calls.length;
        vi.advanceTimersByTime(30000);
        expect(upgrade.getStatus.mock.calls.length).toBeGreaterThan(before);
      } finally {
        vi.useRealTimers();
      }
    });

    it('stops polling when the component is destroyed', async () => {
      // Without the ngOnDestroy clear, navigating away leaves a timer hitting
      // the API for a component nobody is looking at.
      vi.useFakeTimers();
      try {
        await render(status({ phase: 'in_progress' }));
        fixture.destroy();
        const after = upgrade.getStatus.mock.calls.length;
        vi.advanceTimersByTime(60000);
        expect(upgrade.getStatus.mock.calls.length).toBe(after);
      } finally {
        vi.useRealTimers();
      }
    });

    it('does not poll a settled upgrade', async () => {
      vi.useFakeTimers();
      try {
        await render(status({ phase: 'succeeded', noticePending: true }));
        const before = upgrade.getStatus.mock.calls.length;
        vi.advanceTimersByTime(60000);
        expect(upgrade.getStatus.mock.calls.length).toBe(before);
      } finally {
        vi.useRealTimers();
      }
    });
  });
});
