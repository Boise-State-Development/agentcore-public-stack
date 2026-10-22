import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { FEATURES } from '../../../services/features';
import { UserSettingsService } from '../../../services/user-settings.service';
import { SidebarLayoutService } from '../sidebar-layout.service';
import { EditSidebarDialogComponent } from './edit-sidebar-dialog.component';

describe('EditSidebarDialogComponent', () => {
  beforeEach(() => {
    localStorage.removeItem('sidebar-layout');
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        {
          provide: UserSettingsService,
          useValue: {
            getSettings: vi.fn().mockResolvedValue({ defaultModelId: null }),
            updateSettings: vi.fn().mockResolvedValue({}),
          },
        },
        { provide: FEATURES, useValue: { projects: false, conversationSearch: false } },
      ],
    });
  });

  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    localStorage.removeItem('sidebar-layout');
  });

  async function open() {
    const opened = await openInCdkDialog<EditSidebarDialogComponent, unknown, void>(EditSidebarDialogComponent);
    TestBed.tick();
    return opened;
  }

  function rows(container: HTMLElement): HTMLLIElement[] {
    return Array.from(container.querySelectorAll<HTMLLIElement>('li'));
  }

  function labels(container: HTMLElement): string[] {
    return rows(container).map(row => row.querySelector('label')!.textContent!.trim());
  }

  function button(container: HTMLElement, text: string): HTMLButtonElement {
    return Array.from(container.querySelectorAll<HTMLButtonElement>('button')).find(b => b.textContent?.trim() === text)!;
  }

  it('is a dialog named by its title', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'Edit sidebar', description: /Choose which items appear/ });
  });

  it('lists every entry this build offers, checked where it shows in the sidebar', async () => {
    const { container } = await open();
    TestBed.inject(SidebarLayoutService).setVisible('artifacts', false);
    TestBed.tick();
    expect(labels(container)).toEqual(['Agents', 'Artifacts', 'Customize', 'Schedules']);
    const checked = rows(container).map(row => row.querySelector<HTMLInputElement>('input[type="checkbox"]')!.checked);
    expect(checked).toEqual([true, false, true, true]);
  });

  it('shows and hides an entry from its checkbox, and announces it', async () => {
    const { container } = await open();
    const layout = TestBed.inject(SidebarLayoutService);
    const schedules = rows(container)[3].querySelector<HTMLInputElement>('input')!;
    schedules.click();
    TestBed.tick();
    expect(layout.hiddenEntries().map(entry => entry.id)).toEqual(['schedules']);
    expect(container.querySelector('[aria-live]')!.textContent).toContain('Schedules moved to More');

    schedules.click();
    TestBed.tick();
    expect(layout.hiddenEntries()).toEqual([]);
    expect(container.querySelector('[aria-live]')!.textContent).toContain('Schedules shown in the sidebar');
  });

  it('reorders from the keyboard on the drag handle', async () => {
    const { container } = await open();
    const handle = container.querySelector<HTMLButtonElement>('#sidebar-reorder-agents')!;
    expect(handle.getAttribute('aria-label')).toBe('Reorder Agents, position 1 of 4. Use the arrow keys to move.');

    handle.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    TestBed.tick();
    expect(labels(container)).toEqual(['Artifacts', 'Agents', 'Customize', 'Schedules']);
    expect(container.querySelector('[aria-live]')!.textContent).toContain('Agents moved to position 2 of 4');

    container
      .querySelector<HTMLButtonElement>('#sidebar-reorder-schedules')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }));
    TestBed.tick();
    expect(labels(container)[0]).toBe('Schedules');
  });

  it('offers Reset only once something changed, and resets', async () => {
    const { container } = await open();
    expect(button(container, 'Reset to default').disabled).toBe(true);

    rows(container)[0].querySelector<HTMLInputElement>('input')!.click();
    TestBed.tick();
    expect(button(container, 'Reset to default').disabled).toBe(false);

    button(container, 'Reset to default').click();
    TestBed.tick();
    expect(TestBed.inject(SidebarLayoutService).isDefault()).toBe(true);
  });

  it('closes from Done', async () => {
    const { ref, container } = await open();
    let closed = false;
    ref.closed.subscribe(() => (closed = true));
    button(container, 'Done').click();
    expect(closed).toBe(true);
  });
});
