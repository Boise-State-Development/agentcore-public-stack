import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { provideMarkdown } from 'ngx-markdown';
import { expectNamedDialog, openInCdkDialog } from '../../../../../testing/cdk-dialog';
import { UserMenuLinkModalComponent, UserMenuLinkModalData } from './user-menu-link-modal.component';

describe('UserMenuLinkModalComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    // The template renders <markdown>, so the real MarkdownService is needed.
    TestBed.configureTestingModule({ providers: [provideMarkdown()] });
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  function open() {
    return openInCdkDialog<UserMenuLinkModalComponent, UserMenuLinkModalData>(UserMenuLinkModalComponent, {
      data: { label: 'Help & support', bodyMarkdown: 'Email **help@example.edu**.' },
    });
  }

  it('names the dialog from the link label', async () => {
    const { container } = await open();
    expectNamedDialog(container, { name: 'Help & support' });
  });

  it('closes from the footer button', async () => {
    const { ref, container } = await open();
    let closed = false;
    ref.closed.subscribe(() => (closed = true));
    const buttons = Array.from(container.querySelectorAll<HTMLButtonElement>('[dialogFooter] button'));
    buttons.find((b) => b.textContent?.trim() === 'Close')!.click();
    expect(closed).toBe(true);
  });
});
