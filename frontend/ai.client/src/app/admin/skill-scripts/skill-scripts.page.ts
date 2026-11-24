import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DatePipe } from '@angular/common';
import { ActivatedRoute, Router } from '@angular/router';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroMagnifyingGlass, heroNoSymbol, heroPlayCircle } from '@ng-icons/heroicons/outline';
import { SkillScriptReview, SkillScriptsDetail, SkillScriptVersion } from './models/skill-scripts.models';
import { SkillScriptsHttpService, skillScriptsErrorMessage } from './services/skill-scripts-http.service';

const METHOD_LABELS: Record<string, string> = {
  review: 'Reviewed',
  update: 'Reviewed on update',
  authored: 'Bound by its author',
  backfill: 'Backfilled',
};

const FIELD =
  'block w-full rounded-xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500';
const PRIMARY_BUTTON =
  'inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:opacity-90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50';
const DANGER_BUTTON =
  'inline-flex items-center gap-2 rounded-2xl bg-state-danger-600 px-4 py-2 text-sm/6 font-semibold text-white hover:bg-state-danger-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-600 disabled:cursor-not-allowed disabled:opacity-50';
const CHECKBOX =
  'size-4 cursor-pointer rounded accent-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:accent-primary-500';

/**
 * Skill Scripts (skill-code-sandbox §8.10): one skill's scripts, whoever wrote
 * them, where each version is reviewed, and the admin switch that turns them
 * off everywhere, for every version or only some. The Runtime reads the switch
 * on every script call, so it takes effect on the next one.
 *
 * Reached by skill id (`?skillId=`), from a report or an audit record.
 */
@Component({
  selector: 'app-skill-scripts-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, DatePipe],
  providers: [provideIcons({ heroMagnifyingGlass, heroNoSymbol, heroPlayCircle })],
  host: { class: 'block' },
  template: `
    <div class="mb-6">
      <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Skill Scripts</h1>
      <p class="mt-1 max-w-3xl text-gray-600 dark:text-gray-400">
        Look up any skill by its id to read its scripts, see which Agents and projects allowed each version,
        and turn its scripts off everywhere. Only an administrator can turn back on what an administrator
        turned off.
      </p>
    </div>

    <form class="mb-8 flex max-w-xl items-end gap-3" (submit)="lookUp($event)" role="search">
      <div class="min-w-0 flex-1">
        <label for="skill-scripts-id" class="mb-1 block text-sm/6 font-medium text-gray-900 dark:text-white">Skill id</label>
        <input
          id="skill-scripts-id"
          name="skillId"
          type="text"
          autocomplete="off"
          spellcheck="false"
          [class]="field"
          [value]="query()"
          (input)="query.set($any($event.target).value)"
          placeholder="pdf_forms"
        />
      </div>
      <button type="submit" [class]="primaryButton" [disabled]="!query().trim() || loading()">
        <ng-icon name="heroMagnifyingGlass" class="size-4" aria-hidden="true" />
        Look up
      </button>
    </form>

    @if (loading()) {
      <p role="status" class="text-sm/6 text-gray-600 dark:text-gray-400">Loading…</p>
    }
    @if (error(); as message) {
      <p role="alert" class="mb-6 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
        {{ message }}
      </p>
    }

    @if (detail(); as d) {
      <section class="mb-8 rounded-2xl border border-gray-200 p-5 dark:border-white/10" aria-labelledby="skill-scripts-skill">
        <h2 id="skill-scripts-skill" class="text-xl/7 font-semibold text-gray-900 dark:text-white">{{ d.displayName }}</h2>
        <dl class="mt-3 grid grid-cols-1 gap-x-8 gap-y-2 text-sm/6 sm:grid-cols-2">
          <div><dt class="inline text-gray-500 dark:text-gray-400">Id </dt><dd class="inline font-mono text-gray-900 dark:text-white">{{ d.skillId }}</dd></div>
          <div><dt class="inline text-gray-500 dark:text-gray-400">Status </dt><dd class="inline text-gray-900 dark:text-white">{{ d.status }}</dd></div>
          <div>
            <dt class="inline text-gray-500 dark:text-gray-400">Author </dt>
            <dd class="inline text-gray-900 dark:text-white" data-testid="author">
              @if (d.adminCatalog) { Admin catalog } @else { {{ d.authorName || 'Unknown' }} <span class="font-mono text-xs text-gray-500">({{ d.ownerId }})</span> }
            </dd>
          </div>
          <div>
            <dt class="inline text-gray-500 dark:text-gray-400">Provenance </dt>
            <dd class="inline text-gray-900 dark:text-white" data-testid="provenance">{{ provenanceLabel() }}</dd>
          </div>
          <div>
            <dt class="inline text-gray-500 dark:text-gray-400">Live version </dt>
            <dd class="inline font-mono text-gray-900 dark:text-white">{{ d.scriptsHash ? short(d.scriptsHash) : 'No scripts' }}</dd>
          </div>
        </dl>
      </section>

      <section class="mb-8" aria-labelledby="skill-scripts-switch">
        <h2 id="skill-scripts-switch" class="mb-3 text-lg/7 font-semibold text-gray-900 dark:text-white">Switch</h2>
        <p
          class="mb-4 rounded-2xl px-4 py-3 text-sm/6"
          [class]="d.switch.off
            ? 'bg-state-danger-50 text-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-200'
            : 'bg-state-success-50 text-state-success-900 dark:bg-state-success-900/20 dark:text-state-success-200'"
          data-testid="switch-state"
        >
          @if (d.switch.off) {
            {{ d.switch.by === 'admin' ? 'An administrator' : 'The author' }}
            @if (d.switch.byName) { ({{ d.switch.byName }}) }
            turned off
            {{ d.switch.hashes.length ? 'these versions: ' + shortList(d.switch.hashes) : 'every version' }}
            @if (d.switch.at) { on {{ d.switch.at | date: 'mediumDate' }} }.
            @if (d.switch.reason) { Reason: “{{ d.switch.reason }}”. }
          } @else {
            Scripts can run wherever they are allowed.
          }
        </p>

        <fieldset class="mb-4">
          <legend class="mb-2 text-sm/6 font-medium text-gray-900 dark:text-white">Turn off</legend>
          <div class="flex flex-col gap-2 text-sm/6 text-gray-700 dark:text-gray-300">
            <label class="flex items-center gap-2">
              <input type="radio" name="switch-scope" [class]="checkbox" [checked]="scope() === 'all'" (change)="scope.set('all')" />
              Every version
            </label>
            <label class="flex items-center gap-2">
              <input type="radio" name="switch-scope" [class]="checkbox" [checked]="scope() === 'some'" (change)="scope.set('some')" [disabled]="!d.versions.length" />
              Only the versions ticked below
            </label>
          </div>
        </fieldset>

        <label for="skill-scripts-reason" class="mb-1 block text-sm/6 font-medium text-gray-900 dark:text-white">Reason</label>
        <textarea
          id="skill-scripts-reason"
          rows="2"
          maxlength="500"
          [class]="field + ' mb-1 max-w-xl'"
          [value]="reason()"
          (input)="reason.set($any($event.target).value)"
        ></textarea>
        <p class="mb-4 text-xs/5 text-gray-500 dark:text-gray-400">Recorded in the audit log with the switch.</p>

        @if (saveError(); as message) {
          <p role="alert" class="mb-4 text-sm/6 text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
        }
        <div class="flex flex-wrap gap-3">
          <button type="button" [class]="dangerButton" [disabled]="!canTurnOff()" (click)="turnOff()" data-testid="turn-off">
            <ng-icon name="heroNoSymbol" class="size-4" aria-hidden="true" />
            {{ saving() ? 'Saving…' : 'Turn scripts off' }}
          </button>
          @if (d.switch.off) {
            <button type="button" [class]="primaryButton" [disabled]="!reason().trim() || saving()" (click)="turnOn()" data-testid="turn-on">
              <ng-icon name="heroPlayCircle" class="size-4" aria-hidden="true" />
              Turn scripts back on
            </button>
          }
        </div>
      </section>

      <section class="mb-8" aria-labelledby="skill-scripts-versions">
        <h2 id="skill-scripts-versions" class="mb-3 text-lg/7 font-semibold text-gray-900 dark:text-white">Versions and where they are allowed</h2>
        @if (!d.versions.length) {
          <p class="text-sm/6 text-gray-600 dark:text-gray-400">This skill has no scripts and no reviews.</p>
        }
        <ul class="flex flex-col gap-4">
          @for (v of d.versions; track v.scriptsHash) {
            <li class="rounded-2xl border border-gray-200 p-4 dark:border-white/10" [attr.data-testid]="'version-' + short(v.scriptsHash)">
              <div class="mb-2 flex flex-wrap items-center gap-3">
                <label class="flex items-center gap-2 text-sm/6 font-medium text-gray-900 dark:text-white">
                  <input
                    type="checkbox"
                    [class]="checkbox"
                    [checked]="selected().has(v.scriptsHash)"
                    (change)="toggle(v.scriptsHash)"
                    [disabled]="scope() !== 'some'"
                    [attr.aria-label]="'Select version ' + short(v.scriptsHash)"
                  />
                  <span class="font-mono">{{ short(v.scriptsHash) }}</span>
                </label>
                @if (v.isCurrent) {
                  <span class="rounded-full bg-state-info-50 px-2 text-xs/5 font-medium text-state-info-900 dark:bg-state-info-900/20 dark:text-state-info-200">Live</span>
                }
                @if (v.switchedOff) {
                  <span class="rounded-full bg-state-danger-50 px-2 text-xs/5 font-medium text-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-200">Off</span>
                }
              </div>
              @if (v.reviews.length) {
                <table class="w-full text-left text-sm/6">
                  <thead class="text-gray-500 dark:text-gray-400">
                    <tr><th scope="col" class="py-1 pr-4 font-medium">Allowed in</th><th scope="col" class="py-1 pr-4 font-medium">By</th><th scope="col" class="py-1 pr-4 font-medium">When</th><th scope="col" class="py-1 font-medium">How</th></tr>
                  </thead>
                  <tbody class="text-gray-900 dark:text-gray-100">
                    @for (r of v.reviews; track r.scopeType + r.scopeId) {
                      <tr>
                        <td class="py-1 pr-4">{{ placeLabel(r) }}</td>
                        <td class="py-1 pr-4">{{ r.reviewerName || r.reviewedBy }}</td>
                        <td class="py-1 pr-4">{{ r.reviewedAt ? (r.reviewedAt | date: 'mediumDate') : '—' }}</td>
                        <td class="py-1">{{ methodLabel(r) }}@if (r.version !== null) { · v{{ r.version }} }</td>
                      </tr>
                    }
                  </tbody>
                </table>
              } @else {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">Not allowed in any Agent or project.</p>
              }
            </li>
          }
        </ul>
      </section>

      <section aria-labelledby="skill-scripts-source">
        <h2 id="skill-scripts-source" class="mb-3 text-lg/7 font-semibold text-gray-900 dark:text-white">Scripts in the live version</h2>
        @for (s of d.scripts; track s.path) {
          <details class="mb-3 rounded-2xl border border-gray-200 dark:border-white/10">
            <summary class="cursor-pointer px-4 py-2 text-sm/6 font-medium text-gray-900 focus-visible:outline-2 focus-visible:outline-primary-500 dark:text-white">
              <span class="font-mono">{{ s.path }}</span>
              <span class="ml-2 text-gray-500 dark:text-gray-400">{{ s.size }} bytes · {{ short(s.contentHash) }}</span>
            </summary>
            @if (s.unreadable) {
              <p class="px-4 pb-3 text-sm/6 text-state-danger-700 dark:text-state-danger-300">This file could not be read.</p>
            } @else {
              <pre class="max-h-[32rem] overflow-auto border-t border-gray-200 bg-gray-50 px-4 py-3 text-xs/5 text-gray-900 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100"><code>{{ s.source }}</code></pre>
              @if (s.truncated) {
                <p class="px-4 pb-3 text-xs/5 text-gray-500 dark:text-gray-400">Shown up to the first 256 KB.</p>
              }
            }
          </details>
        } @empty {
          <p class="text-sm/6 text-gray-600 dark:text-gray-400">The live version has no scripts.</p>
        }
      </section>
    }
  `,
})
export class SkillScriptsPage {
  private http = inject(SkillScriptsHttpService);
  private route = inject(ActivatedRoute);
  private router = inject(Router);

  protected readonly field = FIELD;
  protected readonly primaryButton = PRIMARY_BUTTON;
  protected readonly dangerButton = DANGER_BUTTON;
  protected readonly checkbox = CHECKBOX;

  protected readonly query = signal('');
  protected readonly detail = signal<SkillScriptsDetail | null>(null);
  protected readonly loading = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly scope = signal<'all' | 'some'>('all');
  protected readonly selected = signal<ReadonlySet<string>>(new Set());
  protected readonly reason = signal('');
  protected readonly saving = signal(false);
  protected readonly saveError = signal<string | null>(null);

  protected readonly canTurnOff = computed(
    () =>
      !this.saving() &&
      !!this.reason().trim() &&
      (this.scope() === 'all' || this.selected().size > 0),
  );

  protected readonly provenanceLabel = computed(() => {
    const p = this.detail()?.provenance;
    if (!p) return 'Written here';
    const parts = [p['owner'], p['repo'], p['path']].filter(v => typeof v === 'string' && v);
    const where = parts.length ? parts.join('/') : String(p['type'] ?? 'imported');
    return `Imported from ${where}`;
  });

  constructor() {
    this.route.queryParamMap.pipe(takeUntilDestroyed()).subscribe(params => {
      const skillId = (params.get('skillId') ?? '').trim();
      this.query.set(skillId);
      if (skillId) void this.load(skillId);
      else this.detail.set(null);
    });
  }

  protected lookUp(event: Event): void {
    event.preventDefault();
    const skillId = this.query().trim();
    if (!skillId) return;
    if (skillId === this.route.snapshot.queryParamMap.get('skillId')) {
      void this.load(skillId);
      return;
    }
    void this.router.navigate([], { relativeTo: this.route, queryParams: { skillId } });
  }

  protected toggle(hash: string): void {
    const next = new Set(this.selected());
    if (next.has(hash)) next.delete(hash);
    else next.add(hash);
    this.selected.set(next);
  }

  protected turnOff(): void {
    if (!this.canTurnOff()) return;
    const hashes = this.scope() === 'some' ? [...this.selected()].sort() : undefined;
    void this.save({ off: true, ...(hashes ? { hashes } : {}), reason: this.reason().trim() });
  }

  protected turnOn(): void {
    if (!this.reason().trim() || this.saving()) return;
    void this.save({ off: false, reason: this.reason().trim() });
  }

  protected short(hash: string): string {
    return hash.slice(0, 8);
  }

  protected shortList(hashes: string[]): string {
    return hashes.map(h => h.slice(0, 8)).join(', ');
  }

  protected placeLabel(r: SkillScriptReview): string {
    const kind = r.scopeKind === 'project' ? 'Project' : r.scopeKind === 'user' ? 'User' : 'Agent';
    if (r.scopeMissing) return `${kind} ${r.scopeId} (deleted)`;
    return `${kind}: ${r.scopeName || r.scopeId}`;
  }

  protected methodLabel(r: SkillScriptReview): string {
    const label = METHOD_LABELS[r.method] ?? r.method;
    return r.state === 'approved' ? label : `${label} (${r.state})`;
  }

  private async load(skillId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    this.saveError.set(null);
    try {
      this.setDetail(await this.http.getDetail(skillId));
    } catch (e) {
      this.detail.set(null);
      this.error.set(skillScriptsErrorMessage(e, 'The skill could not be loaded. Try again.'));
    } finally {
      this.loading.set(false);
    }
  }

  private async save(body: { off: boolean; hashes?: string[]; reason: string }): Promise<void> {
    const d = this.detail();
    if (!d) return;
    this.saving.set(true);
    this.saveError.set(null);
    try {
      this.setDetail(await this.http.setSwitch(d.skillId, body));
      this.reason.set('');
    } catch (e) {
      this.saveError.set(skillScriptsErrorMessage(e, 'The switch could not be saved. Try again.'));
    } finally {
      this.saving.set(false);
    }
  }

  private setDetail(detail: SkillScriptsDetail): void {
    this.detail.set(detail);
    const off: SkillScriptVersion[] = detail.versions.filter(v => v.switchedOff);
    const perVersion = detail.switch.off && detail.switch.hashes.length > 0;
    this.scope.set(perVersion ? 'some' : 'all');
    this.selected.set(new Set(perVersion ? off.map(v => v.scriptsHash) : []));
  }
}
