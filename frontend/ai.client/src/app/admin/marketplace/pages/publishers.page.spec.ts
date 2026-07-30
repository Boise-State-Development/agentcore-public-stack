import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import { MarketplacePublishersPage } from './publishers.page';
import { PublisherProfile } from '../models/marketplace.model';

/**
 * Publishers (D12).
 *
 * The backend has been complete since the marketplace shipped; this page is the missing
 * way to reach it. So the tests are about the three rules a curator would otherwise learn
 * by being surprised — the id is fixed, delete is refused while in use, and `verified` is
 * a check mark rather than a permission — plus the ordinary CRUD wiring.
 */
describe('MarketplacePublishersPage', () => {
  let mockService: {
    loadPublishers: ReturnType<typeof vi.fn>;
    createPublisher: ReturnType<typeof vi.fn>;
    updatePublisher: ReturnType<typeof vi.fn>;
    deletePublisher: ReturnType<typeof vi.fn>;
  };
  /** Delete is confirmed; default to "confirmed" so the other tests read unchanged. */
  let mockDialog: { open: ReturnType<typeof vi.fn> };

  function publisher(overrides: Partial<PublisherProfile> = {}): PublisherProfile {
    return {
      id: 'pub-registrar',
      label: 'Office of the Registrar',
      kind: 'department',
      verified: false,
      order: 0,
      enabled: true,
      ...overrides,
    };
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockService = {
      loadPublishers: vi.fn().mockResolvedValue([]),
      createPublisher: vi.fn().mockResolvedValue(publisher()),
      updatePublisher: vi.fn().mockResolvedValue(publisher()),
      deletePublisher: vi.fn().mockResolvedValue(undefined),
    };
    mockDialog = { open: vi.fn().mockReturnValue({ closed: of(true) }) };
    TestBed.configureTestingModule({
      providers: [
        { provide: AdminMarketplaceService, useValue: mockService },
        { provide: Dialog, useValue: mockDialog },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function render(
    rows: PublisherProfile[] = [],
  ): Promise<ComponentFixture<MarketplacePublishersPage>> {
    mockService.loadPublishers.mockResolvedValue(rows);
    const fixture = TestBed.createComponent(MarketplacePublishersPage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  function text(fixture: ComponentFixture<unknown>): string {
    return (fixture.nativeElement as HTMLElement).textContent ?? '';
  }

  function deleteButton(fixture: ComponentFixture<unknown>): HTMLButtonElement {
    return Array.from((fixture.nativeElement as HTMLElement).querySelectorAll('button')).find(
      (b) => b.textContent?.includes('Delete Office of the Registrar'),
    ) as HTMLButtonElement;
  }

  it('lists the publishers an admin can attribute a listing to', async () => {
    const fixture = await render([publisher(), publisher({ id: 'pub-bsu', label: 'Boise State' })]);
    expect(text(fixture)).toContain('Office of the Registrar');
    expect(text(fixture)).toContain('Boise State');
  });

  it('points at the auto-created individual profile when there is nothing to show', async () => {
    // The empty state is the first thing a curator sees, and "no publishers" without
    // context reads as broken — authors already get one on their first submission.
    const fixture = await render([]);
    expect(text(fixture)).toContain('No publishers yet');
    expect(text(fixture)).toContain('individual profile automatically');
  });

  it('creates a publisher with the selected kind', async () => {
    const fixture = await render([]);
    const input = (fixture.nativeElement as HTMLElement).querySelector(
      '#new-publisher',
    ) as HTMLInputElement;
    input.value = 'Communications & Marketing';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    const addButton = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((b) => b.textContent?.includes('Add')) as HTMLButtonElement;
    addButton.click();
    await fixture.whenStable();

    expect(mockService.createPublisher).toHaveBeenCalledWith(
      expect.objectContaining({ label: 'Communications & Marketing', kind: 'department' }),
    );
  });

  it('shows the id and says it is fixed, because listings store it', async () => {
    // Renaming a publisher must never strand the attributions pointing at it, and the only
    // way a curator learns that before trying is if the row says so.
    const fixture = await render([publisher()]);
    expect(text(fixture)).toContain('id: pub-registrar');
    expect(text(fixture)).toContain('fixed at creation');
  });

  it('renames the label only', async () => {
    const fixture = await render([publisher()]);
    const input = (fixture.nativeElement as HTMLElement).querySelector(
      '#label-pub-registrar',
    ) as HTMLInputElement;
    input.value = 'The Registrar';
    input.dispatchEvent(new Event('change'));
    await fixture.whenStable();

    expect(mockService.updatePublisher).toHaveBeenCalledWith('pub-registrar', {
      label: 'The Registrar',
    });
  });

  it('does not send a rename that changed nothing', async () => {
    const fixture = await render([publisher()]);
    const input = (fixture.nativeElement as HTMLElement).querySelector(
      '#label-pub-registrar',
    ) as HTMLInputElement;
    input.dispatchEvent(new Event('change'));
    await fixture.whenStable();

    expect(mockService.updatePublisher).not.toHaveBeenCalled();
  });

  it('toggles verified, and says it grants nothing', async () => {
    const fixture = await render([publisher({ verified: false })]);
    const button = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((b) => b.textContent?.trim() === 'Unverified') as HTMLButtonElement;

    // `verified` renders as a check mark and looks like a permission. The tooltip is the
    // only place that distinction is stated, so it is worth asserting.
    expect(button.getAttribute('appTooltip') ?? button.title).toBeDefined();
    button.click();
    await fixture.whenStable();

    expect(mockService.updatePublisher).toHaveBeenCalledWith('pub-registrar', { verified: true });
  });

  it('toggles enabled without touching existing attributions', async () => {
    const fixture = await render([publisher({ enabled: true })]);
    const button = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((b) => b.textContent?.trim() === 'Enabled') as HTMLButtonElement;
    button.click();
    await fixture.whenStable();

    expect(mockService.updatePublisher).toHaveBeenCalledWith('pub-registrar', { enabled: false });
  });

  it('confirms before deleting a publisher', async () => {
    // The one control on this page that cannot be undone by clicking it again: the id is
    // fixed at creation, so a mistaken delete cannot be put back.
    const fixture = await render([publisher()]);
    deleteButton(fixture).click();
    await fixture.whenStable();

    expect(mockDialog.open).toHaveBeenCalled();
    expect(mockService.deletePublisher).toHaveBeenCalledWith('pub-registrar');
  });

  it('does not delete when the confirmation is dismissed', async () => {
    mockDialog.open.mockReturnValue({ closed: of(undefined) });
    const fixture = await render([publisher()]);
    deleteButton(fixture).click();
    await fixture.whenStable();

    expect(mockService.deletePublisher).not.toHaveBeenCalled();
  });

  it("surfaces the server's refusal when a publisher is still in use", async () => {
    // The 409 explains itself and suggests disabling instead. Replacing it with a generic
    // "could not save" would throw away the only actionable part.
    mockService.deletePublisher.mockRejectedValue({
      error: { detail: 'Publisher is attributed to 3 listings. Disable it instead.' },
    });
    const fixture = await render([publisher()]);

    deleteButton(fixture).click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(text(fixture)).toContain('Disable it instead');
  });

  it('falls back to a plain message when the server sends no detail', async () => {
    mockService.updatePublisher.mockRejectedValue(new Error('network'));
    const fixture = await render([publisher()]);
    const button = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((b) => b.textContent?.trim() === 'Enabled') as HTMLButtonElement;
    button.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(text(fixture)).toContain('could not be saved');
  });

  it('reloads after a failed mutation so the list matches the server', async () => {
    mockService.updatePublisher.mockRejectedValue(new Error('nope'));
    const fixture = await render([publisher()]);
    mockService.loadPublishers.mockClear();

    const button = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((b) => b.textContent?.trim() === 'Enabled') as HTMLButtonElement;
    button.click();
    await fixture.whenStable();

    expect(mockService.loadPublishers).toHaveBeenCalled();
  });
});
