import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowTopRightOnSquare,
  heroPlus,
  heroSparkles,
} from '@ng-icons/heroicons/outline';
import { heroCheckCircleSolid } from '@ng-icons/heroicons/solid';
import { ManagedModelsService } from './services/managed-models.service';
import { CuratedModelPrefillService } from './services/curated-model-prefill.service';
import { ModelProvider } from './models/managed-model.model';
import {
  CURATED_MODELS_BY_PROVIDER,
  CuratedModel,
} from './models/curated-models';
import {
  AddCuratedModelDialogComponent,
  AddCuratedModelDialogData,
  AddCuratedModelDialogResult,
} from './components/add-curated-model-dialog.component';

interface ProviderTab {
  id: ModelProvider;
  label: string;
}

const PROVIDER_TABS: ProviderTab[] = [
  { id: 'bedrock', label: 'Bedrock' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'gemini', label: 'Gemini' },
  { id: 'mantle', label: 'Bedrock Mantle' },
];

const PROVIDER_LOGO_DIR: Record<string, string> = {
  Anthropic: 'anthropic',
  Amazon: 'amazon',
  Meta: 'meta',
  OpenAI: 'openai',
};

@Component({
  selector: 'app-model-catalog-page',
  imports: [RouterLink, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroArrowTopRightOnSquare,
      heroPlus,
      heroSparkles,
      heroCheckCircleSolid,
    }),
  ],
  templateUrl: './model-catalog.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ModelCatalogPage {
  private managedModelsService = inject(ManagedModelsService);
  private prefillService = inject(CuratedModelPrefillService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  readonly tabs = PROVIDER_TABS;
  readonly activeTab = signal<ModelProvider>('bedrock');

  /** Curated entries for the active provider; empty for OpenAI/Gemini today. */
  readonly visibleModels = computed<CuratedModel[]>(
    () => CURATED_MODELS_BY_PROVIDER[this.activeTab()] ?? [],
  );

  /** Which curated key is currently being POSTed, if any. */
  readonly addingKey = signal<string | null>(null);

  /** Per-card error message keyed by curated key. */
  readonly errors = signal<Record<string, string>>({});

  selectTab(provider: ModelProvider): void {
    this.activeTab.set(provider);
  }

  isAlreadyAdded(modelId: string): boolean {
    return this.managedModelsService.isModelAdded(modelId);
  }

  isAdding(key: string): boolean {
    return this.addingKey() === key;
  }

  errorFor(key: string): string | null {
    return this.errors()[key] ?? null;
  }

  logoDirFor(providerName: string): string | null {
    return PROVIDER_LOGO_DIR[providerName] ?? null;
  }

  /**
   * Hand the curated template to the model form for review/customization.
   * The form consumes it once on init via CuratedModelPrefillService.
   */
  previewCuratedModel(model: CuratedModel): void {
    if (this.addingKey() !== null) return;
    this.prefillService.set(model.template);
    this.router.navigate(['/admin/manage-models/new']);
  }

  /**
   * Open the role-picker dialog for `model` and, if confirmed, POST the
   * curated template with the admin's role selection applied. Cancel is a
   * no-op. Errors stay inline on the card so the admin can retry a different
   * entry without losing context.
   *
   * The curated template ships with an empty `allowedAppRoles` because role
   * IDs vary per deployment — gathering them here keeps newly-added models
   * from being silently invisible to users.
   */
  async addCuratedModel(model: CuratedModel): Promise<void> {
    if (this.addingKey() !== null) return;
    if (this.isAlreadyAdded(model.template.modelId)) return;

    const dialogRef = this.dialog.open<AddCuratedModelDialogResult>(
      AddCuratedModelDialogComponent,
      { data: { model } as AddCuratedModelDialogData },
    );

    const roleIds = await firstValueFrom(dialogRef.closed);
    if (!roleIds) return; // cancelled

    this.errors.update(prev => {
      const next = { ...prev };
      delete next[model.key];
      return next;
    });
    this.addingKey.set(model.key);

    try {
      await this.managedModelsService.createModel({
        ...model.template,
        allowedAppRoles: roleIds,
      });
      this.router.navigate(['/admin/manage-models']);
    } catch (err: unknown) {
      const message = this.extractErrorMessage(err);
      this.errors.update(prev => ({ ...prev, [model.key]: message }));
    } finally {
      this.addingKey.set(null);
    }
  }

  private extractErrorMessage(err: unknown): string {
    if (typeof err === 'object' && err !== null) {
      const httpErr = err as { error?: { detail?: unknown }; message?: unknown };
      const detail = httpErr.error?.detail;
      if (typeof detail === 'string' && detail.length > 0) return detail;
      if (typeof httpErr.message === 'string' && httpErr.message.length > 0) return httpErr.message;
    }
    return 'Failed to add model. Please try again.';
  }
}
