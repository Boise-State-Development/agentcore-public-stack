import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { ConfigService, RuntimeConfig } from './config.service';

describe('ConfigService', () => {
  let service: ConfigService;
  let httpMock: HttpTestingController;

  const validConfig: RuntimeConfig = {
    appApiUrl: 'https://api.example.com',
    inferenceApiUrl: 'https://inference.example.com',
    environment: 'production',
    version: '1.0.0-beta.1',
    cognitoDomainUrl: 'https://myprefix.auth.us-east-1.amazoncognito.com',
    cognitoAppClientId: 'test-client-id',
    cognitoRegion: 'us-east-1',
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [ConfigService]
    });
    
    service = TestBed.inject(ConfigService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  describe('loadConfig', () => {
    it('should load configuration from /config.json successfully', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      expect(req.request.method).toBe('GET');
      req.flush(validConfig);
      
      await loadPromise;
      
      expect(service.loaded()).toBe(true);
      expect(service.error()).toBeNull();
      expect(service.appApiUrl()).toBe(validConfig.appApiUrl);
      expect(service.environment()).toBe(validConfig.environment);
    });

    it('should validate configuration before storing', async () => {
      const invalidConfig = {
        appApiUrl: 'not-a-url',
        environment: 'production'
      };
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(invalidConfig);
      
      await loadPromise;
      
      // Should fall back to environment.ts due to validation error
      expect(service.loaded()).toBe(true);
      expect(service.error()).not.toBeNull();
      expect(service.appApiUrl()).toBe('http://localhost:8000'); // fallback value
    });

    it('should fall back to environment.ts on HTTP error', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.error(new ProgressEvent('error'), { status: 404, statusText: 'Not Found' });
      
      await loadPromise;
      
      expect(service.loaded()).toBe(true);
      expect(service.error()).not.toBeNull();
      expect(service.appApiUrl()).toBe('http://localhost:8000'); // fallback value
    });

    it('should fall back to environment.ts on network error', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.error(new ProgressEvent('Network error'));
      
      await loadPromise;
      
      expect(service.loaded()).toBe(true);
      expect(service.error()).not.toBeNull();
      expect(service.isConfigLoaded()).toBe(true);
    });

    it('should handle missing appApiUrl', async () => {
      const invalidConfig = {
        environment: 'production'
      };
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(invalidConfig);
      
      await loadPromise;
      
      // Should fall back due to validation error
      expect(service.loaded()).toBe(true);
      expect(service.error()).not.toBeNull();
    });

    it('should handle missing environment field', async () => {
      const invalidConfig = {
        appApiUrl: 'https://valid.com'
      };
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(invalidConfig);
      
      await loadPromise;
      
      // Should fall back due to validation error
      expect(service.loaded()).toBe(true);
      expect(service.error()).not.toBeNull();
    });

    it('should handle invalid JSON', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush('invalid json', { status: 200, statusText: 'OK' });
      
      await loadPromise;
      
      // Should fall back due to parse error
      expect(service.loaded()).toBe(true);
      expect(service.error()).not.toBeNull();
    });
  });

  describe('computed signals', () => {
    it('should return empty string for appApiUrl when not loaded', () => {
      expect(service.appApiUrl()).toBe('');
    });

    it('should return "development" for environment when not loaded', () => {
      expect(service.environment()).toBe('development');
    });

    it('should return "unknown" for version when not loaded', () => {
      expect(service.version()).toBe('unknown');
    });

    it('should return correct values after loading', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(validConfig);
      
      await loadPromise;
      
      expect(service.appApiUrl()).toBe(validConfig.appApiUrl);
      expect(service.environment()).toBe(validConfig.environment);
      expect(service.version()).toBe(validConfig.version);
    });
  });

  describe('get method', () => {
    it('should throw error when config not loaded', () => {
      expect(() => service.get('appApiUrl')).toThrowError(
        'Configuration not loaded. Ensure APP_INITIALIZER has completed.'
      );
    });

    it('should return correct value for valid key', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(validConfig);
      
      await loadPromise;
      
      expect(service.get('appApiUrl')).toBe(validConfig.appApiUrl);
      expect(service.get('environment')).toBe(validConfig.environment);
    });
  });

  describe('getConfig method', () => {
    it('should return null when config not loaded', () => {
      expect(service.getConfig()).toBeNull();
    });

    it('should return full config object after loading', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(validConfig);
      
      await loadPromise;
      
      const config = service.getConfig();
      expect(config).toEqual(validConfig);
    });
  });

  describe('isConfigLoaded method', () => {
    it('should return false when config not loaded', () => {
      expect(service.isConfigLoaded()).toBe(false);
    });

    it('should return true after successful load', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(validConfig);
      
      await loadPromise;
      
      expect(service.isConfigLoaded()).toBe(true);
    });

    it('should return true after fallback load', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.error(new ProgressEvent('error'));
      
      await loadPromise;
      
      expect(service.isConfigLoaded()).toBe(true);
    });
  });

  describe('loading state', () => {
    it('should track loading state correctly', async () => {
      expect(service.loaded()).toBe(false);
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(validConfig);
      
      await loadPromise;
      
      expect(service.loaded()).toBe(true);
    });

    it('should track error state on failure', async () => {
      expect(service.error()).toBeNull();
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.error(new ProgressEvent('Network error'));
      
      await loadPromise;
      
      expect(service.error()).not.toBeNull();
    });

    it('should clear error state on successful load', async () => {
      // First load with error
      let loadPromise = service.loadConfig();
      let req = httpMock.expectOne('/config.json');
      req.error(new ProgressEvent('error'));
      await loadPromise;
      
      expect(service.error()).not.toBeNull();
      
      // Second load successfully
      loadPromise = service.loadConfig();
      req = httpMock.expectOne('/config.json');
      req.flush(validConfig);
      await loadPromise;
      
      expect(service.error()).toBeNull();
    });
  });

  describe('URL validation', () => {
    it('should accept valid HTTP URLs', async () => {
      const config = {
        ...validConfig,
        appApiUrl: 'http://localhost:8000'
      };
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(config);
      
      await loadPromise;
      
      expect(service.appApiUrl()).toBe(config.appApiUrl);
    });

    it('should accept valid HTTPS URLs', async () => {
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(validConfig);
      
      await loadPromise;
      
      expect(service.appApiUrl()).toBe(validConfig.appApiUrl);
    });

    it('should reject invalid URLs', async () => {
      const invalidConfig = {
        ...validConfig,
        appApiUrl: 'not a url'
      };
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(invalidConfig);
      
      await loadPromise;
      
      // Should fall back due to validation error
      expect(service.error()).not.toBeNull();
      expect(service.appApiUrl()).toBe('http://localhost:8000'); // fallback
    });

    it('should reject empty URLs', async () => {
      const invalidConfig = {
        ...validConfig,
        appApiUrl: ''
      };
      
      const loadPromise = service.loadConfig();
      
      const req = httpMock.expectOne('/config.json');
      req.flush(invalidConfig);
      
      await loadPromise;
      
      // Should fall back due to validation error
      expect(service.error()).not.toBeNull();
    });
  });
});
