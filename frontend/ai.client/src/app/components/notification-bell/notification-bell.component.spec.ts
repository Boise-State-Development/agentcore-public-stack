import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { Router, provideRouter } from '@angular/router';
import { NotificationBellComponent, describeNotification, relativeTime } from './notification-bell.component';
import { NotificationsService } from '../../services/notifications/notifications.service';
import { AppNotification } from '../../services/notifications/notification.model';
import { SidenavService } from '../../services/sidenav/sidenav.service';

function notif(over: Partial<AppNotification> = {}): AppNotification {
  return {
    notificationId: 'n1', recipientEmail: 'me@x.edu', kind: 'project_invited', projectId: 'prj_1',
    projectName: 'Enrollment Sync', actorEmail: 'ann@x.edu', payload: { role: 'editor' },
    createdAt: '2026-09-24T00:00:00Z', readAt: null, ...over,
  };
}

describe('describeNotification', () => {
  it.each([
    [notif(), 'ann@x.edu added you to Enrollment Sync as an editor.'],
    [notif({ kind: 'project_role_changed', payload: { role: 'viewer' } }), 'ann@x.edu made you a viewer in Enrollment Sync.'],
    [notif({ kind: 'project_removed', payload: {} }), 'ann@x.edu removed you from Enrollment Sync.'],
    [notif({ kind: 'project_ownership_transferred', payload: {} }), 'ann@x.edu made you the owner of Enrollment Sync.'],
    [notif({ kind: 'project_archived', payload: {} }), 'ann@x.edu archived Enrollment Sync. It’s read-only until it’s restored.'],
    [notif({ kind: 'project_restored', payload: {} }), 'ann@x.edu restored Enrollment Sync.'],
    [notif({ kind: 'project_member_left', payload: { role: 'viewer' } }), 'ann@x.edu left Enrollment Sync.'],
    [notif({ kind: 'project_task_shared', payload: { shareId: 'sh-1', title: 'Vendor reply' } }), 'ann@x.edu shared “Vendor reply” with you in Enrollment Sync.'],
    [notif({ kind: 'project_task_shared', payload: { shareId: 'sh-1' } }), 'ann@x.edu shared “a task” with you in Enrollment Sync.'],
    [notif({ kind: 'project_proposal_pending', payload: { proposalId: 'p1', slug: 'sis' } }), 'ann@x.edu proposed a change to “sis” in Enrollment Sync’s memory.'],
    [notif({ kind: 'project_proposal_decided', payload: { slug: 'sis', decision: 'approved' } }), 'ann@x.edu approved your change to “sis” in Enrollment Sync.'],
    [notif({ kind: 'project_proposal_decided', payload: { slug: 'sis', decision: 'rejected' } }), 'ann@x.edu declined your change to “sis” in Enrollment Sync.'],
    [notif({ kind: 'project_memory_maintenance', payload: { runId: 'r1', fileCount: 3 } }), 'Memory maintenance that ann@x.edu ran suggests changes to 3 files in Enrollment Sync’s memory.'],
    [notif({ kind: 'project_memory_maintenance', payload: { runId: 'r1', fileCount: 1 } }), 'Memory maintenance that ann@x.edu ran suggests changes to 1 file in Enrollment Sync’s memory.'],
    [notif({ actorEmail: null, projectName: null, payload: {} }), 'Someone added you to a project.'],
    [notif({ actorName: 'Ann Lee' }), 'Ann Lee added you to Enrollment Sync as an editor.'],
  ])('%#: reads as a sentence', (n, text) => {
    expect(describeNotification(n)).toBe(text);
  });

  it('formats relative times', () => {
    const now = Date.parse('2026-09-24T12:00:00Z');
    expect(relativeTime('2026-09-24T11:59:40Z', now)).toBe('Just now');
    expect(relativeTime('2026-09-24T11:45:00Z', now)).toBe('15m ago');
    expect(relativeTime('2026-09-24T09:00:00Z', now)).toBe('3h ago');
    expect(relativeTime('2026-09-22T12:00:00Z', now)).toBe('2d ago');
  });
});

describe('NotificationBellComponent', () => {
  const notifications = signal<AppNotification[]>([]);
  const unreadCount = signal(0);
  const error = signal<string | null>(null);
  const ready = signal(true);
  const service = {
    notifications, unreadCount,
    loading: signal(false), error, ready,
    refresh: vi.fn().mockResolvedValue(undefined),
    markRead: vi.fn().mockResolvedValue(undefined),
    markAllRead: vi.fn().mockResolvedValue(undefined),
  };
  const sidenav = { close: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    notifications.set([]);
    unreadCount.set(0);
    error.set(null);
    ready.set(true);
    document.querySelectorAll('.cdk-overlay-container').forEach(el => (el.innerHTML = ''));
    TestBed.configureTestingModule({
      imports: [NotificationBellComponent],
      providers: [
        provideRouter([]),
        { provide: NotificationsService, useValue: service },
        { provide: SidenavService, useValue: sidenav },
      ],
    });
  });

  function render() {
    const fixture = TestBed.createComponent(NotificationBellComponent);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement, component: fixture.componentInstance as any };
  }

  it('loads the inbox and labels the bell with the unread count', () => {
    unreadCount.set(3);
    const { el } = render();
    expect(service.refresh).toHaveBeenCalledTimes(1);
    const button = el.querySelector('button')!;
    expect(button.getAttribute('aria-label')).toBe('Notifications, 3 unread');
    expect(button.textContent?.trim()).toBe('3');
  });

  it('caps the badge at 9+ and hides it at zero', () => {
    unreadCount.set(14);
    const { fixture, el } = render();
    expect(el.querySelector('button')!.textContent?.trim()).toBe('9+');
    unreadCount.set(0);
    fixture.detectChanges();
    expect(el.querySelector('button')!.getAttribute('aria-label')).toBe('Notifications');
    expect(el.querySelector('button')!.textContent?.trim()).toBe('');
  });

  /** Opens the panel and returns its menu element (it renders in the CDK overlay). */
  async function openPanel() {
    const rendered = render();
    (rendered.el.querySelector('button') as HTMLButtonElement).click();
    rendered.fixture.detectChanges();
    await rendered.fixture.whenStable();
    const menu = document.querySelector('[role="menu"]') as HTMLElement;
    expect(menu).toBeTruthy();
    return { ...rendered, menu };
  }

  /**
   * axe's `aria-required-children`, the rule the empty panel failed: a menu must
   * own at least one menu item, and nothing else with a role of its own (a `<p>`
   * is a paragraph). Walks through role-less wrappers the way axe does.
   */
  function ownedRoles(menu: HTMLElement): string[] {
    const roles: string[] = [];
    const implicit: Record<string, string> = { P: 'paragraph', BUTTON: 'button', UL: 'list', LI: 'listitem' };
    const walk = (el: Element) => {
      for (const child of Array.from(el.children)) {
        const role = child.getAttribute('role') ?? implicit[child.tagName] ?? null;
        if (role) roles.push(role);
        else walk(child);
      }
    };
    walk(menu);
    return roles;
  }

  it('an empty inbox is a disabled menu item, so the menu is never empty', async () => {
    const { menu } = await openPanel();
    const status = menu.querySelector('[data-testid="notification-status"]') as HTMLElement;
    expect(status.getAttribute('role')).toBe('menuitem');
    expect(status.getAttribute('aria-disabled')).toBe('true');
    expect(status.textContent?.trim()).toBe('You’re all caught up.');
    expect(ownedRoles(menu)).toEqual(['menuitem']);
  });

  it('loading and a failed load are disabled menu items too', async () => {
    ready.set(false);
    const { fixture, menu } = await openPanel();
    let status = menu.querySelector('[data-testid="notification-status"]') as HTMLElement;
    expect(status.getAttribute('aria-disabled')).toBe('true');
    expect(status.textContent?.trim()).toBe('Loading notifications…');
    expect(menu.querySelector('[aria-busy]')).toBeNull();
    expect(ownedRoles(menu)).toEqual(['menuitem']);

    error.set('Notifications couldn’t be loaded.');
    fixture.detectChanges();
    status = menu.querySelector('[data-testid="notification-status"]') as HTMLElement;
    expect(status.textContent?.trim()).toBe('Notifications couldn’t be loaded.');
    expect(ownedRoles(menu)).toEqual(['menuitem']);
  });

  it('with notifications the menu owns only its items, the header included', async () => {
    notifications.set([notif(), notif({ notificationId: 'n2', readAt: '2026-09-24T01:00:00Z' })]);
    unreadCount.set(1);
    const { menu } = await openPanel();
    expect(menu.querySelector('[data-testid="notification-status"]')).toBeNull();
    expect(menu.querySelector('p')).toBeNull();
    expect(ownedRoles(menu)).toEqual(['menuitem', 'menuitem', 'menuitem']); // two + "Mark all as read"
  });

  it('opening a notification marks it read and goes to the project', () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const { component } = render();
    const n = notif();
    component.open(n);
    expect(service.markRead).toHaveBeenCalledWith(n);
    expect(navigate).toHaveBeenCalledWith(['/projects', 'prj_1']);
    expect(sidenav.close).toHaveBeenCalled();
  });

  it('someone leaving opens the project on its Members tab', () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const { component } = render();
    component.open(notif({ kind: 'project_member_left' }));
    expect(navigate).toHaveBeenCalledWith(['/projects', 'prj_1', 'members']);
  });

  it('a proposal opens the Memory tab: the review queue, or the file a decision was about', () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const { component } = render();
    component.open(notif({ kind: 'project_proposal_pending', payload: { proposalId: 'p1', slug: 'sis' } }));
    expect(navigate).toHaveBeenCalledWith(['/projects', 'prj_1', 'memory'], { queryParams: { view: 'review' } });
    component.open(notif({ kind: 'project_proposal_decided', payload: { slug: 'sis', decision: 'approved' } }));
    expect(navigate).toHaveBeenLastCalledWith(['/projects', 'prj_1', 'memory'], { queryParams: { file: 'sis' } });
    component.open(notif({ kind: 'project_memory_maintenance', payload: { runId: 'r1', fileCount: 2 } }));
    expect(navigate).toHaveBeenLastCalledWith(['/projects', 'prj_1', 'memory'], { queryParams: { view: 'review' } });
  });

  it('a shared task opens the shared view, revoked or not', () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const { component } = render();
    component.open(notif({ kind: 'project_task_shared', payload: { shareId: 'sh-1', title: 'T' } }));
    expect(navigate).toHaveBeenCalledWith(['/shared', 'sh-1']);
    expect(sidenav.close).toHaveBeenCalled();
  });

  it('shows a shared task’s note under the sentence', () => {
    const { component } = render();
    expect(component.noteOf(notif({ kind: 'project_task_shared', payload: { shareId: 's', note: 'Over to you' } }))).toBe('Over to you');
    expect(component.noteOf(notif({ payload: { note: 'ignored' } }))).toBeNull();
  });

  it('a removal is only marked read: there is no project to open', () => {
    const navigate = vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
    const { component } = render();
    component.open(notif({ kind: 'project_removed' }));
    expect(service.markRead).toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it('refreshes when the tab comes back, at most once a minute', () => {
    const { component } = render();
    Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
    component.onVisibilityChange();
    expect(service.refresh).toHaveBeenCalledTimes(1);
    component.lastRefresh = Date.now() - 61_000;
    component.onVisibilityChange();
    expect(service.refresh).toHaveBeenCalledTimes(2);
  });
});
