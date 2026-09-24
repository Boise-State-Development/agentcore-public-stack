import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of } from 'rxjs';
import { PeoplePickerComponent, PeoplePickerSubmit, splitEmails } from './people-picker.component';
import { ProjectApiService } from '../services/project-api.service';

describe('splitEmails', () => {
  it('pulls every email out of a pasted list, whatever separates them', () => {
    expect(
      splitEmails('ana@x.edu, ben@x.edu;cy@x.edu\n<dee@x.edu>  not-an-email  eve@x.edu'),
    ).toEqual(['ana@x.edu', 'ben@x.edu', 'cy@x.edu', 'dee@x.edu', 'eve@x.edu']);
    expect(splitEmails('just a name')).toEqual([]);
  });
});

describe('PeoplePickerComponent', () => {
  const api = { directory: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.directory.mockReturnValue(of({ people: [] }));
    TestBed.configureTestingModule({
      imports: [PeoplePickerComponent],
      providers: [{ provide: ProjectApiService, useValue: api }],
    });
  });

  function create() {
    const fixture = TestBed.createComponent(PeoplePickerComponent);
    fixture.componentRef.setInput('projectId', 'prj_1');
    fixture.detectChanges();
    return fixture;
  }

  it('stages a pasted list at once, lowercased and deduplicated, and submits it with a role', () => {
    const fixture = create();
    const submitted: PeoplePickerSubmit[] = [];
    fixture.componentInstance.submitted.subscribe(s => submitted.push(s));

    const input: HTMLInputElement = fixture.nativeElement.querySelector('#people-picker-input');
    const paste = new Event('paste', { cancelable: true }) as ClipboardEvent;
    Object.defineProperty(paste, 'clipboardData', { value: { getData: () => 'Ana@X.edu, ben@x.edu, ana@x.edu' } });
    input.dispatchEvent(paste);
    fixture.detectChanges();

    expect(paste.defaultPrevented).toBe(true);
    const chips = [...fixture.nativeElement.querySelectorAll('[aria-label="Ready to add"] li')].map(
      (li: Element) => li.textContent?.trim(),
    );
    expect(chips).toEqual(['ana@x.edu', 'ben@x.edu']);

    const select: HTMLSelectElement = fixture.nativeElement.querySelector('#people-picker-role');
    select.value = 'editor';
    select.dispatchEvent(new Event('change'));
    const add = [...fixture.nativeElement.querySelectorAll('button')].find(
      (b: HTMLButtonElement) => b.textContent?.trim() === 'Add 2',
    ) as HTMLButtonElement;
    add.click();

    expect(submitted).toEqual([{ emails: ['ana@x.edu', 'ben@x.edu'], role: 'editor' }]);

    fixture.componentInstance.reset();
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('[aria-label="Ready to add"]')).toBeNull();
  });

  it('adds a typed email on Enter even when the directory does not know it', () => {
    const fixture = create();
    const input: HTMLInputElement = fixture.nativeElement.querySelector('#people-picker-input');
    input.value = 'new.person@x.edu';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges(); // as the browser would between two events
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('[aria-label="Ready to add"]')?.textContent).toContain('new.person@x.edu');
    expect(input.value).toBe('');
  });
});
