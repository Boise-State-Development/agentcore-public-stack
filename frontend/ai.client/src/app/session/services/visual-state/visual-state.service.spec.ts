import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { VisualStateService } from './visual-state.service';
import { SessionService } from '../session/session.service';
import { signal } from '@angular/core';

describe('VisualStateService', () => {
  let service: VisualStateService;
  let httpMock: HttpTestingController;
  let mockSessionService: any;
  let currentSessionSignal: any;

  beforeEach(() => {
    TestBed.resetTestingModule();

    currentSessionSignal = signal({
      sessionId: 'session-1',
      preferences: {}
    });

    mockSessionService = {
      currentSession: currentSessionSignal,
      updateSessionMetadata: vi.fn().mockResolvedValue(undefined)
    };

    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        VisualStateService,
        { provide: SessionService, useValue: mockSessionService }
      ]
    });

    service = TestBed.inject(VisualStateService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should check if visual is dismissed (default false)', () => {
    expect(service.isDismissed('tool-1')).toBe(false);
  });

  it('should check if visual is expanded (default true)', () => {
    expect(service.isExpanded('tool-1')).toBe(true);
  });

  it('should dismiss visual', () => {
    service.dismiss('tool-1');
    expect(service.isDismissed('tool-1')).toBe(true);
  });

  it('should toggle expanded state', () => {
    expect(service.isExpanded('tool-1')).toBe(true);
    
    service.toggleExpanded('tool-1');
    expect(service.isExpanded('tool-1')).toBe(false);
    
    service.toggleExpanded('tool-1');
    expect(service.isExpanded('tool-1')).toBe(true);
  });

  it('dismiss should set dismissed to true while preserving expanded default', () => {
    service.dismiss('tool-1');
    expect(service.isDismissed('tool-1')).toBe(true);
    expect(service.isExpanded('tool-1')).toBe(true);
  });

  it('toggleExpanded should preserve dismissed state', () => {
    service.dismiss('tool-1');
    service.toggleExpanded('tool-1');
    expect(service.isDismissed('tool-1')).toBe(true);
    expect(service.isExpanded('tool-1')).toBe(false);
  });

  it('updateState should schedule save', async () => {
    vi.useFakeTimers();
    try {
      service.dismiss('tool-1');
      vi.advanceTimersByTime(600);
      await vi.waitFor(() => {
        expect(mockSessionService.updateSessionMetadata).toHaveBeenCalled();
      });
    } finally {
      vi.useRealTimers();
    }
  });

  it('session change should load visual state from preferences', async () => {
    currentSessionSignal.set({
      sessionId: 'session-2',
      preferences: {
        visualState: {
          'tool-1': { dismissed: true, expanded: false }
        }
      }
    });
    // Effects are async, wait for them
    await vi.waitFor(() => {
      expect(service.isDismissed('tool-1')).toBe(true);
    });
    expect(service.isExpanded('tool-1')).toBe(false);
  });

  it('session change should clear state when no visualState in preferences', async () => {
    service.dismiss('tool-1');
    expect(service.isDismissed('tool-1')).toBe(true);

    currentSessionSignal.set({
      sessionId: 'session-3',
      preferences: {}
    });
    // Effects are async, wait for them
    await vi.waitFor(() => {
      expect(service.isDismissed('tool-1')).toBe(false);
    });
  });

  it('saveToBackend should call updateSessionMetadata with visualState', async () => {
    vi.useFakeTimers();
    try {
      service.dismiss('tool-1');
      vi.advanceTimersByTime(600);
      await vi.waitFor(() => {
        expect(mockSessionService.updateSessionMetadata).toHaveBeenCalledWith(
          'session-1',
          { visualState: expect.any(Object) }
        );
      });
    } finally {
      vi.useRealTimers();
    }
  });
});