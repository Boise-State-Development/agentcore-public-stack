import { Injectable, Injector, computed, inject, signal } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../config.service';
import { FEATURES } from '../features';

/**
 * What an attachment may be, as app-api admission and the turn enforce it.
 *
 * Served by `GET /files/limits` (docs/specs/unified-attachment-delivery.md
 * §4.4), which builds it from the backend attachment planner's active limits
 * profile (`attachment_plan.client_limits`). A `null` limit means none applies.
 * `imagesPerMessage` and `pdfMaxPages` belong to the `token` profile and are
 * `null` until it ships.
 */
export interface AttachmentLimits {
  profile: string;
  filesPerMessage: number | null;
  imagesPerMessage: number | null;
  pdfMaxPages: number | null;
  /** Over this a document-class file is attached by reference, never inline. */
  inlineDocumentMaxBytes: number;
  /** One message's combined inline bytes, while the profile has such a budget. */
  inlineBudgetBytes: number | null;
  maxBytes: {
    document: number;
    image: number;
    spreadsheet: number;
    presentation: number;
    /** `null` while large-document attachments are off. */
    largeDocument: number | null;
  };
  largeDocuments: boolean;
  folders: { enabled: boolean; perMessage: number | null };
}

const MB = 1024 * 1024;

/**
 * The one built-in copy of the limits (§9.1): what the composer judges a drop
 * by before `GET /files/limits` answers, and if it fails. It is the backend's
 * `legacy` profile with default settings. A deployment that overrides a limit
 * on the backend sees this only until the first fetch lands.
 *
 * Large documents and folders follow this build's `FEATURES` until then
 * (`AttachmentLimitsService`), so a deployment with them off never offers them,
 * even for a moment.
 */
export const FALLBACK_ATTACHMENT_LIMITS: Readonly<AttachmentLimits> = Object.freeze({
  profile: 'legacy',
  filesPerMessage: 5,
  imagesPerMessage: null,
  pdfMaxPages: null,
  inlineDocumentMaxBytes: 4 * MB,
  inlineBudgetBytes: 7_000_000,
  maxBytes: Object.freeze({
    document: 4 * MB,
    image: 4 * MB,
    spreadsheet: 25 * MB,
    presentation: 25 * MB,
    largeDocument: 50 * MB,
  }),
  largeDocuments: true,
  folders: Object.freeze({ enabled: true, perMessage: 2 }),
});

function positiveOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
}

function positiveOr(value: unknown, fallback: number): number {
  return positiveOrNull(value) ?? fallback;
}

/**
 * Read a `GET /files/limits` body. A missing or malformed field keeps the
 * fallback's value rather than turning into "no limit", except where the
 * server sent an explicit `null`, which is the server saying none applies.
 */
export function parseAttachmentLimits(
  raw: unknown,
  fallback: Readonly<AttachmentLimits> = FALLBACK_ATTACHMENT_LIMITS,
): AttachmentLimits {
  const body = (raw ?? {}) as Record<string, unknown>;
  const bytes = (body['maxBytes'] ?? {}) as Record<string, unknown>;
  const folders = (body['folders'] ?? {}) as Record<string, unknown>;
  const nullable = (value: unknown, previous: number | null): number | null =>
    value === null ? null : value === undefined ? previous : positiveOrNull(value) ?? previous;
  const largeDocument = nullable(bytes['largeDocument'], fallback.maxBytes.largeDocument);
  return {
    profile: typeof body['profile'] === 'string' ? (body['profile'] as string) : fallback.profile,
    filesPerMessage: nullable(body['filesPerMessage'], fallback.filesPerMessage),
    imagesPerMessage: nullable(body['imagesPerMessage'], fallback.imagesPerMessage),
    pdfMaxPages: nullable(body['pdfMaxPages'], fallback.pdfMaxPages),
    inlineDocumentMaxBytes: positiveOr(body['inlineDocumentMaxBytes'], fallback.inlineDocumentMaxBytes),
    inlineBudgetBytes: nullable(body['inlineBudgetBytes'], fallback.inlineBudgetBytes),
    maxBytes: {
      document: positiveOr(bytes['document'], fallback.maxBytes.document),
      image: positiveOr(bytes['image'], fallback.maxBytes.image),
      spreadsheet: positiveOr(bytes['spreadsheet'], fallback.maxBytes.spreadsheet),
      presentation: positiveOr(bytes['presentation'], fallback.maxBytes.presentation),
      largeDocument,
    },
    largeDocuments:
      typeof body['largeDocuments'] === 'boolean'
        ? (body['largeDocuments'] as boolean) && largeDocument !== null
        : fallback.largeDocuments,
    folders: {
      enabled: typeof folders['enabled'] === 'boolean' ? (folders['enabled'] as boolean) : fallback.folders.enabled,
      perMessage: nullable(folders['perMessage'], fallback.folders.perMessage),
    },
  };
}

/** `filesPerMessage` as a count to subtract from: no limit is unbounded. */
export function filesPerMessageOf(limits: Readonly<AttachmentLimits>): number {
  return limits.filesPerMessage ?? Number.POSITIVE_INFINITY;
}

/**
 * The deployment's attachment limits, as a signal.
 *
 * Starts at the fallback (with this build's large-document and folder flags)
 * so the composer can judge a drop at once, then takes `GET /files/limits` on
 * the first `load()`. The fetch is made once per page; a failure keeps the
 * fallback, since the backend re-checks every upload regardless.
 */
@Injectable({ providedIn: 'root' })
export class AttachmentLimitsService {
  private readonly injector = inject(Injector);
  private readonly features = inject(FEATURES);
  private readonly fallback: Readonly<AttachmentLimits> = {
    ...FALLBACK_ATTACHMENT_LIMITS,
    largeDocuments: !!this.features.largeDocumentAttachments,
    folders: { ...FALLBACK_ATTACHMENT_LIMITS.folders, enabled: !!this.features.folderAttachments },
  };
  private readonly _limits = signal<Readonly<AttachmentLimits>>(this.fallback);
  private pending: Promise<void> | null = null;

  /** The current limits: the fallback until the fetch answers. */
  readonly limits = this._limits.asReadonly();
  /** Whether the server's answer has landed (false while on the fallback). */
  readonly loaded = signal(false);
  readonly filesPerMessage = computed(() => filesPerMessageOf(this._limits()));

  /** Fetch the server's limits once. Safe to call from every component that judges a file. */
  load(): Promise<void> {
    if (!this.pending) {
      this.pending = this.fetch();
    }
    return this.pending;
  }

  private async fetch(): Promise<void> {
    // Resolved lazily so a component that only reads `limits` (a file card)
    // needs no HttpClient in its spec.
    const http = this.injector.get(HttpClient);
    const config = this.injector.get(ConfigService);
    try {
      const raw = await firstValueFrom(http.get<unknown>(`${config.appApiUrl()}/files/limits`));
      this._limits.set(parseAttachmentLimits(raw, this.fallback));
      this.loaded.set(true);
    } catch {
      // Keep the fallback; allow a later load() to try again.
      this.pending = null;
    }
  }
}
