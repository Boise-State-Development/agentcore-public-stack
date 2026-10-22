import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { ConnectorListPage } from './connector-list.page';
import { ConnectorsService } from '../services/connectors.service';
import { Connector } from '../models/connector.model';

const CONNECTOR = {
  providerId: 'example-drive',
  displayName: 'Example Drive',
  providerType: 'google',
  scopes: ['openid'],
  allowedRoles: [],
  enabled: true,
  iconName: 'heroLink',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
} as Connector;

/** Accessible names of every control whose only content is an icon. */
function iconOnlyControlNames(el: HTMLElement): (string | null)[] {
  return Array.from(el.querySelectorAll('button, a'))
    .filter(c => c.querySelector('ng-icon') && !c.textContent?.trim())
    .map(c => c.getAttribute('aria-label'));
}

describe('ConnectorListPage icon-only controls', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [ConnectorListPage],
      providers: [
        provideRouter([]),
        {
          provide: ConnectorsService,
          useValue: {
            connectorsResource: { isLoading: signal(false), error: signal(null) },
            getConnectors: () => [CONNECTOR],
            reload: vi.fn(),
            deleteConnector: vi.fn(),
          },
        },
      ],
    });
  });

  // Icon-only controls: the tooltip only describes them (aria-describedby), so without
  // a label a screen reader announced the row actions and Clear search as "button".
  it('names the row actions after their connector', () => {
    const fixture = TestBed.createComponent(ConnectorListPage);
    fixture.detectChanges();

    expect(iconOnlyControlNames(fixture.nativeElement)).toEqual([
      'Edit Example Drive',
      'Delete Example Drive',
    ]);
  });

  it('names the clear-search button once a query is entered', () => {
    const fixture = TestBed.createComponent(ConnectorListPage);
    fixture.componentInstance.searchQuery.set('example');
    fixture.detectChanges();

    const el = fixture.nativeElement as HTMLElement;
    expect(iconOnlyControlNames(el)).toEqual([
      'Clear search',
      'Edit Example Drive',
      'Delete Example Drive',
    ]);
    for (const b of Array.from(el.querySelectorAll('button[aria-label]'))) {
      expect(b.getAttribute('type')).toBe('button');
    }
  });
});
