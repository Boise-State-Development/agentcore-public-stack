import { TestBed } from '@angular/core/testing';
import { SystemService, FirstBootError } from './system.service';
import { ConfigService } from './config.service';
import { signal } from '@angular/core';
import { describe, it, expect, vi, beforeEach } from 'vitest';

describe('SystemService', () => {
  let service: SystemService;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        SystemService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(SystemService);
    vi.restoreAllMocks();
  });

  describe('checkStatus', () => {
    it('should return true when first_boot_completed is true', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ first_boot_completed: true }), { status: 200 }));
      expect(await service.checkStatus()).toBe(true);
    });

    it('should return false when first_boot_completed is false', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ first_boot_completed: false }), { status: 200 }));
      expect(await service.checkStatus()).toBe(false);
    });

    it('should return cached value on subsequent calls', async () => {
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ first_boot_completed: true }), { status: 200 }));
      await service.checkStatus();
      await service.checkStatus();
      expect(fetchSpy).toHaveBeenCalledTimes(1);
    });

    it('should return false on non-OK response', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('error', { status: 500 }));
      expect(await service.checkStatus()).toBe(false);
    });

    it('should return false on network error', async () => {
      vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('Network error'));
      expect(await service.checkStatus()).toBe(false);
    });
  });

  describe('firstBoot', () => {
    it('should return response and update cache on success', async () => {
      const body = { success: true, user_id: 'u1', message: 'ok' };
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify(body), { status: 200 }));
      const result = await service.firstBoot('admin', 'a@b.com', 'Pass1234!');
      expect(result).toEqual(body);
      // Cache should now be true — next checkStatus should not hit the API again
      fetchSpy.mockClear();
      expect(await service.checkStatus()).toBe(true);
      expect(fetchSpy).not.toHaveBeenCalled();
    });

    it('should throw FirstBootError on non-OK response with JSON body', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ detail: 'Already completed' }), { status: 409 }));
      await expect(service.firstBoot('admin', 'a@b.com', 'Pass1234!')).rejects.toThrow(FirstBootError);
    });

    it('should throw FirstBootError with fallback message on non-JSON error body', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response('not json', { status: 500, headers: { 'Content-Type': 'text/plain' } }));
      try {
        await service.firstBoot('admin', 'a@b.com', 'Pass1234!');
        expect.unreachable('should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(FirstBootError);
        expect((e as FirstBootError).message).toContain('Unknown error');
      }
    });
  });

  describe('clearCache', () => {
    it('should force re-fetch after clearing cache', async () => {
      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(JSON.stringify({ first_boot_completed: true }), { status: 200 }));
      await service.checkStatus();
      expect(fetchSpy).toHaveBeenCalledTimes(1);
      service.clearCache();
      await service.checkStatus();
      expect(fetchSpy).toHaveBeenCalledTimes(2);
    });
  });
});
