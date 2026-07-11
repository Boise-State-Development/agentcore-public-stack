import { Component, ChangeDetectionStrategy, inject, OnInit, OnDestroy } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft, heroHome } from '@ng-icons/heroicons/outline';
import { SidenavService } from '../services/sidenav/sidenav.service';

@Component({
  selector: 'app-not-found',
  imports: [RouterLink, NgIcon],
  providers: [provideIcons({ heroArrowLeft, heroHome })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="nf-shell fixed inset-0 flex items-center justify-center overflow-y-auto">
      <!-- Lava-lamp parallax backdrop + graph-paper grid (mirrors the auth pages) -->
      <div class="nf-bg" aria-hidden="true">
        <div class="nf-lava">
          <!-- Far layer: huge, slow, heavily blurred -->
          <div class="nf-blob nf-blob--a"></div>
          <div class="nf-blob nf-blob--b"></div>
          <!-- Mid layer -->
          <div class="nf-blob nf-blob--c"></div>
          <div class="nf-blob nf-blob--d"></div>
          <!-- Near layer: small, fast, sharper -->
          <div class="nf-blob nf-blob--e"></div>
          <div class="nf-blob nf-blob--f"></div>
        </div>
        <div class="nf-grid"></div>
      </div>

      <main class="relative w-full max-w-md px-4 py-12">
        <!-- Oversized 404 acts as the hero above the frosted card -->
        <div class="mb-8 flex justify-center" aria-label="Error 404">
          <span class="nf-code">404</span>
        </div>

        <div class="nf-card rounded-2xl p-8 text-center">
          <h1 class="text-2xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
            Page Not Found
          </h1>
          <p class="mt-3 text-sm/6 text-gray-600 dark:text-gray-300">
            The page you're looking for has drifted into the void.
          </p>

          <div class="mt-8 flex flex-wrap justify-center gap-3">
            <a
              routerLink="/"
              class="inline-flex items-center gap-2 rounded-lg bg-primary-500 px-5 py-2.5 text-sm font-medium text-white shadow-sm transition hover:bg-primary-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:bg-primary-400 dark:hover:bg-primary-500"
            >
              <ng-icon name="heroHome" class="size-5" />
              <span>Return Home</span>
            </a>
            <button
              type="button"
              (click)="goBack()"
              class="inline-flex items-center gap-2 rounded-lg border border-gray-300 bg-white/60 px-5 py-2.5 text-sm font-medium text-gray-700 transition hover:bg-white/80 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-white/5 dark:text-gray-200 dark:hover:bg-white/10"
            >
              <ng-icon name="heroArrowLeft" class="size-5" />
              <span>Go Back</span>
            </button>
          </div>
        </div>
      </main>
    </div>
  `,
  styles: `
    :host {
      display: block;
    }

    /* ---------- Background canvas ---------- */
    .nf-shell {
      background:
        radial-gradient(120% 80% at 0% 0%, color-mix(in oklab, var(--color-primary-50) 70%, white) 0%, transparent 60%),
        radial-gradient(120% 80% at 100% 100%, color-mix(in oklab, var(--color-primary-100) 60%, white) 0%, transparent 55%),
        var(--color-gray-50);
    }

    :host-context(html.dark) .nf-shell {
      background:
        radial-gradient(120% 80% at 0% 0%, color-mix(in oklab, var(--color-primary-900) 50%, black) 0%, transparent 60%),
        radial-gradient(120% 80% at 100% 100%, color-mix(in oklab, var(--color-primary-800) 35%, black) 0%, transparent 55%),
        var(--color-gray-900);
    }

    .nf-bg {
      position: absolute;
      inset: 0;
      overflow: hidden;
      pointer-events: none;
    }

    /* The .nf-lava wrapper holds the morphing blobs, isolated from the grid. */
    .nf-lava {
      position: absolute;
      inset: 0;
      overflow: hidden;
    }

    .nf-blob {
      position: absolute;
      will-change: transform, border-radius;
      /* Asymmetric radius gives an organic, non-circular blob silhouette;
         keyframes morph these values so the surface "wobbles" as it rises. */
      border-radius: 58% 42% 60% 40% / 50% 55% 45% 50%;
    }

    /* ----- Far tier: huge, slow, heavy blur, low opacity ----- */
    .nf-blob--a {
      width: 70vw;
      height: 86vw;
      max-width: 880px;
      max-height: 1080px;
      bottom: -38vw;
      left: -18vw;
      filter: blur(110px);
      opacity: 0.4;
      background: radial-gradient(circle at 35% 35%, var(--color-primary-400), var(--color-primary-700) 60%, transparent 78%);
      animation:
        nf-rise-a 52s ease-in-out infinite alternate,
        nf-morph-a 28s ease-in-out infinite alternate;
    }

    .nf-blob--b {
      width: 62vw;
      height: 76vw;
      max-width: 800px;
      max-height: 960px;
      top: -34vw;
      right: -20vw;
      filter: blur(100px);
      opacity: 0.36;
      background: radial-gradient(circle at 65% 65%, var(--color-primary-500), var(--color-primary-800) 65%, transparent 82%);
      animation:
        nf-rise-b 60s ease-in-out infinite alternate,
        nf-morph-b 32s ease-in-out infinite alternate;
    }

    /* ----- Mid tier: medium, moderate speed/blur ----- */
    .nf-blob--c {
      width: 32vw;
      height: 40vw;
      max-width: 420px;
      max-height: 520px;
      top: 28%;
      left: 42%;
      filter: blur(60px);
      opacity: 0.5;
      background: radial-gradient(circle, color-mix(in oklab, var(--color-primary-300) 75%, white), transparent 72%);
      animation:
        nf-rise-c 30s ease-in-out infinite alternate,
        nf-morph-c 18s ease-in-out infinite alternate;
    }

    .nf-blob--d {
      width: 28vw;
      height: 36vw;
      max-width: 360px;
      max-height: 460px;
      bottom: -12vw;
      right: 18vw;
      filter: blur(55px);
      opacity: 0.55;
      background: radial-gradient(circle at 50% 50%, var(--color-primary-300), var(--color-primary-500) 60%, transparent 80%);
      animation:
        nf-rise-d 26s ease-in-out infinite alternate,
        nf-morph-a 16s ease-in-out infinite alternate -3s;
    }

    /* ----- Near tier: small, fast, sharper, more opaque ----- */
    .nf-blob--e {
      width: 16vw;
      height: 22vw;
      max-width: 220px;
      max-height: 300px;
      top: -6vw;
      left: 32vw;
      filter: blur(32px);
      opacity: 0.65;
      background: radial-gradient(circle at 50% 50%, var(--color-primary-400), var(--color-primary-700) 65%, transparent 82%);
      animation:
        nf-rise-e 14s ease-in-out infinite alternate,
        nf-morph-b 11s ease-in-out infinite alternate -5s;
    }

    .nf-blob--f {
      width: 12vw;
      height: 16vw;
      max-width: 160px;
      max-height: 220px;
      bottom: -4vw;
      left: 14vw;
      filter: blur(26px);
      opacity: 0.7;
      background: radial-gradient(circle at 45% 45%, var(--color-primary-300), var(--color-primary-600) 65%, transparent 84%);
      animation:
        nf-rise-f 11s ease-in-out infinite alternate,
        nf-morph-c 9s ease-in-out infinite alternate -2s;
    }

    :host-context(html.dark) .nf-blob--a { opacity: 0.32; }
    :host-context(html.dark) .nf-blob--b { opacity: 0.28; }
    :host-context(html.dark) .nf-blob--c { opacity: 0.38; }
    :host-context(html.dark) .nf-blob--d { opacity: 0.42; }
    :host-context(html.dark) .nf-blob--e { opacity: 0.5; }
    :host-context(html.dark) .nf-blob--f { opacity: 0.55; }

    .nf-grid {
      position: absolute;
      inset: 0;
      background-image:
        linear-gradient(to right, color-mix(in oklab, var(--color-primary-500) 8%, transparent) 1px, transparent 1px),
        linear-gradient(to bottom, color-mix(in oklab, var(--color-primary-500) 8%, transparent) 1px, transparent 1px);
      background-size: 64px 64px;
      mask-image: radial-gradient(ellipse 70% 60% at 50% 45%, black 30%, transparent 75%);
      -webkit-mask-image: radial-gradient(ellipse 70% 60% at 50% 45%, black 30%, transparent 75%);
      opacity: 0.6;
    }

    :host-context(html.dark) .nf-grid {
      background-image:
        linear-gradient(to right, color-mix(in oklab, var(--color-primary-300) 6%, transparent) 1px, transparent 1px),
        linear-gradient(to bottom, color-mix(in oklab, var(--color-primary-300) 6%, transparent) 1px, transparent 1px);
      opacity: 0.5;
    }

    /* Rise/fall trajectories — vertical travel with gentle horizontal sway and
       squish/stretch via non-uniform scale. Travel distance scales with depth:
       far tier barely budges, near tier traverses most of the viewport — that
       contrast is what sells the parallax. */

    /* Far: minimal travel, lazy sway */
    @keyframes nf-rise-a {
      0%   { transform: translate3d(0, 0, 0) scale(1, 1) rotate(0deg); }
      50%  { transform: translate3d(2vw, -12vh, 0) scale(1.04, 0.96) rotate(4deg); }
      100% { transform: translate3d(-1vw, -22vh, 0) scale(0.97, 1.05) rotate(-3deg); }
    }
    @keyframes nf-rise-b {
      0%   { transform: translate3d(0, 0, 0) scale(1, 1) rotate(0deg); }
      50%  { transform: translate3d(-2vw, 10vh, 0) scale(0.96, 1.05) rotate(-4deg); }
      100% { transform: translate3d(1vw, 20vh, 0) scale(1.05, 0.96) rotate(3deg); }
    }

    /* Mid: moderate travel */
    @keyframes nf-rise-c {
      0%   { transform: translate3d(0, 0, 0) scale(1, 1) rotate(0deg); }
      50%  { transform: translate3d(-5vw, -25vh, 0) scale(1.1, 0.94) rotate(-10deg); }
      100% { transform: translate3d(4vw, -50vh, 0) scale(0.92, 1.1) rotate(8deg); }
    }
    @keyframes nf-rise-d {
      0%   { transform: translate3d(0, 0, 0) scale(1, 1) rotate(0deg); }
      50%  { transform: translate3d(6vw, -35vh, 0) scale(1.05, 0.95) rotate(12deg); }
      100% { transform: translate3d(-3vw, -68vh, 0) scale(0.92, 1.08) rotate(-7deg); }
    }

    /* Near: dramatic travel, snappy squish/stretch */
    @keyframes nf-rise-e {
      0%   { transform: translate3d(0, 0, 0) scale(1, 1) rotate(0deg); }
      50%  { transform: translate3d(8vw, 55vh, 0) scale(0.88, 1.14) rotate(-18deg); }
      100% { transform: translate3d(-6vw, 100vh, 0) scale(1.16, 0.86) rotate(14deg); }
    }
    @keyframes nf-rise-f {
      0%   { transform: translate3d(0, 0, 0) scale(1, 1) rotate(0deg); }
      50%  { transform: translate3d(-9vw, -55vh, 0) scale(1.18, 0.84) rotate(20deg); }
      100% { transform: translate3d(7vw, -105vh, 0) scale(0.85, 1.18) rotate(-16deg); }
    }

    /* Morphing border-radius makes each blob's surface wobble independently of
       its trajectory — the signature lava-lamp "skin" deformation. */
    @keyframes nf-morph-a {
      0%   { border-radius: 58% 42% 60% 40% / 50% 55% 45% 50%; }
      50%  { border-radius: 42% 58% 38% 62% / 60% 40% 60% 40%; }
      100% { border-radius: 50% 50% 65% 35% / 45% 55% 50% 50%; }
    }
    @keyframes nf-morph-b {
      0%   { border-radius: 50% 50% 40% 60% / 55% 45% 55% 45%; }
      50%  { border-radius: 65% 35% 55% 45% / 40% 60% 40% 60%; }
      100% { border-radius: 38% 62% 50% 50% / 60% 50% 50% 40%; }
    }
    @keyframes nf-morph-c {
      0%   { border-radius: 60% 40% 50% 50% / 45% 60% 40% 55%; }
      50%  { border-radius: 40% 60% 65% 35% / 55% 40% 60% 45%; }
      100% { border-radius: 55% 45% 38% 62% / 50% 55% 45% 50%; }
    }

    @media (prefers-reduced-motion: reduce) {
      .nf-blob,
      .nf-blob--a,
      .nf-blob--b,
      .nf-blob--c,
      .nf-blob--d,
      .nf-blob--e,
      .nf-blob--f {
        animation: none;
      }
    }

    /* ---------- Oversized 404 hero ---------- */
    .nf-code {
      font-weight: 800;
      font-size: clamp(6rem, 22vw, 11rem);
      line-height: 0.85;
      letter-spacing: -0.04em;
      background: linear-gradient(
        135deg,
        var(--color-primary-500),
        var(--color-primary-700)
      );
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
      filter: drop-shadow(0 12px 30px color-mix(in oklab, var(--color-primary-700) 30%, transparent));
    }

    :host-context(html.dark) .nf-code {
      background: linear-gradient(
        135deg,
        var(--color-primary-300),
        var(--color-primary-500)
      );
      -webkit-background-clip: text;
      background-clip: text;
      color: transparent;
    }

    /* ---------- Frosted glass card ---------- */
    .nf-card {
      background: color-mix(in oklab, white 65%, transparent);
      backdrop-filter: blur(24px) saturate(160%);
      -webkit-backdrop-filter: blur(24px) saturate(160%);
      border: 1px solid color-mix(in oklab, white 70%, transparent);
      box-shadow:
        0 1px 0 0 rgba(255, 255, 255, 0.6) inset,
        0 20px 50px -20px color-mix(in oklab, var(--color-primary-900) 35%, transparent),
        0 8px 24px -12px rgba(0, 0, 0, 0.15);
    }

    :host-context(html.dark) .nf-card {
      background: color-mix(in oklab, var(--color-gray-900) 55%, transparent);
      border-color: color-mix(in oklab, white 12%, transparent);
      box-shadow:
        0 1px 0 0 rgba(255, 255, 255, 0.06) inset,
        0 20px 50px -20px rgba(0, 0, 0, 0.6),
        0 8px 24px -12px rgba(0, 0, 0, 0.5);
    }
  `
})
export class NotFoundPage implements OnInit, OnDestroy {
  private sidenavService = inject(SidenavService);

  ngOnInit(): void {
    this.sidenavService.hide();
  }

  ngOnDestroy(): void {
    this.sidenavService.show();
  }

  goBack(): void {
    window.history.back();
  }
}
