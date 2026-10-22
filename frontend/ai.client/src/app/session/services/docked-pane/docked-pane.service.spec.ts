import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { DockedPaneService } from './docked-pane.service';
import { SidenavService } from '../../../services/sidenav/sidenav.service';

describe('DockedPaneService', () => {
  let service: DockedPaneService;
  let sidenav: SidenavService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(DockedPaneService);
    sidenav = TestBed.inject(SidenavService);
  });

  it('starts closed with no owner', () => {
    expect(service.isOpen()).toBe(false);
    expect(service.owner()).toBeNull();
  });

  it('collapses the side nav on open and expands it again on close', () => {
    sidenav.expand();

    service.claim('artifact');
    expect(sidenav.isCollapsed()).toBe(true);

    service.release('artifact');
    expect(sidenav.isCollapsed()).toBe(false);
  });

  it('leaves the side nav collapsed if it already was before opening', () => {
    sidenav.collapse();

    service.claim('artifact');
    service.release('artifact');

    expect(sidenav.isCollapsed()).toBe(true);
  });

  it('evicts the current owner when another claims the rail', () => {
    service.claim('artifact');
    service.claim('file-preview');

    expect(service.owner()).toBe('file-preview');
    expect(service.holds('artifact')).toBe(false);
    expect(service.isOpen()).toBe(true);
  });

  it('does not re-capture the nav state on a handoff between owners', () => {
    // The nav was open before anything docked; after a handoff and a
    // close it must come back. Capturing again mid-handoff would read
    // the collapsed state we set ourselves and strand the nav closed.
    sidenav.expand();

    service.claim('artifact');
    service.claim('file-preview');
    service.release('file-preview');

    expect(sidenav.isCollapsed()).toBe(false);
  });

  it('ignores a release from an owner that no longer holds the rail', () => {
    service.claim('artifact');
    service.claim('file-preview');

    // The artifact pane closing late must not shut the preview that
    // displaced it.
    service.release('artifact');

    expect(service.owner()).toBe('file-preview');
  });

  it('releaseAll closes whoever holds the rail', () => {
    sidenav.expand();
    service.claim('file-preview');

    service.releaseAll();

    expect(service.isOpen()).toBe(false);
    expect(sidenav.isCollapsed()).toBe(false);
  });

  it('clamps width to the allowed range and rounds it', () => {
    service.setWidth(10);
    expect(service.width()).toBe(service.widthMin);

    service.setWidth(99999);
    expect(service.width()).toBe(service.widthMax);

    service.setWidth(700.4);
    expect(service.width()).toBe(700);
  });

  it('keeps the width across a handoff between owners', () => {
    service.setWidth(800);
    service.claim('artifact');
    service.claim('file-preview');

    expect(service.width()).toBe(800);
  });
});
