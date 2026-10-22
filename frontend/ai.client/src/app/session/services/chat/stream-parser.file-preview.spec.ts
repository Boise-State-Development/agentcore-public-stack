import { TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { StreamParserService } from './stream-parser.service';
import { ChatStateService } from './chat-state.service';
import { ErrorService } from '../../../services/error/error.service';
import { QuotaWarningService } from '../../../services/quota/quota-warning.service';
import { FilePreviewStateService } from '../file-preview/file-preview-state.service';

/**
 * A file the turn just produced opens in the docked pane, the way an
 * artifact pops its panel.
 *
 * The office tools have no SSE event of their own — the download card is a
 * `file_download` inline visual inside the tool result — so the hook hangs
 * off `tool_result`. That also gives the live-vs-hydrated distinction for
 * free: `tool_result` only arrives mid-stream, so reopening an old
 * conversation replays the card without seizing the rail.
 */
describe('StreamParserService — auto-opening the file preview pane', () => {
  let service: StreamParserService;
  let chatState: ChatStateService;
  let preview: FilePreviewStateService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        StreamParserService,
        ChatStateService,
        ErrorService,
        QuotaWarningService,
      ],
    });
    service = TestBed.inject(StreamParserService);
    chatState = TestBed.inject(ChatStateService);
    preview = TestBed.inject(FilePreviewStateService);
    chatState.setViewedSession('s1');
    service.reset('s1');
  });

  afterEach(() => TestBed.resetTestingModule());

  /** Drive a turn that calls one tool and returns a file_download card. */
  function streamFile(
    payload: Record<string, unknown>,
    sessionId = 's1',
  ): void {
    service.parseEventSourceMessage(sessionId, 'message_start', {
      role: 'assistant',
    });
    service.parseEventSourceMessage(sessionId, 'content_block_delta', {
      contentBlockIndex: 0,
      text: 'Building it…',
    });
    service.parseEventSourceMessage(sessionId, 'tool_result', {
      tool_result: {
        toolUseId: 't1',
        status: 'success',
        content: [
          {
            text: JSON.stringify({
              success: true,
              ui_type: 'file_download',
              ui_display: 'inline',
              payload,
            }),
          },
        ],
      },
    });
  }

  it('opens a generated .pptx without the user clicking', () => {
    streamFile({ filename: 'deck.pptx', upload_id: 'up1', size_kb: '30 KB' });

    expect(preview.openFile()).toEqual({
      uploadId: 'up1',
      filename: 'deck.pptx',
    });
  });

  it('opens a generated .docx too', () => {
    streamFile({ filename: 'report.docx', upload_id: 'up2' });

    expect(preview.openFile()?.filename).toBe('report.docx');
  });

  it('opens a generated .xlsx too, now that the pane can read one', () => {
    // The pane reads a workbook server-side, so a spreadsheet the agent
    // just produced opens on the same terms as a .docx or .pptx.
    streamFile({ filename: 'budget.xlsx', upload_id: 'up3' });

    expect(preview.openFile()?.filename).toBe('budget.xlsx');
  });

  it('leaves a format the pane cannot render alone', () => {
    // Opening a pane that could only show an error is worse than letting
    // the download card speak for itself. .xls is the pre-2007 binary
    // format, which no reader here can open.
    streamFile({ filename: 'legacy.xls', upload_id: 'up3b' });

    expect(preview.openFile()).toBeNull();
  });

  it('never seizes the rail for a conversation streaming in the background', () => {
    // Same guard as onArtifact: the user is reading a different thread.
    chatState.setViewedSession('other-session');
    service.reset('s2');
    streamFile({ filename: 'deck.pptx', upload_id: 'up4' }, 's2');

    expect(preview.openFile()).toBeNull();
  });

  it('ignores a legacy card that carries no upload id', () => {
    // Pre-upload_id cards hold an expired presigned URL and cannot be
    // resolved; they also never arrive live, but the guard is cheap.
    streamFile({ filename: 'old.docx', download_url: 'https://s3/old.docx' });

    expect(preview.openFile()).toBeNull();
  });
});
