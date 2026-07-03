import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ScrollPositionService } from './scroll-position.service';

describe('ScrollPositionService', () => {
  let service: ScrollPositionService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(ScrollPositionService);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('returns undefined for a conversation with no remembered position', () => {
    expect(service.get('unknown')).toBeUndefined();
  });

  it('remembers positions per conversation independently', () => {
    service.save('a', 1200);
    service.save('b', 0);

    expect(service.get('a')).toBe(1200);
    expect(service.get('b')).toBe(0);
  });

  it('overwrites a conversation position on re-save', () => {
    service.save('a', 100);
    service.save('a', 900);
    expect(service.get('a')).toBe(900);
  });

  it('clears a single conversation without touching others', () => {
    service.save('a', 100);
    service.save('b', 200);

    service.clear('a');

    expect(service.get('a')).toBeUndefined();
    expect(service.get('b')).toBe(200);
  });
});
