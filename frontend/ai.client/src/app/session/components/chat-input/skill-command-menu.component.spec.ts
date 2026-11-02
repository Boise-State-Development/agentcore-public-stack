import { describe, it, expect, beforeEach } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SkillCommandMenuComponent } from './skill-command-menu.component';
import { SkillCommand } from '../../../services/skill/skill-command.service';

const COMMANDS: SkillCommand[] = [
  { skillId: 'grant_writing', slug: 'grant-writing', name: 'Grant Writing', description: 'Narratives.' },
];

describe('SkillCommandMenuComponent', () => {
  let fixture: ComponentFixture<SkillCommandMenuComponent>;

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [SkillCommandMenuComponent] }).compileComponents();
    fixture = TestBed.createComponent(SkillCommandMenuComponent);
    fixture.componentRef.setInput('items', COMMANDS);
    fixture.detectChanges();
  });

  function text(): string {
    return (fixture.nativeElement as HTMLElement).textContent?.replace(/\s+/g, ' ').trim() ?? '';
  }

  function listbox(): HTMLElement {
    return (fixture.nativeElement as HTMLElement).querySelector('[role="listbox"]') as HTMLElement;
  }

  it('offers Browse skills over the user’s own commands', () => {
    expect(text()).toContain('/grant-writing');
    expect(text()).toContain('Browse skills');
    expect(listbox().getAttribute('aria-label')).toBe('Skills you can run');
  });

  it('names the query when nothing matches', () => {
    fixture.componentRef.setInput('items', []);
    fixture.componentRef.setInput('query', 'zzz');
    fixture.detectChanges();
    expect(text()).toContain('No skills match “zzz”.');
  });

  // A project's harness dictates the skills, so Customize cannot change them: the menu
  // speaks for the project, and with nothing bound it says where skills are chosen.
  it("in a project, points at the project's Skills page and says where its skills come from", () => {
    fixture.componentRef.setInput('projectScoped', true);
    fixture.componentRef.setInput('items', []);
    fixture.detectChanges();

    expect(text()).toContain("This project's skills are chosen on its Skills page.");
    expect(text()).toContain('Project skills');
    expect(text()).not.toContain('Browse skills');
    expect(listbox().getAttribute('aria-label')).toBe('Skills this project can run');
  });

  it('in a project, a typed query that matches nothing still says so', () => {
    fixture.componentRef.setInput('projectScoped', true);
    fixture.componentRef.setInput('items', []);
    fixture.componentRef.setInput('query', 'zzz');
    fixture.detectChanges();
    expect(text()).toContain('No skills match “zzz”.');
    expect(text()).not.toContain('chosen on its Skills page');
  });
});
