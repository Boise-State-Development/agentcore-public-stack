import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ConfigService } from '../../../services/config.service';
import { ToastService } from '../../../services/toast/toast.service';
import { DeliveryStateService } from './delivery-state.service';
import { DeliveryService } from './delivery.service';
import { CreateSendBackResponse, DeliveryView, HandoffView } from './handoff.models';

const API = 'https://api.test';

function card(id: string, minute: number, text = `card ${id}`): DeliveryView {
  return {
    deliveryId: id,
    fromSessionId: 'tgt',
    fromTitle: 'Rubric for BIO 101',
    origin: 'user',
    text,
    artifacts: [],
    createdAt: `2026-11-12T00:${String(minute).padStart(2, '0')}:00Z`,
  };
}

function created(overrides: Partial<CreateSendBackResponse> = {}): CreateSendBackResponse {
  return {
    handoffId: 'sb1',
    sourceSessionId: 'src',
    sourceTitle: 'Course planning',
    fromTitle: 'Rubric for BIO 101',
    messageIndex: 3,
    excerpt: 'Four rows, done.',
    excerptHash: 'h',
    artifacts: [{ artifactId: 'rubric', version: 2, title: 'Rubric', contentType: 'text/html' }],
    ...overrides,
  };
}

const ROW: HandoffView = {
  handoffId: 'h1',
  sessionId: 'src',
  kind: 'mention',
  status: 'done',
  agentId: 'rubric',
  agentName: 'Rubric Builder',
  request: 'draft a rubric',
  sourceMessageIndex: 1,
  excerptTokens: 10,
  createdAt: '2026-11-12T00:00:00Z',
  updatedAt: '2026-11-12T00:00:00Z',
  targetSessionId: 'tgt',
};

describe('DeliveryStateService', () => {
  let state: DeliveryStateService;

  beforeEach(() => {
    TestBed.configureTestingModule({});
    state = TestBed.inject(DeliveryStateService);
  });

  it('a send takes the three oldest cards, sending text only for an edited one', () => {
    state.hydrate('src', [card('d', 4), card('a', 1), card('c', 3), card('b', 2)]);
    state.edit('src', 'b', 'shorter');
    state.edit('src', 'c', 'card c'); // back to its own text: unedited

    const { attach } = state.takeForSend('src');

    expect(attach).toEqual([{ deliveryId: 'a' }, { deliveryId: 'b', text: 'shorter' }, { deliveryId: 'c' }]);
    expect(state.cardsFor('src').map((c) => c.deliveryId)).toEqual(['d']);
  });

  it('a send that never reached the server puts its cards back in order', () => {
    state.hydrate('src', [card('a', 1), card('b', 2)]);
    const { cards } = state.takeForSend('src');

    state.restore('src', cards);

    expect(state.cardsFor('src').map((c) => c.deliveryId)).toEqual(['a', 'b']);
  });

  it('a reload of the list keeps an edit made in this tab', () => {
    state.hydrate('src', [card('a', 1)]);
    state.edit('src', 'a', 'mine');

    state.hydrate('src', [card('a', 1), card('b', 2)]);

    expect(state.cardsFor('src').map((c) => c.editedText)).toEqual(['mine', undefined]);
  });
});

describe('DeliveryService', () => {
  let service: DeliveryService;
  let state: DeliveryStateService;
  let http: HttpTestingController;
  const toast = { success: vi.fn(), error: vi.fn(), info: vi.fn() };
  const navigate = vi.fn().mockResolvedValue(true);

  beforeEach(() => {
    Object.values(toast).forEach((fn) => fn.mockClear());
    navigate.mockClear();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: ToastService, useValue: toast },
        { provide: Router, useValue: { navigate } },
      ],
    });
    service = TestBed.inject(DeliveryService);
    state = TestBed.inject(DeliveryStateService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('Bring back result puts the answer on this composer at once and stays put', async () => {
    const done = service.bringBack(ROW);
    const req = http.expectOne(`${API}/sessions/tgt/handoffs/send-back`);
    expect(req.request.body).toEqual({ deliver: true });
    expect(service.isBringingBack('h1')).toBe(true);
    req.flush(created({ deliveryId: 'd1' }));
    await done;

    const [c] = state.cardsFor('src');
    expect(c).toMatchObject({ deliveryId: 'd1', fromSessionId: 'tgt', fromTitle: 'Rubric for BIO 101', text: 'Four rows, done.' });
    expect(c.artifacts.map((a) => a.artifactId)).toEqual(['rubric']);
    expect(service.wasBroughtBack('h1')).toBe(true);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('Remove dismisses the card on the server, and puts it back only when that fails', async () => {
    state.hydrate('src', [card('a', 1), card('b', 2)]);

    const gone = service.dismissCard('src', state.cardsFor('src')[0]);
    expect(state.cardsFor('src').map((c) => c.deliveryId)).toEqual(['b']);
    http.expectOne(`${API}/sessions/src/deliveries/a/dismiss`).flush(null);
    await gone;

    const sent = service.dismissCard('src', state.cardsFor('src')[0]);
    http.expectOne(`${API}/sessions/src/deliveries/b/dismiss`).flush({}, { status: 409, statusText: 'Conflict' });
    await sent;
    expect(state.cardsFor('src')).toEqual([]);

    state.hydrate('src', [card('c', 3)]);
    const failed = service.dismissCard('src', state.cardsFor('src')[0]);
    http.expectOne(`${API}/sessions/src/deliveries/c/dismiss`).flush({}, { status: 500, statusText: 'Error' });
    await failed;
    expect(state.cardsFor('src').map((c) => c.deliveryId)).toEqual(['c']);
  });

  it('Send to {title} reviews first, then sends the edit and the artifacts left ticked', async () => {
    const opening = service.openSendBack('tgt', 3);
    const open = http.expectOne(`${API}/sessions/tgt/handoffs/send-back`);
    expect(open.request.body).toEqual({ messageIndex: 3 });
    open.flush(created());
    await opening;
    expect(state.reviewFor('tgt')).toMatchObject({ phase: 'ready', text: 'Four rows, done.', artifactIds: ['rubric'] });

    service.setSendBackText('tgt', 'Edited: four rows.');
    service.toggleSendBackArtifact('tgt', 'rubric', false);
    const sending = service.sendBack('tgt');
    const deliver = http.expectOne(`${API}/sessions/tgt/handoffs/sb1/deliver`);
    expect(deliver.request.body).toEqual({ text: 'Edited: four rows.', artifactIds: [], fileIds: [] });
    deliver.flush({ deliveryId: 'd9', sourceSessionId: 'src', sourceTitle: 'Course planning' });
    await sending;

    expect(state.reviewFor('tgt')).toBeUndefined();
    expect(state.cardsFor('src')).toEqual([expect.objectContaining({ deliveryId: 'd9', text: 'Edited: four rows.', artifacts: [] })]);
    expect(toast.success).toHaveBeenCalledWith('Sent to Course planning', undefined, expect.anything());
    expect(navigate).not.toHaveBeenCalled();
  });

  it('a send already made elsewhere closes the review and says so', async () => {
    const opening = service.openSendBack('tgt', 3);
    http.expectOne(`${API}/sessions/tgt/handoffs/send-back`).flush(created());
    await opening;

    const sending = service.sendBack('tgt');
    http.expectOne(`${API}/sessions/tgt/handoffs/sb1/deliver`).flush({}, { status: 409, statusText: 'Conflict' });
    await sending;

    expect(state.reviewFor('tgt')).toBeUndefined();
    expect(toast.info).toHaveBeenCalledWith('This result was already sent');
  });

  describe('files by reference (§4.9)', () => {
    const RUBRIC_DOC = { uploadId: 'f1', sessionId: 'tgt', filename: 'rubric.docx', mimeType: 'application/msword', sizeBytes: 2048 };
    const NOTES = { uploadId: 'f2', sessionId: 'tgt', filename: 'notes.md', mimeType: 'text/markdown', sizeBytes: 90 };

    it('Bring back result carries the files the target made onto the card', async () => {
      const done = service.bringBack(ROW);
      http.expectOne(`${API}/sessions/tgt/handoffs/send-back`).flush(created({ deliveryId: 'd1', files: [RUBRIC_DOC] }));
      await done;

      expect(state.cardsFor('src')[0].files).toEqual([RUBRIC_DOC]);
    });

    it('the row\'s Bring back sends one file on its own', async () => {
      const done = service.bringBackFile(ROW, NOTES);
      const req = http.expectOne(`${API}/sessions/tgt/handoffs/send-back`);
      expect(req.request.body).toEqual({ deliver: true, fileIds: ['f2'], filesOnly: true });
      expect(service.isBringingBackFile('h1', 'f2')).toBe(true);
      req.flush(created({ deliveryId: 'd2', excerpt: '', artifacts: [], files: [RUBRIC_DOC, NOTES] }));
      await done;

      expect(state.cardsFor('src')[0]).toMatchObject({ deliveryId: 'd2', text: '', artifacts: [], files: [NOTES] });
      expect(service.wasFileBroughtBack('h1', 'f2')).toBe(true);
      expect(service.wasBroughtBack('h1')).toBe(false);
    });

    it('Send to {title} ticks every offered file and sends only the ones left ticked', async () => {
      const opening = service.openSendBack('tgt', 3);
      http.expectOne(`${API}/sessions/tgt/handoffs/send-back`).flush(created({ files: [RUBRIC_DOC, NOTES] }));
      await opening;
      expect(state.reviewFor('tgt')!.fileIds).toEqual(['f1', 'f2']);

      service.toggleSendBackFile('tgt', 'f1', false);
      const sending = service.sendBack('tgt');
      const deliver = http.expectOne(`${API}/sessions/tgt/handoffs/sb1/deliver`);
      expect(deliver.request.body).toEqual({ text: 'Four rows, done.', artifactIds: ['rubric'], fileIds: ['f2'] });
      deliver.flush({ deliveryId: 'd9', sourceSessionId: 'src', sourceTitle: 'Course planning' });
      await sending;

      expect(state.cardsFor('src')[0].files).toEqual([NOTES]);
    });
  });

  it('cancelling the review closes the send-back row on the server', async () => {
    const opening = service.openSendBack('tgt', 3);
    http.expectOne(`${API}/sessions/tgt/handoffs/send-back`).flush(created());
    await opening;

    service.cancelSendBack('tgt');

    expect(state.reviewFor('tgt')).toBeUndefined();
    http.expectOne(`${API}/sessions/tgt/handoffs/sb1/dismiss`).flush(null);
  });
});
