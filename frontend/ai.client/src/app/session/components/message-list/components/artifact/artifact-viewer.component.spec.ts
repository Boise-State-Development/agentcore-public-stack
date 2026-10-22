import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { DomSanitizer } from '@angular/platform-browser';
import { ArtifactViewerComponent } from './artifact-viewer.component';

describe('ArtifactViewerComponent', () => {
  let fixture: ComponentFixture<ArtifactViewerComponent>;
  /** `[src]` is a resource-URL sink — Angular rejects a bare string,
   *  so the spec must trust the URL the same way the parents do. */
  let url: unknown;

  const el = () => fixture.nativeElement as HTMLElement;
  const iframe = () => el().querySelector('iframe');

  function render(inputs: Record<string, unknown> = {}): void {
    for (const [k, v] of Object.entries(inputs)) {
      fixture.componentRef.setInput(k, v);
    }
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [ArtifactViewerComponent] });
    fixture = TestBed.createComponent(ArtifactViewerComponent);
    url = TestBed.inject(DomSanitizer).bypassSecurityTrustResourceUrl(
      'https://artifacts.x/?t=jwt',
    );
  });

  afterEach(() => {
    // isolate:false shares one DOM across every spec file, so a fixture
    // left mounted keeps its skeleton's infinite shimmer animation
    // running for the rest of the suite. Tear it down.
    fixture?.destroy();
  });

  // ----------------------------------------------------------------
  // Isolation — the reason this component exists in one place
  // ----------------------------------------------------------------

  it('sandboxes the iframe without allow-same-origin', () => {
    render({ safeUrl: url, title: 'Chart' });

    const sandbox = iframe()!.getAttribute('sandbox');
    expect(sandbox).toBe('allow-scripts');
    // `allow-same-origin` alongside `allow-scripts` would hand
    // attacker-authored markup the artifact origin itself. Both the
    // owner panel and the recipient page depend on this staying absent.
    expect(sandbox).not.toContain('allow-same-origin');
    expect(iframe()!.getAttribute('referrerpolicy')).toBe('no-referrer');
  });

  it('names the iframe for screen readers, with a fallback', () => {
    render({ safeUrl: url, title: 'Chart' });
    expect(iframe()!.getAttribute('title')).toBe('Chart');

    render({ title: '' });
    expect(iframe()!.getAttribute('title')).toBe('Artifact');
  });

  it('renders no iframe until a URL is minted', () => {
    render({ safeUrl: null });
    expect(iframe()).toBeNull();
    // The skeleton stands in so the pane is never blank.
    expect(el().querySelector('[role="status"]')).not.toBeNull();
  });

  it('keeps the skeleton until the parent says the preview painted', () => {
    render({ safeUrl: url, previewReady: false });
    expect(el().querySelector('[role="status"]')).not.toBeNull();

    render({ previewReady: true });
    expect(el().querySelector('[role="status"]')).toBeNull();
  });

  it('disables pointer events while the parent marks it inert', () => {
    render({ safeUrl: url, inert: true });
    expect(iframe()!.classList).toContain('pointer-events-none');

    render({ inert: false });
    expect(iframe()!.classList).not.toContain('pointer-events-none');
  });

  // ----------------------------------------------------------------
  // Error branches
  // ----------------------------------------------------------------

  it('shows the preview error instead of the iframe, and can retry', () => {
    let retried = 0;
    render({ safeUrl: url, error: 'Boom' });
    fixture.componentInstance.retry.subscribe(() => retried++);

    expect(el().querySelector('[role="alert"]')?.textContent).toContain('Boom');
    expect(iframe()).toBeNull();

    el().querySelector<HTMLButtonElement>('[role="alert"] button')!.click();
    expect(retried).toBe(1);
  });

  it('shows the code-view error, and can retry it separately', () => {
    let retried = 0;
    render({ view: 'code', sourceError: 'Too large' });
    fixture.componentInstance.retrySource.subscribe(() => retried++);

    expect(el().querySelector('[role="alert"]')?.textContent).toContain(
      'Too large',
    );

    el().querySelector<HTMLButtonElement>('[role="alert"] button')!.click();
    expect(retried).toBe(1);
  });

  it('does not leak a preview error into code view', () => {
    // The two paths fail independently: a dead render token must not
    // blank out source the page already fetched.
    render({
      view: 'code',
      error: 'preview blew up',
      source: { content: 'hello source', contentType: 'text/html', version: 1 },
    });
    expect(el().textContent).not.toContain('preview blew up');
    expect(el().textContent).toContain('hello source');
  });

  it('renders the source once it arrives', () => {
    render({
      view: 'code',
      source: { content: 'const x = 1;', contentType: 'text/javascript', version: 2 },
    });
    expect(el().textContent).toContain('const x = 1;');
  });
});
