import { Injectable, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { Project } from '../models/project.model';
import { ProjectApiService } from './project-api.service';

/**
 * The user-facing sentence for a failed projects call: the API's own `detail`
 * (the backend writes those for people, e.g. "This project is archived. Restore it
 * to make changes."), else a generic line.
 */
export function projectErrorMessage(err: unknown, fallback = 'Something went wrong. Please try again.'): string {
  if (err instanceof HttpErrorResponse) {
    const detail = err.error?.detail;
    if (typeof detail === 'string' && detail) return detail;
    if (err.status === 0) return 'Can’t reach the server. Check your connection and try again.';
  }
  return fallback;
}

/** Whether a failure means "Projects are switched off here" (the kill switch 404s the list). */
export function isUnavailable(err: unknown): boolean {
  return err instanceof HttpErrorResponse && err.status === 404;
}

/** The project list: the caller's own and shared-in projects, most recently updated first. */
@Injectable({ providedIn: 'root' })
export class ProjectsService {
  private api = inject(ProjectApiService);

  private projects = signal<Project[]>([]);
  private loading = signal(false);
  private error = signal<string | null>(null);
  private available = signal<boolean | null>(null);

  readonly projects$ = this.projects.asReadonly();
  readonly loading$ = this.loading.asReadonly();
  readonly error$ = this.error.asReadonly();
  /** False while `PROJECTS_ENABLED` is off in this environment; null until known. */
  readonly available$ = this.available.asReadonly();

  async load(includeArchived = true): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.api.list(includeArchived));
      this.projects.set(response.projects);
      this.available.set(true);
    } catch (err) {
      if (isUnavailable(err)) {
        this.available.set(false);
        this.projects.set([]);
      } else {
        this.error.set(projectErrorMessage(err, 'Your projects could not be loaded.'));
      }
    } finally {
      this.loading.set(false);
    }
  }

  async create(name: string, description: string): Promise<Project> {
    const project = await firstValueFrom(this.api.create({ name, description }));
    this.projects.update(list => [project, ...list]);
    return project;
  }

  /** Replace one project in the list after it changed elsewhere (rename, archive). */
  upsert(project: Project): void {
    this.projects.update(list => {
      const others = list.filter(p => p.projectId !== project.projectId);
      return [project, ...others];
    });
  }

  remove(projectId: string): void {
    this.projects.update(list => list.filter(p => p.projectId !== projectId));
  }
}
