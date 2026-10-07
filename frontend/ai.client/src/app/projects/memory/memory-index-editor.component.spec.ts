import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { of } from 'rxjs';
import { MemoryIndexEditorComponent } from './memory-index-editor.component';
import { ProjectApiService } from '../services/project-api.service';
import { ToastService } from '../../services/toast/toast.service';
import { memoryEntry } from '../../../testing/project-memory.fixtures';

describe('MemoryIndexEditorComponent', () => {
  const api = { saveMemoryIndex: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.saveMemoryIndex.mockReturnValue(of({ content: '' }));
    dialog.open.mockReturnValue({ closed: of('rates') });
    TestBed.configureTestingModule({
      imports: [MemoryIndexEditorComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: ToastService, useValue: { success: vi.fn() } },
        { provide: Dialog, useValue: dialog },
      ],
    });
  });

  async function render() {
    const fixture = TestBed.createComponent(MemoryIndexEditorComponent);
    fixture.componentRef.setInput('projectId', 'prj_1');
    fixture.componentRef.setInput('scope', 'mine');
    fixture.componentRef.setInput('content', '# Mine\n- [[gone]] — kept\n');
    fixture.componentRef.setInput('entries', [memoryEntry('rates')]);
    fixture.componentRef.setInput('budget', 1000);
    const done = vi.fn();
    fixture.componentInstance.done.subscribe(done);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const area = el.querySelector<HTMLTextAreaElement>('#memory-index-text')!;
    const save = () => Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Save index')) as HTMLButtonElement;
    return { fixture, el, area, save, done };
  }

  it('opens with the index and its budget, an old dead link allowed', async () => {
    const { el, area, save } = await render();
    expect(area.value).toBe('# Mine\n- [[gone]] — kept\n');
    expect(el.querySelector('[role=meter]')?.getAttribute('aria-valuemax')).toBe('1000');
    expect(save().disabled).toBe(false);
  });

  it('blocks a new dead link, then saves the fixed text', async () => {
    const { el, area, save, fixture, done } = await render();
    area.value = '- [[nowhere]]\n';
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    expect(el.querySelector('[aria-live=polite]')?.textContent).toContain('[[nowhere]] doesn’t match');
    expect(save().disabled).toBe(true);

    area.value = '- [[rates]] — limits\n';
    area.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    save().click();
    await fixture.whenStable();
    expect(api.saveMemoryIndex).toHaveBeenCalledWith('prj_1', 'mine', '- [[rates]] — limits\n');
    expect(done).toHaveBeenCalled();
  });
});
