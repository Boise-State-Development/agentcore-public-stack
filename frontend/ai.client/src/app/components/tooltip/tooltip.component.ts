import {
  Component,
  ChangeDetectionStrategy,
  signal,
  TemplateRef,
} from '@angular/core';
import { NgTemplateOutlet } from '@angular/common';
import { TooltipPosition } from './tooltip.directive';

@Component({
  selector: 'app-tooltip',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgTemplateOutlet],
  host: {
    'role': 'tooltip',
    '[id]': 'id()',
    '[attr.aria-hidden]': 'false',
  },
  template: `
    <div
      class="tooltip-content relative max-w-xs whitespace-nowrap rounded-md bg-gray-900 px-2 py-1 text-xs/4 font-medium text-white shadow-md dark:bg-gray-700"
      [class]="positionClasses()">
      @if (template()) {
        <ng-container *ngTemplateOutlet="template()" />
      } @else {
        {{ content() }}
      }
      <div
        class="absolute size-1.5 rotate-45 bg-gray-900 dark:bg-gray-700"
        [class]="arrowClasses()">
      </div>
    </div>
  `,
  styles: [`
    :host {
      display: block;
      pointer-events: none;
    }

    .tooltip-content {
      animation: fadeIn 150ms ease-out;
    }

    @keyframes fadeIn {
      from {
        opacity: 0;
        transform: scale(0.95);
      }
      to {
        opacity: 1;
        transform: scale(1);
      }
    }
  `],
})
export class TooltipComponent {
  readonly content = signal<string>('');
  readonly template = signal<TemplateRef<unknown> | null>(null);
  readonly id = signal<string>('');
  readonly position = signal<TooltipPosition>('top');

  protected positionClasses(): string {
    // Add padding to accommodate the arrow
    const map: Record<TooltipPosition, string> = {
      top: 'mb-1',
      bottom: 'mt-1',
      left: 'mr-1',
      right: 'ml-1',
    };
    return map[this.position()];
  }

  protected arrowClasses(): string {
    const map: Record<TooltipPosition, string> = {
      top: 'bottom-0 left-1/2 -translate-x-1/2 translate-y-1/2',
      bottom: 'top-0 left-1/2 -translate-x-1/2 -translate-y-1/2',
      left: 'right-0 top-1/2 translate-x-1/2 -translate-y-1/2',
      right: 'left-0 top-1/2 -translate-x-1/2 -translate-y-1/2',
    };
    return map[this.position()];
  }
}
