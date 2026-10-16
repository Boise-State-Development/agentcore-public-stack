import { Injectable, inject } from '@angular/core';
import { ImportFileRef } from '../../assistants/models/file-source.model';
import { SyncInterval, SyncPolicy } from '../../assistants/models/sync-policy.model';
import { FileSourceService } from '../../assistants/services/file-source.service';
import { SyncPolicyService } from '../../assistants/services/sync-policy.service';
import { FolderAttachment } from '../../services/folder-attachments';
import { Project } from '../models/project.model';
import { ProjectsService } from './projects.service';

/** Files per import call: `POST /projects/{id}/knowledge/import` takes at most 50. */
export const FOLDER_IMPORT_BATCH_SIZE = 50;

/** What saving a folder as a project did. */
export interface SaveFolderAsProjectResult {
  project: Project;
  /** Files handed to the import; each is then downloaded in the background. */
  imported: number;
  /** Files whose import call failed, so they were never handed over. */
  notImported: number;
  /** Why some files were not imported, when any were not. */
  error: string | null;
  /** The folder's sync schedule, when one was asked for and created. */
  syncPolicy: SyncPolicy | null;
  /** Why a schedule that was asked for could not be created; the project is then a copy. */
  syncError: string | null;
}

/** What to do beyond the copy. */
export interface SaveFolderAsProjectOptions {
  /** Keep the project in step with the folder at this interval (folder sync). */
  syncInterval?: SyncInterval | null;
}

/**
 * A folder's KB sync source (assistant-kb-sync spec §11.1): the connector it
 * was attached through, then the provider's folder id.
 */
export function folderSyncSourceRef(row: FolderAttachment): string {
  return `${row.connectorId}:${row.providerFolderId}`;
}

/**
 * The folder's files a project can import: the ones the conversation actually
 * attached and that are still in the folder, once each.
 *
 * Folder attachments keep only the types a conversation can read on demand
 * (documents, spreadsheets, decks). Import checks no type up front, so a file
 * the project's knowledge base cannot ingest shows as failed on its Files
 * page, as any import does. Paths inside the folder are not kept: a project's
 * Files are one flat list.
 */
export function importableFolderFiles(row: FolderAttachment): ImportFileRef[] {
  const seen = new Set<string>();
  const files: ImportFileRef[] = [];
  for (const entry of row.files) {
    if (entry.status !== 'attached' || entry.removedFromSource) continue;
    if (seen.has(entry.providerFileId)) continue;
    seen.add(entry.providerFileId);
    files.push({ fileId: entry.providerFileId, name: entry.name });
  }
  return files;
}

/**
 * "Save as project" on a sent folder chip (composer-project-folder-picker spec
 * §5, PR-3): a project the caller owns, named for the folder, whose Files are
 * the folder's files, imported from the source with the caller's own
 * connection.
 *
 * Built only from routes that exist: `POST /projects`, then the project's
 * Drive import, in batches. On its own it is a copy. With a sync interval
 * (folder sync, spec §5.3) it then creates one `drive_folder` schedule on the
 * project, which keeps its Files in step with the folder: the first run adopts
 * the copies just imported rather than fetching them again.
 */
@Injectable({ providedIn: 'root' })
export class SaveFolderAsProjectService {
  private readonly projects = inject(ProjectsService);
  private readonly fileSources = inject(FileSourceService);
  private readonly syncPolicies = inject(SyncPolicyService);

  /**
   * Create the project, import the folder's files into it, then, when asked,
   * schedule the folder's sync.
   *
   * A failed create throws and leaves nothing behind. A failed import or
   * schedule does not throw: the project exists by then, so the result says
   * how many files went in, whether it is kept in sync, and why not, and the
   * caller can still open the project. A schedule is created even after a
   * partial import: its first run imports what the copy missed.
   */
  async save(
    row: FolderAttachment,
    name: string,
    description: string,
    options: SaveFolderAsProjectOptions = {},
  ): Promise<SaveFolderAsProjectResult> {
    const files = importableFolderFiles(row);
    const project = await this.projects.create(name, description);
    let imported = 0;
    let error: string | null = null;
    for (let start = 0; start < files.length; start += FOLDER_IMPORT_BATCH_SIZE) {
      const batch = files.slice(start, start + FOLDER_IMPORT_BATCH_SIZE);
      try {
        await this.fileSources.importDocuments({ projectId: project.projectId }, row.connectorId, batch);
        imported += batch.length;
      } catch (err) {
        error = err instanceof Error ? err.message : 'The files could not be added to the project.';
        break;
      }
    }

    let syncPolicy: SyncPolicy | null = null;
    let syncError: string | null = null;
    if (options.syncInterval) {
      try {
        syncPolicy = await this.syncPolicies.createPolicy(
          { projectId: project.projectId },
          {
            sourceType: 'drive_folder',
            sourceRef: folderSyncSourceRef(row),
            interval: options.syncInterval,
            sourceName: row.folderPath || row.folderName,
          },
        );
      } catch (err) {
        syncError = err instanceof Error ? err.message : 'The folder could not be scheduled to sync.';
      }
    }

    return { project, imported, notImported: files.length - imported, error, syncPolicy, syncError };
  }
}

/** The project's description: where its files came from. */
export function folderProjectDescription(row: FolderAttachment): string {
  const where = row.folderPath || row.folderName;
  const source = row.connectorName ? ` in ${row.connectorName}` : '';
  return `Files from the folder “${where}”${source}.`;
}
