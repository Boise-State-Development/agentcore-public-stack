import {
  Component,
  ChangeDetectionStrategy,
  inject,
  OnInit,
  OnDestroy,
  signal,
  computed,
} from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroCheck,
  heroXMark,
  heroArrowPath,
  heroLink,
} from '@ng-icons/heroicons/outline';
import { SidenavService } from '../../services/sidenav/sidenav.service';

type CallbackState = 'processing' | 'success' | 'error';

@Component({
  selector: 'app-oauth-callback',
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroCheck,
      heroXMark,
      heroArrowPath,
      heroLink,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="callback-container">
      <!-- Animated background grid -->
      <div class="grid-background" aria-hidden="true">
        @for (i of gridLines; track i) {
          <div class="grid-line" [style.animation-delay]="i * 0.1 + 's'"></div>
        }
      </div>

      <!-- Floating accent shapes -->
      <div class="accent-shape shape-1" aria-hidden="true"></div>
      <div class="accent-shape shape-2" aria-hidden="true"></div>

      <main class="content-wrapper">
        <!-- Processing State -->
        @if (state() === 'processing') {
          <div class="status-display processing" aria-label="Processing">
            <div class="icon-container processing-icon">
              <ng-icon name="heroLink" class="size-16" />
              <div class="pulse-ring"></div>
              <div class="pulse-ring delay-1"></div>
              <div class="pulse-ring delay-2"></div>
            </div>
          </div>
          <div class="message-section">
            <h1 class="title">Connecting</h1>
            <p class="subtitle">
              <span class="typing-text">Establishing secure connection</span>
              <span class="dots">
                <span class="dot">.</span>
                <span class="dot">.</span>
                <span class="dot">.</span>
              </span>
            </p>
          </div>
        }

        <!-- Success State -->
        @if (state() === 'success') {
          <div class="status-display success" aria-label="Success">
            <div class="icon-container success-icon">
              <ng-icon name="heroCheck" class="size-16" />
              <div class="check-ring"></div>
            </div>
          </div>
          <div class="message-section">
            <h1 class="title success-title">Connected</h1>
            <p class="subtitle">
              @if (providerName()) {
                Successfully linked to {{ providerName() }}
              } @else {
                Authorization complete
              }
            </p>
            <p class="redirect-notice">
              Redirecting to your connections<span class="dots"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span>
            </p>
          </div>
        }

        <!-- Error State -->
        @if (state() === 'error') {
          <div class="status-display error" aria-label="Error">
            <div class="icon-container error-icon">
              <ng-icon name="heroXMark" class="size-16" />
              <div class="error-ring"></div>
            </div>
          </div>
          <div class="message-section">
            <h1 class="title error-title">Connection Failed</h1>
            <p class="subtitle error-subtitle">
              {{ errorMessage() }}
            </p>
            <p class="redirect-notice">
              Redirecting back<span class="dots"><span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></span>
            </p>
          </div>
        }

        <!-- Progress bar -->
        <div class="progress-track" aria-hidden="true">
          <div
            class="progress-fill"
            [class.success]="state() === 'success'"
            [class.error]="state() === 'error'"
          ></div>
        </div>
      </main>

      <!-- Bottom accent bar -->
      <div class="bottom-bar" aria-hidden="true">
        <div class="bar-segment" [class]="state()"></div>
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

    .callback-container {
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
      width: 350px;
      height: 350px;
      background: var(--color-primary-500);
      opacity: 0.12;
      top: -80px;
      right: -80px;
      animation-delay: 0s;
    }

    .shape-2 {
      width: 280px;
      height: 280px;
      background: var(--color-secondary-500);
      opacity: 0.1;
      bottom: -60px;
      left: -60px;
      animation-delay: 3s;
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
      animation: fade-up 0.6s ease-out;
      min-width: 320px;
    }

    @keyframes fade-up {
      from {
        opacity: 0;
        transform: translateY(20px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    /* Status display */
    .status-display {
      margin-bottom: 2rem;
      animation: scale-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1);
    }

    @keyframes scale-in {
      from {
        opacity: 0;
        transform: scale(0.8);
      }
      to {
        opacity: 1;
        transform: scale(1);
      }
    }

    .icon-container {
      position: relative;
      width: 120px;
      height: 120px;
      display: flex;
      align-items: center;
      justify-content: center;
      border-radius: 50%;
    }

    /* Processing state */
    .processing-icon {
      background: linear-gradient(135deg, var(--color-primary-100) 0%, var(--color-primary-50) 100%);
      color: var(--color-primary-600);
      animation: rotate-subtle 8s linear infinite;
    }

    :host-context(html.dark) .processing-icon {
      background: linear-gradient(135deg, var(--color-primary-900) 0%, var(--color-primary-950) 100%);
      color: var(--color-primary-400);
    }

    @keyframes rotate-subtle {
      from { transform: rotate(0deg); }
      to { transform: rotate(360deg); }
    }

    .pulse-ring {
      position: absolute;
      inset: -8px;
      border-radius: 50%;
      border: 2px solid var(--color-primary-400);
      animation: pulse-out 2s ease-out infinite;
    }

    .pulse-ring.delay-1 {
      animation-delay: 0.6s;
    }

    .pulse-ring.delay-2 {
      animation-delay: 1.2s;
    }

    @keyframes pulse-out {
      0% {
        opacity: 0.6;
        transform: scale(1);
      }
      100% {
        opacity: 0;
        transform: scale(1.6);
      }
    }

    /* Success state */
    .success-icon {
      background: linear-gradient(135deg, var(--color-green-100) 0%, var(--color-green-50) 100%);
      color: var(--color-green-600);
      animation: success-bounce 0.6s cubic-bezier(0.34, 1.56, 0.64, 1);
    }

    :host-context(html.dark) .success-icon {
      background: linear-gradient(135deg, var(--color-green-900) 0%, var(--color-green-950) 100%);
      color: var(--color-green-400);
    }

    @keyframes success-bounce {
      0% {
        opacity: 0;
        transform: scale(0.3);
      }
      50% {
        transform: scale(1.1);
      }
      100% {
        opacity: 1;
        transform: scale(1);
      }
    }

    .check-ring {
      position: absolute;
      inset: -4px;
      border-radius: 50%;
      border: 3px solid var(--color-green-400);
      animation: ring-appear 0.4s ease-out 0.3s both;
    }

    @keyframes ring-appear {
      from {
        opacity: 0;
        transform: scale(0.8);
      }
      to {
        opacity: 1;
        transform: scale(1);
      }
    }

    /* Error state */
    .error-icon {
      background: linear-gradient(135deg, var(--color-red-100) 0%, var(--color-red-50) 100%);
      color: var(--color-red-600);
      animation: error-shake 0.5s ease-out;
    }

    :host-context(html.dark) .error-icon {
      background: linear-gradient(135deg, var(--color-red-900) 0%, var(--color-red-950) 100%);
      color: var(--color-red-400);
    }

    @keyframes error-shake {
      0%, 100% { transform: translateX(0); }
      20% { transform: translateX(-8px); }
      40% { transform: translateX(8px); }
      60% { transform: translateX(-4px); }
      80% { transform: translateX(4px); }
    }

    .error-ring {
      position: absolute;
      inset: -4px;
      border-radius: 50%;
      border: 3px solid var(--color-red-400);
      animation: ring-appear 0.4s ease-out 0.3s both;
    }

    /* Message section */
    .message-section {
      animation: fade-up 0.6s ease-out 0.2s both;
    }

    .title {
      font-family: 'Outfit', system-ui, sans-serif;
      font-weight: 700;
      font-size: clamp(1.75rem, 5vw, 2.5rem);
      color: var(--color-gray-900);
      margin: 0 0 0.75rem;
      letter-spacing: -0.02em;
    }

    :host-context(html.dark) .title {
      color: var(--color-gray-100);
    }

    .success-title {
      color: var(--color-green-600);
    }

    :host-context(html.dark) .success-title {
      color: var(--color-green-400);
    }

    .error-title {
      color: var(--color-red-600);
    }

    :host-context(html.dark) .error-title {
      color: var(--color-red-400);
    }

    .subtitle {
      font-family: 'Space Mono', monospace;
      font-size: clamp(0.875rem, 2vw, 1rem);
      color: var(--color-gray-600);
      margin: 0;
      max-width: 320px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0;
    }

    :host-context(html.dark) .subtitle {
      color: var(--color-gray-400);
    }

    .error-subtitle {
      color: var(--color-red-600);
    }

    :host-context(html.dark) .error-subtitle {
      color: var(--color-red-400);
    }

    .typing-text {
      overflow: hidden;
      white-space: nowrap;
      animation: typing 1.5s steps(30) forwards;
    }

    @keyframes typing {
      from { width: 0; }
      to { width: 100%; }
    }

    .dots {
      display: inline-flex;
      margin-left: 2px;
    }

    .dot {
      animation: dot-bounce 1.4s ease-in-out infinite;
      opacity: 0;
    }

    .dot:nth-child(1) { animation-delay: 0s; }
    .dot:nth-child(2) { animation-delay: 0.2s; }
    .dot:nth-child(3) { animation-delay: 0.4s; }

    @keyframes dot-bounce {
      0%, 80%, 100% {
        opacity: 0;
        transform: translateY(0);
      }
      40% {
        opacity: 1;
        transform: translateY(-4px);
      }
    }

    .redirect-notice {
      font-family: 'Space Mono', monospace;
      font-size: 0.75rem;
      color: var(--color-gray-500);
      margin-top: 1.5rem;
      animation: fade-up 0.4s ease-out 0.5s both;
    }

    :host-context(html.dark) .redirect-notice {
      color: var(--color-gray-500);
    }

    /* Progress track */
    .progress-track {
      width: 200px;
      height: 4px;
      background: var(--color-gray-200);
      border-radius: 2px;
      margin-top: 2.5rem;
      overflow: hidden;
      animation: fade-up 0.4s ease-out 0.4s both;
    }

    :host-context(html.dark) .progress-track {
      background: var(--color-gray-700);
    }

    .progress-fill {
      height: 100%;
      width: 0%;
      background: var(--color-primary-500);
      border-radius: 2px;
      animation: progress-loading 2.5s ease-out forwards;
    }

    .progress-fill.success {
      background: var(--color-green-500);
      animation: progress-complete 0.4s ease-out forwards;
    }

    .progress-fill.error {
      background: var(--color-red-500);
      animation: progress-complete 0.4s ease-out forwards;
    }

    @keyframes progress-loading {
      0% { width: 0%; }
      20% { width: 15%; }
      50% { width: 45%; }
      80% { width: 75%; }
      100% { width: 90%; }
    }

    @keyframes progress-complete {
      to { width: 100%; }
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
      background: var(--color-primary-500);
      animation: bar-grow 0.8s ease-out both;
    }

    .bar-segment.success {
      background: var(--color-green-500);
    }

    .bar-segment.error {
      background: var(--color-red-500);
    }

    @keyframes bar-grow {
      from { transform: scaleX(0); }
      to { transform: scaleX(1); }
    }
  `,
})
export class OAuthCallbackPage implements OnInit, OnDestroy {
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private sidenavService = inject(SidenavService);

  gridLines = Array.from({ length: 8 }, (_, i) => i);

  // State signals
  state = signal<CallbackState>('processing');
  providerName = signal<string | null>(null);
  errorMessage = signal<string>('Authorization was denied or failed.');

  ngOnInit(): void {
    this.sidenavService.hide();
    this.handleCallback();
  }

  ngOnDestroy(): void {
    this.sidenavService.show();
  }

  private handleCallback(): void {
    const params = this.route.snapshot.queryParams;

    // Simulate brief processing delay for visual feedback
    setTimeout(() => {
      if (params['success'] === 'true') {
        this.handleSuccess(params);
      } else if (params['error']) {
        this.handleError(params);
      } else {
        // No valid params, redirect to connections
        this.redirectToConnections();
      }
    }, 800);
  }

  private handleSuccess(params: Record<string, string>): void {
    const provider = params['provider'];
    if (provider) {
      this.providerName.set(this.formatProviderName(provider));
    }
    this.state.set('success');

    // Redirect after showing success
    setTimeout(() => {
      this.redirectToConnections({ success: 'true', provider });
    }, 1500);
  }

  private handleError(params: Record<string, string>): void {
    const error = params['error'];
    const description = params['error_description'];
    const provider = params['provider'];

    let message = 'Authorization was denied or failed.';
    if (description) {
      message = description;
    } else if (error === 'access_denied') {
      message = 'Authorization was denied. Please try again.';
    } else if (error === 'missing_params') {
      message = 'Invalid callback parameters.';
    } else if (error === 'invalid_state') {
      message = 'Session expired. Please try again.';
    } else if (error === 'token_exchange_failed') {
      message = 'Failed to complete authorization.';
    }

    this.errorMessage.set(message);
    if (provider) {
      this.providerName.set(this.formatProviderName(provider));
    }
    this.state.set('error');

    // Redirect after showing error
    setTimeout(() => {
      this.redirectToConnections({ error, provider });
    }, 2500);
  }

  private redirectToConnections(queryParams?: Record<string, string>): void {
    this.router.navigate(['/settings/connections'], {
      queryParams,
      replaceUrl: true,
    });
  }

  private formatProviderName(providerId: string): string {
    // Convert provider_id to display name
    return providerId
      .replace(/_/g, ' ')
      .replace(/-/g, ' ')
      .split(' ')
      .map(word => word.charAt(0).toUpperCase() + word.slice(1))
      .join(' ');
  }
}
