import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import {
  CreateSpaceRequest,
  EntryContent,
  MembersListResponse,
  MemoryEntryRef,
  MemorySpaceDetail,
  MemorySpaceSummary,
  ShareRequest,
  ShareRole,
  SpaceMember,
  SpaceTemplate,
  UpsertEntryRequest,
} from '../models/memory-space.model';
import { MemorySpaceApiService } from './memory-space-api.service';

/**
 * Signal-based state for the Memory Spaces feature. Mirrors the assistants /
 * schedules facades: private mutable signals, readonly public views, and async
 * methods that round-trip through the API service and keep local state in sync.
 *
 * `accessible$` rides the list call the same way ScheduleService does: a
 * successful (even empty) list means the caller can use the feature and the
 * `MEMORY_SPACES_ENABLED` kill switch is on; a 404 flips it false so the nav
 * entry and page hide gracefully instead of showing an error.
 */
@Injectable({ providedIn: 'root' })
export class MemorySpaceService {
  private apiService = inject(MemorySpaceApiService);

  private spaces = signal<MemorySpaceSummary[]>([]);
  private templates = signal<SpaceTemplate[]>([]);
  private loading = signal<boolean>(false);
  private error = signal<string | null>(null);
  private accessible = signal<boolean | null>(null);

  readonly spaces$ = this.spaces.asReadonly();
  readonly templates$ = this.templates.asReadonly();
  readonly loading$ = this.loading.asReadonly();
  readonly error$ = this.error.asReadonly();
  readonly accessible$ = this.accessible.asReadonly();

  async loadSpaces(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const response = await firstValueFrom(this.apiService.list());
      this.spaces.set(response?.spaces ?? []);
      this.templates.set(response?.templates ?? []);
      this.accessible.set(true);
    } catch (err: unknown) {
      const status = (err as { status?: number } | null)?.status;
      if (status === 404) {
        // Kill switch off — fail gracefully rather than surfacing an error.
        this.accessible.set(false);
        this.spaces.set([]);
        this.templates.set([]);
        return;
      }
      this.error.set(err instanceof Error ? err.message : 'Failed to load memory spaces');
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async createSpace(request: CreateSpaceRequest): Promise<MemorySpaceSummary> {
    this.error.set(null);
    try {
      const space = await firstValueFrom(this.apiService.create(request));
      this.spaces.update((current) => [...current, space]);
      return space;
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to create memory space');
      throw err;
    }
  }

  getSpace(spaceId: string): Promise<MemorySpaceDetail> {
    return firstValueFrom(this.apiService.get(spaceId));
  }

  /** Owner deletes the whole space; a member drops their own grant (leave). */
  async deleteOrLeave(spaceId: string): Promise<void> {
    this.error.set(null);
    try {
      await firstValueFrom(this.apiService.remove(spaceId));
      this.spaces.update((current) => current.filter((s) => s.spaceId !== spaceId));
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to remove memory space');
      throw err;
    }
  }

  /** Fetch the `.zip` export blob for a space. */
  exportSpace(spaceId: string): Promise<Blob> {
    return firstValueFrom(this.apiService.export(spaceId));
  }

  // ---- index (MEMORY.md) + entries ------------------------------------

  updateIndex(spaceId: string, content: string): Promise<{ content: string }> {
    return firstValueFrom(this.apiService.updateIndex(spaceId, content));
  }

  readEntry(spaceId: string, slug: string): Promise<EntryContent> {
    return firstValueFrom(this.apiService.readEntry(spaceId, slug));
  }

  upsertEntry(
    spaceId: string,
    slug: string,
    request: UpsertEntryRequest,
  ): Promise<MemoryEntryRef> {
    return firstValueFrom(this.apiService.upsertEntry(spaceId, slug, request));
  }

  deleteEntry(spaceId: string, slug: string): Promise<void> {
    return firstValueFrom(this.apiService.deleteEntry(spaceId, slug));
  }

  // ---- sharing ---------------------------------------------------------

  listShares(spaceId: string): Promise<MembersListResponse> {
    return firstValueFrom(this.apiService.listShares(spaceId));
  }

  addShare(spaceId: string, request: ShareRequest): Promise<SpaceMember> {
    return firstValueFrom(this.apiService.addShare(spaceId, request));
  }

  updateShare(spaceId: string, email: string, permission: ShareRole): Promise<SpaceMember> {
    return firstValueFrom(this.apiService.updateShare(spaceId, email, permission));
  }

  removeShare(spaceId: string, email: string): Promise<void> {
    return firstValueFrom(this.apiService.removeShare(spaceId, email));
  }
}
