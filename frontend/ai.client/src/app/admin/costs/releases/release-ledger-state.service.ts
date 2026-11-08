import { Injectable, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ReleaseLedgerHttpService } from '../services/release-ledger-http.service';
import {
  LedgerQualityResponse,
  LedgerReadout,
  LedgerSeriesResponse,
  LedgerToolsResponse,
  LedgerUnitKind,
  LedgerVersionsResponse,
} from '../models';
import { defaultUnit, rangeEndingToday, unitChips } from './release-ledger.util';

/** The range choices for the strip, the trend and the quality runs. */
export const RANGE_DAYS = [30, 90, 180] as const;
/** The readout's window choices (D5: default 7, capped by what each side has). */
export const WINDOW_DAYS = [3, 7, 14, 28] as const;

/**
 * State for the Releases tab.
 *
 * In a root service, like `AdminCostStateService`, because the tab panel is
 * destroyed on every switch away (the Chart.js canvas cannot size itself while
 * hidden), and the loaded readout must survive the round trip.
 *
 * Every load is tagged with a generation so a slower, older response never
 * overwrites a newer one when the admin clicks through the strip quickly.
 */
@Injectable({ providedIn: 'root' })
export class ReleaseLedgerStateService {
  private readonly http = inject(ReleaseLedgerHttpService);

  readonly rangeDays = signal<number>(30);
  /** `null` until chosen: then `defaultUnit` decides from the versions in range. */
  private readonly unitOverride = signal<LedgerUnitKind | null>(null);
  readonly windowDays = signal<number>(7);
  readonly candidateKey = signal<string | null>(null);
  readonly baselineKey = signal<string | null>(null);

  readonly versions = signal<LedgerVersionsResponse | null>(null);
  readonly series = signal<LedgerSeriesResponse | null>(null);
  readonly quality = signal<LedgerQualityResponse | null>(null);
  readonly readout = signal<LedgerReadout | null>(null);
  readonly tools = signal<LedgerToolsResponse | null>(null);

  readonly loading = signal(false);
  readonly loadingReadout = signal(false);
  readonly error = signal<string | null>(null);
  readonly readoutError = signal<string | null>(null);
  readonly loaded = signal(false);

  readonly unit = computed<LedgerUnitKind>(() => this.unitOverride() ?? defaultUnit(this.versions()));
  readonly chips = computed(() => unitChips(this.versions(), this.unit()));
  readonly range = computed(() => rangeEndingToday(this.rangeDays(), new Date()));

  private generation = 0;
  private readoutGeneration = 0;

  /** Load once per visit; a later visit keeps what is there. */
  async ensureLoaded(): Promise<void> {
    if (this.loaded() || this.loading()) return;
    await this.load();
  }

  async load(): Promise<void> {
    const generation = ++this.generation;
    this.loading.set(true);
    this.error.set(null);
    try {
      const range = this.range();
      const versions = await firstValueFrom(this.http.getVersions(range));
      if (generation !== this.generation) return;
      this.versions.set(versions);

      const unit = this.unit();
      const [series, quality] = await Promise.all([
        firstValueFrom(this.http.getSeries(range, unit)),
        firstValueFrom(this.http.getQuality(range, unit)),
        this.loadReadout(),
      ]);
      if (generation !== this.generation) return;
      this.series.set(series);
      this.quality.set(quality);
      this.loaded.set(true);
    } catch (err) {
      if (generation !== this.generation) return;
      this.error.set(describeError(err, 'Failed to load the release ledger'));
    } finally {
      if (generation === this.generation) this.loading.set(false);
    }
  }

  /**
   * The comparison. With nothing chosen the endpoint picks the candidate (the
   * newest version with a full day) and its baseline; at build granularity it
   * cannot be asked for "the newest build" by name, so the strip's newest is sent.
   */
  async loadReadout(): Promise<void> {
    const generation = ++this.readoutGeneration;
    this.loadingReadout.set(true);
    this.readoutError.set(null);
    try {
      let version = this.candidateKey() ?? undefined;
      if (!version && this.unit() === 'build') version = this.chips().at(-1)?.key;
      const readout = await firstValueFrom(
        this.http.getReadout({
          version,
          baseline: this.baselineKey() ?? undefined,
          days: this.windowDays(),
        }),
      );
      if (generation !== this.readoutGeneration) return;
      this.readout.set(readout);
      this.candidateKey.set(readout.candidate.key);
      this.baselineKey.set(readout.baseline?.key ?? null);
      void this.loadTools(readout, generation);
    } catch (err) {
      if (generation !== this.readoutGeneration) return;
      this.readout.set(null);
      this.tools.set(null);
      this.readoutError.set(describeError(err, 'Failed to load the readout'));
    } finally {
      if (generation === this.readoutGeneration) this.loadingReadout.set(false);
    }
  }

  /** The tools route over the candidate's window: what the census does not measure yet. */
  private async loadTools(readout: LedgerReadout, generation: number): Promise<void> {
    const [from, to] = readout.candidate.window;
    try {
      const tools = await firstValueFrom(
        this.http.getTools({ from: from ?? undefined, to: to ?? undefined }, readout.candidate.key),
      );
      if (generation === this.readoutGeneration) this.tools.set(tools);
    } catch {
      // A footnote's source; the table renders from the readout without it.
      if (generation === this.readoutGeneration) this.tools.set(null);
    }
  }

  selectCandidate(key: string): void {
    if (key === this.candidateKey()) return;
    this.candidateKey.set(key);
    // The endpoint's default baseline is the unit before the candidate.
    this.baselineKey.set(null);
    void this.loadReadout();
  }

  selectBaseline(key: string | null): void {
    if (key === this.baselineKey()) return;
    this.baselineKey.set(key);
    void this.loadReadout();
  }

  setWindowDays(days: number): void {
    if (days === this.windowDays()) return;
    this.windowDays.set(days);
    void this.loadReadout();
  }

  setUnit(unit: LedgerUnitKind): void {
    if (unit === this.unit()) return;
    this.unitOverride.set(unit);
    this.candidateKey.set(null);
    this.baselineKey.set(null);
    // A version readout under a strip of builds would be read as a build's.
    this.readout.set(null);
    this.tools.set(null);
    void this.load();
  }

  setRangeDays(days: number): void {
    if (days === this.rangeDays()) return;
    this.rangeDays.set(days);
    void this.load();
  }

  async exportNdjson(): Promise<void> {
    const range = this.range();
    try {
      const blob = await firstValueFrom(this.http.exportNdjson(range));
      downloadBlob(blob, `release-ledger-${range.from}-${range.to}.ndjson`);
    } catch (err) {
      this.error.set(describeError(err, 'Failed to export the ledger'));
    }
  }

  /** The readout exactly as the endpoint returned it: the bundle a script would save. */
  exportReadout(): void {
    const readout = this.readout();
    if (!readout) return;
    const blob = new Blob([JSON.stringify(readout, null, 2)], { type: 'application/json' });
    const name = `release-readout-${readout.candidate.key}-vs-${readout.baseline?.key ?? 'none'}.json`;
    downloadBlob(blob, name.replace(/#/g, '_'));
  }
}

function describeError(err: unknown, fallback: string): string {
  if (err instanceof HttpErrorResponse) {
    const detail = (err.error as { detail?: unknown } | null)?.detail;
    if (typeof detail === 'string' && detail) return detail;
    return err.status ? `${fallback} (HTTP ${err.status})` : fallback;
  }
  return err instanceof Error && err.message ? err.message : fallback;
}

function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
