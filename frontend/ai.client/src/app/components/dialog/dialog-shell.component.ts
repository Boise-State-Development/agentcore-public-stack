import { ChangeDetectionStrategy, Component, HostAttributeToken, computed, contentChild, inject, input, output, viewChild } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroXMark } from '@ng-icons/heroicons/outline';
import { DialogDescriptionDirective } from './dialog-description.directive';
import { DialogDismissDirective } from './dialog-dismiss.directive';
import { DialogTitleDirective } from './dialog-title.directive';
import { injectHostDialog, parseDialogRole, setHostDialogRole } from './host-dialog';

export type DialogShellSize = 'md' | 'lg' | 'xl';

const WIDTHS: Record<DialogShellSize, string> = {
  md: 'sm:max-w-lg',
  lg: 'sm:max-w-2xl',
  xl: 'sm:max-w-4xl',
};

/**
 * The chrome every CDK dialog in this codebase draws by hand: backdrop, centred
 * panel, title row with a close button, a scrolling body and an optional footer.
 *
 * Use it from a component opened with `Dialog.open(...)`:
 *
 * ```html
 * <app-dialog-shell title="Members" description="…" size="lg" (closed)="dialogRef.close()">
 *   <button dialogActions …>Add</button>     <!-- beside the close button -->
 *   …body…
 *   <div dialogFooter …>…</div>              <!-- pinned under the body -->
 * </app-dialog-shell>
 * ```
 *
 * Two more slots sit in the header: `[dialogIcon]` before the title (the danger badge on a
 * delete confirmation) and `[appDialogDescription]` under it, for a description that needs
 * markup — import `DialogDescriptionDirective` and use it instead of `description`.
 *
 * A confirmation adds the static attribute `dialogRole="alertdialog"`; the container (the
 * element assistive tech treats as the dialog) takes that role however it was opened.
 *
 * Escape and a click outside the panel both emit `closed`; the opener decides what
 * closing means (usually `dialogRef.close(result)`). The panel never grows past the
 * viewport: the body scrolls while the title and footer stay put.
 *
 * **Accessible name.** CDK's container is itself the `role="dialog"` element, and it has
 * no name unless the opener passes one (axe `aria-dialog-name`). Opened through `Dialog`,
 * the shell names that container with its title and describes it with its description
 * (`appDialogTitle` / `appDialogDescription`), and its own panel carries no dialog role,
 * so assistive tech meets one named dialog rather than an unnamed one wrapping a named
 * one. Rendered outside a CDK dialog (a spec, a harness), the panel keeps `role="dialog"`
 * and labels itself.
 *
 * **Focus on open.** CDK focuses the first tabbable element, which is the close button
 * (or a `dialogActions` button) unless the content marks a better one with
 * `cdkFocusInitial`: the first field of a form, Cancel on a destructive confirmation.
 */
@Component({
  selector: 'app-dialog-shell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogDescriptionDirective, DialogDismissDirective, DialogTitleDirective, NgIcon],
  providers: [provideIcons({ heroXMark })],
  host: {
    class: 'block',
    '(keydown.escape)': 'closed.emit()',
  },
  template: `
    <div class="dialog-backdrop fixed inset-0 bg-gray-900/40 dark:bg-gray-900/70" aria-hidden="true"></div>

    <div
      class="fixed inset-0 z-10 flex min-h-full items-end justify-center p-3 sm:items-center sm:p-6"
      appDialogDismiss
      (dismissed)="closed.emit()"
    >
      <div
        class="dialog-panel relative flex max-h-[calc(100dvh-1.5rem)] w-full flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white text-left shadow-xl sm:max-h-[calc(100dvh-3rem)] dark:border-gray-700 dark:bg-gray-800"
        [class]="widthClass()"
        [attr.role]="hosted ? null : 'dialog'"
        [attr.aria-modal]="hosted ? null : 'true'"
        [attr.aria-labelledby]="hosted ? null : heading.id"
        [attr.aria-describedby]="hosted ? null : panelDescriptionId()"
      >
        <div class="flex items-start gap-3 px-6 pt-5 pb-3">
          <ng-content select="[dialogIcon]" />
          <div class="min-w-0 flex-1">
            <h2 appDialogTitle #heading="appDialogTitle" class="text-lg/7 font-semibold text-gray-900 dark:text-white">{{ title() }}</h2>
            @if (description()) {
              <p appDialogDescription class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">{{ description() }}</p>
            }
            <ng-content select="[appDialogDescription]" />
          </div>
          <div class="flex shrink-0 items-center gap-1">
            <ng-content select="[dialogActions]" />
            @if (closeButton()) {
              <button
                type="button"
                (click)="closed.emit()"
                [attr.aria-label]="closeLabel()"
                class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
              >
                <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
              </button>
            }
          </div>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto px-6 pb-5">
          <ng-content />
        </div>

        <ng-content select="[dialogFooter]" />
      </div>
    </div>
  `,
  styles: `
    .dialog-backdrop {
      animation: dialog-shell-backdrop 200ms ease-out;
    }

    .dialog-panel {
      animation: dialog-shell-panel 200ms ease-out;
    }

    @keyframes dialog-shell-backdrop {
      from { opacity: 0; }
      to { opacity: 1; }
    }

    @keyframes dialog-shell-panel {
      from { opacity: 0; transform: translateY(1rem) scale(0.97); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }

    @media (prefers-reduced-motion: reduce) {
      .dialog-backdrop,
      .dialog-panel {
        animation: none;
      }
    }
  `,
})
export class DialogShellComponent {
  readonly title = input.required<string>();
  readonly description = input<string | null | undefined>(null);
  readonly size = input<DialogShellSize>('md');
  /** Off for a dialog the user must answer (an announcement that needs acknowledging). */
  readonly closeButton = input(true);
  readonly closeLabel = input('Close dialog');

  /** Escape, the close button, or a click outside the panel. */
  readonly closed = output<void>();

  protected readonly widthClass = computed(() => WIDTHS[this.size()]);

  private readonly ownDescription = viewChild(DialogDescriptionDirective);
  private readonly projectedDescription = contentChild(DialogDescriptionDirective);
  /** Only read outside a CDK dialog, where the panel describes itself. */
  protected readonly panelDescriptionId = computed(
    () => (this.ownDescription() ?? this.projectedDescription())?.id ?? null,
  );

  private readonly dialog = injectHostDialog();
  /** Opened through CDK's `Dialog`, whose container is the dialog element (a stub `DialogRef` in a spec has none). */
  protected readonly hosted = !!this.dialog;

  constructor() {
    const role = parseDialogRole(inject(new HostAttributeToken('dialogRole'), { optional: true }));
    if (this.dialog && role) setHostDialogRole(this.dialog, role);
  }
}
