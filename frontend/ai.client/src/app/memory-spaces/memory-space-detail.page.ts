import { Component, ChangeDetectionStrategy, inject, OnInit, signal, computed } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroPlus,
  heroShare,
  heroArrowDownTray,
  heroTrash,
  heroPencilSquare,
} from '@ng-icons/heroicons/outline';
import { MemorySpaceService } from './services/memory-space.service';
import { MemoryEntryRef, MemorySpaceDetail } from './models/memory-space.model';
import { EntryDialogComponent, EntryDialogData, EntryDialogResult } from './components/entry-dialog.component';
import {
  ShareSpaceDialogComponent,
  ShareSpaceDialogData,
  ShareSpaceDialogResult,
} from './components/share-space-dialog.component';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';

/**
 * Memory Space detail — view/edit the MEMORY.md index and the space's entries.
 * Editors get editable fields; viewers get read-only. Owner-only actions
 * (share) plus download/delete-or-leave live in the header.
 */
@Component({
  selector: 'app-memory-space-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroPlus,
      heroShare,
      heroArrowDownTray,
      heroTrash,
      heroPencilSquare,
    }),
  ],
  templateUrl: './memory-space-detail.page.html',
})
export class MemorySpaceDetailPage implements OnInit {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private dialog = inject(Dialog);
  private service = inject(MemorySpaceService);

  protected readonly space = signal<MemorySpaceDetail | null>(null);
  protected readonly indexText = signal<string>('');
  protected readonly loading = signal<boolean>(true);
  protected readonly error = signal<string | null>(null);
  protected readonly savingIndex = signal<boolean>(false);
  protected readonly exporting = signal<boolean>(false);

  protected readonly canEdit = computed<boolean>(() => {
    const role = this.space()?.role;
    return role === 'owner' || role === 'editor';
  });
  protected readonly isOwner = computed<boolean>(() => this.space()?.role === 'owner');
  protected readonly entries = computed<MemoryEntryRef[]>(() => this.space()?.entries ?? []);

  private get spaceId(): string {
    return this.route.snapshot.paramMap.get('id') ?? '';
  }

  ngOnInit(): void {
    void this.load();
  }

  private async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const detail = await this.service.getSpace(this.spaceId);
      this.space.set(detail);
      this.indexText.set(detail.index);
    } catch (err) {
      const status = (err as { status?: number } | null)?.status;
      this.error.set(
        status === 404
          ? 'This memory space no longer exists or you no longer have access.'
          : err instanceof Error
            ? err.message
            : 'Failed to load memory space',
      );
    } finally {
      this.loading.set(false);
    }
  }

  protected goBack(): void {
    void this.router.navigate(['/memory-spaces']);
  }

  protected async onSaveIndex(): Promise<void> {
    if (!this.canEdit()) {
      return;
    }
    this.savingIndex.set(true);
    this.error.set(null);
    try {
      await this.service.updateIndex(this.spaceId, this.indexText());
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to save index');
    } finally {
      this.savingIndex.set(false);
    }
  }

  protected async onNewEntry(): Promise<void> {
    const dialogRef = this.dialog.open<EntryDialogResult>(EntryDialogComponent, {
      data: { spaceId: this.spaceId, canEdit: true } as EntryDialogData,
    });
    const result = await firstValueFrom(dialogRef.closed);
    if (result?.action === 'saved') {
      await this.load();
    }
  }

  protected async onOpenEntry(entry: MemoryEntryRef): Promise<void> {
    const dialogRef = this.dialog.open<EntryDialogResult>(EntryDialogComponent, {
      data: {
        spaceId: this.spaceId,
        slug: entry.slug,
        type: entry.type,
        description: entry.description,
        canEdit: this.canEdit(),
      } as EntryDialogData,
    });
    const result = await firstValueFrom(dialogRef.closed);
    if (result?.action === 'saved') {
      await this.load();
    }
  }

  protected async onDeleteEntry(entry: MemoryEntryRef, event: Event): Promise<void> {
    event.stopPropagation();
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete entry',
        message: `Delete "${entry.slug}"? This cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });
    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed) {
      try {
        await this.service.deleteEntry(this.spaceId, entry.slug);
        await this.load();
      } catch (err) {
        this.error.set(err instanceof Error ? err.message : 'Failed to delete entry');
      }
    }
  }

  protected async onShare(): Promise<void> {
    const space = this.space();
    if (!space) {
      return;
    }
    const dialogRef = this.dialog.open<ShareSpaceDialogResult>(ShareSpaceDialogComponent, {
      data: { space } as ShareSpaceDialogData,
    });
    await firstValueFrom(dialogRef.closed);
  }

  protected async onDownload(): Promise<void> {
    const space = this.space();
    if (!space) {
      return;
    }
    this.exporting.set(true);
    try {
      const blob = await this.service.exportSpace(this.spaceId);
      this.triggerDownload(blob, `${this.safeFileName(space.name)}.zip`);
    } catch (err) {
      this.error.set(err instanceof Error ? err.message : 'Failed to export space');
    } finally {
      this.exporting.set(false);
    }
  }

  protected async onDeleteOrLeave(): Promise<void> {
    const space = this.space();
    if (!space) {
      return;
    }
    const isOwner = space.role === 'owner';
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: isOwner ? 'Delete memory space' : 'Leave memory space',
        message: isOwner
          ? `Permanently delete "${space.name}" and all its entries? This cannot be undone.`
          : `Leave "${space.name}"? You'll lose access until the owner shares it again.`,
        confirmText: isOwner ? 'Delete' : 'Leave',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });
    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed) {
      try {
        await this.service.deleteOrLeave(this.spaceId);
        this.goBack();
      } catch (err) {
        this.error.set(err instanceof Error ? err.message : 'Failed to remove space');
      }
    }
  }

  private triggerDownload(blob: Blob, fileName: string): void {
    if (typeof document === 'undefined') {
      return;
    }
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = fileName;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  }

  private safeFileName(name: string): string {
    const cleaned = name.trim().replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-._]+|[-._]+$/g, '');
    return cleaned || 'memory-space';
  }
}
