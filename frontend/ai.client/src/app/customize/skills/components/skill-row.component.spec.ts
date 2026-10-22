import { describe, it, expect, beforeEach } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { SkillRowComponent } from './skill-row.component';

describe('SkillRowComponent', () => {
  let fixture: ComponentFixture<SkillRowComponent>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [provideRouter([])] });
    fixture = TestBed.createComponent(SkillRowComponent);
    fixture.componentRef.setInput('name', 'Rubric Writer');
    fixture.componentRef.setInput('detailLink', '/customize/skills/sk_rubric');
    fixture.detectChanges();
  });

  const text = () => fixture.nativeElement.textContent ?? '';
  const buttons = () => [...fixture.nativeElement.querySelectorAll('button')];
  const openMenu = () => {
    const trigger = buttons().find(b => b.getAttribute('aria-label')?.startsWith('Actions for'));
    (trigger as HTMLButtonElement).click();
    fixture.detectChanges();
    // The CDK menu renders into an overlay, outside the fixture's own element.
    return document.querySelector('[cdkMenu], [role="menu"]');
  };

  it('renders the name as the link to the detail page', () => {
    const link = fixture.nativeElement.querySelector('h3 a');
    expect(link.getAttribute('href')).toBe('/customize/skills/sk_rubric');
    expect(link.textContent).toContain('Rubric Writer');
  });

  it('keeps the actions out of the row link', () => {
    // The row link spreads over the whole row via `after:inset-0`. A control
    // swallowed by it is one the user cannot reach without navigating away.
    const trigger = buttons().find(b => b.getAttribute('aria-label')?.startsWith('Actions for'));
    expect(trigger?.closest('a')).toBeNull();
  });

  it('offers Turn on while off, and hides it once on', () => {
    expect(text()).toContain('Turn on');

    fixture.componentRef.setInput('enabled', true);
    fixture.detectChanges();
    expect(text()).not.toContain('Turn on');
  });

  it('offers no switch at all when the skill is not toggleable', () => {
    // A DRAFT skill is absent from GET /skills/, so `toggleSkill` would find no
    // row and return silently — the button would do nothing.
    fixture.componentRef.setInput('toggleable', false);
    fixture.detectChanges();
    expect(text()).not.toContain('Turn on');

    const menu = openMenu();
    expect(menu?.textContent).not.toContain('Turn off');
  });

  it('puts Turn off in the menu once the skill is on', () => {
    fixture.componentRef.setInput('enabled', true);
    fixture.detectChanges();

    const menu = openMenu();
    expect(menu?.textContent).toContain('Turn off');
  });

  it('offers edit and delete only for a skill the user owns', () => {
    const catalogMenu = openMenu();
    expect(catalogMenu?.textContent).toContain('View details');
    expect(catalogMenu?.textContent).not.toContain('Edit');
    expect(catalogMenu?.textContent).not.toContain('Delete');
  });

  it('offers edit and delete for an owned skill', () => {
    fixture.componentRef.setInput('owned', true);
    fixture.componentRef.setInput('editLink', '/customize/skills/sk_rubric/edit');
    fixture.detectChanges();

    const menu = openMenu();
    expect(menu?.textContent).toContain('Edit');
    expect(menu?.textContent).toContain('Delete');
    expect(menu?.querySelector('a[href="/customize/skills/sk_rubric/edit"]')).toBeTruthy();
  });

  it('refuses a second toggle while one is in flight', () => {
    fixture.componentRef.setInput('pending', true);
    fixture.detectChanges();

    const turnOn = buttons().find(b => (b.textContent ?? '').includes('Turning on'));
    expect(turnOn?.disabled).toBe(true);
  });
});
