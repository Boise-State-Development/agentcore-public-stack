import { ChangeDetectionStrategy, Component, OnInit, inject, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../../components/confirmation-dialog/confirmation-dialog.component';
import {
  heroBuildingLibrary,
  heroCheckBadge,
  heroPlus,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import {
  PUBLISHER_KIND_HINTS,
  PUBLISHER_KIND_LABELS,
  PublisherKind,
  PublisherProfile,
} from '../models/marketplace.model';

/**
 * Publishers — the display identity a listing is attributed to (D12).
 *
 * The backend for this has been complete since the marketplace shipped: full CRUD plus an
 * eligibility allowlist at `/admin/agents/publishers`. What was missing was any way to
 * reach it, so a profile like "Registrar" or "Communications & Marketing" could only be
 * created by calling the API directly. This page is that gap, and nothing more.
 *
 * Three things the UI has to make legible, because each is surprising otherwise:
 *
 * - **⚠️ Publisher is display only.** It is a name on a shelf. It never grants access to
 *   anything, and `ownerName` on a listing remains who actually owns the agent and whose
 *   skills resolve when it runs. The page says so where an admin is most likely to assume
 *   otherwise — next to `verified`, which looks like a permission and is not.
 * - **The id is fixed at creation.** Listings store the id, so renaming a publisher would
 *   strand every attribution pointing at it. Renaming changes the label only — the same
 *   rule categories follow.
 * - **Disable, don't delete.** Deleting is refused (409) while listings are attributed to
 *   the publisher. Disabling drops it from the submit picker while existing attributions
 *   keep rendering, which is nearly always what was meant.
 */
@Component({
  selector: 'app-marketplace-publishers',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
  providers: [
    provideIcons({ heroBuildingLibrary, heroCheckBadge, heroPlus, heroTrash }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Publishers</h1>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            Who a listing is attributed to in Discover — the Registrar, Communications &
            Marketing, the university itself. An agent built by one person can still speak
            as the office that owns it.
          </p>
          <p class="mt-2 max-w-2xl text-sm/6 text-gray-500 dark:text-gray-400">
            Attribution is a name, not a permission. It never changes who can open an agent
            or whose skills run when it does.
          </p>
        </div>

        @if (error()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ error() }}
          </div>
        }

        <!-- Add -->
        <div
          class="mb-6 flex flex-col gap-2 rounded-2xl border border-gray-200 bg-white p-4 sm:flex-row sm:items-end dark:border-gray-700 dark:bg-gray-800"
        >
          <div class="flex-1">
            <label
              for="new-publisher"
              class="block text-sm/6 font-medium text-gray-900 dark:text-white"
            >
              New publisher
            </label>
            <input
              type="text"
              id="new-publisher"
              [value]="newLabel()"
              (input)="onNewLabelInput($event)"
              placeholder="e.g. Office of the Registrar"
              class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
            />
          </div>
          <div class="sm:w-52">
            <label
              for="new-publisher-kind"
              class="block text-sm/6 font-medium text-gray-900 dark:text-white"
            >
              Kind
            </label>
            <select
              id="new-publisher-kind"
              [value]="newKind()"
              (change)="onNewKindChange($event)"
              class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
            >
              @for (kind of kinds; track kind) {
                <option [value]="kind">{{ kindLabels[kind] }}</option>
              }
            </select>
          </div>
          <button
            type="button"
            [disabled]="!newLabel().trim() || busy()"
            (click)="addPublisher()"
            class="inline-flex shrink-0 items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
            Add
          </button>
        </div>
        <p class="-mt-4 mb-6 text-sm/6 text-gray-500 dark:text-gray-400">
          {{ kindHints[newKind()] }}
        </p>

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading publishers</span>
          </div>
        } @else if (publishers().length === 0) {
          <div
            class="rounded-2xl border border-dashed border-gray-300 px-6 py-16 text-center dark:border-gray-600"
          >
            <ng-icon
              name="heroBuildingLibrary"
              class="mx-auto size-8 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <h2 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
              No publishers yet
            </h2>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Authors get an individual profile automatically on their first submission.
              Add one here for an office or the university.
            </p>
          </div>
        } @else {
          <ul class="flex flex-col gap-2">
            @for (publisher of publishers(); track publisher.id) {
              <li
                class="flex flex-col gap-3 rounded-2xl border border-gray-200 bg-white p-4 sm:flex-row sm:items-center dark:border-gray-700 dark:bg-gray-800"
              >
                <div class="min-w-0 flex-1">
                  <label [for]="'label-' + publisher.id" class="sr-only">
                    Label for {{ publisher.label }}
                  </label>
                  <div class="flex items-center gap-1.5">
                    <input
                      type="text"
                      [id]="'label-' + publisher.id"
                      [value]="publisher.label"
                      (change)="rename(publisher, $event)"
                      class="block w-full rounded-2xl border border-transparent bg-transparent px-2 py-1 text-sm/6 font-medium text-gray-900 hover:border-gray-300 focus:border-blue-500 focus:bg-white focus:outline-none focus:ring-2 focus:ring-blue-500 dark:text-white dark:hover:border-gray-600 dark:focus:bg-gray-900"
                    />
                    @if (publisher.verified) {
                      <ng-icon
                        name="heroCheckBadge"
                        class="size-5 shrink-0 text-blue-600 dark:text-blue-400"
                        aria-label="Verified"
                      />
                    }
                  </div>
                  <p class="px-2 text-xs text-gray-400 dark:text-gray-500">
                    {{ kindLabels[publisher.kind] }} · id: {{ publisher.id }} — fixed at
                    creation
                  </p>
                </div>

                <div class="flex shrink-0 flex-wrap items-center gap-2">
                  <button
                    type="button"
                    [disabled]="busy()"
                    (click)="toggleVerified(publisher)"
                    class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    appTooltip="A check mark meaning a university team stands behind this. Display only — it grants nothing."
                    appTooltipPosition="top"
                  >
                    {{ publisher.verified ? 'Verified' : 'Unverified' }}
                  </button>
                  <button
                    type="button"
                    [disabled]="busy()"
                    (click)="toggleEnabled(publisher)"
                    class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    [appTooltip]="
                      publisher.enabled
                        ? 'Stop offering this in the submit picker. Listings already attributed to it keep rendering.'
                        : 'Offer this publisher again'
                    "
                    appTooltipPosition="top"
                  >
                    {{ publisher.enabled ? 'Enabled' : 'Disabled' }}
                  </button>
                  <button
                    type="button"
                    [disabled]="busy()"
                    (click)="remove(publisher)"
                    class="rounded-2xl border border-gray-300 bg-white p-1.5 text-gray-500 hover:bg-rose-50 hover:text-rose-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:hover:bg-rose-900/20 dark:hover:text-rose-400"
                    appTooltip="Delete. Refused while listings are attributed to it — disable instead."
                    appTooltipPosition="top"
                  >
                    <ng-icon name="heroTrash" class="size-5" aria-hidden="true" />
                    <span class="sr-only">Delete {{ publisher.label }}</span>
                  </button>
                </div>
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class MarketplacePublishersPage implements OnInit {
  private readonly service = inject(AdminMarketplaceService);
  private readonly dialog = inject(Dialog);

  readonly publishers = signal<PublisherProfile[]>([]);
  readonly loading = signal(true);
  readonly error = signal<string | null>(null);
  readonly busy = signal(false);
  readonly newLabel = signal('');
  readonly newKind = signal<PublisherKind>('department');

  /** `department` first: it is what an admin adding a publisher almost always wants. */
  protected readonly kinds: PublisherKind[] = ['department', 'institution', 'individual'];
  protected readonly kindLabels = PUBLISHER_KIND_LABELS;
  protected readonly kindHints = PUBLISHER_KIND_HINTS;

  ngOnInit(): void {
    void this.reload();
  }

  onNewLabelInput(event: Event): void {
    this.newLabel.set((event.target as HTMLInputElement).value);
  }

  onNewKindChange(event: Event): void {
    this.newKind.set((event.target as HTMLSelectElement).value as PublisherKind);
  }

  async reload(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.publishers.set(await this.service.loadPublishers());
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to load publishers.'));
    } finally {
      this.loading.set(false);
    }
  }

  async addPublisher(): Promise<void> {
    const label = this.newLabel().trim();
    if (!label) return;
    await this.mutate(async () => {
      await this.service.createPublisher({
        label,
        kind: this.newKind(),
        order: this.publishers().length * 10,
      });
      this.newLabel.set('');
    });
  }

  async rename(publisher: PublisherProfile, event: Event): Promise<void> {
    const label = (event.target as HTMLInputElement).value.trim();
    if (!label || label === publisher.label) return;
    await this.mutate(() => this.service.updatePublisher(publisher.id, { label }));
  }

  async toggleVerified(publisher: PublisherProfile): Promise<void> {
    await this.mutate(() =>
      this.service.updatePublisher(publisher.id, { verified: !publisher.verified }),
    );
  }

  async toggleEnabled(publisher: PublisherProfile): Promise<void> {
    await this.mutate(() =>
      this.service.updatePublisher(publisher.id, { enabled: !publisher.enabled }),
    );
  }

  /**
   * Delete a publisher profile, behind a confirmation.
   *
   * Confirmed because it is irreversible and the id is not recoverable: the id is fixed at
   * creation, so a profile deleted by mistake cannot be recreated as the same one, and
   * every listing that named it stays unattributed. Every other control on this page is a
   * toggle or a rename that can be undone by doing it again; this one cannot.
   *
   * The server refuses (409) while listings are still attributed, and that message names
   * disabling as the alternative — so the dialog does not repeat it here.
   */
  async remove(publisher: PublisherProfile): Promise<void> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete this publisher?',
        message:
          `"${publisher.label}" is removed for good — its id is fixed at creation, so it ` +
          'cannot be recreated as the same profile. Listings can only be deleted when ' +
          'nothing is attributed to them; if you want it out of the submit picker while ' +
          'existing credits keep rendering, disable it instead.',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true,
      } satisfies ConfirmationDialogData,
    });
    if (!(await firstValueFrom(ref.closed))) return;

    await this.mutate(() => this.service.deletePublisher(publisher.id));
  }

  /** Run a mutation, surface its message, and reload so the list matches the server. */
  private async mutate(action: () => Promise<unknown>): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      await action();
      await this.reload();
    } catch (err) {
      // The in-use delete refusal (409) explains itself and suggests disabling instead, so
      // surface the server's message rather than replacing it with a generic one.
      //
      // ⚠️ Set *after* the reload, not before: ``reload`` clears the error banner on entry,
      // so the obvious order silently swallows every message this branch exists to show.
      // ``categories.page.ts`` had exactly that bug until this page's tests found it.
      const message = this.messageFor(err, 'That change could not be saved.');
      await this.reload();
      this.error.set(message);
    } finally {
      this.busy.set(false);
    }
  }

  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : fallback;
  }
}
