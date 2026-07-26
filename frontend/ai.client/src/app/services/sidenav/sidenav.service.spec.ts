import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { SidenavService } from './sidenav.service';

describe('SidenavService', () => {
  let service: SidenavService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [SidenavService]
    });
    service = TestBed.inject(SidenavService);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should initialize with default states', () => {
    expect(service.isOpen()).toBe(false);
    expect(service.isClosing()).toBe(false);
    expect(service.isCollapsed()).toBe(false);
    expect(service.isHidden()).toBe(false);
    expect(service.isVisible()).toBe(false);
  });

  describe('mobile sidenav (open/close)', () => {
    it('should open sidenav', () => {
      service.open();
      
      expect(service.isOpen()).toBe(true);
      expect(service.isClosing()).toBe(false);
      expect(service.isVisible()).toBe(true);
    });

    it('should close sidenav with animation', () => {
      service.open();
      expect(service.isOpen()).toBe(true);
      
      service.close();
      
      expect(service.isOpen()).toBe(false);
      expect(service.isClosing()).toBe(true);
      expect(service.isVisible()).toBe(true); // Still visible during animation
    });

    it('should complete close animation after timeout', () => {
      service.open();
      service.close();
      
      expect(service.isClosing()).toBe(true);
      
      vi.advanceTimersByTime(300);
      
      expect(service.isClosing()).toBe(false);
      expect(service.isVisible()).toBe(false);
    });

    it('should handle close when already closed', () => {
      expect(service.isOpen()).toBe(false);
      
      service.close();
      
      expect(service.isOpen()).toBe(false);
      expect(service.isClosing()).toBe(false);
    });

    it('should reset closing state when opening during close animation', () => {
      service.open();
      service.close();
      expect(service.isClosing()).toBe(true);
      
      service.open();
      
      expect(service.isOpen()).toBe(true);
      expect(service.isClosing()).toBe(false);
      expect(service.isVisible()).toBe(true);
    });
  });

  describe('toggle', () => {
    it('should toggle from closed to open', () => {
      expect(service.isOpen()).toBe(false);
      
      service.toggle();
      
      expect(service.isOpen()).toBe(true);
    });

    it('should toggle from open to closed', () => {
      service.open();
      expect(service.isOpen()).toBe(true);
      
      service.toggle();
      
      expect(service.isOpen()).toBe(false);
      expect(service.isClosing()).toBe(true);
    });
  });

  describe('desktop sidenav (collapse/expand)', () => {
    it('should toggle collapsed state', () => {
      expect(service.isCollapsed()).toBe(false);
      
      service.toggleCollapsed();
      
      expect(service.isCollapsed()).toBe(true);
      
      service.toggleCollapsed();
      
      expect(service.isCollapsed()).toBe(false);
    });

    it('should collapse sidenav', () => {
      service.collapse();
      
      expect(service.isCollapsed()).toBe(true);
    });

    it('should expand sidenav', () => {
      service.collapse();
      expect(service.isCollapsed()).toBe(true);
      
      service.expand();
      
      expect(service.isCollapsed()).toBe(false);
    });
  });

  describe('hide/show', () => {
    it('should hide sidenav', () => {
      service.hide();
      
      expect(service.isHidden()).toBe(true);
    });

    it('should show sidenav', () => {
      service.hide();
      expect(service.isHidden()).toBe(true);
      
      service.show();
      
      expect(service.isHidden()).toBe(false);
    });
  });

  describe('isVisible computed', () => {
    it('should be visible when open', () => {
      service.open();
      
      expect(service.isVisible()).toBe(true);
    });

    it('should be visible when closing', () => {
      service.open();
      service.close();
      
      expect(service.isVisible()).toBe(true);
    });

    it('should not be visible when closed and not closing', () => {
      expect(service.isVisible()).toBe(false);
      
      service.open();
      service.close();
      vi.advanceTimersByTime(300);
      
      expect(service.isVisible()).toBe(false);
    });
  });

  describe('animation timing', () => {
    it('should use 300ms animation duration', () => {
      service.open();
      service.close();
      
      expect(service.isClosing()).toBe(true);
      
      vi.advanceTimersByTime(299);
      expect(service.isClosing()).toBe(true);
      
      vi.advanceTimersByTime(1);
      expect(service.isClosing()).toBe(false);
    });

    it('should handle multiple close calls during animation', () => {
      service.open();
      service.close();
      service.close(); // Second call should not interfere
      
      vi.advanceTimersByTime(300);
      
      expect(service.isClosing()).toBe(false);
    });
  });
});