import {
  Directive,
  ElementRef,
  inject,
  input,
  OnDestroy,
  TemplateRef,
  ViewContainerRef,
  signal,
  DestroyRef,
} from '@angular/core';
import {
  Overlay,
  OverlayRef,
  ConnectedPosition,
} from '@angular/cdk/overlay';
import { ComponentPortal } from '@angular/cdk/portal';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { fromEvent } from 'rxjs';
import { filter } from 'rxjs/operators';
import { TooltipComponent } from './tooltip.component';

export type TooltipPosition = 'top' | 'bottom' | 'left' | 'right';

const POSITION_MAP: Record<TooltipPosition, ConnectedPosition[]> = {
  top: [
    { originX: 'center', originY: 'top', overlayX: 'center', overlayY: 'bottom', offsetY: -8 },
    { originX: 'center', originY: 'bottom', overlayX: 'center', overlayY: 'top', offsetY: 8 },
  ],
  bottom: [
    { originX: 'center', originY: 'bottom', overlayX: 'center', overlayY: 'top', offsetY: 8 },
    { originX: 'center', originY: 'top', overlayX: 'center', overlayY: 'bottom', offsetY: -8 },
  ],
  left: [
    { originX: 'start', originY: 'center', overlayX: 'end', overlayY: 'center', offsetX: -8 },
    { originX: 'end', originY: 'center', overlayX: 'start', overlayY: 'center', offsetX: 8 },
  ],
  right: [
    { originX: 'end', originY: 'center', overlayX: 'start', overlayY: 'center', offsetX: 8 },
    { originX: 'start', originY: 'center', overlayX: 'end', overlayY: 'center', offsetX: -8 },
  ],
};

@Directive({
  selector: '[appTooltip]',
  exportAs: 'appTooltip',
  host: {
    '(mouseenter)': 'show()',
    '(mouseleave)': 'hide()',
    '(focus)': 'show()',
    '(blur)': 'hide()',
    '(keydown.escape)': 'hide()',
    '[attr.aria-describedby]': 'tooltipId()',
  },
})
export class TooltipDirective implements OnDestroy {
  private readonly overlay = inject(Overlay);
  private readonly elementRef = inject(ElementRef<HTMLElement>);
  private readonly viewContainerRef = inject(ViewContainerRef);
  private readonly destroyRef = inject(DestroyRef);

  /** The tooltip text content */
  appTooltip = input.required<string>();

  /** Position preference for the tooltip */
  appTooltipPosition = input<TooltipPosition>('top');

  /** Delay before showing tooltip (ms) */
  appTooltipShowDelay = input<number>(200);

  /** Delay before hiding tooltip (ms) */
  appTooltipHideDelay = input<number>(0);

  /** Whether the tooltip is disabled */
  appTooltipDisabled = input<boolean>(false);

  /** Custom template for tooltip content */
  appTooltipTemplate = input<TemplateRef<unknown>>();

  private overlayRef: OverlayRef | null = null;
  private showTimeout: ReturnType<typeof setTimeout> | null = null;
  private hideTimeout: ReturnType<typeof setTimeout> | null = null;
  private tooltipInstance: TooltipComponent | null = null;

  protected readonly tooltipId = signal<string | null>(null);
  private readonly isVisible = signal(false);

  constructor() {
    // Generate unique ID for ARIA
    this.tooltipId.set(`tooltip-${Math.random().toString(36).substring(2, 9)}`);
  }

  show(): void {
    if (this.appTooltipDisabled() || !this.appTooltip()) {
      return;
    }

    this.clearTimeouts();

    this.showTimeout = setTimeout(() => {
      this.createOverlay();
      this.isVisible.set(true);
    }, this.appTooltipShowDelay());
  }

  hide(): void {
    this.clearTimeouts();

    this.hideTimeout = setTimeout(() => {
      this.destroyOverlay();
      this.isVisible.set(false);
    }, this.appTooltipHideDelay());
  }

  toggle(): void {
    if (this.isVisible()) {
      this.hide();
    } else {
      this.show();
    }
  }

  private createOverlay(): void {
    if (this.overlayRef) {
      return;
    }

    const positions = POSITION_MAP[this.appTooltipPosition()];
    const positionStrategy = this.overlay
      .position()
      .flexibleConnectedTo(this.elementRef)
      .withPositions(positions)
      .withPush(true);

    const scrollStrategy = this.overlay.scrollStrategies.reposition();

    this.overlayRef = this.overlay.create({
      positionStrategy,
      scrollStrategy,
      panelClass: 'tooltip-panel',
      hasBackdrop: false,
    });

    const portal = new ComponentPortal(
      TooltipComponent,
      this.viewContainerRef
    );

    const componentRef = this.overlayRef.attach(portal);
    this.tooltipInstance = componentRef.instance;
    this.tooltipInstance.content.set(this.appTooltip());
    this.tooltipInstance.template.set(this.appTooltipTemplate() ?? null);
    this.tooltipInstance.id.set(this.tooltipId()!);
    this.tooltipInstance.position.set(this.appTooltipPosition());

    // Close on outside click or scroll
    this.setupCloseListeners();
  }

  private destroyOverlay(): void {
    if (this.overlayRef) {
      this.overlayRef.dispose();
      this.overlayRef = null;
      this.tooltipInstance = null;
    }
  }

  private setupCloseListeners(): void {
    if (!this.overlayRef) return;

    // Close on Escape key press anywhere
    fromEvent<KeyboardEvent>(document, 'keydown')
      .pipe(
        filter((event) => event.key === 'Escape'),
        takeUntilDestroyed(this.destroyRef)
      )
      .subscribe(() => this.hide());
  }

  private clearTimeouts(): void {
    if (this.showTimeout) {
      clearTimeout(this.showTimeout);
      this.showTimeout = null;
    }
    if (this.hideTimeout) {
      clearTimeout(this.hideTimeout);
      this.hideTimeout = null;
    }
  }

  ngOnDestroy(): void {
    this.clearTimeouts();
    this.destroyOverlay();
  }
}
