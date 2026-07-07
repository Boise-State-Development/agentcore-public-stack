import { describe, it, expect, beforeEach } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { BackgroundTaskService } from './background-task.service';

describe('BackgroundTaskService', () => {
  let service: BackgroundTaskService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({ providers: [BackgroundTaskService] });
    service = TestBed.inject(BackgroundTaskService);
  });

  it('starts a processing task and returns its id', () => {
    const id = service.start('Running "X"', 'Working…');
    const [task] = service.tasks();
    expect(task.id).toBe(id);
    expect(task.status).toBe('processing');
    expect(task.title).toBe('Running "X"');
    expect(task.detail).toBe('Working…');
    expect(task.route).toBeNull();
  });

  it('completes a task with a view route and detail', () => {
    const id = service.start('Run');
    service.complete(id, { detail: 'Done', route: ['/s', 'sess-1'], viewLabel: 'View result' });
    const [task] = service.tasks();
    expect(task.status).toBe('completed');
    expect(task.detail).toBe('Done');
    expect(task.route).toEqual(['/s', 'sess-1']);
    expect(task.viewLabel).toBe('View result');
  });

  it('fails a task, optionally keeping a route to a partial session', () => {
    const id = service.start('Run');
    service.fail(id, 'It broke', ['/s', 'sess-2']);
    const [task] = service.tasks();
    expect(task.status).toBe('error');
    expect(task.detail).toBe('It broke');
    expect(task.route).toEqual(['/s', 'sess-2']);
  });

  it('dismisses a task by id and leaves others intact', () => {
    const a = service.start('A');
    const b = service.start('B');
    service.dismiss(a);
    expect(service.tasks().map((t) => t.id)).toEqual([b]);
  });

  it('only patches the targeted task', () => {
    const a = service.start('A');
    const b = service.start('B');
    service.complete(a, { detail: 'done' });
    const byId = Object.fromEntries(service.tasks().map((t) => [t.id, t.status]));
    expect(byId[a]).toBe('completed');
    expect(byId[b]).toBe('processing');
  });
});
