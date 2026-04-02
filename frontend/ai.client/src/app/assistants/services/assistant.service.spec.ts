import { TestBed } from '@angular/core/testing';
import { HttpClientTestingModule, HttpTestingController } from '@angular/common/http/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { AssistantService } from './assistant.service';
import { AssistantApiService } from './assistant-api.service';
import { of } from 'rxjs';

describe('AssistantService', () => {
  let service: AssistantService;
  let httpMock: HttpTestingController;
  let mockApiService: any;

  beforeEach(() => {
    TestBed.resetTestingModule();

    mockApiService = {
      createDraft: vi.fn(),
      getAssistants: vi.fn(),
      createAssistant: vi.fn(),
      updateAssistant: vi.fn(),
      deleteAssistant: vi.fn(),
      getAssistant: vi.fn(),
      shareAssistant: vi.fn(),
      unshareAssistant: vi.fn(),
      getAssistantShares: vi.fn()
    };

    TestBed.configureTestingModule({
      imports: [HttpClientTestingModule],
      providers: [
        AssistantService,
        { provide: AssistantApiService, useValue: mockApiService }
      ]
    });

    service = TestBed.inject(AssistantService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    httpMock.match(() => true);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should create draft assistant', async () => {
    const mockAssistant = { assistantId: 'draft-1', name: 'Draft Assistant' };
    mockApiService.createDraft.mockReturnValue(of(mockAssistant));

    const result = await service.createDraft({});

    expect(mockApiService.createDraft).toHaveBeenCalledWith({});
    expect(result).toEqual(mockAssistant);
  });

  it('should load assistants', async () => {
    const mockResponse = { assistants: [{ assistantId: '1', name: 'Assistant 1' }] };
    mockApiService.getAssistants.mockReturnValue(of(mockResponse));

    await service.loadAssistants(true, true);

    expect(mockApiService.getAssistants).toHaveBeenCalledWith({
      includeDrafts: true,
      includePublic: true
    });
    expect(service.assistants$()).toEqual(mockResponse.assistants);
  });

  it('should create assistant', async () => {
    const mockRequest = { name: 'New Assistant', description: 'Test' } as any;
    const mockAssistant = { assistantId: '1', ...mockRequest };
    mockApiService.createAssistant.mockReturnValue(of(mockAssistant));

    const result = await service.createAssistant(mockRequest);

    expect(mockApiService.createAssistant).toHaveBeenCalledWith(mockRequest);
    expect(result).toEqual(mockAssistant);
    expect(service.assistants$()).toContain(mockAssistant);
  });

  it('should get assistant', async () => {
    const mockAssistant = { assistantId: '1', name: 'Assistant' };
    mockApiService.getAssistant.mockReturnValue(of(mockAssistant));

    const result = await service.getAssistant('1');

    expect(mockApiService.getAssistant).toHaveBeenCalledWith('1');
    expect(result).toEqual(mockAssistant);
    expect(service.assistants$()).toContain(mockAssistant);
  });

  it('should share assistant', async () => {
    const mockResponse = { sharedWith: ['user1@example.com'] };
    mockApiService.shareAssistant.mockReturnValue(of(mockResponse));

    const result = await service.shareAssistant('1', ['user1@example.com']);

    expect(mockApiService.shareAssistant).toHaveBeenCalledWith('1', { emails: ['user1@example.com'] });
    expect(result).toEqual(mockResponse);
  });

  it('should unshare assistant', async () => {
    const mockResponse = { sharedWith: [] };
    mockApiService.unshareAssistant.mockReturnValue(of(mockResponse));

    const result = await service.unshareAssistant('1', ['user1@example.com']);

    expect(mockApiService.unshareAssistant).toHaveBeenCalledWith('1', { emails: ['user1@example.com'] });
    expect(result).toEqual(mockResponse);
  });

  it('should get assistant shares', async () => {
    const mockResponse = { sharedWith: ['user1@example.com', 'user2@example.com'] };
    mockApiService.getAssistantShares.mockReturnValue(of(mockResponse));

    const result = await service.getAssistantShares('1');

    expect(mockApiService.getAssistantShares).toHaveBeenCalledWith('1');
    expect(result).toEqual(['user1@example.com', 'user2@example.com']);
  });
});