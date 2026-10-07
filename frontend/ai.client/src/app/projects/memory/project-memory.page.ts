import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArchiveBox, heroChevronLeft, heroInboxArrowDown } from '@ng-icons/heroicons/outline';
import { parseIso } from '../../utils/date';
import { MemoryEntry, MemoryLimits, MemoryScope, Project, ProjectMemory } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { isUnavailable, projectErrorMessage } from '../services/projects.service';
import { MemoryArchiveComponent } from './memory-archive.component';
import { MemoryFileViewComponent } from './memory-file-view.component';
import { MemoryIndexViewComponent } from './memory-index-view.component';
import { MemoryMeterComponent } from './memory-meter.component';
import { MemoryReviewComponent, ReviewOutcome } from './memory-review.component';
import { INDEX_SLUG, estimateTokens } from './memory-text';

/** The deployment defaults (shared-projects §4.6), for an API that predates `limits`. */
const DEFAULT_LIMITS: MemoryLimits = {
  fileHardCapTokens: 8000,
  fileSoftThresholdTokens: 6000,
  projectIndexBudgetTokens: 2000,
  personalIndexBudgetTokens: 1000,
};

type MemoryView = 'files' | 'review' | 'archive';

interface ScopeState {
  entries: MemoryEntry[];
  index: string;
}

const EMPTY: ScopeState = { entries: [], index: '' };

/**
 * `/projects/:id/memory` — the project's Memory tab (shared-projects §6, 2.8).
 *
 * Two scopes: **Project**, the shared memory that loads into every member's tasks, and
 * **Just me**, the caller's own memory in this project, which only they can see. Each is
 * a file browser beside the selected file, with the index (`MEMORY.md`) first because it
 * is what every task sees. The review queue for proposed changes and the archive of
 * removed items are views of the same page.
 *
 * Where you are lives in the query string (`scope`, `file`, `view`), so a notification or a
 * `[[link]]` can land on one file and the back button walks back through what was opened.
 */
@Component({
  selector: 'app-project-memory',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    DatePipe,
    NgIcon,
    RouterLink,
    MemoryArchiveComponent,
    MemoryFileViewComponent,
    MemoryIndexViewComponent,
    MemoryMeterComponent,
    MemoryReviewComponent,
  ],
  providers: [provideIcons({ heroArchiveBox, heroChevronLeft, heroInboxArrowDown })],
  templateUrl: './project-memory.page.html',
})
export class ProjectMemoryPage {
  private api = inject(ProjectApiService);

  protected readonly indexSlug = INDEX_SLUG;

  /** Route param and query params (`withComponentInputBinding`). */
  readonly id = input.required<string>();
  readonly scope = input<string | undefined>(undefined);
  readonly file = input<string | undefined>(undefined);
  readonly view = input<string | undefined>(undefined);

  protected readonly project = signal<Project | null>(null);
  protected readonly memory = signal<ProjectMemory | null>(null);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);
  /** Why the project has no memory to show (switched off here, or none yet). */
  protected readonly unavailable = signal<string | null>(null);
  private readonly scopes = signal<Record<MemoryScope, ScopeState>>({ project: EMPTY, mine: EMPTY });
  protected readonly scopeError = signal<string | null>(null);
  protected readonly pending = signal(0);

  protected readonly activeScope = computed<MemoryScope>(() => (this.scope() === 'mine' ? 'mine' : 'project'));
  protected readonly activeView = computed<MemoryView>(() => {
    const view = this.view();
    if (view === 'archive' && this.spaceId()) return 'archive';
    if (view === 'review' && this.activeScope() === 'project' && this.spaceId()) return 'review';
    return 'files';
  });
  protected readonly spaceId = computed(() => {
    const m = this.memory();
    if (!m) return null;
    return this.activeScope() === 'mine' ? m.personalSpaceId : m.sharedSpaceId;
  });
  protected readonly entries = computed(() => this.scopes()[this.activeScope()].entries);
  protected readonly index = computed(() => this.scopes()[this.activeScope()].index);
  protected readonly counts = computed(() => ({
    project: this.scopes().project.entries.length,
    mine: this.scopes().mine.entries.length,
  }));
  protected readonly limits = computed(() => this.memory()?.limits ?? DEFAULT_LIMITS);
  protected readonly indexBudget = computed(() =>
    this.activeScope() === 'mine' ? this.limits().personalIndexBudgetTokens : this.limits().projectIndexBudgetTokens,
  );
  protected readonly indexTokens = computed(() => estimateTokens(this.index()));

  /** The file shown: the one asked for if it exists, else the first file, else the index. */
  protected readonly selected = computed(() => {
    const asked = this.file();
    const entries = this.entries();
    if (asked === INDEX_SLUG) return INDEX_SLUG;
    if (asked && entries.some(e => e.slug === asked)) return asked;
    return entries[0]?.slug ?? INDEX_SLUG;
  });
  protected readonly selectedEntry = computed(() => this.entries().find(e => e.slug === this.selected()) ?? null);

  protected readonly archived = computed(() => this.project()?.status === 'archived');
  /** Owner or editor of an active project. */
  protected readonly canReview = computed(() => {
    const p = this.project();
    return !!p && p.status === 'active' && p.role !== 'viewer';
  });
  /** Pin, restore (and, in 2.8b, edit) in the scope shown: your own always; the project's as an editor. */
  protected readonly canEdit = computed(() => {
    const p = this.project();
    if (!p || p.status !== 'active') return false;
    return this.activeScope() === 'mine' || p.role !== 'viewer';
  });

  constructor() {
    effect(() => {
      const id = this.id();
      untracked(() => void this.load(id));
    });
  }

  protected updated(entry: MemoryEntry): Date {
    return parseIso(entry.updated);
  }

  private async load(id: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    this.unavailable.set(null);
    this.memory.set(null);
    this.scopes.set({ project: EMPTY, mine: EMPTY });
    this.pending.set(0);
    try {
      this.project.set(await firstValueFrom(this.api.get(id)));
    } catch (err) {
      if (id !== this.id()) return;
      this.error.set(
        isUnavailable(err)
          ? 'This project doesn’t exist, or you’re not a member of it.'
          : projectErrorMessage(err, 'This project could not be loaded.'),
      );
      this.loading.set(false);
      return;
    }
    try {
      const memory = await firstValueFrom(this.api.memory(id));
      if (id !== this.id()) return;
      this.memory.set(memory);
      if (!memory.sharedSpaceId) this.unavailable.set('This project has no shared memory yet.');
      await Promise.all([this.loadScope('project'), this.loadScope('mine'), this.loadPending()]);
    } catch (err) {
      if (id !== this.id()) return;
      this.unavailable.set(
        err instanceof HttpErrorResponse && (err.status === 404 || err.status === 409)
          ? 'Memory isn’t available for this project.'
          : projectErrorMessage(err, 'Memory could not be loaded.'),
      );
    } finally {
      if (id === this.id()) this.loading.set(false);
    }
  }

  /** A scope's files and index. Re-read after anything that saves (a restore, an approval). */
  private async loadScope(scope: MemoryScope): Promise<void> {
    const memory = this.memory();
    const spaceId = scope === 'mine' ? memory?.personalSpaceId : memory?.sharedSpaceId;
    if (!spaceId) return;
    try {
      const [entries, index] = await Promise.all([
        firstValueFrom(this.api.memoryEntries(spaceId)),
        firstValueFrom(this.api.memoryIndex(spaceId)).catch(() => ({ content: '' })),
      ]);
      this.scopes.update(s => ({ ...s, [scope]: { entries: entries.entries, index: index.content } }));
      this.scopeError.set(null);
    } catch (err) {
      this.scopeError.set(projectErrorMessage(err, 'These memory files could not be loaded.'));
    }
  }

  private async loadPending(): Promise<void> {
    const id = this.id();
    if (!this.memory()?.sharedSpaceId) return;
    try {
      const response = await firstValueFrom(this.api.proposals(id, 'pending'));
      if (id === this.id()) this.pending.set(response.proposals.length);
    } catch {
      // The count is a hint on a button; the review view reports its own failure.
    }
  }

  protected onDecided(outcome: ReviewOutcome): void {
    this.pending.update(n => Math.max(0, n - 1));
    if (outcome.applied) void this.loadScope('project');
  }

  /** The restored item's file has a new version; the archive stays open for the next one. */
  protected onRestored(): void {
    void this.loadScope(this.activeScope());
  }
}
