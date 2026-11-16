import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { Dialog } from '@angular/cdk/dialog';
import { TestBed } from '@angular/core/testing';
import { expectNamedDialog } from '../../../../testing/cdk-dialog';
import { confirmProviderReviewEnable } from './provider-review-confirm';

describe('confirmProviderReviewEnable', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.inject(Dialog).closeAll());

  async function open(notice: string | null) {
    const answer = confirmProviderReviewEnable(TestBed.inject(Dialog), 'Claude Fable 5.2', notice);
    await new Promise(resolve => setTimeout(resolve, 0));
    const containers = document.querySelectorAll<HTMLElement>('.cdk-dialog-container');
    return { answer, container: containers[containers.length - 1] };
  }

  function button(container: HTMLElement, label: string): HTMLButtonElement {
    return Array.from(container.querySelectorAll<HTMLButtonElement>('[dialogFooter] button')).find(
      b => b.textContent?.trim() === label,
    )!;
  }

  it('is a named alertdialog that quotes the notice users will see, starting on Cancel', async () => {
    const { container } = await open('Kept for up to 30 days.');
    expectNamedDialog(container, {
      role: 'alertdialog',
      name: 'Make Claude Fable 5.2 available?',
      description: /aws_review.*“Kept for up to 30 days\.”.*audit log/,
    });
    expect(container.querySelector('[cdkFocusInitial]')?.textContent?.trim()).toBe('Cancel');
  });

  it('resolves true on Make available and false on Cancel', async () => {
    const first = await open(null);
    button(first.container, 'Make available').click();
    expect(await first.answer).toBe(true);

    const second = await open(null);
    button(second.container, 'Cancel').click();
    expect(await second.answer).toBe(false);
  });
});
