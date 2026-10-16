import { Signal, computed, effect, inject, signal, untracked } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectsService, isUnavailable, projectErrorMessage } from '../services/projects.service';

/** One project, as a settings page holds it. */
export interface ProjectPageState {
  readonly project: Signal<Project | null>;
  readonly loading: Signal<boolean>;
  readonly error: Signal<string | null>;
  /** Editor or owner, on an active project. */
  readonly canEdit: Signal<boolean>;
  /** A change made on the page (a transfer, a new member count), kept here and in the sidebar's list. */
  update(project: Project): void;
}

/**
 * Load the project a settings page is about, from its route's `id` (an input bound
 * by `withComponentInputBinding`). Moving between projects reuses the page, so the
 * read follows the input rather than running once. Call it in a field initializer.
 */
export function injectProjectPage(id: Signal<string>): ProjectPageState {
  const api = inject(ProjectApiService);
  const projects = inject(ProjectsService);
  const project = signal<Project | null>(null);
  const loading = signal(true);
  const error = signal<string | null>(null);

  async function load(projectId: string): Promise<void> {
    loading.set(true);
    error.set(null);
    try {
      const loaded = await firstValueFrom(api.get(projectId));
      if (projectId === untracked(id)) project.set(loaded);
    } catch (err) {
      if (projectId !== untracked(id)) return;
      project.set(null);
      error.set(
        isUnavailable(err)
          ? 'This project doesn’t exist, or you’re not a member of it.'
          : projectErrorMessage(err, 'This project could not be loaded.'),
      );
    } finally {
      if (projectId === untracked(id)) loading.set(false);
    }
  }

  effect(() => {
    const projectId = id();
    untracked(() => void load(projectId));
  });

  return {
    project: project.asReadonly(),
    loading: loading.asReadonly(),
    error: error.asReadonly(),
    canEdit: computed(() => {
      const p = project();
      return !!p && p.role !== 'viewer' && p.status === 'active';
    }),
    update(changed: Project): void {
      project.set(changed);
      projects.upsert(changed);
    },
  };
}
