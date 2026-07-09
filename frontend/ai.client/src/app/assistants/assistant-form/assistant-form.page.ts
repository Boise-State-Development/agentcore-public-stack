import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
  OnDestroy,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  ReactiveFormsModule,
  FormBuilder,
  FormGroup,
  FormArray,
  FormControl,
  Validators,
} from '@angular/forms';
import { Subscription } from 'rxjs';
import { AssistantService } from '../services/assistant.service';
import { AssistantPreviewComponent } from './components/assistant-preview.component';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroChevronRight,
  heroFaceSmile,
  heroXMark,
  heroUser,
  heroUserGroup,
  heroPlus,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { Dialog } from '@angular/cdk/dialog';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { PickerComponent } from '@ctrl/ngx-emoji-mart';
import { CdkConnectedOverlay, CdkOverlayOrigin, ConnectedPosition } from '@angular/cdk/overlay';
import { ThemeService } from '../../components/topnav/components/theme-toggle/theme.service';
import {
  ShareAssistantDialogComponent,
  ShareAssistantDialogData,
} from '../components/share-assistant-dialog.component';
import { KnowledgeBaseSectionComponent } from '../../knowledge-base/knowledge-base-section.component';

@Component({
  selector: 'app-assistant-form-page',
  templateUrl: './assistant-form.page.html',
  styleUrl: './assistant-form.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    ReactiveFormsModule,
    AssistantPreviewComponent,
    NgIcon,
    RouterLink,
    PickerComponent,
    CdkOverlayOrigin,
    CdkConnectedOverlay,
    KnowledgeBaseSectionComponent,
  ],
  providers: [
    provideIcons({
      heroChevronRight,
      heroFaceSmile,
      heroXMark,
      heroUser,
      heroUserGroup,
      heroPlus,
      heroTrash,
    }),
  ],
})
export class AssistantFormPage implements OnInit, OnDestroy {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private fb = inject(FormBuilder);
  private assistantService = inject(AssistantService);
  readonly sidenavService = inject(SidenavService);
  private readonly themeService = inject(ThemeService);
  private readonly dialog = inject(Dialog);

  // Emoji picker popover state
  readonly isEmojiPickerOpen = signal(false);

  // Expose theme for emoji picker dark mode
  readonly isDarkMode = this.themeService.theme;

  readonly assistantId = signal<string | null>(null);
  readonly mode = computed<'create' | 'edit'>(() => (this.assistantId() ? 'edit' : 'create'));
  /** The requesting user's permission on the loaded assistant — populated by loadAssistant.
   *  In create mode the user is implicitly the owner, so we seed it that way. */
  readonly userPermission = signal<'owner' | 'editor' | 'viewer'>('owner');
  /**
   * Whether {@link userPermission} reflects a value loaded from the server.
   * The knowledge-base section waits on this before issuing its edit-gated
   * sync-policy calls (a viewer would 403 on the default 'owner' guess). Create
   * mode has no record to load, so the user is implicitly the resolved owner.
   */
  readonly permissionResolved = signal(false);
  /** Owner display name surfaced on the editor banner when the requester is an editor. */
  readonly ownerName = signal<string>('');
  readonly canManageShares = computed(() => this.userPermission() === 'owner');
  readonly isEditorView = computed(() => this.userPermission() === 'editor');

  // Live form value signals — kept in sync via form.valueChanges so the
  // preview component (OnPush) receives updates as the user types.
  readonly liveFormName = signal('');
  readonly liveFormDescription = signal('');
  readonly liveFormInstructions = signal('');
  readonly liveFormEmoji = signal('');
  readonly liveFormStarters = signal<string[]>([]);

  private formSub?: Subscription;

  form!: FormGroup;

  // Emoji picker positioning - opens below and to the right
  readonly emojiPickerPositions: ConnectedPosition[] = [
    {
      originX: 'start',
      originY: 'bottom',
      overlayX: 'start',
      overlayY: 'top',
      offsetY: 8,
    },
    {
      originX: 'start',
      originY: 'top',
      overlayX: 'start',
      overlayY: 'bottom',
      offsetY: -8,
    },
  ];

  get starters(): FormArray {
    return this.form.get('starters') as FormArray;
  }

  ngOnInit(): void {
    // Hide sidenav when entering the form page
    this.sidenavService.hide();

    // Check if we're editing an existing assistant
    const id = this.route.snapshot.paramMap.get('id');
    this.assistantId.set(id);

    // Initialize the form with all required fields
    this.form = this.fb.group({
      name: ['', [Validators.required, Validators.minLength(3)]],
      description: ['', [Validators.required, Validators.minLength(10)]],
      instructions: ['', [Validators.required, Validators.minLength(20)]],
      vectorIndexId: ['idx_assistants', [Validators.required]],
      visibility: ['PRIVATE'],
      tags: [[]],
      starters: this.fb.array([]),
      emoji: [''],
      status: ['DRAFT'],
    });

    if (id) {
      // Edit mode: load the assistant, then mark the permission resolved so the
      // knowledge-base section can safely issue its edit-gated sync calls.
      void this.loadAssistant(id).finally(() => this.permissionResolved.set(true));
    } else {
      // Create mode: the user is implicitly the owner — no record to resolve.
      this.permissionResolved.set(true);
    }

    // Sync form changes into signals so the preview (OnPush) updates live
    this.syncFormToSignals();
    this.formSub = this.form.valueChanges.subscribe(() => this.syncFormToSignals());
  }

  /** Push current form values into the live signals */
  private syncFormToSignals(): void {
    this.liveFormName.set(this.form.get('name')?.value || '');
    this.liveFormDescription.set(this.form.get('description')?.value || '');
    this.liveFormInstructions.set(this.form.get('instructions')?.value || '');
    this.liveFormEmoji.set(this.form.get('emoji')?.value || '');
    this.liveFormStarters.set(this.starters.value || []);
  }

  ngOnDestroy(): void {
    // Show sidenav when leaving the form page
    this.sidenavService.show();
    this.formSub?.unsubscribe();
  }

  async loadAssistant(id: string): Promise<void> {
    try {
      // First check local cache
      let assistant = this.assistantService.getAssistantById(id);

      // If not in cache, fetch from API
      if (!assistant) {
        const response = await this.assistantService.getAssistant(id);
        assistant = response;
      }

      if (assistant) {
        this.form.patchValue({
          name: assistant.name,
          description: assistant.description,
          instructions: assistant.instructions,
          vectorIndexId: assistant.vectorIndexId,
          visibility: assistant.visibility,
          tags: assistant.tags,
          emoji: assistant.emoji || '',
          status: assistant.status,
        });

        // Cached assistants from the list view do not carry userPermission
        // (the list synthesises it locally) — fall back to 'owner' for cache hits
        // so the owner's editor experience stays identical.
        this.userPermission.set(assistant.userPermission ?? 'owner');
        this.ownerName.set(assistant.ownerName ?? '');

        // Populate starters FormArray
        this.starters.clear();
        if (assistant.starters && assistant.starters.length > 0) {
          assistant.starters.forEach((starter) => {
            this.starters.push(new FormControl(starter, Validators.required));
          });
        }
      }
    } catch (error) {
      console.error('Error loading assistant:', error);
      // TODO: Show error message to user
    }
  }

  async onSubmit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }

    const formData = this.form.value;

    try {
      if (this.mode() === 'create') {
        // For create mode, we don't have an ID yet
        // Use createAssistant which will generate one
        await this.assistantService.createAssistant(formData);
      } else {
        // For edit mode, update the existing assistant
        // Set status to COMPLETE when saving from draft
        const updateData = {
          ...formData,
          status: 'COMPLETE' as const,
        };
        await this.assistantService.updateAssistant(this.assistantId()!, updateData);
      }

      // Navigate back to assistants list
      this.router.navigate(['/assistants']);
    } catch (error) {
      console.error('Error saving assistant:', error);
      // TODO: Show error message to user
    }
  }

  onCancel(): void {
    this.router.navigate(['/assistants']);
  }

  addStarter(): void {
    this.starters.push(new FormControl('', Validators.required));
  }

  removeStarter(index: number): void {
    this.starters.removeAt(index);
  }

  getFieldError(fieldName: string): string | null {
    const field = this.form.get(fieldName);
    if (!field || !field.touched || !field.errors) {
      return null;
    }

    if (field.errors['required']) {
      return 'This field is required';
    }
    if (field.errors['minlength']) {
      const minLength = field.errors['minlength'].requiredLength;
      return `Minimum length is ${minLength} characters`;
    }

    return null;
  }

  toggleEmojiPicker(): void {
    this.isEmojiPickerOpen.update((open) => !open);
  }

  closeEmojiPicker(): void {
    this.isEmojiPickerOpen.set(false);
  }

  onEmojiSelect(event: { emoji: { native: string } }): void {
    this.form.patchValue({ emoji: event.emoji.native });
    this.closeEmojiPicker();
  }

  clearEmoji(): void {
    this.form.patchValue({ emoji: '' });
  }

  openShareDialog(): void {
    const assistantId = this.assistantId();
    if (!assistantId) return;

    // Build a minimal assistant object from the current form state
    const assistant = {
      assistantId,
      name: this.form.get('name')?.value || '',
      visibility: this.form.get('visibility')?.value || 'PRIVATE',
    } as import('../models/assistant.model').Assistant;

    this.dialog.open<unknown, ShareAssistantDialogData>(ShareAssistantDialogComponent, {
      data: { assistant },
      hasBackdrop: false,
    });
  }

  /**
   * Ensure an assistant record exists so documents have a parent to attach to.
   * Passed to the knowledge-base section as its create-draft callback: in
   * create mode the first content-adding action mints a draft and the form is
   * patched with its server-assigned fields. Returns the assistant id; throws
   * if draft creation fails.
   */
  readonly createDraftAssistant = async (): Promise<string> => {
    const draft = await this.assistantService.createDraft({
      name: this.form.get('name')?.value || 'Untitled Assistant',
    });
    this.assistantId.set(draft.assistantId);
    this.form.patchValue({
      name: draft.name,
      description: draft.description || '',
      instructions: draft.instructions || '',
      vectorIndexId: draft.vectorIndexId,
      visibility: draft.visibility,
      tags: draft.tags,
      status: draft.status,
    });
    return draft.assistantId;
  };

  getStatusBadgeClasses(): string {
    const status = this.form?.get('status')?.value || 'DRAFT';
    const baseClasses = 'inline-flex items-center rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';

    switch (status) {
      case 'COMPLETE':
        return `${baseClasses} bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300`;
      case 'DRAFT':
        return `${baseClasses} bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300`;
      default:
        return `${baseClasses} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  }
}
