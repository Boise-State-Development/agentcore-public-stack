import {
  ChangeDetectionStrategy,
  Component,
  forwardRef,
  inject,
  signal,
} from '@angular/core';
import { ControlValueAccessor, NG_VALUE_ACCESSOR } from '@angular/forms';
import {
  CdkConnectedOverlay,
  CdkOverlayOrigin,
  ConnectedPosition,
} from '@angular/cdk/overlay';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroFaceSmile, heroXMark } from '@ng-icons/heroicons/outline';
import { PickerComponent } from '@ctrl/ngx-emoji-mart';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';

/**
 * A reusable emoji picker: a small trigger button that shows the current emoji and opens
 * an `emoji-mart` popup in a CDK connected overlay. Extracted from the create-agent
 * builder's inline emoji field (`agents/agent-form`) so the admin Agent Templates form
 * (and any other form) gets the same popup instead of a raw text input.
 *
 * Implemented as a `ControlValueAccessor`, so it drops into a reactive form with
 * `formControlName="emoji"` exactly like a native input — the form drives its value
 * (including programmatic `patchValue` on edit) and reads selections back. The bound
 * value is the emoji's native glyph (e.g. `🎓`); clearing emits `''`.
 */
@Component({
  selector: 'app-emoji-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, CdkOverlayOrigin, CdkConnectedOverlay, PickerComponent],
  providers: [
    provideIcons({ heroFaceSmile, heroXMark }),
    {
      provide: NG_VALUE_ACCESSOR,
      useExisting: forwardRef(() => EmojiPickerComponent),
      multi: true,
    },
  ],
  template: `
    <div class="relative">
      <button
        type="button"
        cdkOverlayOrigin
        #emojiTrigger="cdkOverlayOrigin"
        (click)="toggle()"
        [disabled]="disabled()"
        [attr.aria-label]="value() ? 'Change emoji' : 'Pick an emoji'"
        class="flex size-10 items-center justify-center rounded-2xl border border-gray-300 bg-white text-xl hover:bg-gray-50 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:hover:bg-gray-700"
      >
        @if (value()) {
          <span>{{ value() }}</span>
        } @else {
          <ng-icon name="heroFaceSmile" class="size-5 text-gray-400" />
        }
      </button>

      <ng-template
        cdkConnectedOverlay
        [cdkConnectedOverlayOrigin]="emojiTrigger"
        [cdkConnectedOverlayOpen]="isOpen()"
        [cdkConnectedOverlayPositions]="positions"
        [cdkConnectedOverlayHasBackdrop]="true"
        cdkConnectedOverlayBackdropClass="cdk-overlay-transparent-backdrop"
        (backdropClick)="close()"
        (detach)="close()"
      >
        <div class="overflow-hidden rounded-2xl shadow-lg ring-1 ring-black/5 dark:ring-white/10">
          <div class="flex items-center justify-between border-b border-gray-200 bg-white px-3 py-2 dark:border-gray-700 dark:bg-gray-800">
            <span class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Select Emoji</span>
            <button
              type="button"
              (click)="close()"
              class="rounded-2xl p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-200"
              aria-label="Close emoji picker"
            >
              <ng-icon name="heroXMark" class="size-4" />
            </button>
          </div>
          <emoji-mart
            [darkMode]="isDarkMode() === 'dark'"
            [perLine]="8"
            [emojiSize]="24"
            [showPreview]="false"
            (emojiSelect)="onSelect($event)"
          />
        </div>
      </ng-template>

      @if (value()) {
        <button
          type="button"
          (click)="clear()"
          class="mt-1 block text-xs/5 text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
        >
          Clear
        </button>
      }
    </div>
  `,
})
export class EmojiPickerComponent implements ControlValueAccessor {
  private readonly themeService = inject(ThemeService);
  protected readonly isDarkMode = this.themeService.theme;

  /** Current emoji glyph bound by the form (`''` = none). */
  protected readonly value = signal<string>('');
  protected readonly disabled = signal(false);
  protected readonly isOpen = signal(false);

  protected readonly positions: ConnectedPosition[] = [
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
    { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -8 },
  ];

  private onChange: (value: string) => void = () => {};
  private onTouched: () => void = () => {};

  // ── ControlValueAccessor ──────────────────────────────────────────────
  writeValue(value: string | null): void {
    this.value.set(value ?? '');
  }
  registerOnChange(fn: (value: string) => void): void {
    this.onChange = fn;
  }
  registerOnTouched(fn: () => void): void {
    this.onTouched = fn;
  }
  setDisabledState(isDisabled: boolean): void {
    this.disabled.set(isDisabled);
  }

  // ── interaction ───────────────────────────────────────────────────────
  protected toggle(): void {
    if (this.disabled()) return;
    this.isOpen.update(o => !o);
  }
  protected close(): void {
    this.isOpen.set(false);
  }
  protected onSelect(event: { emoji: { native: string } }): void {
    const native = event.emoji.native;
    this.value.set(native);
    this.onChange(native);
    this.onTouched();
    this.close();
  }
  protected clear(): void {
    this.value.set('');
    this.onChange('');
    this.onTouched();
  }
}
