import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import {
  AgentIconDialogComponent,
  AgentIconDialogData,
  AgentIconDialogResult,
} from './agent-icon-dialog.component';
import { AgentApiService } from '../services/agent-api.service';

describe('AgentIconDialogComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: AgentApiService, useValue: { uploadIcon: vi.fn(), removeIcon: vi.fn() } }],
    });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open(data: AgentIconDialogData) {
    return openInCdkDialog<AgentIconDialogComponent, AgentIconDialogData, AgentIconDialogResult>(
      AgentIconDialogComponent,
      { data },
    );
  }

  it('names the dialog from its title, describes it, and starts on the file picker', async () => {
    const { container } = await open({ agentId: 'ast-1', agentName: 'Policy Lookup' });
    expectNamedDialog(container, { name: 'Icon', description: /^A square image for Policy Lookup/ });
    expect(container.querySelector('[cdkFocusInitial]')?.getAttribute('type')).toBe('file');
  });

  it('offers Remove only when the agent already has an icon, and closes undefined on Escape', async () => {
    const plain = await open({ agentId: 'ast-1', agentName: 'Policy Lookup' });
    expect(plain.container.querySelector('[dialogFooter]')?.textContent).not.toContain('Remove icon');
    let result: AgentIconDialogResult | 'open' = 'open';
    plain.ref.closed.subscribe((r) => (result = r));
    plain.container
      .querySelector('app-dialog-shell')!
      .dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    expect(result).toBeUndefined();

    const withIcon = await open({ agentId: 'ast-1', agentName: 'Policy Lookup', iconUrl: 'https://x/icon.png' });
    expect(withIcon.container.querySelector('[dialogFooter]')?.textContent).toContain('Remove icon');
  });
});
