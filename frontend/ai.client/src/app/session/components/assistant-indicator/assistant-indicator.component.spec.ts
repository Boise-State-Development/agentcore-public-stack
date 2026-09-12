import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import {
  AgentGovernance,
  AssistantIndicatorComponent,
} from './assistant-indicator.component';

describe('AssistantIndicatorComponent', () => {
  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
  });

  afterEach(() => TestBed.resetTestingModule());

  function create(governance: AgentGovernance | null = null, variant: 'card' | 'compact' = 'card') {
    const fixture = TestBed.createComponent(AssistantIndicatorComponent);
    fixture.componentRef.setInput('name', 'Rubric Builder');
    fixture.componentRef.setInput('variant', variant);
    fixture.componentRef.setInput('governance', governance);
    fixture.detectChanges();
    return fixture;
  }

  const text = (fixture: { nativeElement: HTMLElement }) => fixture.nativeElement.textContent ?? '';

  describe('governance', () => {
    it('says nothing when the caller does not know', () => {
      // The Designer preview and the marketplace test-drive both render this
      // component without applying the picker locks, so null must be silent
      // rather than a guess. See docs/specs/customize-surface.md step 4.
      const fixture = create(null);
      expect(fixture.componentInstance.isGoverned()).toBe(false);
      expect(fixture.nativeElement.querySelector('.pill-lock')).toBeNull();
      expect(fixture.nativeElement.querySelector('.indicator-lock')).toBeNull();
    });

    it('says nothing when an agent binds nothing', () => {
      const fixture = create({ modelName: null, toolCount: null, skillCount: null });
      expect(fixture.componentInstance.isGoverned()).toBe(false);
    });

    it('shows a lock on the chip once anything is fixed', () => {
      const fixture = create({ modelName: null, toolCount: 4, skillCount: null });
      expect(fixture.componentInstance.isGoverned()).toBe(true);
      expect(fixture.nativeElement.querySelector('.indicator-lock')).toBeTruthy();
    });

    it('shows the lock on the compact pill too', () => {
      const fixture = create({ modelName: 'Claude Sonnet 5', toolCount: null, skillCount: null }, 'compact');
      expect(fixture.nativeElement.querySelector('.pill-lock')).toBeTruthy();
    });

    it('lists what is fixed in the menu, and only what is fixed', () => {
      const fixture = create({ modelName: 'Claude Sonnet 5', toolCount: 4, skillCount: null });
      fixture.componentInstance.menuOpen.set(true);
      fixture.detectChanges();

      const menu = fixture.nativeElement.querySelector('.menu-governance') as HTMLElement;
      expect(menu).toBeTruthy();
      expect(menu.textContent).toContain('Claude Sonnet 5');
      expect(menu.textContent).toContain('4 tools');
      expect(menu.textContent).not.toContain('Skills');
    });

    it('tells the user why their Customize choices are being ignored', () => {
      // The whole point of step 4: with the settings drawer gone, this is the
      // only place that explains why a skill they enabled does nothing here.
      const fixture = create({ modelName: null, toolCount: null, skillCount: 2 });
      fixture.componentInstance.menuOpen.set(true);
      fixture.detectChanges();
      expect(text(fixture)).toContain("Customize don't apply in this conversation");
    });

    it('renders no governance block when nothing is fixed', () => {
      const fixture = create(null);
      fixture.componentInstance.menuOpen.set(true);
      fixture.detectChanges();
      expect(fixture.nativeElement.querySelector('.menu-governance')).toBeNull();
    });

    it('counts in the singular when there is one', () => {
      const fixture = create({ modelName: null, toolCount: 1, skillCount: 1 });
      expect(fixture.componentInstance.toolLabel()).toBe('1 tool');
      expect(fixture.componentInstance.skillLabel()).toBe('1 skill');
    });

    it('treats a zero count as not governed rather than as "0 tools"', () => {
      const fixture = create({ modelName: 'Claude Haiku 4.5', toolCount: 0, skillCount: 0 });
      expect(fixture.componentInstance.toolLabel()).toBeNull();
      expect(fixture.componentInstance.skillLabel()).toBeNull();
      // …but the pinned model still counts as governance.
      expect(fixture.componentInstance.isGoverned()).toBe(true);
    });

    it('names what is fixed in the lock’s accessible label', () => {
      // The glyph is the only cue on a collapsed chip; a screen-reader user
      // should not have to open the menu to learn what this overrides.
      const fixture = create({ modelName: 'Claude Sonnet 5', toolCount: 4, skillCount: 2 });
      expect(fixture.componentInstance.governanceLabel()).toBe(
        'This agent fixes model Claude Sonnet 5, 4 tools and 2 skills for this conversation.',
      );
    });

    it('reads naturally with a single fixed thing', () => {
      const fixture = create({ modelName: null, toolCount: 3, skillCount: null });
      expect(fixture.componentInstance.governanceLabel()).toBe(
        'This agent fixes 3 tools for this conversation.',
      );
    });
  });
});
