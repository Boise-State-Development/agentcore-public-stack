// @vitest-environment jsdom
import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';

import { EmojiPickerComponent } from './emoji-picker.component';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';

/**
 * The picker is a ControlValueAccessor: a form drives its value via writeValue and reads
 * selections back through the registered onChange. We test that contract directly (the
 * popup and emoji-mart grid are presentation), so the admin template form's
 * `formControlName="emoji"` behaves like a native control.
 *
 * No detectChanges: instantiating renders none of the (heavy) emoji-mart dataset — we
 * exercise the accessor methods on the instance.
 */
describe('EmojiPickerComponent', () => {
  let component: any;
  let changes: string[];

  beforeEach(() => {
    changes = [];
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [{ provide: ThemeService, useValue: { theme: () => 'light' } }],
    });
    component = TestBed.createComponent(EmojiPickerComponent).componentInstance;
    component.registerOnChange((v: string) => changes.push(v));
  });

  it('writeValue preselects the current emoji (edit mode)', () => {
    component.writeValue('🎓');
    expect(component.value()).toBe('🎓');
  });

  it('treats a null/empty value as no emoji', () => {
    component.writeValue(null);
    expect(component.value()).toBe('');
  });

  it('selecting an emoji updates the value, emits it, and closes the popup', () => {
    component.isOpen.set(true);
    component.onSelect({ emoji: { native: '🚀' } });
    expect(component.value()).toBe('🚀');
    expect(changes).toEqual(['🚀']);
    expect(component.isOpen()).toBe(false);
  });

  it('clear resets the value and emits an empty string', () => {
    component.writeValue('🎓');
    component.clear();
    expect(component.value()).toBe('');
    expect(changes).toEqual(['']);
  });

  it('toggle opens and closes, but stays closed when disabled', () => {
    component.toggle();
    expect(component.isOpen()).toBe(true);
    component.toggle();
    expect(component.isOpen()).toBe(false);

    component.setDisabledState(true);
    component.toggle();
    expect(component.isOpen()).toBe(false);
  });
});
