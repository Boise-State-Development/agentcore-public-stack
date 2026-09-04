import {
  ChangeDetectionStrategy,
  Component,
  OnDestroy,
  computed,
  inject,
  signal,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowUpTray, heroXMark } from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import { AgentApiService } from '../services/agent-api.service';
import { AgentIconResponse } from '../models/store.model';
import { AgentIconComponent } from './agent-icon.component';
import { DialogDismissDirective } from '../../components/dialog/dialog-dismiss.directive';

export interface AgentIconDialogData {
  agentId: string;
  agentName: string;
  emoji?: string;
  /** The icon on the record now, if any. */
  iconUrl?: string;
}

/** The record's new icon state, or `undefined` if cancelled. */
export type AgentIconDialogResult = AgentIconResponse | undefined;

/** Mirrors the server's D5 gate, so the common rejections never cost a round trip. */
const MAX_BYTES = 400 * 1024;
const MIN_SOURCE_PX = 256;
const ACCEPTED = ['image/png', 'image/jpeg'];

/**
 * Upload, replace or remove an Agent's square icon (D5).
 *
 * The preview strip is the reason this is a dialog rather than a bare file input. An
 * icon is authored at a size nobody browses at: it reads beautifully at 84px and turns
 * to mud at 28px, where fine detail and thin type disappear into four hundred pixels.
 * So all four store sizes render at their true pixel size, live, before the upload — the
 * small ones are the ones worth looking at.
 *
 * Removing shows the same strip with the generated gradient, so "revert to default" is a
 * thing the author can *see* rather than a thing they have to risk. Per D5 that gradient
 * is the designed default, not an absence.
 *
 * Client-side checks mirror the server's and are not trusted by it: the server re-derives
 * every one of them, and does the thing a browser cannot — re-encoding to strip EXIF, so
 * an icon cropped from a phone photo does not publish its GPS coordinates.
 */
@Component({
  selector: 'app-agent-icon-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDismissDirective, AgentIconComponent, NgIcon],
  providers: [provideIcons({ heroArrowUpTray, heroXMark })],
  host: {
    class: 'block',
    '(keydown.escape)': 'onCancel()',
  },
  template: `
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70"
      aria-hidden="true"
    ></div>

    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-4 sm:items-center sm:p-0"
      appDialogDismiss
      (dismissed)="onCancel()"
    >
      <div
        class="dialog-panel relative flex max-h-[90vh] w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:my-8 sm:max-w-lg dark:border-gray-700 dark:bg-gray-800"
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-icon-title"
        aria-describedby="agent-icon-description"
      >
        <div class="flex items-start justify-between gap-3 px-6 pt-5">
          <div class="min-w-0">
            <h2
              id="agent-icon-title"
              class="text-lg/7 font-semibold text-gray-900 dark:text-white"
            >
              Icon
            </h2>
            <p
              id="agent-icon-description"
              class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400"
            >
              A square image for <span class="font-medium">{{ data.agentName }}</span> —
              512×512 PNG or JPEG, up to 400 KB.
            </p>
          </div>
          <button
            type="button"
            (click)="onCancel()"
            aria-label="Close dialog"
            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
          </button>
        </div>

        <div class="flex-1 overflow-y-auto px-6 py-4">
          <!-- The preview strip: every size the store renders, at true scale. -->
          <div
            class="flex flex-wrap items-end gap-6 rounded-2xl border border-gray-200 bg-gray-50 px-5 py-5 dark:border-gray-700 dark:bg-gray-900/40"
          >
            @for (preview of previews; track preview.size) {
              <div class="flex flex-col items-center gap-2">
                <app-agent-icon
                  [agentId]="data.agentId"
                  [iconUrl]="previewUrl()"
                  [emoji]="data.emoji"
                  [size]="preview.size"
                />
                <span class="text-xs/5 text-gray-500 dark:text-gray-400">{{ preview.label }}</span>
              </div>
            }
          </div>
          <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
            {{ previewUrl() ? previewCaption() : 'No icon yet — this is the default.' }}
          </p>

          <label
            class="mt-5 flex cursor-pointer items-center justify-center gap-2 rounded-2xl border border-dashed border-gray-300 px-4 py-5 text-sm/6 font-medium text-gray-700 hover:border-primary-500 hover:text-primary-accessible focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-primary-500 dark:border-gray-600 dark:text-gray-200 dark:hover:border-primary-400 dark:hover:text-primary-accessible-dark"
          >
            <ng-icon name="heroArrowUpTray" class="size-5" aria-hidden="true" />
            {{ file() ? 'Choose a different image' : 'Choose an image' }}
            <input
              type="file"
              class="sr-only"
              accept="image/png,image/jpeg"
              (change)="onFileChosen($event)"
            />
          </label>

          @if (error(); as message) {
            <p role="alert" class="mt-4 text-sm/6 text-state-danger-700 dark:text-state-danger-400">
              {{ message }}
            </p>
          }
        </div>

        <div
          class="flex items-center justify-between gap-2 border-t border-gray-200 px-6 py-4 dark:border-gray-700"
        >
          @if (data.iconUrl) {
            <button
              type="button"
              (click)="onRemove()"
              [disabled]="busy()"
              class="rounded-2xl px-3 py-2 text-sm/6 font-medium text-gray-500 hover:bg-gray-100 hover:text-gray-800 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-100"
            >
              Remove icon
            </button>
          } @else {
            <span></span>
          }
          <div class="flex gap-2">
            <button
              type="button"
              (click)="onCancel()"
              class="rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
            >
              Cancel
            </button>
            <button
              type="button"
              [disabled]="!canSave()"
              (click)="onSave()"
              class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 "
            >
              {{ busy() ? 'Saving…' : 'Save icon' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class AgentIconDialogComponent implements OnDestroy {
  private dialogRef = inject<DialogRef<AgentIconDialogResult>>(DialogRef);
  private api = inject(AgentApiService);
  readonly data = inject<AgentIconDialogData>(DIALOG_DATA);

  /** 84 first because that is what an author designs for; the small ones follow. */
  readonly previews = [
    { size: 84 as const, label: '84 · detail' },
    { size: 52 as const, label: '52 · card' },
    { size: 40 as const, label: '40 · browse' },
    { size: 28 as const, label: '28 · menu' },
  ];

  readonly file = signal<File | null>(null);
  readonly localUrl = signal<string | null>(null);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  /** The chosen file while there is one, otherwise whatever is on the record. */
  readonly previewUrl = computed(() => this.localUrl() ?? this.data.iconUrl ?? undefined);
  readonly previewCaption = computed(() =>
    this.localUrl() ? 'Not saved yet.' : 'The icon on this agent now.',
  );
  readonly canSave = computed(() => !!this.file() && !this.busy());

  async onFileChosen(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const chosen = input.files?.[0];
    // Clear the input so re-picking the same file after a rejection still fires `change`.
    input.value = '';
    if (!chosen) return;

    this.error.set(null);
    const problem = await this.validate(chosen);
    if (problem) {
      this.error.set(problem);
      return;
    }
    this.revokeLocal();
    this.file.set(chosen);
    this.localUrl.set(URL.createObjectURL(chosen));
  }

  async onSave(): Promise<void> {
    const chosen = this.file();
    if (!chosen) return;
    this.busy.set(true);
    this.error.set(null);
    try {
      const result = await firstValueFrom(this.api.uploadIcon(this.data.agentId, chosen));
      this.dialogRef.close(result);
    } catch (err) {
      this.error.set(this.detail(err) ?? 'Could not save this icon.');
      this.busy.set(false);
    }
  }

  async onRemove(): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      const result = await firstValueFrom(this.api.removeIcon(this.data.agentId));
      this.dialogRef.close(result);
    } catch (err) {
      this.error.set(this.detail(err) ?? 'Could not remove this icon.');
      this.busy.set(false);
    }
  }

  onCancel(): void {
    this.dialogRef.close(undefined);
  }

  ngOnDestroy(): void {
    this.revokeLocal();
  }

  /**
   * The server's own limits, checked here so the usual mistakes (a screenshot, a 4:3
   * photo) are answered instantly. Anything this misses the server still refuses.
   */
  private async validate(file: File): Promise<string | null> {
    if (!ACCEPTED.includes(file.type)) {
      return 'Icons must be a PNG or JPEG image.';
    }
    if (file.size > MAX_BYTES) {
      return `Icons must be 400 KB or smaller (this one is ${Math.round(file.size / 1024)} KB).`;
    }
    const size = await this.dimensions(file);
    if (!size) {
      return 'That file could not be read as an image.';
    }
    const { width, height } = size;
    if (Math.abs(width - height) > 2) {
      return `Icons must be square (this one is ${width}×${height}). Crop it first.`;
    }
    if (Math.min(width, height) < MIN_SOURCE_PX) {
      return `Icons must be at least ${MIN_SOURCE_PX}×${MIN_SOURCE_PX} (this one is ${width}×${height}).`;
    }
    return null;
  }

  private dimensions(file: File): Promise<{ width: number; height: number } | null> {
    return new Promise((resolve) => {
      const url = URL.createObjectURL(file);
      const image = new Image();
      image.onload = () => {
        resolve({ width: image.naturalWidth, height: image.naturalHeight });
        URL.revokeObjectURL(url);
      };
      image.onerror = () => {
        resolve(null);
        URL.revokeObjectURL(url);
      };
      image.src = url;
    });
  }

  private revokeLocal(): void {
    const url = this.localUrl();
    if (url) {
      URL.revokeObjectURL(url);
      this.localUrl.set(null);
    }
  }

  private detail(err: unknown): string | null {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : null;
  }
}
