import { Component, ChangeDetectionStrategy, inject, OnInit, signal } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroCircleStack,
  heroShare,
  heroArrowDownTray,
  heroTrash,
  heroArrowRightStartOnRectangle,
} from '@ng-icons/heroicons/outline';
import { MemorySpaceService } from './services/memory-space.service';
import { MemorySpaceSummary } from './models/memory-space.model';
import {
  CreateSpaceDialogComponent,
  CreateSpaceDialogData,
  CreateSpaceDialogResult,
} from './components/create-space-dialog.component';
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
 * Memory Spaces list page — the "own your data" surface. Lists a user's owned
 * and shared-in spaces, creates from a template, and per space: open (detail),
 * share (owner), download `.zip`, delete/leave. The whole feature 404s while
 * the backend kill switch is off; the facade's `accessible$` drives a graceful
 * "unavailable" state rather than an error.
 */
@Component({
  selector: 'app-memory-spaces',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroPlus,
      heroCircleStack,
      heroShare,
      heroArrowDownTray,
      heroTrash,
      heroArrowRightStartOnRectangle,
    }),
  ],
  templateUrl: './memory-spaces.page.html',
})
export class MemorySpacesPage implements OnInit {
  private router = inject(Router);
  private dialog = inject(Dialog);
  protected readonly service = inject(MemorySpaceService);

  readonly spaces = this.service.spaces$;
  readonly loading = this.service.loading$;
  readonly error = this.service.error$;
  readonly accessible = this.service.accessible$;

  /** Space id currently exporting, so its button can show progress. */
  protected readonly exportingId = signal<string | null>(null);

  ngOnInit(): void {
    void this.service.loadSpaces();
  }

  protected openSpace(space: MemorySpaceSummary): void {
    void this.router.navigate(['/memory-spaces', space.spaceId]);
  }

  /** Friendly template name for the card badge, falling back to the raw id. */
  protected templateLabel(templateId: string): string {
    const match = this.service.templates$().find((t) => t.templateId === templateId);
    if (match) {
      return match.name;
    }
    return templateId
      .split('-')
      .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : word))
      .join(' ');
  }

  protected async onCreate(): Promise<void> {
    const dialogRef = this.dialog.open<CreateSpaceDialogResult>(CreateSpaceDialogComponent, {
      data: { templates: this.service.templates$() } as CreateSpaceDialogData,
    });
    const created = await firstValueFrom(dialogRef.closed);
    if (created) {
      void this.router.navigate(['/memory-spaces', created.spaceId]);
    }
  }

  protected async onShare(space: MemorySpaceSummary, event: Event): Promise<void> {
    event.stopPropagation();
    const dialogRef = this.dialog.open<ShareSpaceDialogResult>(ShareSpaceDialogComponent, {
      data: { space } as ShareSpaceDialogData,
    });
    await firstValueFrom(dialogRef.closed);
  }

  protected async onDownload(space: MemorySpaceSummary, event: Event): Promise<void> {
    event.stopPropagation();
    this.exportingId.set(space.spaceId);
    try {
      const blob = await this.service.exportSpace(space.spaceId);
      this.triggerDownload(blob, `${this.safeFileName(space.name)}.zip`);
    } catch (err) {
      console.error('Failed to export memory space:', err);
    } finally {
      this.exportingId.set(null);
    }
  }

  protected async onDeleteOrLeave(space: MemorySpaceSummary, event: Event): Promise<void> {
    event.stopPropagation();
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
        await this.service.deleteOrLeave(space.spaceId);
      } catch (err) {
        console.error('Failed to remove memory space:', err);
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
