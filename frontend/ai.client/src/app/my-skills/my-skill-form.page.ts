import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { FormBuilder, ReactiveFormsModule, Validators } from '@angular/forms';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowUpTray,
  heroEye,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { parseSkillMarkdown } from '../admin/skills/models/skill-import.util';
import {
  DISALLOWED_RESOURCE_MESSAGE,
  RESOURCE_ACCEPT_ATTR,
  isAllowedResourceFilename,
} from '../shared/skills/skill-resource-types';
import {
  MAX_RESOURCE_BYTES,
  MAX_RESOURCES_PER_SKILL,
  MySkillResourceRef,
  RESOURCE_KINDS,
  SkillResourceKind,
} from './models/my-skill.model';
import { MySkillService } from './services/my-skill.service';

/**
 * Create / edit one user-authored skill (Skills v2 PR-3).
 *
 * Two modes, mirroring the admin skill form:
 * - **Create** — supporting files are *staged* client-side and uploaded after
 *   the skill exists (uploads need a skill id).
 * - **Edit** — each file uploads immediately and the returned manifest is
 *   authoritative.
 *
 * The skill id is allocated by the backend from the display name, so unlike
 * the admin form there is no id field to fill in or validate.
 */
@Component({
  selector: 'app-my-skill-form',
  imports: [ReactiveFormsModule, RouterLink, NgIcon],
  templateUrl: './my-skill-form.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  viewProviders: [provideIcons({ heroArrowLeft, heroArrowUpTray, heroEye, heroTrash })],
})
export class MySkillFormPage {
  private fb = inject(FormBuilder);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private skillService = inject(MySkillService);

  protected readonly resourceKinds = RESOURCE_KINDS;
  protected readonly maxFiles = MAX_RESOURCES_PER_SKILL;
  /** `accept` filter for the file picker — the allowed extensions. */
  protected readonly acceptedFileTypes = RESOURCE_ACCEPT_ATTR;

  protected readonly skillId = signal<string | null>(null);
  protected readonly isEdit = computed(() => this.skillId() !== null);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly importNotice = signal<string | null>(null);

  /** Manifest of files already on the skill (edit mode). */
  protected readonly resources = signal<MySkillResourceRef[]>([]);
  /** Files staged for upload after create (create mode), keyed by filename. */
  protected readonly pendingFiles = signal<Map<string, { file: File; kind: SkillResourceKind }>>(
    new Map(),
  );
  protected readonly uploadKind = signal<SkillResourceKind>('reference');
  protected readonly uploading = signal(false);
  protected readonly viewing = signal<{ filename: string; content: string } | null>(null);

  /**
   * Frontmatter carried through from an import (or a loaded skill) that has no
   * form control of its own. Preserved verbatim on save so a bundle
   * round-trips (D2); `allowedTools` is advisory display metadata (D4).
   */
  protected readonly allowedTools = signal<string[]>([]);
  private readonly skillMetadata = signal<Record<string, unknown>>({});

  protected readonly form = this.fb.nonNullable.group({
    displayName: ['', [Validators.required, Validators.maxLength(200)]],
    description: ['', [Validators.required, Validators.maxLength(2000)]],
    instructions: [''],
  });

  protected readonly pendingList = computed(() =>
    [...this.pendingFiles().entries()].map(([filename, staged]) => ({
      filename,
      kind: staged.kind,
      size: staged.file.size,
    })),
  );

  protected readonly fileCount = computed(
    () => this.resources().length + this.pendingFiles().size,
  );

  constructor() {
    const id = this.route.snapshot.paramMap.get('skillId');
    if (id) {
      this.skillId.set(id);
      void this.loadSkill(id);
    }
  }

  private async loadSkill(id: string): Promise<void> {
    try {
      const skill = await this.skillService.getSkill(id);
      this.form.patchValue({
        displayName: skill.displayName,
        description: skill.description,
        instructions: skill.instructions,
      });
      this.resources.set(skill.resources);
      this.allowedTools.set(skill.allowedTools ?? []);
      this.skillMetadata.set(skill.skillMetadata ?? {});
    } catch {
      this.error.set("We couldn't load that skill. It may have been deleted.");
    }
  }

  // ---- SKILL.md import ---------------------------------------------------

  protected async onImportSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) {
      return;
    }

    const text = await file.text();
    const parsed = parseSkillMarkdown(text);

    this.form.patchValue({
      displayName: parsed.name || this.form.controls.displayName.value,
      description: parsed.description || this.form.controls.description.value,
      instructions: parsed.instructions,
    });
    // Everything the form has no control for still round-trips (D2/D4).
    this.allowedTools.set(parsed.allowedTools);
    this.skillMetadata.set(parsed.metadata);
    this.importNotice.set(
      parsed.name || parsed.description
        ? `Prefilled from ${file.name}. Review the name and description before saving.`
        : `${file.name} had no frontmatter, so its whole body became the instructions.`,
    );

    // Let the same file be re-picked if the user wants to redo the import.
    input.value = '';
  }

  // ---- bundle files ------------------------------------------------------

  protected setUploadKind(kind: string): void {
    this.uploadKind.set(kind as SkillResourceKind);
  }

  protected async onFilesSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const files = [...(input.files ?? [])];
    input.value = '';
    if (files.length === 0) {
      return;
    }

    const oversized = files.filter((f) => f.size > MAX_RESOURCE_BYTES);
    if (oversized.length > 0) {
      this.error.set(
        `${oversized.map((f) => f.name).join(', ')} exceeds the 1 MB per-file limit.`,
      );
      return;
    }
    // Mirror of the backend type allowlist. The server is the control (it
    // rejects these with a 400); this just turns that into an immediate,
    // specific message instead of a failed round-trip. Web-document types are
    // refused because a resource is downloaded by other users from this app's
    // own origin, where a rendered document could run script in their session.
    const disallowed = files.filter((f) => !isAllowedResourceFilename(f.name));
    if (disallowed.length > 0) {
      this.error.set(
        `${disallowed.map((f) => f.name).join(', ')} ${DISALLOWED_RESOURCE_MESSAGE}`,
      );
      return;
    }
    if (this.fileCount() + files.length > this.maxFiles) {
      this.error.set(`A skill can hold at most ${this.maxFiles} supporting files.`);
      return;
    }

    this.error.set(null);
    const kind = this.uploadKind();
    const id = this.skillId();

    if (!id) {
      // Create mode — stage until the skill exists.
      this.pendingFiles.update((current) => {
        const next = new Map(current);
        for (const file of files) {
          next.set(file.name, { file, kind });
        }
        return next;
      });
      return;
    }

    this.uploading.set(true);
    try {
      for (const file of files) {
        this.resources.set(await this.skillService.uploadResource(id, file, kind));
      }
    } catch {
      this.error.set(this.skillService.error$() ?? 'Failed to upload the file.');
    } finally {
      this.uploading.set(false);
    }
  }

  protected removePending(filename: string): void {
    this.pendingFiles.update((current) => {
      const next = new Map(current);
      next.delete(filename);
      return next;
    });
  }

  protected async viewResource(filename: string): Promise<void> {
    const id = this.skillId();
    if (!id) {
      return;
    }
    try {
      const content = await this.skillService.readResource(id, filename);
      this.viewing.set({ filename, content });
    } catch {
      this.error.set(`Couldn't read ${filename}.`);
    }
  }

  protected closeViewer(): void {
    this.viewing.set(null);
  }

  protected async deleteResource(filename: string): Promise<void> {
    const id = this.skillId();
    if (!id) {
      return;
    }
    try {
      this.resources.set(await this.skillService.deleteResource(id, filename));
      if (this.viewing()?.filename === filename) {
        this.viewing.set(null);
      }
    } catch {
      this.error.set(`Couldn't delete ${filename}.`);
    }
  }

  protected kindHint(kind: SkillResourceKind): string {
    return this.resourceKinds.find((k) => k.value === kind)?.hint ?? '';
  }

  // ---- save --------------------------------------------------------------

  protected async onSubmit(): Promise<void> {
    if (this.form.invalid || this.saving()) {
      this.form.markAllAsTouched();
      return;
    }

    this.saving.set(true);
    this.error.set(null);
    const value = {
      ...this.form.getRawValue(),
      allowedTools: this.allowedTools(),
      skillMetadata: this.skillMetadata(),
    };

    try {
      const id = this.skillId();
      if (id) {
        await this.skillService.updateSkill(id, value);
      } else {
        const created = await this.skillService.createSkill(value);
        // Uploads need a skill id, so staged files go up now.
        for (const [, staged] of this.pendingFiles()) {
          await this.skillService.uploadResource(created.skillId, staged.file, staged.kind);
        }
      }
      await this.router.navigate(['/my-skills']);
    } catch {
      this.error.set(this.skillService.error$() ?? 'Failed to save the skill.');
    } finally {
      this.saving.set(false);
    }
  }
}
