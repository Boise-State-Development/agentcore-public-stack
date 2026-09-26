import { DatePipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, effect, inject, input, signal, untracked } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronRight } from '@ng-icons/heroicons/outline';
import { SettingsVersion, SettingsVersionSummary } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

const FIELD_LABELS: Record<string, string> = {
  instructions: 'Instructions',
  bindings: 'Tools & skills',
  modelConfig: 'Model',
  name: 'Name',
  description: 'Description',
};

/**
 * Every saved change to a project's settings, newest first (PR-1.5a).
 *
 * The list is loaded when the section is opened (this component is created then)
 * and each version's diff when that version is expanded, so an unopened history
 * costs nothing.
 */
@Component({
  selector: 'app-project-history',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, DatePipe],
  providers: [provideIcons({ heroChevronRight })],
  template: `
    @if (error()) {
      <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
    } @else if (versions() === null) {
      <div class="h-16 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
    } @else if (versions()!.length === 0) {
      <p class="text-sm/6 text-gray-600 dark:text-gray-400">No changes yet. Each save will be listed here.</p>
    } @else {
      <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
        @for (v of versions(); track v.version) {
          <li>
            <button
              type="button"
              (click)="toggle(v.version)"
              [attr.aria-expanded]="open() === v.version"
              [attr.aria-controls]="'version-' + v.version"
              class="flex w-full items-center gap-3 px-4 py-2.5 text-left text-sm/6 hover:bg-gray-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700/50"
            >
              <ng-icon name="heroChevronRight" class="size-4 shrink-0 text-gray-500 transition-transform dark:text-gray-400" [class.rotate-90]="open() === v.version" aria-hidden="true" />
              <span class="font-medium text-gray-900 dark:text-white">Version {{ v.version }}</span>
              <span class="min-w-0 flex-1 truncate text-gray-600 dark:text-gray-400">
                @if (v.createdByEmail) { {{ labels(v.changes) }} · {{ v.createdByEmail }} } @else { Starting point }
              </span>
              @if (v.createdAt) {
                <span class="shrink-0 text-xs/5 text-gray-600 dark:text-gray-400">{{ v.createdAt | date: 'MMM d, y, h:mm a' }}</span>
              }
            </button>
            @if (open() === v.version) {
              <div [id]="'version-' + v.version" class="border-t border-gray-200 px-4 py-3 dark:border-gray-700">
                @if (detail(); as d) {
                  @if (d.instructionsDiff.length) {
                    <pre class="max-h-80 overflow-auto rounded-xl bg-gray-50 p-3 font-mono text-xs/5 whitespace-pre-wrap dark:bg-gray-900"><code>@for (line of d.instructionsDiff; track $index) {<span [class]="lineClasses(line)">{{ line }}
</span>}</code></pre>
                  }
                  @for (change of otherChanges(d); track change.field) {
                    <p class="mt-2 text-sm/6 text-gray-700 dark:text-gray-300">
                      <span class="font-medium">{{ labels([change.field]) }}</span> changed.
                    </p>
                  }
                  @if (!d.instructionsDiff.length && !otherChanges(d).length) {
                    <p class="text-sm/6 text-gray-600 dark:text-gray-400">The project’s settings when it was created.</p>
                  }
                } @else {
                  <div class="h-10 animate-pulse rounded bg-gray-100 dark:bg-gray-700" aria-busy="true"></div>
                }
              </div>
            }
          </li>
        }
      </ul>
    }
  `,
})
export class ProjectHistoryComponent {
  private api = inject(ProjectApiService);

  readonly projectId = input.required<string>();

  protected readonly versions = signal<SettingsVersionSummary[] | null>(null);
  protected readonly open = signal<number | null>(null);
  protected readonly detail = signal<SettingsVersion | null>(null);
  protected readonly error = signal<string | null>(null);

  constructor() {
    // Opening the section creates this component, so it loads on arrival.
    effect(() => {
      this.projectId();
      untracked(() => void this.load());
    });
  }

  /** Load (or reload, after a save) the version list. */
  async load(): Promise<void> {
    try {
      const response = await firstValueFrom(this.api.versions(this.projectId()));
      this.versions.set(response.versions);
      this.error.set(null);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'History could not be loaded.'));
    }
  }

  protected async toggle(version: number): Promise<void> {
    if (this.open() === version) {
      this.open.set(null);
      return;
    }
    this.open.set(version);
    this.detail.set(null);
    try {
      const d = await firstValueFrom(this.api.version(this.projectId(), version));
      if (this.open() === version) this.detail.set(d);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'That version could not be loaded.'));
    }
  }

  protected labels(fields: string[]): string {
    return fields.map(f => FIELD_LABELS[f] ?? f).join(', ') || 'No changes';
  }

  protected otherChanges(d: SettingsVersion) {
    return d.version > 1 ? d.fieldChanges.filter(c => c.field !== 'instructions') : [];
  }

  /** Colour the +/- lines; hunk headers and context stay neutral. */
  protected lineClasses(line: string): string {
    if (line.startsWith('+++') || line.startsWith('---')) return 'text-gray-600 dark:text-gray-400';
    if (line.startsWith('+')) return 'text-state-success-700 dark:text-state-success-400';
    if (line.startsWith('-')) return 'text-state-danger-600 dark:text-state-danger-400';
    return 'text-gray-600 dark:text-gray-300';
  }
}
