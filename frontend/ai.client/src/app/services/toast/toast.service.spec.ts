import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ToastService, ToastType } from './toast.service';

describe('ToastService', () => {
  let service: ToastService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [ToastService]
    });
    service = TestBed.inject(ToastService);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  describe('toast types', () => {
    it('should show success toast', () => {
      service.success('Success', 'Operation completed');
      
      const toasts = service.toasts();
      expect(toasts).toHaveLength(1);
      expect(toasts[0].type).toBe('success');
      expect(toasts[0].title).toBe('Success');
      expect(toasts[0].message).toBe('Operation completed');
      expect(toasts[0].duration).toBe(5000);
    });

    it('should show error toast with extended duration', () => {
      service.error('Error', 'Something went wrong');
      
      const toasts = service.toasts();
      expect(toasts).toHaveLength(1);
      expect(toasts[0].type).toBe('error');
      expect(toasts[0].duration).toBe(8000);
    });

    it('should show warning toast', () => {
      service.warning('Warning', 'Please be careful');
      
      const toasts = service.toasts();
      expect(toasts).toHaveLength(1);
      expect(toasts[0].type).toBe('warning');
      expect(toasts[0].title).toBe('Warning');
    });

    it('should show info toast', () => {
      service.info('Info', 'For your information');
      
      const toasts = service.toasts();
      expect(toasts).toHaveLength(1);
      expect(toasts[0].type).toBe('info');
      expect(toasts[0].title).toBe('Info');
    });
  });

  describe('toast options', () => {
    it('should use custom duration', () => {
      service.success('Test', undefined, { duration: 3000 });
      
      const toasts = service.toasts();
      expect(toasts[0].duration).toBe(3000);
    });

    it('should set dismissible option', () => {
      service.success('Test', undefined, { dismissible: false });
      
      const toasts = service.toasts();
      expect(toasts[0].dismissible).toBe(false);
    });

    it('should override error duration with custom option', () => {
      service.error('Error', undefined, { duration: 2000 });
      
      const toasts = service.toasts();
      expect(toasts[0].duration).toBe(2000);
    });
  });

  describe('auto-dismiss', () => {
    it('should auto-dismiss after duration', () => {
      service.success('Test', undefined, { duration: 1000 });
      
      expect(service.toasts()).toHaveLength(1);
      
      vi.advanceTimersByTime(1000);
      
      expect(service.toasts()).toHaveLength(0);
    });

    it('should not auto-dismiss when duration is 0', () => {
      service.success('Test', undefined, { duration: 0 });
      
      expect(service.toasts()).toHaveLength(1);
      
      vi.advanceTimersByTime(10000);
      
      expect(service.toasts()).toHaveLength(1);
    });

    it('should handle multiple toasts with different durations', () => {
      service.success('First', undefined, { duration: 1000 });
      service.success('Second', undefined, { duration: 2000 });
      
      expect(service.toasts()).toHaveLength(2);
      
      vi.advanceTimersByTime(1000);
      expect(service.toasts()).toHaveLength(1);
      expect(service.toasts()[0].title).toBe('Second');
      
      vi.advanceTimersByTime(1000);
      expect(service.toasts()).toHaveLength(0);
    });
  });

  describe('dismiss methods', () => {
    it('should dismiss toast by ID', () => {
      service.success('First');
      service.success('Second');
      
      const toasts = service.toasts();
      const firstId = toasts[0].id;
      
      service.dismiss(firstId);
      
      const remainingToasts = service.toasts();
      expect(remainingToasts).toHaveLength(1);
      expect(remainingToasts[0].title).toBe('Second');
    });

    it('should dismiss all toasts', () => {
      service.success('First');
      service.error('Second');
      service.warning('Third');
      
      expect(service.toasts()).toHaveLength(3);
      
      service.dismissAll();
      
      expect(service.toasts()).toHaveLength(0);
    });

    it('should handle dismiss of non-existent ID gracefully', () => {
      service.success('Test');
      
      service.dismiss('non-existent-id');
      
      expect(service.toasts()).toHaveLength(1);
    });
  });

  describe('toast properties', () => {
    it('should generate unique IDs', () => {
      service.success('First');
      service.success('Second');
      
      const toasts = service.toasts();
      expect(toasts[0].id).not.toBe(toasts[1].id);
      expect(toasts[0].id).toMatch(/^toast-\d+-[a-z0-9]+$/);
    });

    it('should set timestamp', () => {
      const before = new Date();
      service.success('Test');
      const after = new Date();
      
      const toast = service.toasts()[0];
      expect(toast.timestamp.getTime()).toBeGreaterThanOrEqual(before.getTime());
      expect(toast.timestamp.getTime()).toBeLessThanOrEqual(after.getTime());
    });

    it('should default to dismissible true', () => {
      service.success('Test');
      
      expect(service.toasts()[0].dismissible).toBe(true);
    });
  });
});