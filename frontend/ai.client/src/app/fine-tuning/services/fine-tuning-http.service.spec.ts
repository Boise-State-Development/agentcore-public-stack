import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { FineTuningHttpService } from './fine-tuning-http.service';
import { ConfigService } from '../../services/config.service';

const BASE = 'http://localhost:8000/fine-tuning';

describe('FineTuningHttpService', () => {
  let service: FineTuningHttpService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        FineTuningHttpService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(FineTuningHttpService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
    TestBed.resetTestingModule();
  });

  // ── Access ──────────────────────────────────────────────────────────

  it('should check access via GET /access', () => {
    const mockResponse = { has_access: true, monthly_quota_hours: 10, current_month_usage_hours: 2, quota_period: '2026-03' };
    service.checkAccess().subscribe(result => {
      expect(result).toEqual(mockResponse);
    });
    const req = httpMock.expectOne(`${BASE}/access`);
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  // ── Model Catalog ─────────────────────────────────────────────────

  it('should list models via GET /models', () => {
    const mockModels = [{ model_id: 'm1', model_name: 'Llama', huggingface_model_id: 'meta-llama/3', description: 'desc', default_instance_type: 'ml.g5.2xlarge', default_hyperparameters: {} }];
    service.listModels().subscribe(result => {
      expect(result).toEqual(mockModels);
    });
    const req = httpMock.expectOne(`${BASE}/models`);
    expect(req.request.method).toBe('GET');
    req.flush(mockModels);
  });

  // ── Training Jobs ─────────────────────────────────────────────────

  it('should presign dataset upload via POST /presign', () => {
    const request = { filename: 'data.jsonl', content_type: 'application/json' };
    const mockResponse = { presigned_url: 'https://s3.example.com', s3_key: 'key', expires_at: '2026-03-14T00:00:00Z' };
    service.presignDatasetUpload(request).subscribe(result => {
      expect(result).toEqual(mockResponse);
    });
    const req = httpMock.expectOne(`${BASE}/presign`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(request);
    req.flush(mockResponse);
  });

  it('should create training job via POST /jobs', () => {
    const request = { model_id: 'm1', dataset_s3_key: 'key' };
    const mockJob = { job_id: 'j1', status: 'PENDING' };
    service.createTrainingJob(request).subscribe(result => {
      expect(result).toEqual(mockJob);
    });
    const req = httpMock.expectOne(`${BASE}/jobs`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(request);
    req.flush(mockJob);
  });

  it('should list training jobs via GET /jobs', () => {
    const mockResponse = { jobs: [], total_count: 0 };
    service.listTrainingJobs().subscribe(result => {
      expect(result).toEqual(mockResponse);
    });
    const req = httpMock.expectOne(`${BASE}/jobs`);
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should get a single training job via GET /jobs/:id', () => {
    const mockJob = { job_id: 'j1', status: 'TRAINING' };
    service.getTrainingJob('j1').subscribe(result => {
      expect(result).toEqual(mockJob);
    });
    const req = httpMock.expectOne(`${BASE}/jobs/j1`);
    expect(req.request.method).toBe('GET');
    req.flush(mockJob);
  });

  it('should stop training job via DELETE /jobs/:id', () => {
    const mockJob = { job_id: 'j1', status: 'STOPPED' };
    service.stopTrainingJob('j1').subscribe(result => {
      expect(result).toEqual(mockJob);
    });
    const req = httpMock.expectOne(`${BASE}/jobs/j1`);
    expect(req.request.method).toBe('DELETE');
    req.flush(mockJob);
  });

  it('should get training job logs via GET /jobs/:id/logs', () => {
    const mockLogs = { logs: ['line1', 'line2'] };
    service.getTrainingJobLogs('j1').subscribe(result => {
      expect(result).toEqual(mockLogs);
    });
    const req = httpMock.expectOne(`${BASE}/jobs/j1/logs`);
    expect(req.request.method).toBe('GET');
    req.flush(mockLogs);
  });

  it('should download training artifact via GET /jobs/:id/download', () => {
    const mockDownload = { download_url: 'https://s3.example.com/artifact', expires_at: '2026-03-14T00:00:00Z' };
    service.downloadTrainingArtifact('j1').subscribe(result => {
      expect(result).toEqual(mockDownload);
    });
    const req = httpMock.expectOne(`${BASE}/jobs/j1/download`);
    expect(req.request.method).toBe('GET');
    req.flush(mockDownload);
  });

  // ── Trained Models ────────────────────────────────────────────────

  it('should list trained models via GET /trained-models', () => {
    const mockModels = [{ training_job_id: 'j1', model_id: 'm1', model_name: 'Llama', model_s3_path: 's3://bucket/model', instance_type: 'ml.g5.2xlarge', completed_at: null, estimated_cost_usd: null }];
    service.listTrainedModels().subscribe(result => {
      expect(result).toEqual(mockModels);
    });
    const req = httpMock.expectOne(`${BASE}/trained-models`);
    expect(req.request.method).toBe('GET');
    req.flush(mockModels);
  });

  // ── Inference Jobs ────────────────────────────────────────────────

  it('should presign inference upload via POST /inference/presign', () => {
    const request = { filename: 'input.jsonl', content_type: 'application/json' };
    const mockResponse = { presigned_url: 'https://s3.example.com', s3_key: 'key', expires_at: '2026-03-14T00:00:00Z' };
    service.presignInferenceUpload(request).subscribe(result => {
      expect(result).toEqual(mockResponse);
    });
    const req = httpMock.expectOne(`${BASE}/inference/presign`);
    expect(req.request.method).toBe('POST');
    req.flush(mockResponse);
  });

  it('should create inference job via POST /inference', () => {
    const request = { training_job_id: 'j1', input_s3_key: 'key' };
    const mockJob = { job_id: 'i1', status: 'PENDING' };
    service.createInferenceJob(request).subscribe(result => {
      expect(result).toEqual(mockJob);
    });
    const req = httpMock.expectOne(`${BASE}/inference`);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual(request);
    req.flush(mockJob);
  });

  it('should list inference jobs via GET /inference', () => {
    const mockResponse = { jobs: [], total_count: 0 };
    service.listInferenceJobs().subscribe(result => {
      expect(result).toEqual(mockResponse);
    });
    const req = httpMock.expectOne(`${BASE}/inference`);
    expect(req.request.method).toBe('GET');
    req.flush(mockResponse);
  });

  it('should get a single inference job via GET /inference/:id', () => {
    const mockJob = { job_id: 'i1', status: 'TRANSFORMING' };
    service.getInferenceJob('i1').subscribe(result => {
      expect(result).toEqual(mockJob);
    });
    const req = httpMock.expectOne(`${BASE}/inference/i1`);
    expect(req.request.method).toBe('GET');
    req.flush(mockJob);
  });

  it('should stop inference job via DELETE /inference/:id', () => {
    const mockJob = { job_id: 'i1', status: 'STOPPED' };
    service.stopInferenceJob('i1').subscribe(result => {
      expect(result).toEqual(mockJob);
    });
    const req = httpMock.expectOne(`${BASE}/inference/i1`);
    expect(req.request.method).toBe('DELETE');
    req.flush(mockJob);
  });

  it('should get inference job logs via GET /inference/:id/logs', () => {
    const mockLogs = { logs: ['log1'] };
    service.getInferenceJobLogs('i1').subscribe(result => {
      expect(result).toEqual(mockLogs);
    });
    const req = httpMock.expectOne(`${BASE}/inference/i1/logs`);
    expect(req.request.method).toBe('GET');
    req.flush(mockLogs);
  });

  it('should download inference results via GET /inference/:id/download', () => {
    const mockDownload = { download_url: 'https://s3.example.com/results', expires_at: '2026-03-14T00:00:00Z', result_s3_key: 'output.out' };
    service.downloadInferenceResults('i1').subscribe(result => {
      expect(result).toEqual(mockDownload);
    });
    const req = httpMock.expectOne(`${BASE}/inference/i1/download`);
    expect(req.request.method).toBe('GET');
    req.flush(mockDownload);
  });

  it('should encode job IDs with special characters', () => {
    service.getTrainingJob('job/with spaces').subscribe();
    const req = httpMock.expectOne(`${BASE}/jobs/job%2Fwith%20spaces`);
    expect(req.request.method).toBe('GET');
    req.flush({});
  });
});
