import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { StatusBadgeComponent } from './status-badge.component';

describe('StatusBadgeComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function createComponent(status: string) {
    const fixture = TestBed.createComponent(StatusBadgeComponent);
    fixture.componentRef.setInput('status', status);
    fixture.detectChanges();
    return fixture.componentInstance;
  }

  it('should display the status text', () => {
    const component = createComponent('PENDING');
    expect(component.status()).toBe('PENDING');
  });

  it('should return warning classes for PENDING', () => {
    const component = createComponent('PENDING');
    expect(component.badgeClasses()).toContain('bg-state-warning-100');
    expect(component.badgeClasses()).toContain('text-state-warning-800');
  });

  it('should return info classes for TRAINING', () => {
    const component = createComponent('TRAINING');
    expect(component.badgeClasses()).toContain('bg-state-info-100');
    expect(component.badgeClasses()).toContain('text-state-info-800');
  });

  it('should return info classes for TRANSFORMING', () => {
    const component = createComponent('TRANSFORMING');
    expect(component.badgeClasses()).toContain('bg-state-info-100');
  });

  it('should return success classes for COMPLETED', () => {
    const component = createComponent('COMPLETED');
    expect(component.badgeClasses()).toContain('bg-state-success-100');
    expect(component.badgeClasses()).toContain('text-state-success-800');
  });

  it('should return danger classes for FAILED', () => {
    const component = createComponent('FAILED');
    expect(component.badgeClasses()).toContain('bg-state-danger-100');
    expect(component.badgeClasses()).toContain('text-state-danger-800');
  });

  it('should return gray classes for STOPPED', () => {
    const component = createComponent('STOPPED');
    expect(component.badgeClasses()).toContain('bg-gray-100');
    expect(component.badgeClasses()).toContain('text-gray-800');
  });

  it('should return gray classes for unknown status', () => {
    const component = createComponent('UNKNOWN');
    expect(component.badgeClasses()).toContain('bg-gray-100');
  });

  it('should include base pill badge classes', () => {
    const component = createComponent('PENDING');
    expect(component.badgeClasses()).toContain('rounded-full');
    expect(component.badgeClasses()).toContain('text-xs');
    expect(component.badgeClasses()).toContain('font-medium');
  });
});
