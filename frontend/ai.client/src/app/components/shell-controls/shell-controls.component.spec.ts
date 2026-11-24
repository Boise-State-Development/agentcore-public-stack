import { describe, it, expect, beforeEach, vi } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { FEATURES } from '../../services/features';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { SearchDialogService } from '../search/search-dialog.service';
import { ShellControlsComponent } from './shell-controls.component';

describe('ShellControlsComponent', () => {
  let router: { navigate: ReturnType<typeof vi.fn> };
  let searchDialog: { open: ReturnType<typeof vi.fn> };
  let sidenav: SidenavService;

  function render(conversationSearch: boolean): ComponentFixture<ShellControlsComponent> {
    TestBed.overrideProvider(FEATURES, { useValue: { conversationSearch } });
    sidenav = TestBed.inject(SidenavService);
    const fixture = TestBed.createComponent(ShellControlsComponent);
    fixture.detectChanges();
    return fixture;
  }

  function button(fixture: ComponentFixture<ShellControlsComponent>, label: string): HTMLButtonElement {
    const found = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(
      `button[aria-label="${label}"]`,
    );
    if (!found) throw new Error(`no "${label}" button`);
    return found;
  }

  beforeEach(() => {
    router = { navigate: vi.fn().mockResolvedValue(true) };
    searchDialog = { open: vi.fn().mockResolvedValue(undefined) };
    TestBed.configureTestingModule({
      imports: [ShellControlsComponent],
      providers: [
        { provide: Router, useValue: router },
        { provide: SearchDialogService, useValue: searchDialog },
      ],
    });
  });

  it('is a named group, so the buttons read as one set of controls', () => {
    const host = render(true).nativeElement as HTMLElement;
    expect(host.getAttribute('role')).toBe('group');
    expect(host.getAttribute('aria-label')).toBe('Sidebar and quick actions');
  });

  it('opens the sidebar overlay from the small-screen button', () => {
    const fixture = render(true);
    button(fixture, 'Open sidebar').click();
    expect(sidenav.isOpen()).toBe(true);
  });

  it('expands the collapsed sidebar from the desktop button', () => {
    const fixture = render(true);
    sidenav.collapse();
    button(fixture, 'Expand sidebar').click();
    expect(sidenav.isCollapsed()).toBe(false);
    expect(sidenav.isOpen()).toBe(false);
  });

  it('shows exactly one of the two sidebar buttons at any width', () => {
    const fixture = render(true);
    expect(button(fixture, 'Open sidebar').classList.contains('lg:hidden')).toBe(true);
    expect(button(fixture, 'Expand sidebar').classList.contains('max-lg:hidden')).toBe(true);
  });

  it('starts a new chat', () => {
    const fixture = render(true);
    button(fixture, 'New chat').click();
    expect(router.navigate).toHaveBeenCalledWith(['']);
  });

  it('opens search, and keeps the button off a phone so the title has the room', () => {
    const fixture = render(true);
    const search = button(fixture, 'Search');
    expect(search.classList.contains('max-sm:hidden')).toBe(true);
    search.click();
    expect(searchDialog.open).toHaveBeenCalled();
  });

  it('draws no search button when conversation search is off', () => {
    const host = render(false).nativeElement as HTMLElement;
    expect(host.querySelector('button[aria-label="Search"]')).toBeNull();
    expect(host.querySelectorAll('button').length).toBe(3);
  });
});
