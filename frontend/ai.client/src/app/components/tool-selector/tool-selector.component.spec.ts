import { Component, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';
import { ToolSelectorComponent } from './tool-selector.component';
import { ToolSelectorItem } from './tool-selector.model';

const ITEMS: ToolSelectorItem[] = [
  { id: 'calendar', name: 'Calendar', description: 'Read your schedule', group: 'account' },
  { id: 'mail', name: 'Mail', description: 'Search your inbox', group: 'account' },
  { id: 'browse_web', name: 'Web Browser', description: 'Open and read pages', group: 'browser' },
  {
    id: 'canvas',
    name: 'Canvas',
    description: 'Courses and grading',
    group: 'browser',
    children: [
      { name: 'list_courses', summary: 'List courses.', detail: 'Args:\n  term: the term' },
      { name: 'grade_submission', summary: 'Grade a submission.' },
    ],
  },
  {
    id: 'legacy_search',
    name: 'Legacy Search',
    description: 'Old search',
    group: 'browser',
    badge: 'retiring',
    note: 'Being retired and can no longer be added.',
    addBlocked: true,
  },
  { id: 'foreign', name: 'foreign', note: 'Added by someone else.', noteTone: 'muted', locked: true },
];

@Component({
  imports: [ToolSelectorComponent],
  template: `
    <h2 id="host-heading">Tools</h2>
    <app-tool-selector
      [items]="items()"
      [(selected)]="selected"
      labelledBy="host-heading"
      [disabled]="disabled()"
      [helperText]="helper()"
      (itemAction)="actions.push($event.id)"
    />
  `,
})
class HostComponent {
  readonly items = signal<ToolSelectorItem[]>(ITEMS);
  readonly selected = signal<ReadonlySet<string>>(new Set(['calendar', 'foreign']));
  readonly disabled = signal(false);
  readonly helper = signal('');
  readonly actions: string[] = [];
}

describe('ToolSelectorComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let host: HostComponent;
  let el: HTMLElement;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [HostComponent] }).compileComponents();
    fixture = TestBed.createComponent(HostComponent);
    host = fixture.componentInstance;
    el = fixture.nativeElement;
    fixture.detectChanges();
  });

  const rowNames = (): string[] =>
    [...el.querySelectorAll('li [id$="-name"]')]
      .filter((n) => !n.closest('[id$="-children"]'))
      .map((n) => n.textContent!.trim());
  const checkboxFor = (name: string): HTMLInputElement => {
    const label = [...el.querySelectorAll('[id$="-name"]')].find((n) => n.textContent!.trim() === name)!;
    return el.querySelector(`input[aria-labelledby="${label.id}"]`) as HTMLInputElement;
  };
  const button = (text: string): HTMLButtonElement =>
    [...el.querySelectorAll('button')].find((b) => b.textContent!.trim() === text) as HTMLButtonElement;
  const search = (value: string): void => {
    const input = el.querySelector('input[type="search"]') as HTMLInputElement;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  };
  const sorted = (): string[] => [...host.selected()].sort();

  describe('search', () => {
    it('filters case-insensitively on name and description', () => {
      search('CAL');
      expect(rowNames()).toEqual(['Calendar']);
      search('inbox');
      expect(rowNames()).toEqual(['Mail']);
    });

    it('shows an empty state naming the query', () => {
      search('zzz');
      expect(rowNames()).toEqual([]);
      expect(el.textContent).toContain('No tools match “zzz”.');
    });

    it('announces the result count in a polite live region', () => {
      const status = el.querySelector('[role="status"]')!;
      expect(status.getAttribute('aria-live')).toBe('polite');
      // Quiet on load, so the list doesn't announce itself.
      expect(status.textContent!.trim()).toBe('');
      search('mail');
      expect(status.textContent!.trim()).toBe('1 of 6 tools shown');
    });

    it('clears from the clear button and returns focus to the field', () => {
      search('mail');
      (el.querySelector('button[aria-label="Clear search"]') as HTMLButtonElement).click();
      fixture.detectChanges();
      expect(el.querySelector('button[aria-label="Clear search"]')).toBeNull();
      expect(rowNames()).toHaveLength(ITEMS.length);
      expect(document.activeElement).toBe(el.querySelector('input[type="search"]'));
    });

    it('clears on Escape without letting the key reach a surrounding dialog', () => {
      search('mail');
      const input = el.querySelector('input[type="search"]') as HTMLInputElement;
      const event = new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
      let reachedParent = false;
      el.addEventListener('keydown', () => (reachedParent = true));
      input.dispatchEvent(event);
      fixture.detectChanges();
      expect(reachedParent).toBe(false);
      expect(rowNames()).toHaveLength(ITEMS.length);
    });
  });

  describe('filters', () => {
    it('narrows to one group, with group headers when grouped', () => {
      const headers = [...el.querySelectorAll('[role="group"] > p[id]')].map((p) => p.textContent!.trim());
      expect(headers).toEqual(['account', 'browser', 'Other']);

      button('browser').click();
      fixture.detectChanges();
      expect(rowNames()).toEqual(['Web Browser', 'Canvas', 'Legacy Search']);
      expect(button('browser').getAttribute('aria-pressed')).toBe('true');
      expect(button('All').getAttribute('aria-pressed')).toBe('false');
    });

    it('shows selected rows only', () => {
      button('Selected only').click();
      fixture.detectChanges();
      expect(rowNames()).toEqual(['Calendar', 'foreign']);
    });

    it('combines search with the group filter', () => {
      button('account').click();
      search('read');
      // "Open and read pages" matches too, but it's in another group.
      expect(rowNames()).toEqual(['Calendar']);
    });
  });

  describe('selection', () => {
    it('toggles a row and writes back through two-way binding', () => {
      checkboxFor('Mail').click();
      fixture.detectChanges();
      expect(sorted()).toEqual(['calendar', 'foreign', 'mail']);
      checkboxFor('Calendar').click();
      fixture.detectChanges();
      expect(sorted()).toEqual(['foreign', 'mail']);
    });

    it('selects all of the filtered set only, skipping blocked and locked rows', () => {
      button('browser').click();
      fixture.detectChanges();
      button('Select all').click();
      fixture.detectChanges();
      // Legacy Search is addBlocked; Mail is not shown.
      expect(sorted()).toEqual(['browse_web', 'calendar', 'canvas', 'foreign']);
    });

    it('clears the filtered set only, keeping locked rows', () => {
      host.selected.set(new Set(['calendar', 'mail', 'browse_web', 'foreign']));
      fixture.detectChanges();
      search('a'); // Calendar, Mail, Web Browser (description), Canvas, Legacy Search
      button('Clear').click();
      fixture.detectChanges();
      expect(sorted()).toEqual(['foreign']);
    });

    it('names the bulk actions by what they will do', () => {
      button('account').click();
      fixture.detectChanges();
      expect(button('Select all').getAttribute('aria-label')).toBe('Select all 1 shown tool');
      expect(button('Clear').getAttribute('aria-label')).toBe('Clear 1 shown tool');
    });

    it('shows the selected count', () => {
      expect(el.textContent).toContain('2 of 6 selected');
    });

    it('refuses to add an addBlocked row but lets a selected one be removed', () => {
      const legacy = checkboxFor('Legacy Search');
      expect(legacy.disabled).toBe(true);

      host.selected.set(new Set(['legacy_search']));
      fixture.detectChanges();
      expect(checkboxFor('Legacy Search').disabled).toBe(false);
      checkboxFor('Legacy Search').click();
      fixture.detectChanges();
      expect(sorted()).toEqual([]);
      expect(checkboxFor('Legacy Search').disabled).toBe(true);
    });

    it('never toggles a locked row', () => {
      expect(checkboxFor('foreign').disabled).toBe(true);
    });
  });

  describe('per-tool narrowing', () => {
    it('offers it only once the server is selected, and writes scoped refs', () => {
      const expandLabel = 'button[aria-label="Choose individual Canvas tools"]';
      expect(el.querySelector(expandLabel)).toBeNull();

      checkboxFor('Canvas').click();
      fixture.detectChanges();
      const expand = el.querySelector(expandLabel) as HTMLButtonElement;
      expect(expand.getAttribute('aria-expanded')).toBe('false');
      expand.click();
      fixture.detectChanges();
      expect(expand.getAttribute('aria-expanded')).toBe('true');
      const panel = el.querySelector(`#${expand.getAttribute('aria-controls')}`)!;
      expect(panel.querySelectorAll('input[type="checkbox"]')).toHaveLength(2);
      expect(panel.textContent).toContain('All 2 tools.');

      checkboxFor('grade_submission').click();
      fixture.detectChanges();
      expect(sorted()).toEqual(['calendar', 'canvas::list_courses', 'foreign']);
      expect(el.textContent).toContain('1 of 2 tools');
    });

    it('reveals a tool’s Args: detail on request', () => {
      host.selected.set(new Set(['canvas']));
      fixture.detectChanges();
      (el.querySelector('button[aria-label="Choose individual Canvas tools"]') as HTMLButtonElement).click();
      fixture.detectChanges();
      button('Show details').click();
      fixture.detectChanges();
      expect(el.querySelector('pre')!.textContent).toContain('term: the term');
    });
  });

  describe('read-only', () => {
    beforeEach(() => {
      host.disabled.set(true);
      fixture.detectChanges();
    });

    it('disables every control and hides the bulk actions', () => {
      const boxes = [...el.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')];
      expect(boxes.length).toBeGreaterThan(0);
      expect(boxes.every((b) => b.disabled)).toBe(true);
      expect(button('Select all')).toBeUndefined();
      expect(button('Clear')).toBeUndefined();
    });

    it('still lets the user search and filter', () => {
      search('mail');
      expect(rowNames()).toEqual(['Mail']);
    });

    it('keeps the list reachable by keyboard for scrolling', () => {
      const list = el.querySelector('[role="group"][aria-labelledby="host-heading"]')!;
      expect(list.getAttribute('tabindex')).toBe('0');
    });
  });

  describe('accessibility', () => {
    it('is a group named by the parent heading and described by the helper text', () => {
      host.helper.set('Snapshotted when created.');
      fixture.detectChanges();
      const list = el.querySelector('[role="group"][aria-labelledby="host-heading"]')!;
      expect(list).not.toBeNull();
      const helperId = list.getAttribute('aria-describedby')!;
      expect(el.querySelector(`#${helperId}`)!.textContent).toBe('Snapshotted when created.');
    });

    it('labels each control by the tool name and describes it by its description', () => {
      const box = checkboxFor('Mail');
      const labelledBy = el.querySelector(`#${box.getAttribute('aria-labelledby')}`)!;
      expect(labelledBy.textContent!.trim()).toBe('Mail');
      const describedBy = box.getAttribute('aria-describedby')!.split(' ');
      expect(describedBy.map((id) => el.querySelector(`#${id}`)!.textContent!.trim())).toEqual([
        'Search your inbox',
      ]);
    });

    it('describes a row by its note before its description', () => {
      const box = checkboxFor('Legacy Search');
      const texts = box
        .getAttribute('aria-describedby')!
        .split(' ')
        .map((id) => el.querySelector(`#${id}`)!.textContent!.trim());
      expect(texts).toEqual(['Being retired and can no longer be added.', 'Old search']);
    });

    it('gives the search field a label', () => {
      const input = el.querySelector('input[type="search"]')!;
      expect(el.querySelector(`label[for="${input.id}"]`)!.textContent!.trim()).toBe('Search tools');
    });

    it('labels each group of rows by its header', () => {
      const groups = [...el.querySelectorAll('[role="group"][aria-labelledby]')].filter(
        (g) => g.getAttribute('aria-labelledby') !== 'host-heading',
      );
      expect(groups.map((g) => el.querySelector(`#${g.getAttribute('aria-labelledby')}`)!.textContent!.trim())).toEqual([
        'account',
        'browser',
        'Other',
      ]);
    });

    it('keeps DOM ids unique across instances', () => {
      const second = TestBed.createComponent(HostComponent);
      second.detectChanges();
      const ids = new Set([...el.querySelectorAll('[id]')].map((n) => n.id));
      const clashes = [...second.nativeElement.querySelectorAll('[id]')]
        .map((n: Element) => n.id)
        .filter((id: string) => id !== 'host-heading' && ids.has(id));
      expect(clashes).toEqual([]);
    });
  });

  it('stays flat when no row has a group', () => {
    host.items.set([
      { id: 'a', name: 'Alpha' },
      { id: 'b', name: 'Beta' },
    ]);
    fixture.detectChanges();
    expect(button('All')).toBeUndefined();
    expect(el.querySelectorAll('[role="group"] > p[id]')).toHaveLength(0);
    expect(rowNames()).toEqual(['Alpha', 'Beta']);
  });

  it('says so when there is nothing to pick', () => {
    host.items.set([]);
    fixture.detectChanges();
    expect(el.textContent).toContain('No tools available.');
    expect(el.querySelector('input[type="search"]')).toBeNull();
  });

  it('reports a row action without toggling the row, and not while it is disabled', () => {
    host.items.set([
      { ...ITEMS[0], action: { label: 'Update', ariaLabel: 'Update Calendar' } },
      { ...ITEMS[1], action: { label: 'Update', disabled: true } },
    ]);
    fixture.detectChanges();
    const [calendar, mail] = [...el.querySelectorAll<HTMLButtonElement>('li button')];
    expect(calendar.getAttribute('aria-label')).toBe('Update Calendar');
    expect(calendar.closest('label')).toBeNull();

    calendar.click();
    mail.click();
    fixture.detectChanges();

    expect(host.actions).toEqual(['calendar']);
    expect(mail.disabled).toBe(true);
    expect(host.selected()).toEqual(new Set(['calendar', 'foreign']));
  });
});
