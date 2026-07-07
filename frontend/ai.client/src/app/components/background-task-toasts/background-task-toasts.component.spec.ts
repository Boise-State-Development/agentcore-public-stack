import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { BackgroundTaskToastsComponent } from './background-task-toasts.component';
import { BackgroundTaskService } from '../../services/background-tasks/background-task.service';

describe('BackgroundTaskToastsComponent', () => {
  let fixture: ComponentFixture<BackgroundTaskToastsComponent>;
  let tasks: BackgroundTaskService;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [BackgroundTaskToastsComponent],
      providers: [provideRouter([{ path: 's/:id', children: [] }]), BackgroundTaskService],
    }).compileComponents();

    fixture = TestBed.createComponent(BackgroundTaskToastsComponent);
    tasks = TestBed.inject(BackgroundTaskService);
    fixture.detectChanges();
  });

  it('renders a toast per task with its title', () => {
    tasks.start('Running "Briefing"', 'Working…');
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Running "Briefing"');
    expect(text).toContain('Working…');
  });

  it('shows a View button only once a route is attached', () => {
    const id = tasks.start('Run');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('button[aria-label="Dismiss"]')).toBeTruthy();

    tasks.complete(id, { route: ['/s', 'sess-1'], viewLabel: 'View result' });
    fixture.detectChanges();
    expect((fixture.nativeElement.textContent as string)).toContain('View result');
  });

  it('View navigates to the route and dismisses the toast', () => {
    const router = TestBed.inject(Router);
    const navigateSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    const id = tasks.start('Run');
    tasks.complete(id, { route: ['/s', 'sess-1'] });
    fixture.detectChanges();

    clickView();

    expect(navigateSpy).toHaveBeenCalledWith(['/s', 'sess-1']);
    expect(tasks.tasks().length).toBe(0);
  });

  it('View runs a custom onView handler instead of route navigation, then dismisses', () => {
    const router = TestBed.inject(Router);
    const navigateSpy = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    const onView = vi.fn();
    const id = tasks.start('Run');
    tasks.complete(id, { route: ['/s', 'sess-1'], onView });
    fixture.detectChanges();

    clickView();

    expect(onView).toHaveBeenCalled();
    expect(navigateSpy).not.toHaveBeenCalled();
    expect(tasks.tasks().length).toBe(0);
  });

  function clickView(): void {
    const viewBtn = Array.from(fixture.nativeElement.querySelectorAll('button')).find((b) =>
      (b as HTMLButtonElement).textContent?.includes('View'),
    ) as HTMLButtonElement;
    viewBtn.click();
  }
});
