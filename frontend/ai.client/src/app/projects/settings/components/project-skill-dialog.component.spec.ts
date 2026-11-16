import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { HttpErrorResponse } from '@angular/common/http';
import { provideMarkdown } from 'ngx-markdown';
import { of, throwError } from 'rxjs';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { expectNamedDialog, openInCdkDialog } from '../../../../testing/cdk-dialog';
import { ProjectSkillContent } from '../../models/project.model';
import { ProjectApiService } from '../../services/project-api.service';
import {
  MAX_READABLE_BYTES,
  ProjectSkillDialogComponent,
  ProjectSkillDialogData,
  isReadableFile,
} from './project-skill-dialog.component';

const SKILL: ProjectSkillContent = {
  skillId: 'eds_redline',
  displayName: 'Contract redline',
  description: 'Redlines a contract against the playbooks.',
  instructions: '# Redline\n\nRead **process.md** first.',
  version: 3,
  compose: [],
  allowedTools: [],
  files: [
    { filename: 'redline.py', kind: 'script', size: 2048, contentType: 'text/x-python' },
    { filename: 'process.md', kind: 'reference', size: 900, contentType: 'text/markdown' },
    { filename: 'logo.png', kind: 'asset', size: 4000, contentType: 'image/png' },
  ],
};

const DATA: ProjectSkillDialogData = { projectId: 'prj_1', skillId: 'eds_redline', name: 'eds_redline' };

async function settle(): Promise<void> {
  for (let i = 0; i < 3; i++) await new Promise(resolve => setTimeout(resolve, 0));
}

describe('ProjectSkillDialogComponent', () => {
  const api = { skillContent: vi.fn(), skillFile: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.skillContent.mockReturnValue(of(SKILL));
    api.skillFile.mockImplementation((_p: string, _s: string, name: string) =>
      of(name === 'process.md' ? '## Step one\n\nCheck the parties.' : 'print("redline")'),
    );
    TestBed.configureTestingModule({ providers: [provideMarkdown(), { provide: ProjectApiService, useValue: api }] });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open() {
    const opened = await openInCdkDialog(ProjectSkillDialogComponent, { data: DATA });
    await settle();
    return opened;
  }

  const buttonNamed = (root: Element, label: string): HTMLButtonElement | undefined =>
    [...root.querySelectorAll('button')].find(b => b.getAttribute('aria-label') === label || b.textContent!.trim() === label);

  it('is a dialog named for the skill, showing the version the project runs and its instructions', async () => {
    const { container } = await open();
    expect(api.skillContent).toHaveBeenCalledWith('prj_1', 'eds_redline');
    expectNamedDialog(container, { name: 'Contract redline' });
    expect(container.textContent).toContain('Redlines a contract against the playbooks.');
    expect(container.textContent).toContain('Version 3, the one this project runs');
    // Rendered markdown, not literal `#`.
    expect(container.querySelector('markdown h1')?.textContent).toContain('Redline');
    expect(container.textContent).toContain('Read-only.');
  });

  it('lists the bundle by directory, references first', async () => {
    const { container } = await open();
    const headings = [...container.querySelectorAll('h4')].map(h => h.textContent!.replace(/\s+/g, ' ').trim());
    expect(headings).toEqual(['References (references/)', 'Scripts (scripts/)', 'Assets (assets/)']);
    expect(container.textContent).toContain('Can\'t be shown here');
    expect(buttonNamed(container, 'Read logo.png')).toBeUndefined();
  });

  it('reads a text file in place, once, and hides it again', async () => {
    const { container } = await open();
    const read = buttonNamed(container, 'Read process.md')!;
    expect(read.getAttribute('aria-expanded')).toBe('false');
    read.click();
    await settle();

    expect(api.skillFile).toHaveBeenCalledWith('prj_1', 'eds_redline', 'process.md');
    const region = document.getElementById(read.getAttribute('aria-controls')!)!;
    expect(region.getAttribute('role')).toBe('region');
    expect(region.querySelector('markdown h2')?.textContent).toContain('Step one');
    expect(read.getAttribute('aria-expanded')).toBe('true');

    buttonNamed(container, 'Hide process.md')!.click();
    await settle();
    expect(document.getElementById(read.getAttribute('aria-controls')!)).toBeNull();
    buttonNamed(container, 'Read process.md')!.click();
    await settle();
    expect(api.skillFile).toHaveBeenCalledTimes(1);
  });

  it('shows a script verbatim', async () => {
    const { container } = await open();
    buttonNamed(container, 'Read redline.py')!.click();
    await settle();
    expect(container.querySelector('pre')?.textContent).toBe('print("redline")');
  });

  it('says why a file could not be read, and retries on the next open', async () => {
    api.skillFile.mockReturnValueOnce(throwError(() => new HttpErrorResponse({ status: 404, error: { detail: 'That file could not be read.' } })));
    const { container } = await open();
    buttonNamed(container, 'Read redline.py')!.click();
    await settle();
    expect(container.querySelector('[role=alert]')?.textContent).toContain('That file could not be read.');

    buttonNamed(container, 'Hide redline.py')!.click();
    await settle();
    buttonNamed(container, 'Read redline.py')!.click();
    await settle();
    expect(container.querySelector('pre')?.textContent).toBe('print("redline")');
  });

  it('says plainly when the skill is not available to the caller', async () => {
    api.skillContent.mockReturnValue(throwError(() => new HttpErrorResponse({ status: 403 })));
    const { container } = await open();
    expectNamedDialog(container, { name: 'eds_redline' });
    expect(container.querySelector('[role=alert]')?.textContent).toContain('isn’t available to you');
  });

  it('closes with no result', async () => {
    const { ref, container } = await open();
    let result: unknown = 'open';
    ref.closed.subscribe(value => (result = value));
    buttonNamed(container, 'Close')!.click();
    expect(result).toBeUndefined();
  });
});

describe('isReadableFile', () => {
  it('reads text by type or by name, and refuses what is large or binary', () => {
    expect(isReadableFile({ filename: 'a.md', kind: 'reference', size: 10, contentType: 'application/octet-stream' })).toBe(true);
    expect(isReadableFile({ filename: 'run', kind: 'script', size: 10, contentType: 'text/x-shellscript' })).toBe(true);
    expect(isReadableFile({ filename: 'a.png', kind: 'asset', size: 10, contentType: 'image/png' })).toBe(false);
    expect(isReadableFile({ filename: 'a.md', kind: 'reference', size: MAX_READABLE_BYTES + 1, contentType: 'text/markdown' })).toBe(false);
  });
});
