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
    <div class="not-found-container">
      <!-- Animated background grid -->
      <div class="grid-background" aria-hidden="true">
        @for (i of gridLines; track i) {
          <div class="grid-line" [style.animation-delay]="i * 0.1 + 's'"></div>
        }
      </div>

      <!-- Floating accent shapes -->
      <div class="accent-shape shape-1" aria-hidden="true"></div>
      <div class="accent-shape shape-2" aria-hidden="true"></div>
      <div class="accent-shape shape-3" aria-hidden="true"></div>

      <main class="content-wrapper">
        <!-- Massive 404 display -->
        <div class="error-display" aria-label="Error 404">
          <span class="digit digit-4-first">4</span>
          <span class="digit digit-0">0</span>
          <span class="digit digit-4-last">4</span>
        </div>

        <!-- Message section -->
        <div class="message-section">
          <h1 class="title">Page Not Found</h1>
          <p class="subtitle">
            The page you're looking for has drifted into the void.
          </p>
        </div>

        <!-- Action buttons -->
        <div class="actions">
          <a routerLink="/" class="btn-primary">
            <ng-icon name="heroHome" class="size-5" />
            <span>Return Home</span>
          </a>
          <button type="button" (click)="goBack()" class="btn-secondary">
            <ng-icon name="heroArrowLeft" class="size-5" />
            <span>Go Back</span>
          </button>
        </div>
      </main>

      <!-- Bottom accent bar -->
      <div class="bottom-bar" aria-hidden="true">
        <div class="bar-segment segment-1"></div>
        <div class="bar-segment segment-2"></div>
        <div class="bar-segment segment-3"></div>
      </div>
    </div>
  `,
  styles: `
    @import url('https://fonts.googleapis.com/css2?family=Space+Mono:wght@400;700&family=Outfit:wght@300;500;700;900&display=swap');

    :host {
      display: block;
      min-height: 100dvh;
      background: var(--color-gray-50);
    }

    :host-context(html.dark) {
      background: var(--color-gray-900);
    }

    .not-found-container {
      position: relative;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      overflow: hidden;
      padding: 2rem;
    }

    /* Animated grid background */
    .grid-background {
      position: absolute;
      inset: 0;
      display: grid;
      grid-template-columns: repeat(8, 1fr);
      opacity: 0.04;
      pointer-events: none;
    }

    :host-context(html.dark) .grid-background {
      opacity: 0.06;
    }

    .grid-line {
      border-right: 1px solid var(--color-primary-500);
      height: 100%;
      animation: pulse-line 4s ease-in-out infinite;
    }

    @keyframes pulse-line {
      0%, 100% { opacity: 0.3; }
      50% { opacity: 1; }
    }

    /* Floating accent shapes */
    .accent-shape {
      position: absolute;
      border-radius: 50%;
      filter: blur(80px);
      pointer-events: none;
      animation: float 8s ease-in-out infinite;
    }

    .shape-1 {
      width: 400px;
      height: 400px;
      background: var(--color-primary-500);
      opacity: 0.15;
      top: -100px;
      right: -100px;
      animation-delay: 0s;
    }

    .shape-2 {
      width: 300px;
      height: 300px;
      background: var(--color-secondary-500);
      opacity: 0.12;
      bottom: -50px;
      left: -50px;
      animation-delay: 2s;
    }

    .shape-3 {
      width: 200px;
      height: 200px;
      background: var(--color-tertiary-500);
      opacity: 0.1;
      top: 40%;
      left: 20%;
      animation-delay: 4s;
    }

    @keyframes float {
      0%, 100% { transform: translate(0, 0) scale(1); }
      25% { transform: translate(10px, -20px) scale(1.05); }
      50% { transform: translate(-5px, 10px) scale(0.95); }
      75% { transform: translate(-15px, -10px) scale(1.02); }
    }

    /* Main content */
    .content-wrapper {
      position: relative;
      z-index: 10;
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      animation: fade-up 0.8s ease-out;
    }

    @keyframes fade-up {
      from {
        opacity: 0;
        transform: translateY(30px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    /* Massive 404 display */
    .error-display {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0;
      margin-bottom: 2rem;
      perspective: 1000px;
    }

    .digit {
      font-family: 'Outfit', system-ui, sans-serif;
      font-weight: 900;
      font-size: clamp(8rem, 25vw, 20rem);
      line-height: 0.85;
      color: var(--color-primary-500);
      text-shadow:
        4px 4px 0 var(--color-primary-200),
        8px 8px 0 var(--color-primary-100);
      animation: digit-entrance 0.6s cubic-bezier(0.34, 1.56, 0.64, 1) both;
    }

    :host-context(html.dark) .digit {
      color: var(--color-primary-400);
      text-shadow:
        4px 4px 0 var(--color-primary-800),
        8px 8px 0 var(--color-primary-900);
    }

    .digit-4-first {
      animation-delay: 0.1s;
    }

    .digit-0 {
      animation-delay: 0.2s;
      color: var(--color-secondary-500);
      text-shadow:
        4px 4px 0 var(--color-secondary-200),
        8px 8px 0 var(--color-secondary-100);
      animation: digit-entrance 0.6s cubic-bezier(0.34, 1.56, 0.64, 1) both,
                 wiggle 3s ease-in-out 1s infinite;
    }

    :host-context(html.dark) .digit-0 {
      color: var(--color-secondary-400);
      text-shadow:
        4px 4px 0 var(--color-secondary-800),
        8px 8px 0 var(--color-secondary-900);
    }

    .digit-4-last {
      animation-delay: 0.3s;
    }

    @keyframes digit-entrance {
      from {
        opacity: 0;
        transform: translateY(-50px) rotateX(-15deg);
      }
      to {
        opacity: 1;
        transform: translateY(0) rotateX(0);
      }
    }

    @keyframes wiggle {
      0%, 100% { transform: rotate(0deg); }
      25% { transform: rotate(-3deg); }
      75% { transform: rotate(3deg); }
    }

    /* Message section */
    .message-section {
      margin-bottom: 3rem;
      animation: fade-up 0.8s ease-out 0.4s both;
    }

    .title {
      font-family: 'Outfit', system-ui, sans-serif;
      font-weight: 700;
      font-size: clamp(1.5rem, 4vw, 2.5rem);
      color: var(--color-gray-900);
      margin: 0 0 0.75rem;
      letter-spacing: -0.02em;
    }

    :host-context(html.dark) .title {
      color: var(--color-gray-100);
    }

    .subtitle {
      font-family: 'Space Mono', monospace;
      font-size: clamp(0.875rem, 2vw, 1.125rem);
      color: var(--color-gray-600);
      margin: 0;
      max-width: 400px;
    }

    :host-context(html.dark) .subtitle {
      color: var(--color-gray-400);
    }

    /* Action buttons */
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 1rem;
      justify-content: center;
      animation: fade-up 0.8s ease-out 0.6s both;
    }

    .btn-primary,
    .btn-secondary {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.875rem 1.5rem;
      font-family: 'Outfit', system-ui, sans-serif;
      font-weight: 500;
      font-size: 1rem;
      border-radius: 0.5rem;
      cursor: pointer;
      transition: all 0.2s ease;
      text-decoration: none;
    }

    .btn-primary {
      background: var(--color-primary-500);
      color: white;
      border: 2px solid var(--color-primary-500);
      box-shadow: 0 4px 0 var(--color-primary-700);
    }

    .btn-primary:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 0 var(--color-primary-700);
    }

    .btn-primary:active {
      transform: translateY(2px);
      box-shadow: 0 2px 0 var(--color-primary-700);
    }

    :host-context(html.dark) .btn-primary {
      background: var(--color-primary-400);
      border-color: var(--color-primary-400);
      box-shadow: 0 4px 0 var(--color-primary-600);
    }

    :host-context(html.dark) .btn-primary:hover {
      box-shadow: 0 6px 0 var(--color-primary-600);
    }

    :host-context(html.dark) .btn-primary:active {
      box-shadow: 0 2px 0 var(--color-primary-600);
    }

    .btn-secondary {
      background: transparent;
      color: var(--color-gray-700);
      border: 2px solid var(--color-gray-300);
      box-shadow: 0 4px 0 var(--color-gray-400);
    }

    .btn-secondary:hover {
      transform: translateY(-2px);
      box-shadow: 0 6px 0 var(--color-gray-400);
      border-color: var(--color-gray-400);
    }

    .btn-secondary:active {
      transform: translateY(2px);
      box-shadow: 0 2px 0 var(--color-gray-400);
    }

    :host-context(html.dark) .btn-secondary {
      color: var(--color-gray-300);
      border-color: var(--color-gray-600);
      box-shadow: 0 4px 0 var(--color-gray-700);
    }

    :host-context(html.dark) .btn-secondary:hover {
      border-color: var(--color-gray-500);
      box-shadow: 0 6px 0 var(--color-gray-700);
    }

    :host-context(html.dark) .btn-secondary:active {
      box-shadow: 0 2px 0 var(--color-gray-700);
    }

    /* Bottom accent bar */
    .bottom-bar {
      position: absolute;
      bottom: 0;
      left: 0;
      right: 0;
      height: 6px;
      display: flex;
    }

    .bar-segment {
      flex: 1;
      animation: bar-grow 0.8s ease-out both;
    }

    .segment-1 {
      background: var(--color-primary-500);
      animation-delay: 0.8s;
    }

    .segment-2 {
      background: var(--color-secondary-500);
      animation-delay: 0.9s;
    }

    .segment-3 {
      background: var(--color-tertiary-500);
      animation-delay: 1s;
    }

    @keyframes bar-grow {
      from {
        transform: scaleX(0);
      }
      to {
        transform: scaleX(1);
      }
    }

    /* Focus states for accessibility */
    .btn-primary:focus-visible,
    .btn-secondary:focus-visible {
      outline: 3px solid var(--color-primary-300);
      outline-offset: 2px;
    }

    :host-context(html.dark) .btn-primary:focus-visible,
    :host-context(html.dark) .btn-secondary:focus-visible {
      outline-color: var(--color-primary-500);
    }
  `
})
export class NotFoundPage implements OnInit, OnDestroy {
  private sidenavService = inject(SidenavService);

  gridLines = Array.from({ length: 8 }, (_, i) => i);

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
