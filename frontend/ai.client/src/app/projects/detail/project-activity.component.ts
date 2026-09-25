import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { DatePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { UserService } from '../../auth/user.service';
import { BindableKind } from '../../agents/models/agent.model';
import { AgentService } from '../../agents/services/agent.service';
import { parseIso } from '../../utils/date';
import { Project, ProjectAuditRecord } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

const PAGE_SIZE = 50;

const ROLE_PHRASES: Record<string, string> = { editor: 'an editor', viewer: 'a viewer', owner: 'the owner' };

/** What an entry says after its actor, and the settings version it cut, if any. */
export interface ActivityLine {
  text: string;
  version: number | null;
}

/** A model, tool or skill's display name for its id; the id itself when unknown. */
export type ActivityLabel = (kind: BindableKind, ref: string) => string;

const idsOnly: ActivityLabel = (_kind, ref) => ref;

function str(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function role(value: unknown): string {
  const r = str(value);
  return ROLE_PHRASES[r] ?? (r ? `a ${r}` : 'a member');
}

function refs(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : [];
}

function list(items: string[]): string {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

/** "added X and removed Y" for a tools/skills save; the kind word is "tool" or "skill". */
function bindingChange(before: string[], after: string[], kind: 'tool' | 'skill', label: ActivityLabel): string {
  const added = after.filter(r => !before.includes(r)).map(r => label(kind, r));
  const removed = before.filter(r => !after.includes(r)).map(r => label(kind, r));
  const parts: string[] = [];
  if (added.length) parts.push(`added the ${kind}${added.length === 1 ? '' : 's'} ${list(added)}`);
  if (removed.length) parts.push(`removed the ${kind}${removed.length === 1 ? '' : 's'} ${list(removed)}`);
  return parts.length ? parts.join(' and ') : `updated the ${kind}s`;
}

/**
 * One audit record as a sentence (without its actor, which the row shows).
 * The trail keeps no instruction text, only the version a save cut, so settings
 * entries point at the version whose diff is in Settings › History. The trail
 * stores model, tool and skill ids; `label` turns them into display names.
 */
export function describeActivity(record: ProjectAuditRecord, label: ActivityLabel = idsOnly): ActivityLine {
  const before = record.before ?? {};
  const after = record.after ?? {};
  // The audit log stores detail values as strings ("2"), so accept either.
  const rawVersion = Number(after['version']);
  const version = after['version'] != null && Number.isInteger(rawVersion) && rawVersion > 0 ? rawVersion : null;
  const line = (text: string): ActivityLine => ({ text, version });

  switch (record.action) {
    case 'project.created':
      return line('created the project');
    case 'project.updated': {
      const parts = (record.changes ?? []).map(field => {
        if (field === 'name') return `renamed the project to “${str(after['name'])}”`;
        if (field === 'description') return 'updated the description';
        if (field === 'editorsManageMembers') {
          return after['editorsManageMembers'] ? 'let editors manage members' : 'limited managing members to the owner';
        }
        return `changed ${field}`;
      });
      return line(parts.length ? list(parts) : 'updated the project');
    }
    case 'project.archived':
      return line(record.reason ? `archived the project (${record.reason})` : 'archived the project');
    case 'project.restored':
      return line(record.reason ? `restored the project (${record.reason})` : 'restored the project');
    case 'project.deleted':
      return line('deleted the project');
    case 'project.transferred':
      return line(`made ${str(after['ownerEmail'])} the owner`);
    case 'project.member_added':
      return line(`added ${str(after['email'])} as ${role(after['role'])}`);
    case 'project.member_role_changed':
      return line(`made ${str(after['email'])} ${role(after['role'])} (was ${role(before['role'])})`);
    case 'project.member_removed':
      return line(record.reason === 'left' ? 'left the project' : `removed ${str(before['email'])}`);
    case 'project.instructions_updated':
      return line('updated the instructions');
    case 'project.model_updated':
      return line(str(after['modelId']) ? `changed the model to ${label('model', str(after['modelId']))}` : 'changed the model');
    case 'project.tools_updated':
      return line(bindingChange(refs(before['refs']), refs(after['refs']), 'tool', label));
    case 'project.skills_updated':
      return line(bindingChange(refs(before['refs']), refs(after['refs']), 'skill', label));
    case 'project.knowledge_added':
      return line(
        str(after['source']) === 'web'
          ? `started adding pages from ${str(after['url'])}`
          : `added the file ${str(after['filename']) || 'a file'}`,
      );
    case 'project.knowledge_removed':
      return line(`removed the file ${str(before['filename']) || 'a file'}`);
    case 'project.task_shared':
      return line(`shared the task “${str(after['title']) || 'Untitled task'}” with the project`);
    case 'project.task_unshared':
      return line(`stopped sharing the task “${str(after['title']) || 'Untitled task'}”`);
    default:
      return line(record.action.replace(/^project\./, '').replace(/_/g, ' '));
  }
}

/**
 * A project's Activity tab (shared-projects §5, PR-1.7): its audit trail, newest
 * first, for editors and the owner. The detail page only offers the tab to them;
 * the server refuses anyone else.
 */
@Component({
  selector: 'app-project-activity',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DatePipe, RouterLink],
  template: `
    <section class="max-w-3xl" aria-labelledby="activity-heading">
      <h2 id="activity-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Activity</h2>
      <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Who changed what in this project. Only editors and the owner can see this.
      </p>

      @if (error()) {
        <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
      }

      @if (loading() && entries().length === 0) {
        <div class="mt-4 h-32 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
      } @else if (loaded() && entries().length === 0 && !error()) {
        <div class="mt-4 rounded-2xl border border-dashed border-gray-300 p-6 text-center dark:border-gray-700">
          <p class="text-sm/6 text-gray-600 dark:text-gray-400">Nothing has happened here yet.</p>
        </div>
      } @else if (entries().length > 0) {
        <ul class="mt-4 divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
          @for (entry of entries(); track entry.record.auditId) {
            <li class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-4 py-3">
              <p class="min-w-0 flex-1 text-sm/6 text-gray-700 dark:text-gray-300">
                <span class="font-medium text-gray-900 dark:text-white">{{ entry.actor }}</span>
                {{ entry.line.text }}.
                @if (entry.line.version !== null) {
                  <a
                    [routerLink]="['/projects', project().projectId, 'settings']"
                    class="rounded-sm font-medium whitespace-nowrap text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-50"
                  >Version {{ entry.line.version }}</a>
                }
              </p>
              <time class="shrink-0 text-xs/5 text-gray-600 dark:text-gray-400" [attr.datetime]="entry.record.timestamp">
                {{ entry.at | date: 'MMM d, y, h:mm a' }}
              </time>
            </li>
          }
        </ul>
        @if (nextCursor()) {
          <button
            type="button"
            (click)="loadMore()"
            [disabled]="loading()"
            class="mt-3 rounded-2xl border border-gray-300 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-60 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            {{ loading() ? 'Loading…' : 'Show older' }}
          </button>
        }
      }
    </section>
  `,
})
export class ProjectActivityComponent {
  private api = inject(ProjectApiService);
  private user = inject(UserService);
  private agents = inject(AgentService);

  readonly project = input.required<Project>();

  private readonly records = signal<ProjectAuditRecord[]>([]);
  protected readonly nextCursor = signal<string | null>(null);
  protected readonly loading = signal(false);
  protected readonly loaded = signal(false);
  protected readonly error = signal<string | null>(null);

  /**
   * Display names by `kind:ref`, from the bindable palettes Settings loads (memoised
   * in `AgentService`, so this usually costs nothing). A ref the viewer's palette
   * doesn't have (a tool they can't use, one since retired) stays an id.
   */
  private readonly labels = signal<ReadonlyMap<string, string>>(new Map());
  private readonly label = computed<ActivityLabel>(() => {
    const names = this.labels();
    return (kind, ref) => names.get(`${kind}:${ref}`) || ref;
  });

  private readonly me = computed(() => (this.user.currentUser()?.email ?? '').toLowerCase());
  protected readonly entries = computed(() =>
    this.records().map(record => ({
      record,
      line: describeActivity(record, this.label()),
      actor: record.actorEmail
        ? record.actorEmail.toLowerCase() === this.me() ? 'You' : record.actorEmail
        : 'Someone',
      at: parseIso(record.timestamp),
    })),
  );

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    void this.loadLabels();
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.records.set([]);
        this.loaded.set(false);
        void this.load(id, null);
      });
    });
  }

  private async loadLabels(): Promise<void> {
    const kinds: BindableKind[] = ['model', 'tool', 'skill'];
    const palettes = await Promise.all(kinds.map(kind => this.agents.loadBindable(kind)));
    const names = new Map<string, string>();
    palettes.forEach((items, i) => items.forEach(item => names.set(`${kinds[i]}:${item.ref}`, item.label)));
    this.labels.set(names);
  }

  protected loadMore(): void {
    void this.load(this.projectId(), this.nextCursor());
  }

  private async load(projectId: string, cursor: string | null): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const page = await firstValueFrom(this.api.audit(projectId, PAGE_SIZE, cursor));
      if (projectId !== this.projectId()) return;
      this.records.update(list => (cursor ? [...list, ...page.records] : page.records));
      this.nextCursor.set(page.nextCursor ?? null);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'The activity could not be loaded.'));
    } finally {
      this.loading.set(false);
      this.loaded.set(true);
    }
  }
}
