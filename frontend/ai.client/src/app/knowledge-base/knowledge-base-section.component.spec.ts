import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { KnowledgeBaseSectionComponent } from './knowledge-base-section.component';
import { KbUpgradeService, UpgradeStatus, DocumentNotCarried } from './kb-upgrade.service';
import { Document, KbUsage, PROCESSING_STATUSES } from '../assistants/models/document.model';
import { ConfigService } from '../services/config.service';
import { ToastService } from '../services/toast/toast.service';
import { DocumentService, DocumentUploadError } from '../assistants/services/document.service';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { BackgroundTaskService } from '../services/background-tasks/background-task.service';
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
    engine: 'classic',
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
        startUserConsent: vi.fn(),
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

  describe('engine visibility (task 16.4)', () => {
    /** A minimal complete Document row for the list. */
    function doc(overrides: Partial<Document> = {}): Document {
      return {
        documentId: 'doc-1',
        assistantId: 'ast-1',
        filename: 'notes.pdf',
        contentType: 'application/pdf',
        sizeBytes: 1234,
        status: 'complete',
        createdAt: '2026-09-01T00:00:00Z',
        updatedAt: '2026-09-01T00:00:00Z',
        ...overrides,
      } as Document;
    }

    describe('status vocabulary', () => {
      it('reads managed documents as processing → ready, never chunking/embedding', async () => {
        await render(status({ phase: 'succeeded', engine: 'managed' }));
        const c = fixture.componentInstance;
        expect(c.statusLabel('uploading')).toBe('Processing');
        expect(c.statusLabel('complete')).toBe('Ready');
        expect(c.statusLabel('failed')).toBe('Failed');
        // A mid-migration record might still carry a legacy word; it must not
        // surface the legacy vocabulary on the managed path.
        expect(c.statusLabel('chunking')).toBe('Processing');
        expect(c.statusLabel('embedding')).toBe('Processing');
      });

      it('keeps the finer-grained words for a classic knowledge base', async () => {
        await render(status({ phase: 'none', engine: 'classic' }));
        const c = fixture.componentInstance;
        expect(c.statusLabel('uploading')).toBe('Uploading');
        expect(c.statusLabel('chunking')).toBe('Chunking');
        expect(c.statusLabel('embedding')).toBe('Embedding');
        expect(c.statusLabel('complete')).toBe('Complete');
        expect(c.statusLabel('failed')).toBe('Failed');
      });

      it('shows "Processing", not "Uploading", for a managed doc still indexing', async () => {
        await render(status({ phase: 'succeeded', engine: 'managed' }));
        fixture.componentInstance.uploadedDocuments.set([doc({ status: 'uploading' })]);
        fixture.detectChanges();
        expect(text()).toContain('Processing');
        expect(text()).not.toContain('Uploading');
      });

      it('names the born-managed provisioning wait, on either engine reading', async () => {
        // The label must NOT depend on the engine. A born-managed first upload sets
        // `provisioning` in the same request that declares the knowledge base
        // managed, but the engine here comes from the upgrade-status poll, which
        // has not necessarily caught up — so keying this on the engine would show a
        // first-time author "Uploading" for the minutes their knowledge base is
        // being built.
        await render(status({ phase: 'none', engine: 'classic' }));
        expect(fixture.componentInstance.statusLabel('provisioning')).toBe(
          'Provisioning knowledge base…',
        );

        await render(status({ phase: 'succeeded', engine: 'managed' }));
        expect(fixture.componentInstance.statusLabel('provisioning')).toBe(
          'Provisioning knowledge base…',
        );
      });

      it('keeps polling a provisioning document', async () => {
        // Provisioning is non-terminal, so it has to be in PROCESSING_STATUSES or
        // the row would sit there until the author reloaded the page.
        expect(PROCESSING_STATUSES).toContain('provisioning');
      });
    });

    describe('the Managed/Classic badge', () => {
      it('labels a managed knowledge base "Managed"', async () => {
        await render(status({ phase: 'succeeded', engine: 'managed' }));
        expect(fixture.componentInstance.isManagedEngine()).toBe(true);
        expect(fixture.componentInstance.engineBadgeLabel()).toBe('Managed');
      });

      it('labels a legacy knowledge base "Classic"', async () => {
        await render(status({ phase: 'none', engine: 'classic' }));
        expect(fixture.componentInstance.isManagedEngine()).toBe(false);
        expect(fixture.componentInstance.engineBadgeLabel()).toBe('Classic');
      });

      it('defaults to classic when no status has been read', () => {
        expect(fixture.componentInstance.engineBadgeLabel()).toBe('Classic');
      });

      it('renders the badge next to the document list when documents exist', async () => {
        await render(status({ phase: 'succeeded', engine: 'managed' }));
        fixture.componentInstance.uploadedDocuments.set([doc()]);
        fixture.detectChanges();
        expect(fixture.componentInstance.showEngineBadge()).toBe(true);
        expect(text()).toContain('Managed');
      });

      it('hides the badge when the knowledge base has no documents', async () => {
        await render(status({ phase: 'succeeded', engine: 'managed' }));
        fixture.componentInstance.uploadedDocuments.set([]);
        fixture.detectChanges();
        expect(fixture.componentInstance.showEngineBadge()).toBe(false);
      });
    });
  });

  describe('storage usage bar (Requirement 12.11)', () => {
    function usage(overrides: Partial<KbUsage> = {}): KbUsage {
      return {
        engine: 'managed',
        storedBytes: 0,
        reservedBytes: 0,
        cap: 100 * 1024 * 1024,
        elevated: false,
        ...overrides,
      };
    }

    function doc(): Document {
      return {
        documentId: 'doc-1',
        assistantId: 'ast-1',
        filename: 'notes.pdf',
        contentType: 'application/pdf',
        sizeBytes: 1234,
        status: 'complete',
        createdAt: '2026-09-01T00:00:00Z',
        updatedAt: '2026-09-01T00:00:00Z',
      } as Document;
    }

    it('is green well under the cap and shows "of the cap" copy', () => {
      const c = fixture.componentInstance;
      c.kbUsage.set(usage({ storedBytes: 10 * 1024 * 1024 })); // 10%
      expect(c.kbHasCap()).toBe(true);
      expect(c.kbUsageLevel()).toBe('green');
      expect(c.kbUsageLabel()).toContain('of');
      expect(c.kbUsagePercentLabel()).toBe('10%');
    });

    it('turns yellow at 75% of the cap', () => {
      const c = fixture.componentInstance;
      c.kbUsage.set(usage({ storedBytes: 80 * 1024 * 1024 })); // 80%
      expect(c.kbUsageLevel()).toBe('yellow');
    });

    it('turns red at 90% of the cap', () => {
      const c = fixture.componentInstance;
      c.kbUsage.set(usage({ storedBytes: 95 * 1024 * 1024 })); // 95%
      expect(c.kbUsageLevel()).toBe('red');
    });

    it('counts in-flight reserved bytes toward usage', () => {
      const c = fixture.componentInstance;
      // 70 MB stored + 20 MB reserved = 90 MB of 100 MB → red.
      c.kbUsage.set(usage({ storedBytes: 70 * 1024 * 1024, reservedBytes: 20 * 1024 * 1024 }));
      expect(c.kbUsedBytes()).toBe(90 * 1024 * 1024);
      expect(c.kbUsageLevel()).toBe('red');
    });

    it('is uncapped and always green for a legacy knowledge base', () => {
      const c = fixture.componentInstance;
      c.kbUsage.set(usage({ engine: 's3vectors', cap: null, storedBytes: 42 * 1024 * 1024 }));
      expect(c.kbHasCap()).toBe(false);
      expect(c.kbUsageLevel()).toBe('green');
      expect(c.kbUsageLabel()).toContain('stored');
      expect(c.kbUsageLabel()).not.toContain('of');
      expect(c.kbUsagePercentLabel()).toBe('');
    });

    it('renders the bar alongside the documents list for a managed KB', async () => {
      await render(status({ phase: 'succeeded', engine: 'managed' }));
      fixture.componentInstance.uploadedDocuments.set([doc()]);
      fixture.componentInstance.kbUsage.set(usage({ storedBytes: 50 * 1024 * 1024 }));
      fixture.detectChanges();
      expect(fixture.componentInstance.showUsageBar()).toBe(true);
      expect(fixture.nativeElement.querySelector('[role="progressbar"]')).not.toBeNull();
      expect(text()).toContain('used');
    });

    it('hides the bar for a legacy KB, whose "0 B stored" beside real docs confuses', async () => {
      // A Classic KB is uncapped and tracks no bytes, so the bar would read
      // "0 B stored" next to actual documents. Hidden entirely; the per-document
      // sizes are the only storage signal for legacy.
      await render(status({ phase: 'none', engine: 'classic' }));
      fixture.componentInstance.uploadedDocuments.set([doc()]);
      fixture.componentInstance.kbUsage.set(
        usage({ engine: 's3vectors', cap: null, storedBytes: 0 }),
      );
      fixture.detectChanges();
      expect(fixture.componentInstance.showUsageBar()).toBe(false);
      expect(fixture.nativeElement.querySelector('[role="progressbar"]')).toBeNull();
    });

    it('does not render the bar in create mode, where there is no record', () => {
      // No entityId set → create mode, so nothing is stored yet.
      fixture.componentInstance.kbUsage.set(usage());
      expect(fixture.componentInstance.showUsageBar()).toBe(false);
    });
  });
});

describe('KnowledgeBaseSectionComponent — replace document', () => {
  let fixture: ComponentFixture<KnowledgeBaseSectionComponent>;
  let documents: Record<string, ReturnType<typeof vi.fn>>;
  let dialog: { open: ReturnType<typeof vi.fn> };
  let toast: Record<'success' | 'error' | 'warning' | 'info', ReturnType<typeof vi.fn>>;

  function doc(overrides: Partial<Document> = {}): Document {
    return {
      documentId: 'doc-1',
      assistantId: 'ast-1',
      filename: 'notes.pdf',
      contentType: 'application/pdf',
      sizeBytes: 1234,
      status: 'complete',
      createdAt: '2026-09-01T00:00:00Z',
      updatedAt: '2026-09-01T00:00:00Z',
      ...overrides,
    } as Document;
  }

  const pdf = () => new File(['v2'], 'Notes v2.PDF', { type: 'application/pdf' });

  beforeEach(async () => {
    TestBed.resetTestingModule();
    const collaborators = quietCollaborators();
    documents = (collaborators[0] as unknown as { useValue: Record<string, ReturnType<typeof vi.fn>> })
      .useValue;
    documents['requestUploadUrl'].mockResolvedValue({ documentId: 'doc-new', uploadUrl: 'https://s3/put' });
    documents['uploadToS3'].mockResolvedValue(undefined);
    documents['reportUploadFailure'].mockResolvedValue(undefined);
    dialog = { open: vi.fn() };
    toast = { success: vi.fn(), error: vi.fn(), warning: vi.fn(), info: vi.fn() };

    await TestBed.configureTestingModule({
      imports: [KnowledgeBaseSectionComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ...collaborators,
        { provide: ConfigService, useValue: { appApiUrl: () => 'http://api.test' } },
        {
          provide: KbUpgradeService,
          useValue: { getStatus: vi.fn().mockResolvedValue(status()), start: vi.fn(), retry: vi.fn(), dismissNotice: vi.fn() },
        },
        { provide: ToastService, useValue: toast },
        { provide: Dialog, useValue: dialog },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(KnowledgeBaseSectionComponent);
    fixture.componentRef.setInput('createDraft', async () => 'ast-1');
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(docs: Document[]) {
    documents['listDocuments'].mockResolvedValue({ documents: docs, nextToken: null });
    fixture.componentRef.setInput('entityId', 'ast-1');
    fixture.componentRef.setInput('userPermission', 'owner');
    fixture.componentRef.setInput('permissionResolved', true);
    fixture.detectChanges();
    // Documents, crawls and sync policies load in separate awaited steps.
    await vi.waitFor(() => {
      fixture.detectChanges();
      expect(fixture.componentInstance.isLoadingKnowledge()).toBe(false);
      expect(el().querySelectorAll('li').length).toBe(docs.length);
    });
  }

  function el(): HTMLElement {
    return fixture.nativeElement as HTMLElement;
  }

  function replaceButton(name = 'notes.pdf'): HTMLButtonElement | null {
    return el().querySelector(`button[aria-label="Replace ${name}"]`);
  }

  /** Open the picker for a row and "choose" a file, as the browser would. */
  function pick(target: Document, file: File): Promise<void> {
    const c = fixture.componentInstance;
    c.startReplace(target);
    return c.onReplaceFilePicked({ target: { files: [file], value: 'x' } } as unknown as Event);
  }

  function rows(): string[] {
    return Array.from(el().querySelectorAll('li')).map((li) => li.textContent ?? '');
  }

  it('offers Replace on complete and failed device uploads only', async () => {
    await render([
      doc({ documentId: 'a', filename: 'a.pdf' }),
      doc({ documentId: 'b', filename: 'b.pdf', status: 'failed' }),
      doc({ documentId: 'c', filename: 'c.pdf', status: 'uploading' }),
      doc({ documentId: 'd', filename: 'd.pdf', status: 'chunking' }),
      doc({ documentId: 'e', filename: 'e.pdf', status: 'embedding' }),
      doc({ documentId: 'f', filename: 'f.pdf', status: 'provisioning' }),
    ]);
    expect(replaceButton('a.pdf')).not.toBeNull();
    expect(replaceButton('a.pdf')?.getAttribute('title')).toBe('Replace with a newer version');
    expect(replaceButton('b.pdf')).not.toBeNull();
    for (const name of ['c.pdf', 'd.pdf', 'e.pdf', 'f.pdf']) {
      expect(replaceButton(name)).toBeNull();
    }
  });

  it('hides Replace for imported and web documents, leaving their sync control', async () => {
    await render([
      doc({ documentId: 'g', filename: 'Roster.pdf', sourceConnectorId: 'google-drive', sourceFileId: 'f1' }),
      doc({ documentId: 'w', filename: 'Catalog', sourceConnectorId: 'web', sourceFileId: 'https://x.edu/' }),
    ]);
    expect(replaceButton('Roster.pdf')).toBeNull();
    expect(replaceButton('Catalog')).toBeNull();
    expect(el().querySelector('select[aria-label="Auto-sync schedule for Roster.pdf"]')).not.toBeNull();
  });

  it('deletes the old version only after the new one is ready, under the same name', async () => {
    const old = doc();
    await render([old]);
    documents['pollDocumentStatus'].mockResolvedValue(doc({ documentId: 'doc-new', status: 'complete' }));
    documents['listDocuments'].mockResolvedValue({ documents: [doc({ documentId: 'doc-new' })], nextToken: null });

    await pick(old, pdf());
    fixture.detectChanges();

    const sent = documents['requestUploadUrl'].mock.calls[0];
    expect(sent[0]).toBe('ast-1');
    expect((sent[1] as File).name).toBe('notes.pdf');
    expect(sent[2]).toEqual({ suppressErrorToast: true });
    expect(documents['deleteDocument']).toHaveBeenCalledWith('ast-1', 'doc-1');
    expect(documents['deleteDocument'].mock.invocationCallOrder[0]).toBeGreaterThan(
      documents['pollDocumentStatus'].mock.invocationCallOrder[0],
    );
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toContain('notes.pdf');
    expect(toast.success).toHaveBeenCalled();
  });

  it('still deletes the old version when a reload during the upload starts polling the new one', async () => {
    const old = doc();
    await render([old]);
    const c = fixture.componentInstance;
    const uploading = doc({ documentId: 'doc-new', status: 'uploading' });
    const complete = doc({ documentId: 'doc-new', status: 'complete' });
    let finishUpload!: () => void;
    documents['uploadToS3'].mockReturnValue(new Promise<void>((resolve) => (finishUpload = resolve)));
    documents['listDocuments'].mockResolvedValue({ documents: [old, uploading], nextToken: null });

    // A stray poll on the new document that ends on its own schedule, the way
    // one started by a crawl-watcher reload would.
    let finishStrayPoll!: (d: Document) => void;
    documents['pollDocumentStatus'].mockReturnValueOnce(new Promise<Document>((resolve) => (finishStrayPoll = resolve)));
    // The replace's own poll: honours cancellation like the real one.
    documents['pollDocumentStatus'].mockImplementation(
      (_r: string, _d: string, _u: unknown, _t: unknown, _a: unknown, _b: unknown, isCancelled?: () => boolean) =>
        new Promise<Document>((resolve, reject) => {
          let ticks = 0;
          const tick = () => {
            if (isCancelled?.()) return reject(new DocumentUploadError('cancelled', 'POLL_CANCELLED'));
            if (++ticks >= 5) return resolve(complete);
            setTimeout(tick, 5);
          };
          setTimeout(tick, 5);
        }),
    );

    const running = pick(old, pdf());
    await vi.waitFor(() => expect(documents['uploadToS3']).toHaveBeenCalled());

    // A reload mid-upload leaves the in-flight replacement alone...
    await c.loadDocuments();
    expect(documents['pollDocumentStatus']).not.toHaveBeenCalled();
    // ...and a poll that got in before the replace knew the new id can't cancel it.
    void c.startPollingDocument('doc-new', 'ast-1');
    expect(documents['pollDocumentStatus']).toHaveBeenCalledTimes(1);

    finishUpload();
    await vi.waitFor(() => expect(documents['pollDocumentStatus']).toHaveBeenCalledTimes(2));
    finishStrayPoll(complete);

    await running;
    expect(documents['deleteDocument']).toHaveBeenCalledWith('ast-1', 'doc-1');
    expect(toast.success).toHaveBeenCalled();
    expect(toast.warning).not.toHaveBeenCalled();
  });

  it('keeps the old version when the new one fails, with the failed row right after it', async () => {
    const old = doc();
    const other = doc({ documentId: 'doc-2', filename: 'other.pdf' });
    await render([old, other]);
    const failed = doc({ documentId: 'doc-new', status: 'failed', errorMessage: 'Parse error' });
    documents['pollDocumentStatus'].mockResolvedValue(failed);
    documents['listDocuments'].mockResolvedValue({ documents: [failed, other, old], nextToken: null });

    await pick(old, pdf());
    fixture.detectChanges();

    expect(documents['deleteDocument']).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledTimes(1);
    const texts = rows();
    const failedIndex = texts.findIndex((t) => t.includes('Parse error'));
    const oldIndex = texts.findIndex((t) => t.includes('notes.pdf') && !t.includes('Parse error'));
    expect(failedIndex).toBe(oldIndex + 1);
    expect(texts[failedIndex]).toContain('New version of the file above');
  });

  it('asks before removing the current version when the byte cap has no room', async () => {
    const old = doc();
    await render([old]);
    documents['requestUploadUrl'].mockRejectedValue(new DocumentUploadError('over cap', 'HTTP_413'));
    dialog.open.mockReturnValue({ closed: of(false) });

    await pick(old, pdf());

    expect(dialog.open).toHaveBeenCalledTimes(1);
    expect(dialog.open.mock.calls[0][1].data.destructive).toBe(true);
    expect(documents['deleteDocument']).not.toHaveBeenCalled();
    expect(documents['uploadToS3']).not.toHaveBeenCalled();
  });

  it('removes the current version first once the byte-cap fallback is confirmed', async () => {
    const old = doc();
    await render([old]);
    documents['requestUploadUrl']
      .mockRejectedValueOnce(new DocumentUploadError('over cap', 'HTTP_413'))
      .mockResolvedValueOnce({ documentId: 'doc-new', uploadUrl: 'https://s3/put' });
    dialog.open.mockReturnValue({ closed: of(true) });
    documents['pollDocumentStatus'].mockResolvedValue(doc({ documentId: 'doc-new', status: 'complete' }));

    await pick(old, pdf());

    expect(documents['deleteDocument']).toHaveBeenCalledWith('ast-1', 'doc-1');
    expect(documents['deleteDocument'].mock.invocationCallOrder[0]).toBeLessThan(
      documents['requestUploadUrl'].mock.invocationCallOrder[1],
    );
    expect(documents['uploadToS3']).toHaveBeenCalled();
  });

  it('refuses a file of a different type without uploading', async () => {
    const old = doc();
    await render([old]);
    await pick(old, new File(['x'], 'notes.docx'));
    expect(documents['requestUploadUrl']).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledTimes(1);
  });

  it('warns to keep the tab open while replacing, and clears the warning after', async () => {
    const old = doc();
    await render([old, doc({ documentId: 'doc-2', filename: 'other.pdf' })]);
    let finishUpload!: () => void;
    documents['uploadToS3'].mockReturnValue(new Promise<void>((resolve) => (finishUpload = resolve)));
    documents['pollDocumentStatus'].mockResolvedValue(doc({ documentId: 'doc-new', status: 'complete' }));
    const tasks = TestBed.inject(BackgroundTaskService);
    const unloadAllowed = () => window.dispatchEvent(new Event('beforeunload', { cancelable: true }));

    const running = pick(old, pdf());
    await vi.waitFor(() => expect(documents['uploadToS3']).toHaveBeenCalled());
    fixture.detectChanges();

    expect(tasks.tasks()).toHaveLength(1);
    expect(replaceButton('notes.pdf')?.disabled).toBe(true);
    expect(replaceButton('other.pdf')?.disabled).toBe(true);
    expect(el().querySelector<HTMLButtonElement>('button[aria-label="Delete notes.pdf"]')?.disabled).toBe(true);
    expect(el().querySelector<HTMLButtonElement>('button[aria-label="Delete other.pdf"]')?.disabled).toBe(false);
    expect(el().textContent).toContain('Replacing…');
    expect(unloadAllowed()).toBe(false);

    finishUpload();
    await running;
    fixture.detectChanges();
    expect(tasks.tasks()).toHaveLength(0);
    expect(unloadAllowed()).toBe(true);
    expect(el().textContent).not.toContain('Replacing…');
  });
});
