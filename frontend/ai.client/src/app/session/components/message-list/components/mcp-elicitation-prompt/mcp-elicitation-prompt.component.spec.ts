import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { McpElicitationPromptComponent } from './mcp-elicitation-prompt.component';
import {
  McpElicitationRequest,
  McpElicitationResumeHandler,
  McpElicitationService,
} from '../../../../../services/mcp-elicitation/mcp-elicitation.service';
import type { McpElicitationField } from '../../../../../shared/utils/stream-parser/stream-parser-types';
import { buildElicitationForm, readElicitationAnswer, toolLabel } from './mcp-elicitation-form';

const REPO_FIELD: McpElicitationField = {
  name: 'repository_name',
  kind: 'text',
  title: 'Repository name',
  description: 'Enter acme/scratch exactly',
  required: true,
};

function request(fields: McpElicitationField[], overrides: Partial<McpElicitationRequest> = {}) {
  return {
    interruptId: 'int-1',
    toolUseId: 'tu-1',
    toolName: 'github_issues___delete_repository',
    serverName: 'GitHub',
    message: 'Type "acme/scratch" to confirm permanent deletion.',
    fields,
    sessionId: 's1',
    ...overrides,
  };
}

describe('McpElicitationPromptComponent', () => {
  let fixture: ComponentFixture<McpElicitationPromptComponent>;
  let service: McpElicitationService;
  let resume: ReturnType<typeof vi.fn<McpElicitationResumeHandler>>;

  function render(fields: McpElicitationField[], overrides: Partial<McpElicitationRequest> = {}) {
    const input = request(fields, overrides);
    service.requestInput(input);
    fixture = TestBed.createComponent(McpElicitationPromptComponent);
    fixture.componentRef.setInput('request', service.pending()[0]);
    fixture.detectChanges();
    return fixture.nativeElement as HTMLElement;
  }

  function button(root: HTMLElement, text: string): HTMLButtonElement {
    const found = Array.from(root.querySelectorAll('button')).find(
      (b) => b.textContent?.trim().startsWith(text),
    );
    if (!found) throw new Error(`no button ${text}`);
    return found as HTMLButtonElement;
  }

  function type(input: HTMLInputElement, value: string) {
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ imports: [McpElicitationPromptComponent] });
    service = TestBed.inject(McpElicitationService);
    resume = vi.fn<McpElicitationResumeHandler>().mockResolvedValue(undefined);
    service.setResumeHandler(resume);
  });

  describe('who is asking', () => {
    it('names the server, not the assistant, and shows its message as text', () => {
      const root = render([REPO_FIELD], { message: '<b>Type</b> it' });

      expect(root.querySelector('h3')?.textContent).toContain('GitHub is asking for this');
      expect(root.querySelector('h3')?.textContent).toContain('delete_repository');
      const message = root.querySelector('.server-message');
      expect(message?.textContent?.trim()).toBe('<b>Type</b> it');
      expect(message?.querySelector('b')).toBeNull();
    });

    it('falls back to a generic name when the server name is empty', () => {
      const root = render([REPO_FIELD], { serverName: '' });
      expect(root.querySelector('h3')?.textContent).toContain('An MCP server is asking for this');
    });

    it('names the card by its heading', () => {
      const root = render([REPO_FIELD]);
      const section = root.querySelector('section')!;
      const heading = root.querySelector('h3')!;
      expect(section.getAttribute('aria-labelledby')).toBe(heading.id);
    });
  });

  describe('a text field', () => {
    it('warns against secrets', () => {
      const root = render([REPO_FIELD]);
      expect(root.textContent).toContain("Don't enter passwords or other secrets here.");
    });

    it('labels the input and links its hint', () => {
      const root = render([REPO_FIELD]);
      const input = root.querySelector('input') as HTMLInputElement;
      const label = root.querySelector(`label[for="${input.id}"]`);
      expect(label?.textContent).toContain('Repository name');
      expect(label?.textContent).toContain('(required)');
      const hint = document.getElementById(input.getAttribute('aria-describedby') ?? '');
      expect(root.contains(hint)).toBe(true);
      expect(hint?.textContent).toContain('Enter acme/scratch exactly');
    });

    it('keeps Submit disabled until the required field is filled, then sends accept', async () => {
      const root = render([REPO_FIELD]);
      const submit = button(root, 'Submit');
      expect(submit.disabled).toBe(true);

      type(root.querySelector('input') as HTMLInputElement, 'acme/scratch');
      expect(submit.disabled).toBe(false);

      submit.click();
      await fixture.whenStable();

      expect(resume).toHaveBeenCalledWith(
        'int-1',
        { action: 'accept', content: { repository_name: 'acme/scratch' } },
        { sessionId: 's1' },
      );
      expect(service.hasPending()).toBe(false);
    });

    it('shows why a touched required field blocks', () => {
      const root = render([REPO_FIELD]);
      const input = root.querySelector('input') as HTMLInputElement;
      input.dispatchEvent(new Event('blur'));
      fixture.detectChanges();

      expect(input.getAttribute('aria-invalid')).toBe('true');
      expect(root.querySelector('.field-error')?.textContent).toContain('This is required.');
    });
  });

  it('Decline sends decline, never null', async () => {
    const root = render([REPO_FIELD]);
    button(root, 'Decline').click();
    await fixture.whenStable();

    expect(resume).toHaveBeenCalledWith('int-1', { action: 'decline' }, { sessionId: 's1' });
  });

  it('has no secrets note on a form without text', () => {
    const root = render([{ name: 'ok', kind: 'boolean', title: 'Proceed?', required: true }]);
    expect(root.textContent).not.toContain("Don't enter passwords");
  });

  describe('choices', () => {
    const few: McpElicitationField = {
      name: 'size',
      kind: 'choice',
      title: 'Size',
      required: true,
      multiSelect: false,
      options: [
        { value: 's', label: 'Small' },
        { value: 'l', label: 'Large' },
      ],
    };

    it('renders a few options as radio chips with no "Other"', async () => {
      const root = render([few]);
      const radios = root.querySelectorAll('[role="radio"]');
      expect(radios).toHaveLength(2);
      expect(root.textContent).not.toContain('Other');

      (radios[1] as HTMLButtonElement).click();
      fixture.detectChanges();
      expect(radios[1].getAttribute('aria-checked')).toBe('true');

      button(root, 'Submit').click();
      await fixture.whenStable();
      expect(resume).toHaveBeenCalledWith('int-1', { action: 'accept', content: { size: 'l' } }, expect.anything());
    });

    it('renders more than four options as a select', () => {
      const many: McpElicitationField = {
        ...few,
        options: ['a', 'b', 'c', 'd', 'e'].map((v) => ({ value: v, label: v.toUpperCase() })),
      };
      const root = render([many]);
      const select = root.querySelector('select');
      expect(select).not.toBeNull();
      expect(select!.querySelectorAll('option')).toHaveLength(6);
    });

    it('toggles a multi-select and sends the chosen values', async () => {
      const multi: McpElicitationField = {
        name: 'labels',
        kind: 'choice',
        title: 'Labels',
        required: false,
        multiSelect: true,
        options: [
          { value: 'bug', label: 'Bug' },
          { value: 'docs', label: 'Docs' },
        ],
      };
      const root = render([multi]);
      const boxes = root.querySelectorAll<HTMLButtonElement>('[role="checkbox"]');
      expect(boxes).toHaveLength(2);
      boxes[0].click();
      boxes[1].click();
      boxes[0].click();
      fixture.detectChanges();

      button(root, 'Submit').click();
      await fixture.whenStable();
      expect(resume).toHaveBeenCalledWith('int-1', { action: 'accept', content: { labels: ['docs'] } }, expect.anything());
    });
  });

  it('starts a required boolean with no default unset', async () => {
    const root = render([{ name: 'ok', kind: 'boolean', title: 'Proceed?', required: true }]);
    const [yes, no] = Array.from(root.querySelectorAll<HTMLButtonElement>('[role="radio"]'));
    expect(yes.getAttribute('aria-checked')).toBe('false');
    expect(no.getAttribute('aria-checked')).toBe('false');
    expect(button(root, 'Submit').disabled).toBe(true);

    no.click();
    fixture.detectChanges();
    button(root, 'Submit').click();
    await fixture.whenStable();
    expect(resume).toHaveBeenCalledWith('int-1', { action: 'accept', content: { ok: false } }, expect.anything());
  });

  it('refuses a fractional integer and a number out of range', () => {
    const root = render([
      { name: 'count', kind: 'integer', title: 'Count', required: true, minimum: 1, maximum: 5 },
    ]);
    const input = root.querySelector('input[type="number"]') as HTMLInputElement;
    const submit = button(root, 'Submit');

    type(input, '2.5');
    expect(submit.disabled).toBe(true);
    type(input, '9');
    expect(submit.disabled).toBe(true);
    type(input, '3');
    expect(submit.disabled).toBe(false);
  });
});

describe('mcp-elicitation-form helpers', () => {
  it('omits optional fields left empty and keeps the server default', () => {
    const fields: McpElicitationField[] = [
      { name: 'note', kind: 'text', title: 'Note', required: false, minLength: 3 },
      { name: 'tags', kind: 'choice', title: 'Tags', required: false, multiSelect: true, options: [{ value: 'a', label: 'A' }] },
      { name: 'n', kind: 'number', title: 'N', required: false },
      { name: 'flag', kind: 'boolean', title: 'Flag', required: false, default: true },
    ];
    const form = buildElicitationForm(fields);

    expect(form.valid).toBe(true);
    expect(readElicitationAnswer(fields, form.getRawValue())).toEqual({ flag: true });
  });

  it('drops a default the server did not offer as a choice', () => {
    const fields: McpElicitationField[] = [
      { name: 'c', kind: 'choice', title: 'C', required: false, default: 'z', options: [{ value: 'a', label: 'A' }] },
    ];
    expect(buildElicitationForm(fields).getRawValue()).toEqual({ c: null });
  });

  it('sends a date-time in UTC', () => {
    const fields: McpElicitationField[] = [
      { name: 'when', kind: 'text', title: 'When', required: true, format: 'date-time' },
    ];
    const answer = readElicitationAnswer(fields, { when: '2026-11-25T10:30' });
    expect(answer['when']).toBe(new Date('2026-11-25T10:30').toISOString());
  });

  it('labels a tool by its own name, without the server prefix', () => {
    expect(toolLabel('github_issues___delete_repository')).toBe('delete_repository');
    expect(toolLabel('plain')).toBe('plain');
  });
});
