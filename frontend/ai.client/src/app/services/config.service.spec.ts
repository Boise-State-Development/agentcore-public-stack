import { TestBed } from '@angular/core/testing';
import { ConfigService } from './config.service';
import { environment } from '../../environments/environment';

describe('ConfigService', () => {
  let service: ConfigService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(ConfigService);
  });

  it('exposes appApiUrl from the build-time environment', () => {
    expect(service.appApiUrl()).toBe(environment.appApiUrl);
  });

  it('returns localhost in the dev (un-replaced) environment', () => {
    // Sanity check that the spec is exercising the dev environment.ts —
    // production builds swap this out via angular.json fileReplacements.
    expect(service.appApiUrl()).toBe('http://localhost:8000');
  });
});
