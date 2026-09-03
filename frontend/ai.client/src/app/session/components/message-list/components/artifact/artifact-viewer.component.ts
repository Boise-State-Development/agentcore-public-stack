import {
  ChangeDetectionStrategy,
  Component,
  input,
  output,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { SafeResourceUrl } from '@angular/platform-browser';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { ArtifactSourceComponent } from './artifact-source.component';
import type { ArtifactContent } from '../../../../services/artifacts/artifact-http.service';

/**
 * The body of an artifact view: the sandboxed preview iframe, the code
 * view, their error branches, and the loading skeleton.
 *
 * Purely presentational — it fetches nothing and owns no access
 * control. The parent mints the render URL and loads the source, then
 * hands both down; that is what lets the docked owner panel and the
 * recipient page share one viewer while minting through two different
 * endpoints (`/artifacts/…` vs `/shared-artifacts/…`).
 *
 * Isolation model (unchanged from the panel it was extracted from): the
 * artifact origin is a separate subdomain, the render Lambda and
 * CloudFront stamp a strict CSP, and the iframe carries
 * `sandbox="allow-scripts"` *without* `allow-same-origin`, so the framed
 * document is a null origin and cannot reach the artifact origin's
 * storage or cookies. Do not add `allow-same-origin` here — it would
 * hand attacker-authored markup the artifact origin itself.
 */
@Component({
  selector: 'app-artifact-viewer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, NgTemplateOutlet, ArtifactSourceComponent],
  providers: [provideIcons({ heroExclamationTriangle })],
  template: `
    <div class="relative min-h-0 flex-1">
      @if (view() === 'code') {
        @if (sourceError()) {
          <div
            class="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center"
            role="alert"
          >
            <ng-icon
              name="heroExclamationTriangle"
              class="text-3xl text-amber-500"
              aria-hidden="true"
            />
            <p class="text-sm text-gray-700 dark:text-gray-300">
              {{ sourceError() }}
            </p>
            <button
              type="button"
              class="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 dark:focus-visible:ring-offset-gray-900"
              (click)="retrySource.emit()"
            >
              Try again
            </button>
          </div>
        } @else if (source(); as src) {
          <app-artifact-source
            [content]="src.content"
            [contentType]="src.contentType"
          />
        } @else {
          <ng-container
            [ngTemplateOutlet]="skeleton"
            [ngTemplateOutletContext]="{ label: 'Building source view…' }"
          />
        }
      } @else {
        @if (error()) {
          <div
            class="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center"
            role="alert"
          >
            <ng-icon
              name="heroExclamationTriangle"
              class="text-3xl text-amber-500"
              aria-hidden="true"
            />
            <p class="text-sm text-gray-700 dark:text-gray-300">
              {{ error() }}
            </p>
            <button
              type="button"
              class="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white transition-colors hover:bg-blue-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 dark:focus-visible:ring-offset-gray-900"
              (click)="retry.emit()"
            >
              Try again
            </button>
          </div>
        } @else {
          @if (safeUrl(); as url) {
            <iframe
              [src]="url"
              class="h-full w-full border-0 bg-white"
              [class.pointer-events-none]="inert()"
              [title]="title() || 'Artifact'"
              sandbox="allow-scripts"
              referrerpolicy="no-referrer"
              loading="lazy"
              (load)="iframeLoad.emit()"
            ></iframe>
          }
          @if (!previewReady()) {
            <ng-container
              [ngTemplateOutlet]="skeleton"
              [ngTemplateOutletContext]="{ label: 'Rendering artifact…' }"
            />
          }
        }
      }
    </div>

    <ng-template #skeleton let-label="label">
      <div
        class="absolute inset-0 overflow-hidden bg-white p-8 dark:bg-gray-900"
        role="status"
        [attr.aria-label]="label"
      >
        <div aria-hidden="true" class="mx-auto flex max-w-2xl flex-col gap-6">
          <div class="flex flex-col gap-3">
            <div
              class="skeleton-shimmer h-8 w-1/2 rounded-lg bg-gray-200 dark:bg-gray-700"
            ></div>
            <div
              class="skeleton-shimmer h-4 w-1/4 rounded bg-gray-200 dark:bg-gray-700"
            ></div>
          </div>
          <div class="flex flex-col gap-3">
            <div
              class="skeleton-shimmer h-3.5 w-full rounded bg-gray-200 dark:bg-gray-700"
            ></div>
            <div
              class="skeleton-shimmer h-3.5 w-11/12 rounded bg-gray-200 dark:bg-gray-700"
            ></div>
            <div
              class="skeleton-shimmer h-3.5 w-4/5 rounded bg-gray-200 dark:bg-gray-700"
            ></div>
          </div>
          <div
            class="skeleton-shimmer h-48 w-full rounded-xl bg-gray-200 dark:bg-gray-700"
          ></div>
          <div class="flex flex-col gap-3">
            <div
              class="skeleton-shimmer h-3.5 w-full rounded bg-gray-200 dark:bg-gray-700"
            ></div>
            <div
              class="skeleton-shimmer h-3.5 w-10/12 rounded bg-gray-200 dark:bg-gray-700"
            ></div>
            <div
              class="skeleton-shimmer h-3.5 w-2/3 rounded bg-gray-200 dark:bg-gray-700"
            ></div>
          </div>
        </div>
        <span class="sr-only">{{ label }}</span>
      </div>
    </ng-template>
  `,
  styles: `
    :host {
      display: contents;
    }
    .skeleton-shimmer {
      background-image: linear-gradient(
        90deg,
        transparent 0%,
        rgba(255, 255, 255, 0.45) 50%,
        transparent 100%
      );
      background-size: 220% 100%;
      background-repeat: no-repeat;
      animation: artifact-skeleton-shimmer 1.5s ease-in-out infinite;
    }
    @keyframes artifact-skeleton-shimmer {
      0% {
        background-position: 130% 0;
      }
      100% {
        background-position: -130% 0;
      }
    }
    @media (prefers-reduced-motion: reduce) {
      .skeleton-shimmer {
        animation: none;
      }
    }
  `,
})
export class ArtifactViewerComponent {
  /** Artifact-origin URL carrying the render token. Null while minting. */
  readonly safeUrl = input<SafeResourceUrl | null>(null);
  /** Accessible name for the iframe. */
  readonly title = input<string>('');
  readonly view = input<'preview' | 'code'>('preview');
  readonly source = input<ArtifactContent | null>(null);
  /** Preview-path failure message; null when healthy. */
  readonly error = input<string | null>(null);
  /** Code-path failure message; null when healthy. */
  readonly sourceError = input<string | null>(null);
  /**
   * Skeleton clears only when the parent says the preview has actually
   * painted — a minted URL alone is not enough.
   */
  readonly previewReady = input<boolean>(false);
  /**
   * Disables pointer events on the iframe. The panel sets it while the
   * resize handle is being dragged, since the iframe would otherwise
   * swallow the pointer stream.
   */
  readonly inert = input<boolean>(false);

  readonly retry = output<void>();
  readonly retrySource = output<void>();
  readonly iframeLoad = output<void>();
}
