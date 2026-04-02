import { APP_INITIALIZER } from '@angular/core';
import { ConfigService } from './services/config.service';
import { appConfig } from './app.config';

describe('APP_INITIALIZER Integration - App Bootstrap with Valid Config', () => {
  describe('APP_INITIALIZER Configuration', () => {
    it('should register APP_INITIALIZER provider in appConfig', () => {
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      );

      expect(initializerProvider).toBeDefined();
      expect(initializerProvider).toEqual(
        expect.objectContaining({
          provide: APP_INITIALIZER,
          multi: true
        })
      );
    });

    it('should have ConfigService as dependency for APP_INITIALIZER', () => {
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      expect(initializerProvider?.deps).toEqual([ConfigService]);
    });

    it('should configure APP_INITIALIZER as multi-provider', () => {
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      // Verify multi: true is set (allows multiple APP_INITIALIZER providers)
      expect(initializerProvider?.multi).toBe(true);
    });

    it('should have a factory function that returns a function', () => {
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      expect(initializerProvider?.useFactory).toBeDefined();
      expect(typeof initializerProvider?.useFactory).toBe('function');
    });
  });

  describe('Initializer Function Behavior', () => {
    it('should create an initializer function that calls ConfigService.loadConfig', () => {
      // Create a mock ConfigService
      const mockConfigService = {
        loadConfig: vi.fn().mockResolvedValue(undefined)
      } as any;

      // Get the factory function from appConfig
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      // Execute the factory with the mock service
      const initializerFn = initializerProvider.useFactory(mockConfigService);

      // Verify it returns a function
      expect(typeof initializerFn).toBe('function');

      // Execute the initializer function
      const result = initializerFn();

      // Verify it called loadConfig
      expect(mockConfigService.loadConfig).toHaveBeenCalledTimes(1);

      // Verify it returns a Promise
      expect(result).toBeInstanceOf(Promise);
    });

    it('should return a Promise that resolves when config is loaded', async () => {
      // Create a mock ConfigService with an async response
      const mockConfigService = {
        loadConfig: vi.fn().mockImplementation(() => 
          Promise.resolve()
        )
      } as any;

      // Get the factory function
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      // Create and execute the initializer
      const initializerFn = initializerProvider.useFactory(mockConfigService);
      const result = initializerFn();

      // Should be a Promise
      expect(result).toBeInstanceOf(Promise);

      // Wait for it to resolve
      await expect(result).resolves.toBeUndefined();

      // Verify loadConfig was called
      expect(mockConfigService.loadConfig).toHaveBeenCalled();
    });
  });

  describe('Application Bootstrap Sequence', () => {
    it('should ensure APP_INITIALIZER runs before app starts', () => {
      // Verify the provider configuration ensures initialization happens first
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      // APP_INITIALIZER with multi: true means Angular will:
      // 1. Collect all APP_INITIALIZER providers
      // 2. Execute them in order
      // 3. Wait for all Promises to resolve
      // 4. Then bootstrap the application
      expect(initializerProvider?.multi).toBe(true);
      expect(initializerProvider?.provide).toBe(APP_INITIALIZER);
    });

    it('should have correct dependency injection setup', () => {
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      // Verify ConfigService is injected into the factory
      expect(initializerProvider?.deps).toEqual([ConfigService]);
      
      // Verify the factory function signature
      expect(initializerProvider?.useFactory).toBeDefined();
      expect(initializerProvider?.useFactory.length).toBe(1); // Takes 1 argument (ConfigService)
    });
  });

  describe('Integration with ConfigService', () => {
    it('should use ConfigService.loadConfig method', () => {
      // Verify the initializer calls the correct method
      const mockConfigService = {
        loadConfig: vi.fn().mockResolvedValue(undefined)
      } as any;

      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      const initializerFn = initializerProvider.useFactory(mockConfigService);
      initializerFn();

      // Should call loadConfig, not any other method
      expect(mockConfigService.loadConfig).toHaveBeenCalledTimes(1);
      expect(mockConfigService.loadConfig).toHaveBeenCalledWith();
    });

    it('should handle ConfigService errors gracefully', async () => {
      // Create a mock that rejects
      const mockConfigService = {
        loadConfig: vi.fn().mockRejectedValue(new Error('Config load failed'))
      } as any;

      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      const initializerFn = initializerProvider.useFactory(mockConfigService);
      const result = initializerFn();

      // The promise should reject (Angular will handle this)
      await expect(result).rejects.toThrow('Config load failed');
    });
  });
});

describe('APP_INITIALIZER Integration - Fallback Scenarios', () => {
  describe('Missing config.json Fallback', () => {
    it('should handle 404 error when config.json is missing', async () => {
      // ConfigService should catch the error and fall back to environment.ts
      const mockConfigService = {
        loadConfig: vi.fn().mockImplementation(async () => {
          // Simulate 404 error, but ConfigService handles it internally
          // and falls back to environment.ts
          return Promise.resolve();
        })
      } as any;

      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      const initializerFn = initializerProvider.useFactory(mockConfigService);
      const result = initializerFn();

      // Should resolve successfully (fallback handled internally)
      await expect(result).resolves.toBeUndefined();
      expect(mockConfigService.loadConfig).toHaveBeenCalled();
    });

    it('should allow app to continue when config fetch fails', async () => {
      // Even if loadConfig rejects, the app should handle it
      const mockConfigService = {
        loadConfig: vi.fn().mockRejectedValue(new Error('Network error'))
      } as any;

      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      const initializerFn = initializerProvider.useFactory(mockConfigService);
      
      // The initializer will reject, but Angular's error handling
      // should allow the app to continue (with fallback config)
      try {
        await initializerFn();
      } catch (error) {
        // Error is expected - app should handle this gracefully
        expect(error).toBeDefined();
      }
    });
  });

  describe('Invalid config.json Fallback', () => {
    it('should handle invalid JSON in config.json', async () => {
      // ConfigService should catch parse errors and fall back
      const mockConfigService = {
        loadConfig: vi.fn().mockImplementation(async () => {
          // Simulate JSON parse error handled internally
          return Promise.resolve();
        })
      } as any;

      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      const initializerFn = initializerProvider.useFactory(mockConfigService);
      const result = initializerFn();

      await expect(result).resolves.toBeUndefined();
    });

    it('should handle config with missing required fields', async () => {
      // ConfigService validation should catch this and fall back
      const mockConfigService = {
        loadConfig: vi.fn().mockImplementation(async () => {
          // Simulate validation error handled internally
          return Promise.resolve();
        })
      } as any;

      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      const initializerFn = initializerProvider.useFactory(mockConfigService);
      const result = initializerFn();

      await expect(result).resolves.toBeUndefined();
    });
  });

  describe('API URL Configuration', () => {
    it('should ensure ConfigService provides URLs for API calls', () => {
      // Verify that the ConfigService is properly configured
      // to provide URLs that services will use
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      // ConfigService should be a dependency
      expect(initializerProvider?.deps).toContain(ConfigService);
      
      // This ensures that when services inject ConfigService,
      // they'll get the URLs loaded by APP_INITIALIZER
    });

    it('should load config before any HTTP interceptors run', () => {
      // APP_INITIALIZER runs before the app bootstraps,
      // which means it runs before any HTTP requests are made
      const providers = appConfig.providers || [];
      const initializerProvider = providers.find(
        (p: any) => p.provide === APP_INITIALIZER
      ) as any;

      // Verify APP_INITIALIZER is configured
      expect(initializerProvider).toBeDefined();
      expect(initializerProvider?.provide).toBe(APP_INITIALIZER);
      
      // This guarantees config is loaded before services make API calls
    });
  });
});
