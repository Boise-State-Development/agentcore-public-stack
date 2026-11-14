import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FieldSetService } from '../services/field-set.service';
import { FieldSetsSectionComponent } from './field-sets-section.component';

describe('FieldSetsSectionComponent', () => {
  afterEach(() => TestBed.resetTestingModule());

  async function render(read: ReturnType<typeof vi.fn>): Promise<HTMLElement> {
    TestBed.configureTestingModule({
      imports: [FieldSetsSectionComponent],
      providers: [
        provideRouter([]),
        {
          provide: FieldSetService,
          useValue: {
            list: vi.fn().mockResolvedValue({
              skillId: 'grants',
              maxFields: 14,
              fieldSets: [
                { name: 'intake', filename: 'field-set.intake.md', size: 1, contentHash: 'a' },
                { name: 'legacy', filename: 'field-set.legacy.md', size: 1, contentHash: 'b' },
              ],
            }),
            read,
            messageFor: (_: unknown, fallback: string) => fallback,
          },
        },
      ],
    });
    const fixture = TestBed.createComponent(FieldSetsSectionComponent);
    fixture.componentRef.setInput('skillId', 'grants');
    fixture.detectChanges();
    for (let i = 0; i < 3; i++) {
      await new Promise((resolve) => setTimeout(resolve));
      await fixture.whenStable();
      fixture.detectChanges();
    }
    return fixture.nativeElement as HTMLElement;
  }

  it('lists each set with its title, field count and version, and a broken one marked', async () => {
    const read = vi.fn(async (_skill: string, name: string) =>
      name === 'intake'
        ? { ok: true, set: { title: 'Grant Intake', version: '2', fields: [{}, {}, {}] } }
        : { ok: false, error: { message: "'find' is required", markdown: '' } },
    );

    const el = await render(read);

    expect(el.textContent).toContain('Grant Intake');
    expect(el.textContent).toContain('3 fields · version 2');
    expect(el.textContent).toContain("Can't be opened as a form");
    const links = [...el.querySelectorAll('a')].map((a) => a.getAttribute('href'));
    expect(links).toContain('/customize/skills/grants/field-sets/intake');
    expect(links).toContain('/customize/skills/grants/field-sets/legacy');
    expect(links).toContain('/customize/skills/grants/field-sets/new');
  });
});
