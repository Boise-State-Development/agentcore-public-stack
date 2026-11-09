import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { LedgerEvalTrack, LedgerEvalsResponse } from '../costs/models';
import { ReleaseLedgerHttpService } from '../costs/services/release-ledger-http.service';
import { rangeEndingToday, trendBands } from '../costs/releases/release-ledger.util';
import { RANGE_DAYS } from '../costs/releases/release-ledger-state.service';
import { TRACKS, familiesOf, latestByTrack, runColumns, runId, trackOf } from './evaluations.util';

/** What a link into the page can preselect (the quality panel's eval lines). */
export interface EvaluationsSelection {
  model?: string | null;
  track?: string | null;
  build?: string | null;
  run?: string | null;
  family?: string | null;
}

export const EVAL_RANGE_DAYS = RANGE_DAYS;

/**
 * State for `/admin/evaluations` (eval-harness spec §16): one read of
 * `GET /admin/costs/ledger/evals` per range and model, and every section
 * derived from it.
 *
 * The model filter is the server's once a model is chosen, so a range holds
 * one model's rows (D22); the first read has none and picks the model with the
 * most runs. The track and build filters are the page's own, because the
 * header shows the latest run of every track for the chosen model.
 */
@Injectable()
export class EvaluationsStateService {
  private readonly http = inject(ReleaseLedgerHttpService);

  readonly rangeDays = signal<number>(30);
  readonly modelId = signal<string | null>(null);
  readonly track = signal<LedgerEvalTrack | null>(null);
  readonly build = signal<string | null>(null);
  readonly runSelection = signal<string | null>(null);
  readonly familySelection = signal<string | null>(null);

  readonly response = signal<LedgerEvalsResponse | null>(null);
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  private request = 0;

  /** The chosen model's runs in range, every track, newest first. */
  readonly modelRuns = computed(() => {
    const model = this.modelId();
    return (this.response()?.runs ?? []).filter(r => !model || r.modelId === model);
  });

  /** Runs of the chosen model per track, for the selector. */
  readonly trackCounts = computed(() => {
    const counts: Record<LedgerEvalTrack, number> = { nightly: 0, release: 0, manual: 0, untracked: 0 };
    for (const run of this.modelRuns()) counts[trackOf(run)]++;
    return counts;
  });

  readonly latest = computed(() => latestByTrack(this.modelRuns()));

  /** The chosen model, track and (optional) build: what every section draws. Newest first. */
  readonly runs = computed(() => {
    const track = this.track();
    const build = this.build();
    return this.modelRuns().filter(r => (!track || trackOf(r) === track) && (!build || r.build === build));
  });

  /** One column per run, oldest first. */
  readonly columns = computed(() => runColumns(this.runs()));
  readonly families = computed(() => familiesOf(this.runs()));
  /**
   * The Releases tab's build bands, kept to the builds a plotted run ran on.
   * On dev every merge is a build and a day can hold several, so the full set
   * stacks a band per merge over each run; the one that matters is the run's own.
   */
  readonly bands = computed(() => {
    const builds = new Set(this.runs().map(r => r.build));
    return trendBands((this.response()?.bands ?? []).filter(b => !b.build || builds.has(b.build)));
  });

  readonly family = computed<string | null>(() => {
    const families = this.families();
    const chosen = this.familySelection();
    return chosen && families.includes(chosen) ? chosen : (families[0] ?? null);
  });

  readonly selectedRun = computed(() => {
    const runs = this.runs();
    const chosen = this.runSelection();
    return runs.find((r, i) => runId(r, i) === chosen) ?? runs[0] ?? null;
  });

  readonly selectedRunId = computed(() => {
    const run = this.selectedRun();
    return run ? runId(run, this.runs().indexOf(run)) : null;
  });

  /** Apply a link's preselection before the first load. */
  select(selection: EvaluationsSelection): void {
    if (selection.model) this.modelId.set(selection.model);
    if (selection.track && (TRACKS as readonly string[]).includes(selection.track)) {
      this.track.set(selection.track as LedgerEvalTrack);
    }
    if (selection.build) this.build.set(selection.build);
    if (selection.run) this.runSelection.set(selection.run);
    if (selection.family) this.familySelection.set(selection.family);
  }

  async load(): Promise<void> {
    const request = ++this.request;
    this.loading.set(true);
    this.error.set(null);
    const range = rangeEndingToday(this.rangeDays(), new Date());
    try {
      const response = await firstValueFrom(
        this.http.getEvals({ ...range, modelId: this.modelId() ?? undefined, unit: 'build' }),
      );
      if (request !== this.request) return;
      this.response.set(response);
      this.chooseDefaults(response);
    } catch (err) {
      if (request !== this.request) return;
      this.error.set(describe(err));
    } finally {
      if (request === this.request) this.loading.set(false);
    }
  }

  setRangeDays(days: number): void {
    this.rangeDays.set(days);
    void this.load();
  }

  setModel(modelId: string): void {
    if (modelId === this.modelId()) return;
    this.modelId.set(modelId);
    this.runSelection.set(null);
    void this.load();
  }

  setTrack(track: LedgerEvalTrack): void {
    this.track.set(track);
    this.runSelection.set(null);
  }

  clearBuild(): void {
    this.build.set(null);
  }

  private chooseDefaults(response: LedgerEvalsResponse): void {
    if (!this.modelId() && response.models.length) this.modelId.set(response.models[0].modelId);
    if (this.track()) return;
    const counts = this.trackCounts();
    const busiest = [...TRACKS].sort((a, b) => counts[b] - counts[a])[0];
    // Nightly is the trend this page exists for; another track only when nightly has none.
    this.track.set(counts.nightly > 0 ? 'nightly' : counts[busiest] > 0 ? busiest : 'nightly');
  }
}

function describe(err: unknown): string {
  if (err instanceof HttpErrorResponse) {
    if (err.status === 404) return 'The release ledger is off in this environment, so there are no evaluation rows to read.';
    if (err.status === 403) return 'Reading evaluations needs the Cost Analytics scope.';
  }
  return 'Could not read the evaluation runs. Try again.';
}
