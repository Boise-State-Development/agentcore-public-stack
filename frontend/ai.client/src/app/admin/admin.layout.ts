import { Component, ChangeDetectionStrategy, computed, inject } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { filter, scan } from 'rxjs/operators';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroBars3, heroShieldCheck } from '@ng-icons/heroicons/outline';
import { SidenavService } from '../services/sidenav/sidenav.service';

/**
 * The admin console shell.
 *
 * Deliberately thin. The console's navigation lives in `AdminNav`, which the
 * app sidenav renders in place of the chat nav for any route under `/admin`
 * (see `ADMIN_CHROME` in `shared/utils/route-chrome.ts`), so this layout is
 * only responsible for giving the active page the whole content area — no
 * second nav column, and no `max-w-7xl` reading-width cap, which `app.html`
 * also drops for admin chrome.
 *
 * `max-w-[100rem]` is a sprawl guard, not that cap: it is wider than any
 * laptop the console is used on, so it changes nothing there, and only stops
 * a 20-column cost table from stretching to 3000px on an ultrawide, where a
 * row's rank and its dollars end up a head-turn apart. Anything narrower
 * would be the reading-width cap this shell exists to drop.
 *
 * The one piece of chrome left is the small-screen bar below: on desktop the
 * sidenav is always present, but on mobile it is an overlay, and the shell's
 * floating hamburger is conditioned on `HeaderService.showContent()` — state
 * the *session page* owns and admin pages never set. Relying on it would make
 * the admin nav reachable or not depending on what the user did before they
 * came here, so the console carries its own opener.
 */
@Component({
  selector: 'app-admin-layout',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterOutlet, NgIcon],
  providers: [provideIcons({ heroBars3, heroShieldCheck })],
  host: { class: 'block' },
  styles: [
    `
    /*
     * Page-entry motion, replayed on every navigation inside the console.
     *
     * Two names for one gesture, and the duplication is the point: a CSS
     * animation restarts only when its animation-name changes, so re-applying
     * the same class to a element that never leaves the DOM does nothing at
     * all. Alternating between two identical keyframe sets makes every
     * navigation a new animation, with no class-off/reflow/class-on dance and
     * no dependence on change-detection ordering.
     *
     * The alternative — keying the router-outlet's wrapper so the DOM node is
     * recreated — would tear down and re-activate the outlet itself, which is
     * a real cost (and a real risk) to pay for a fade.
     *
     * Kept to a 6px rise: the content area is the full width of the shell now,
     * and a large translate on a surface that size reads as the page sliding
     * rather than settling.
     */
    .page-enter-a {
      animation: page-enter-a 300ms cubic-bezier(0.16, 1, 0.3, 1) both;
    }

    .page-enter-b {
      animation: page-enter-b 300ms cubic-bezier(0.16, 1, 0.3, 1) both;
    }

    @keyframes page-enter-a {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @keyframes page-enter-b {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @media (prefers-reduced-motion: reduce) {
      .page-enter-a,
      .page-enter-b {
        animation: none;
      }
    }
  `,
  ],
  template: `
    <div class="flex min-h-dvh flex-col">
      <!-- Small-screen bar: the only way to the admin nav when the sidenav is
           an overlay. Hidden from lg up, where the sidenav is always on. -->
      <div
        class="sticky top-0 z-10 flex h-14 shrink-0 items-center gap-3 border-b border-gray-200 bg-gray-50/80 px-4 backdrop-blur-sm lg:hidden dark:border-white/10 dark:bg-gray-900/50"
      >
        <button
          type="button"
          (click)="sidenavService.open()"
          class="-ml-2 flex size-9 items-center justify-center rounded-md text-gray-500 transition-colors hover:bg-gray-200 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-white"
          aria-label="Open admin navigation"
        >
          <ng-icon name="heroBars3" class="size-6" />
        </button>
        <div class="flex items-center gap-2">
          <ng-icon name="heroShieldCheck" class="size-5 text-gray-400 dark:text-gray-500" />
          <h1 class="text-base/7 font-semibold text-gray-900 dark:text-white">Admin</h1>
        </div>
      </div>

      <main
        class="mx-auto w-full min-w-0 max-w-[100rem] flex-1 px-4 py-8 sm:px-6 lg:px-8"
        [class.page-enter-a]="contentEnterOnA()"
        [class.page-enter-b]="!contentEnterOnA()"
      >
        <router-outlet />
      </main>
    </div>
  `,
})
export class AdminLayout {
  protected sidenavService = inject(SidenavService);
  private router = inject(Router);

  /**
   * Which of the two entry animations the content area is wearing.
   *
   * Flips on every completed navigation, including the one that opened the
   * console — `scan` starts at 0 and the initial value is 0, so the first
   * paint already carries a class and the animation plays on arrival rather
   * than only from the second page onwards.
   */
  private readonly navigationCount = toSignal(
    this.router.events.pipe(
      filter(e => e instanceof NavigationEnd),
      scan(n => n + 1, 0),
    ),
    { initialValue: 0 },
  );

  protected readonly contentEnterOnA = computed(() => this.navigationCount() % 2 === 0);
}
