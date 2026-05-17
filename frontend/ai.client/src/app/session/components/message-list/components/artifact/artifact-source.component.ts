import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
} from '@angular/core';

/**
 * Read-only source view for the artifact panel's "code" mode. Highlights
 * inert artifact text with the globally-loaded Prism (same build/theme
 * the chat code blocks use) and renders a line-number gutter beside it.
 *
 * Safety: the content is never executed. Prism.highlight HTML-escapes
 * the source and only injects its own `<span class="token">` wrappers,
 * and the `[innerHTML]` binding is additionally run through Angular's
 * sanitizer (which strips scripts/handlers but keeps the token spans) —
 * so this is XSS-safe without `bypassSecurityTrustHtml`.
 */
interface PrismLike {
  languages: Record<string, unknown>;
  highlight(text: string, grammar: unknown, language: string): string;
}

function getPrism(): PrismLike | null {
  const p = (globalThis as { Prism?: PrismLike }).Prism;
  return p && typeof p.highlight === 'function' ? p : null;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

/** Bare MIME type → Prism language id. Unknown types render as escaped
 *  plain text (no grammar) so the view never breaks on an exotic type. */
function mimeToPrismLang(contentType: string): string {
  const bare = (contentType || '').split(';')[0].trim().toLowerCase();
  switch (bare) {
    case 'text/html':
    case 'application/xhtml+xml':
    case 'image/svg+xml':
      return 'markup';
    case 'text/javascript':
    case 'application/javascript':
      return 'javascript';
    case 'text/css':
      return 'css';
    case 'application/json':
      return 'json';
    case 'text/markdown':
    case 'text/x-markdown':
      return 'markdown';
    default:
      return 'none';
  }
}

@Component({
  selector: 'app-artifact-source',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="h-full w-full overflow-auto bg-[#272822] text-[13px]"
      tabindex="0"
      role="region"
      [attr.aria-label]="'Artifact source (' + language() + ')'"
    >
      <div class="flex min-h-full min-w-full w-max">
        <div
          aria-hidden="true"
          class="sticky left-0 z-10 select-none border-r border-white/10 bg-[#272822] px-3 py-4 text-right font-mono leading-[1.6] text-white/35 tabular-nums"
        >
          @for (n of lineNumbers(); track n) {
            <div>{{ n }}</div>
          }
        </div>
        <pre
          class="m-0 flex-1 px-4 py-4 font-mono leading-[1.6]"
        ><code [class]="'language-' + language()" [innerHTML]="highlighted()"></code></pre>
      </div>
    </div>
  `,
  styles: `
    :host {
      display: block;
      height: 100%;
    }
    /* The global okaidia theme paints its own background/padding on
       pre[class*="language-"]; we host the surface on the scroll
       container instead so the gutter and code share it. */
    :host pre {
      background: transparent;
      white-space: pre;
      tab-size: 2;
    }
  `,
})
export class ArtifactSourceComponent {
  readonly content = input.required<string>();
  readonly contentType = input.required<string>();

  protected readonly language = computed(() =>
    mimeToPrismLang(this.contentType()),
  );

  protected readonly lineNumbers = computed(() => {
    const lines = this.content().split('\n').length;
    return Array.from({ length: lines }, (_, i) => i + 1);
  });

  protected readonly highlighted = computed(() => {
    const code = this.content();
    const lang = this.language();
    const prism = getPrism();
    const grammar = prism?.languages[lang];
    if (lang === 'none' || !prism || !grammar) {
      return escapeHtml(code);
    }
    try {
      return prism.highlight(code, grammar, lang);
    } catch {
      return escapeHtml(code);
    }
  });
}
