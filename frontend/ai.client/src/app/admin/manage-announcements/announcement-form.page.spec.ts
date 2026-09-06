import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router } from '@angular/router';
import { AnnouncementFormPage } from './announcement-form.page';
import { AnnouncementsAdminService } from './services/announcements-admin.service';
import { AppRolesService } from '../roles/services/app-roles.service';
import { Announcement } from './models/announcement.model';

function makeAnnouncement(overrides: Partial<Announcement> = {}): Announcement {
  return {
    announcement_id: 'a1',
    title: 'Skills are here',
    body_markdown: '# Skills',
    summary: null,
    surfaces: ['panel'],
    severity: 'info',
    state: 'draft',
    publish_at: '2026-01-01T00:00:00Z',
    expires_at: null,
    target_roles: ['*'],
    show_to_new_users: false,
    requires_ack: false,
    cta_label: null,
    cta_url: null,
    revision: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    created_by: 'admin@example.com',
    ...overrides,
  };
}

describe('AnnouncementFormPage', () => {
  let service: {
    get: ReturnType<typeof vi.fn>;
    create: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
  };
  let router: { navigate: ReturnType<typeof vi.fn> };
  let paramId: string | null;

  beforeEach(() => {
    TestBed.resetTestingModule();
    paramId = null;
    service = {
      get: vi.fn(async () => makeAnnouncement()),
      create: vi.fn(async () => makeAnnouncement()),
      update: vi.fn(async () => makeAnnouncement()),
    };
    router = { navigate: vi.fn() };

    // DI-token overrides rather than vi.mock, per house convention.
    TestBed.configureTestingModule({
      providers: [
        { provide: AnnouncementsAdminService, useValue: service },
        { provide: AppRolesService, useValue: { getRoles: () => [
          { roleId: 'faculty', displayName: 'Faculty' },
          { roleId: 'student', displayName: 'Student' },
        ] } },
        { provide: Router, useValue: router },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { paramMap: { get: () => paramId } } },
        },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  async function createPage() {
    const page = TestBed.runInInjectionContext(() => new AnnouncementFormPage());
    await page.ngOnInit();
    return page as any;
  }

  function fill(page: any, values: Record<string, unknown>) {
    page.form.patchValue({
      title: 'Skills are here',
      body_markdown: '# Skills',
      ...values,
    });
  }

  describe('submit gate reactivity', () => {
    /**
     * The regression that browser verification caught and the original specs
     * missed: they only ever read `canSubmit()` *after* filling the form, so
     * its first evaluation saw a valid form, tracked every signal, and stayed
     * reactive. In the real page the first read happens while the form is
     * empty — and an early `return` on the non-signal `form.invalid` shortened
     * the computed's dependency set to `isSubmitting` alone. Nothing could
     * re-enable the button afterwards.
     *
     * Reading it empty FIRST is the whole point of these tests.
     */
    it('enables once the form is filled, having first been read while empty', async () => {
      const page = await createPage();

      expect(page.canSubmit()).toBe(false); // first evaluation, empty form

      fill(page, {});

      expect(page.canSubmit()).toBe(true);
    });

    it('stays reactive to a later invalidation', async () => {
      const page = await createPage();
      expect(page.canSubmit()).toBe(false);

      fill(page, {});
      expect(page.canSubmit()).toBe(true);

      // Clearing a required field must disable it again.
      page.form.patchValue({ title: '' });
      expect(page.canSubmit()).toBe(false);
    });

    it('re-enables after a blocking rule is satisfied, read empty first', async () => {
      const page = await createPage();
      expect(page.canSubmit()).toBe(false);

      fill(page, { banner: true });
      expect(page.canSubmit()).toBe(false); // banner with no expiry

      page.form.patchValue({ expires_at: '2099-01-01T00:00' });
      expect(page.canSubmit()).toBe(true);
    });
  });

  describe('surfaces', () => {
    it('always sends panel, even though the form has no panel checkbox', async () => {
      // The server forces it too (§D1), but sending it keeps the payload
      // honest about what was chosen.
      const page = await createPage();
      fill(page, {});
      await page.onSubmit();

      expect(service.create).toHaveBeenCalledWith(
        expect.objectContaining({ surfaces: ['panel'] }),
      );
    });

    it('adds banner and modal when selected', async () => {
      const page = await createPage();
      fill(page, { banner: true, modal: true, expires_at: '2099-01-01T00:00' });
      await page.onSubmit();

      expect(service.create).toHaveBeenCalledWith(
        expect.objectContaining({ surfaces: ['panel', 'banner', 'modal'] }),
      );
    });

    it('clears requiresAck when the modal is unchecked', async () => {
      // Otherwise unchecking modal leaves a stale flag on the record.
      const page = await createPage();
      fill(page, { modal: true, expires_at: '2099-01-01T00:00', requires_ack: true });
      expect(page.form.controls.requires_ack.value).toBe(true);

      page.form.patchValue({ modal: false });
      expect(page.form.controls.requires_ack.value).toBe(false);
    });
  });

  describe('expiry rule (§5)', () => {
    it('blocks submit when a banner has no expiry', async () => {
      const page = await createPage();
      fill(page, { banner: true });

      expect(page.expiryMissing()).toBe(true);
      expect(page.canSubmit()).toBe(false);
    });

    it('blocks submit when a modal has no expiry', async () => {
      const page = await createPage();
      fill(page, { modal: true });

      expect(page.canSubmit()).toBe(false);
    });

    it('allows a panel-only announcement with no expiry', async () => {
      const page = await createPage();
      fill(page, {});

      expect(page.expiryMissing()).toBe(false);
      expect(page.canSubmit()).toBe(true);
    });

    it('rejects an expiry that precedes the publish date', async () => {
      const page = await createPage();
      fill(page, {
        banner: true,
        publish_at: '2099-06-01T00:00',
        expires_at: '2099-01-01T00:00',
      });

      expect(page.expiryBeforePublish()).toBe(true);
      expect(page.canSubmit()).toBe(false);
    });
  });

  describe('call to action', () => {
    it('rejects a label with no URL', async () => {
      const page = await createPage();
      fill(page, { cta_label: 'Read more' });
      expect(page.ctaIncomplete()).toBe(true);
    });

    it('rejects a URL with no label', async () => {
      const page = await createPage();
      fill(page, { cta_url: 'https://example.test' });
      expect(page.ctaIncomplete()).toBe(true);
    });

    it('rejects a javascript: URL before it reaches the API', async () => {
      // The server rejects it too — this just saves a round trip.
      const page = await createPage();
      fill(page, { cta_label: 'Click', cta_url: 'javascript:alert(1)' });

      expect(page.ctaIncomplete()).toBe(true);
      expect(page.canSubmit()).toBe(false);
    });

    it('accepts both together', async () => {
      const page = await createPage();
      fill(page, { cta_label: 'Read more', cta_url: 'https://example.test' });
      expect(page.ctaIncomplete()).toBe(false);
    });

    it('sends nulls rather than empty strings when blank', async () => {
      const page = await createPage();
      fill(page, {});
      await page.onSubmit();

      expect(service.create).toHaveBeenCalledWith(
        expect.objectContaining({ cta_label: null, cta_url: null, summary: null }),
      );
    });
  });

  describe('audience', () => {
    it('sends the wildcard when Everyone is checked', async () => {
      const page = await createPage();
      fill(page, {});
      await page.onSubmit();

      expect(service.create).toHaveBeenCalledWith(
        expect.objectContaining({ target_roles: ['*'] }),
      );
    });

    it('sends the picked roles when Everyone is unchecked', async () => {
      const page = await createPage();
      fill(page, { all_roles: false });
      page.toggleRole('faculty');
      await page.onSubmit();

      expect(service.create).toHaveBeenCalledWith(
        expect.objectContaining({ target_roles: ['faculty'] }),
      );
    });

    it('blocks submit when Everyone is off and no role is picked', async () => {
      const page = await createPage();
      fill(page, { all_roles: false });
      expect(page.canSubmit()).toBe(false);
    });

    it('defaults showToNewUsers off (§D6)', async () => {
      const page = await createPage();
      fill(page, {});
      await page.onSubmit();

      expect(service.create).toHaveBeenCalledWith(
        expect.objectContaining({ show_to_new_users: false }),
      );
    });
  });

  describe('body size', () => {
    it('blocks submit past 16 KB', async () => {
      const page = await createPage();
      fill(page, { body_markdown: 'x'.repeat(16 * 1024 + 1) });
      expect(page.bodyOverLimit()).toBe(true);
      expect(page.canSubmit()).toBe(false);
    });

    it('counts bytes, not characters', async () => {
      const page = await createPage();
      fill(page, { body_markdown: '✅'.repeat(6000) }); // 3 bytes each
      expect(page.bodyBytes()).toBe(18000);
      expect(page.bodyOverLimit()).toBe(true);
    });
  });

  describe('create vs edit', () => {
    it('creates as a draft — publishing is a separate action', async () => {
      const page = await createPage();
      fill(page, {});
      await page.onSubmit();

      expect(service.create).toHaveBeenCalledWith(
        expect.objectContaining({ state: 'draft' }),
      );
    });

    it('never sends state on an edit', async () => {
      // state is owned by publish/archive. Sending it from an edit form would
      // be a route around the lifecycle guard.
      paramId = 'a1';
      const page = await createPage();
      await page.onSubmit();

      expect(service.update).toHaveBeenCalledTimes(1);
      const [, payload] = service.update.mock.calls[0];
      expect(payload).not.toHaveProperty('state');
      expect(payload).not.toHaveProperty('revision');
    });

    it('loads an existing announcement into the form', async () => {
      paramId = 'a1';
      service.get = vi.fn(async () =>
        makeAnnouncement({
          title: 'Loaded',
          surfaces: ['panel', 'banner'],
          expires_at: '2099-01-01T00:00:00Z',
          target_roles: ['faculty'],
          severity: 'warning',
        }),
      );
      const page = await createPage();

      expect(page.form.controls.title.value).toBe('Loaded');
      expect(page.form.controls.banner.value).toBe(true);
      expect(page.form.controls.all_roles.value).toBe(false);
      expect(page.selectedRoles()).toEqual(['faculty']);
      expect(page.form.controls.severity.value).toBe('warning');
    });

    it('treats an empty target list as Everyone', async () => {
      paramId = 'a1';
      service.get = vi.fn(async () => makeAnnouncement({ target_roles: [] }));
      const page = await createPage();

      expect(page.form.controls.all_roles.value).toBe(true);
    });

    it('surfaces the server detail when a save fails', async () => {
      const page = await createPage();
      fill(page, {});
      service.create = vi.fn(async () => {
        throw { error: { detail: 'expiresAt is required when surfaces include banner' } };
      });
      await page.onSubmit();

      expect(page.submitError()).toContain('expiresAt is required');
      expect(router.navigate).not.toHaveBeenCalled();
    });

    it('returns to the list on success', async () => {
      const page = await createPage();
      fill(page, {});
      await page.onSubmit();

      expect(router.navigate).toHaveBeenCalledWith(['/admin/manage-announcements']);
    });
  });
});
