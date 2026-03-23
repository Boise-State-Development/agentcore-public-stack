import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { ApiKeyService, ApiKey, CreateApiKeyResponse } from './api-key.service';
import { ConfigService } from '../../../services/config.service';

describe('ApiKeyService', () => {
  let service: ApiKeyService;
  let httpMock: HttpTestingController;
  let mockConfigService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockConfigService = {
      appApiUrl: vi.fn(() => 'http://localhost:8000')
    };

    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        ApiKeyService,
        { provide: ConfigService, useValue: mockConfigService }
      ]
    });

    service = TestBed.inject(ApiKeyService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
    vi.clearAllMocks();
  });

  describe('getKey', () => {
    it('should make GET request to correct URL', () => {
      const mockApiKey: ApiKey = {
        key_id: 'key-123',
        name: 'Test Key',
        created_at: '2024-01-01T00:00:00Z',
        expires_at: '2025-01-01T00:00:00Z',
        last_used_at: null
      };

      service.getKey().subscribe(key => {
        expect(key).toEqual(mockApiKey);
      });

      const req = httpMock.expectOne('http://localhost:8000/auth/api-keys');
      expect(req.request.method).toBe('GET');
      req.flush({ key: mockApiKey });
    });

    it('should return null when no key exists', () => {
      service.getKey().subscribe(key => {
        expect(key).toBeNull();
      });

      const req = httpMock.expectOne('http://localhost:8000/auth/api-keys');
      expect(req.request.method).toBe('GET');
      req.flush({ key: null });
    });
  });

  describe('createKey', () => {
    it('should make POST request with correct URL and body', () => {
      const keyName = 'My API Key';
      const mockResponse: CreateApiKeyResponse = {
        key_id: 'key-123',
        name: keyName,
        key: 'ak_test_key_value',
        created_at: '2024-01-01T00:00:00Z',
        expires_at: '2025-01-01T00:00:00Z'
      };

      service.createKey(keyName).subscribe(response => {
        expect(response).toEqual(mockResponse);
      });

      const req = httpMock.expectOne('http://localhost:8000/auth/api-keys');
      expect(req.request.method).toBe('POST');
      expect(req.request.body).toEqual({ name: keyName });
      req.flush(mockResponse);
    });
  });

  describe('deleteKey', () => {
    it('should make DELETE request to correct URL', () => {
      const keyId = 'key-123';
      const mockResponse = {
        key_id: keyId,
        deleted: true
      };

      service.deleteKey(keyId).subscribe(response => {
        expect(response).toEqual(mockResponse);
      });

      const req = httpMock.expectOne(`http://localhost:8000/auth/api-keys/${keyId}`);
      expect(req.request.method).toBe('DELETE');
      req.flush(mockResponse);
    });
  });
});