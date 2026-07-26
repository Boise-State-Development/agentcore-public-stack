import {
  Component,
  ChangeDetectionStrategy,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDown,
  heroArrowUp,
  heroBookmark,
  heroLockClosed,
  heroLockOpen,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { AppRole } from '../../roles/models/app-role.model';
import { AdminMarketplaceService } from '../services/admin-marketplace.service';
import {
  AdminListingRow,
  LOCK_WARN_THRESHOLD,
  MAX_ROLE_PINS,
  RoleAgentPinRow,
} from '../models/marketplace.model';
import { AgentTileComponent } from '../components/agent-tile.component';
import {
  RolePinSaveDialogComponent,
  RolePinSaveDialogResult,
} from '../components/role-pin-save-dialog.component';

/** A staged row: the resolved server row, plus the lock the admin is editing. */
interface StagedPin {
  row: RoleAgentPinRow | null;
  agentId: string;
  name: string;
  tagline?: string;
  emoji?: string;
  iconUrl?: string;
  locked: boolean;
}

/**
 * Default pins by role — the seventh of D10's admin surfaces (D9).
 *
 * A role's members start with a useful sidebar instead of an empty one. Three things this
 * page has to keep saying out loud, because each is a way an admin ends up doing something
 * other than what they intended:
 *
 * 1. **Removal is not "new members only."** Pins resolve live, so removing one unpins for
 *    everyone in the role who has not pinned it themselves. The mockup's "apply to new
 *    members only" option is unrepresentable and was dropped deliberately — the save
 *    dialog carries the real consequence.
 * 2. **⚠️ `default` is a substitute, not a baseline.** It is consulted only for users who
 *    matched *zero* roles. An admin who reads that chip as "everyone" seeds nobody, which
 *    is why the banner says so in the role's own words rather than in a tooltip.
 * 3. **A seed can be inert for two different reasons.** The agent may be unreachable
 *    (PRIVATE — nobody but its owner resolves it) or unrunnable (the role does not grant
 *    its model or a bound tool, D9.5). They have different fixes and different owners, so
 *    the row names which one it is rather than showing one generic warning.
 *
 * The editor is **staged**, like the store front: order and locks change locally and Save
 * writes the whole list in one PUT. Reordering has to be atomic, and per-row autosave
 * would make every mis-click a live change to somebody else's sidebar.
 */
@Component({
  selector: 'app-marketplace-default-pins',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, AgentTileComponent, TooltipDirective],
  providers: [
    provideIcons({
      heroArrowDown,
      heroArrowUp,
      heroBookmark,
      heroLockClosed,
      heroLockOpen,
      heroXMark,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <div class="mb-6">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Default pins</h1>
          <p class="mt-1 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            Agents every member of a role starts with. Pins resolve live — there is no
            copy per person — so adding or removing one takes effect on the next page load.
            Up to {{ maxPins }} per role.
          </p>
        </div>

        <!-- Role picker -->
        <div class="mb-6">
          <label
            for="role-picker"
            class="block text-sm/6 font-medium text-gray-900 dark:text-white"
          >
            Role
          </label>
          <select
            id="role-picker"
            [value]="roleId()"
            (change)="onRoleChange($event)"
            class="mt-2 block w-full max-w-sm rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
          >
            @for (role of roles(); track role.roleId) {
              <option [value]="role.roleId">
                {{ role.displayName || role.roleId }}
              </option>
            }
          </select>
        </div>

        @if (error()) {
          <div
            role="alert"
            class="mb-4 rounded-2xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm/6 text-rose-800 dark:border-rose-900 dark:bg-rose-900/20 dark:text-rose-300"
          >
            {{ error() }}
          </div>
        }

        <!-- ⚠️ D9.6: the two ways a seed list reaches nobody. -->
        @if (fallbackOnly()) {
          <div
            role="status"
            class="mb-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm/6 text-amber-900 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-200"
          >
            <span class="font-semibold">Fallback only.</span>
            The <code class="font-mono">default</code> role applies to users who match no
            other role — it is never merged alongside one. Pins seeded here do not reach
            anyone who holds faculty, staff, student or any other role. To reach everyone,
            seed each role.
          </div>
        } @else if (unmapped()) {
          <div
            role="status"
            class="mb-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm/6 text-amber-900 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-200"
          >
            <span class="font-semibold">No members.</span>
            This role has no identity-provider mappings, so nobody currently matches it.
            Pins seeded here will apply once a mapping is added.
          </div>
        }

        <!--
          #748 — locking friction. Two separate facts, and the second is the one an admin
          cannot work out for themselves: their own locked count, and what every other
          role already locks. A member's shelf is the union across the roles they match.
        -->
        @if (lockWarning() || lockedElsewhere()) {
          <div
            role="status"
            class="mb-4 rounded-2xl border px-4 py-3 text-sm/6"
            [class]="
              lockWarning()
                ? 'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-200'
                : 'border-gray-200 bg-gray-50 text-gray-700 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300'
            "
          >
            @if (lockWarning()) {
              <span class="font-semibold">
                {{ lockedCount() }} of {{ staged().length }} seeds are locked.
              </span>
              Members cannot remove a locked agent — it is in their sidebar for good. Lock
              the ones that genuinely must be there and leave the rest removable, or Pinned
              stops being the user's own shelf.
            }
            @if (lockedElsewhere()) {
              <span [class]="lockWarning() ? 'mt-2 block' : ''">
                {{ lockedElsewhereRoles() }}
                other {{ lockedElsewhereRoles() === 1 ? 'role locks' : 'roles lock' }}
                {{ lockedElsewhere() }} more.
                A member who matches several roles gets the union of all of them, and a
                lock from any one role wins — so the shelf an individual ends up with can
                be larger than any single role's list.
              </span>
            }
          </div>
        }

        @if (unavailable().length) {
          <div
            role="status"
            class="mb-4 rounded-2xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm/6 text-amber-900 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-200"
          >
            {{ unavailable().length }}
            seeded
            {{ unavailable().length === 1 ? 'agent no longer exists' : 'agents no longer exist' }}.
            Saving this list clears
            {{ unavailable().length === 1 ? 'it' : 'them' }}.
          </div>
        }

        @if (loading()) {
          <div class="flex items-center justify-center py-16">
            <div
              class="size-8 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"
            ></div>
            <span class="sr-only">Loading default pins</span>
          </div>
        } @else {
          <!-- The seed list -->
          <section class="mb-8">
            <div class="mb-3 flex items-center justify-between gap-4">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                Seeded for {{ roleLabel() }} ({{ staged().length }}/{{ maxPins }})
                @if (lockedCount()) {
                  <span class="ml-2 text-sm/6 font-normal text-gray-500 dark:text-gray-400">
                    · {{ lockedCount() }} locked
                  </span>
                }
              </h2>
              <div class="flex items-center gap-2">
                @if (dirty()) {
                  <button
                    type="button"
                    (click)="reloadPins()"
                    [disabled]="busy()"
                    class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                  >
                    Discard
                  </button>
                }
                <button
                  type="button"
                  (click)="save()"
                  [disabled]="!dirty() || busy()"
                  class="rounded-2xl bg-blue-600 px-4 py-1.5 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  {{ dirty() ? 'Save pins' : 'Saved' }}
                </button>
              </div>
            </div>

            @if (staged().length === 0) {
              <div
                class="rounded-2xl border border-dashed border-gray-300 px-6 py-12 text-center dark:border-gray-600"
              >
                <ng-icon
                  name="heroBookmark"
                  class="mx-auto size-8 text-gray-400 dark:text-gray-500"
                  aria-hidden="true"
                />
                <h3 class="mt-3 text-sm/6 font-semibold text-gray-900 dark:text-white">
                  Nothing seeded
                </h3>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  Members of this role start with an empty Pinned tab.
                </p>
              </div>
            } @else {
              <ol class="flex flex-col gap-2">
                @for (pin of staged(); track pin.agentId; let i = $index) {
                  <li
                    class="rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800"
                  >
                    <div class="flex items-center gap-3">
                      <span
                        class="w-6 shrink-0 text-center text-sm/6 font-semibold tabular-nums text-gray-400 dark:text-gray-500"
                        aria-hidden="true"
                      >
                        {{ i + 1 }}
                      </span>
                      <app-agent-tile
                        [agentId]="pin.agentId"
                        [iconUrl]="pin.iconUrl"
                        [emoji]="pin.emoji"
                        size="sm"
                      />
                      <div class="min-w-0 flex-1">
                        <p
                          class="truncate text-sm/6 font-medium text-gray-900 dark:text-white"
                        >
                          {{ pin.name }}
                        </p>
                        <p class="truncate text-xs text-gray-500 dark:text-gray-400">
                          {{ pin.tagline }}
                        </p>
                      </div>
                      <div class="flex shrink-0 items-center gap-1">
                        <button
                          type="button"
                          (click)="toggleLock(pin.agentId)"
                          [disabled]="busy()"
                          [appTooltip]="
                            pin.locked
                              ? 'Locked — members cannot remove this. Click to unlock.'
                              : 'Members can remove this. Click to lock it in place.'
                          "
                          appTooltipPosition="top"
                          [attr.aria-pressed]="pin.locked"
                          class="rounded-2xl p-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
                          [class]="
                            pin.locked
                              ? 'text-amber-600 hover:bg-amber-50 dark:text-amber-400 dark:hover:bg-amber-900/20'
                              : 'text-gray-500 hover:bg-gray-100 hover:text-gray-900 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white'
                          "
                        >
                          <ng-icon
                            [name]="pin.locked ? 'heroLockClosed' : 'heroLockOpen'"
                            class="size-5"
                            aria-hidden="true"
                          />
                          <span class="sr-only">
                            {{ pin.locked ? 'Unlock' : 'Lock' }} {{ pin.name }}
                          </span>
                        </button>
                        <button
                          type="button"
                          (click)="moveUp(i)"
                          [disabled]="i === 0 || busy()"
                          [appTooltip]="'Move up'"
                          appTooltipPosition="top"
                          class="rounded-2xl p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-30 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
                        >
                          <ng-icon name="heroArrowUp" class="size-5" aria-hidden="true" />
                          <span class="sr-only">Move {{ pin.name }} up</span>
                        </button>
                        <button
                          type="button"
                          (click)="moveDown(i)"
                          [disabled]="i === staged().length - 1 || busy()"
                          [appTooltip]="'Move down'"
                          appTooltipPosition="top"
                          class="rounded-2xl p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-30 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
                        >
                          <ng-icon name="heroArrowDown" class="size-5" aria-hidden="true" />
                          <span class="sr-only">Move {{ pin.name }} down</span>
                        </button>
                        <button
                          type="button"
                          (click)="remove(pin.agentId)"
                          [disabled]="busy()"
                          [appTooltip]="'Remove from this role'"
                          appTooltipPosition="top"
                          class="rounded-2xl p-2 text-gray-500 hover:bg-rose-50 hover:text-rose-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-rose-900/20 dark:hover:text-rose-400"
                        >
                          <ng-icon name="heroXMark" class="size-5" aria-hidden="true" />
                          <span class="sr-only">Remove {{ pin.name }}</span>
                        </button>
                      </div>
                    </div>

                    <!-- D9.5, and the reachability check beside it. Two different fixes. -->
                    @if (pin.row && !pin.row.reachable) {
                      <p
                        class="mt-2 ml-9 text-xs text-amber-700 dark:text-amber-300"
                        role="status"
                      >
                        Not visible to this role — it is
                        {{ pin.row.visibility === 'SHARED' ? 'shared with named people' : 'private to its owner' }},
                        so the pin resolves to nothing for members. Its author has to
                        publish it or make it public.
                      </p>
                    }
                    @if (pin.row && pin.row.missing.length) {
                      <p
                        class="mt-2 ml-9 text-xs text-rose-700 dark:text-rose-300"
                        role="status"
                      >
                        Won't run for this role — it does not grant
                        {{ missingLabels(pin.row) }}.
                      </p>
                    }
                    @for (note of pin.row?.notes ?? []; track note) {
                      <p class="mt-2 ml-9 text-xs text-gray-500 dark:text-gray-400">
                        {{ note }}
                      </p>
                    }
                  </li>
                }
              </ol>
            }
          </section>

          <!-- The candidates -->
          <section>
            <h2 class="mb-1 text-base/7 font-semibold text-gray-900 dark:text-white">
              Agents in the store
            </h2>
            <p class="mb-3 text-sm/6 text-gray-600 dark:text-gray-400">
              Everything that has been submitted to the store. Seeding an agent that is not
              published still works — it is a pin, not a shelf slot — but members can only
              open one they could reach on their own.
            </p>

            @if (candidates().length === 0) {
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Nothing left to add.
              </p>
            } @else {
              <ul class="flex flex-col gap-2">
                @for (row of candidates(); track row.agentId) {
                  <li
                    class="flex items-center gap-3 rounded-2xl border border-gray-200 bg-white p-3 dark:border-gray-700 dark:bg-gray-800"
                  >
                    <app-agent-tile
                      [agentId]="row.agentId"
                      [iconUrl]="row.iconUrl"
                      [emoji]="row.emoji"
                      size="sm"
                    />
                    <div class="min-w-0 flex-1">
                      <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ row.name }}
                      </p>
                      <p class="truncate text-xs text-gray-500 dark:text-gray-400">
                        {{ row.tagline || row.category }}
                      </p>
                    </div>
                    <button
                      type="button"
                      (click)="add(row)"
                      [disabled]="isFull() || busy()"
                      [appTooltip]="
                        isFull()
                          ? 'This role is at the limit — remove one first'
                          : 'Seed this agent to the role'
                      "
                      appTooltipPosition="top"
                      class="shrink-0 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
                    >
                      Add
                    </button>
                  </li>
                }
              </ul>
            }
          </section>
        }
      </div>
    </div>
  `,
})
export class MarketplaceDefaultPinsPage implements OnInit {
  private service = inject(AdminMarketplaceService);
  private rolesService = inject(AppRolesService);
  private dialog = inject(Dialog);

  readonly maxPins = MAX_ROLE_PINS;
  readonly lockWarnThreshold = LOCK_WARN_THRESHOLD;

  readonly roles = signal<AppRole[]>([]);
  readonly roleId = signal('');
  readonly roleLabel = signal('');
  readonly fallbackOnly = signal(false);
  readonly unmapped = signal(false);
  readonly unavailable = signal<string[]>([]);
  readonly lockedElsewhere = signal(0);
  readonly lockedElsewhereRoles = signal(0);
  readonly staged = signal<StagedPin[]>([]);
  readonly listings = signal<AdminListingRow[]>([]);
  readonly loading = signal(true);
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  /** The saved list, to compare against — staging is what makes the reorder atomic. */
  private saved = signal<string>('');

  /** The rows as the server last resolved them, so a removal can be named, not id'd. */
  private savedPins = signal<RoleAgentPinRow[]>([]);

  private readonly fingerprint = computed(() =>
    this.staged()
      .map((pin) => `${pin.agentId}:${pin.locked}`)
      .join(' '),
  );

  readonly dirty = computed(() => this.fingerprint() !== this.saved());

  readonly isFull = computed(() => this.staged().length >= MAX_ROLE_PINS);

  /**
   * Locked-seed friction (#748). There is deliberately **no cap** — see
   * `count_locked_outside` in the backend for why the union across a user's roles cannot
   * be bounded at write time. What the console can do is make the cost visible, because
   * an admin weighing "seed" against "seed locked" otherwise has no reason not to lock:
   * locking guarantees the rollout lands and the cost falls on someone else's sidebar.
   */
  readonly lockedCount = computed(() => this.staged().filter((pin) => pin.locked).length);

  readonly lockWarning = computed(() => this.lockedCount() > LOCK_WARN_THRESHOLD);

  readonly candidates = computed(() => {
    const already = new Set(this.staged().map((pin) => pin.agentId));
    return this.listings().filter((row) => !already.has(row.agentId));
  });

  ngOnInit(): void {
    void this.load();
  }

  private async load(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const [roleList, listings] = await Promise.all([
        this.rolesService.fetchRoles(),
        this.service.loadListings(),
      ]);
      // Highest priority first, matching how the shelf orders seeds from several roles.
      const roles = [...(roleList.roles ?? [])].sort(
        (a, b) => b.priority - a.priority || a.roleId.localeCompare(b.roleId),
      );
      this.roles.set(roles);
      this.listings.set(listings);
      if (roles.length && !this.roleId()) {
        this.roleId.set(roles[0].roleId);
      }
      await this.reloadPins();
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to load default pins.'));
    } finally {
      this.loading.set(false);
    }
  }

  async reloadPins(): Promise<void> {
    const roleId = this.roleId();
    if (!roleId) return;
    this.error.set(null);
    try {
      const response = await this.service.loadRolePins(roleId);
      this.roleLabel.set(response.roleLabel || roleId);
      this.fallbackOnly.set(response.fallbackOnly);
      this.unmapped.set(response.unmapped);
      this.unavailable.set(response.unavailable ?? []);
      this.lockedElsewhere.set(response.lockedElsewhere ?? 0);
      this.lockedElsewhereRoles.set(response.lockedElsewhereRoles ?? 0);
      this.savedPins.set(response.pins ?? []);
      this.staged.set(
        (response.pins ?? []).map((row) => ({
          row,
          agentId: row.agentId,
          name: row.name,
          tagline: row.tagline,
          emoji: row.emoji,
          iconUrl: row.iconUrl,
          locked: row.locked,
        })),
      );
      this.saved.set(this.fingerprint());
    } catch (err) {
      this.error.set(this.messageFor(err, 'Failed to load default pins.'));
    }
  }

  async onRoleChange(event: Event): Promise<void> {
    this.roleId.set((event.target as HTMLSelectElement).value);
    await this.reloadPins();
  }

  /**
   * Seed from the listings table. The staged row carries no `row`, so it renders without
   * warnings until Save round-trips the authoritative diff back — the alternative would
   * be a second copy of the D9.5 rules in the browser, which is exactly how a resource
   * ends up listed by one path and denied by another.
   */
  add(row: AdminListingRow): void {
    if (this.isFull()) return;
    this.staged.update((current) => [
      ...current,
      {
        row: null,
        agentId: row.agentId,
        name: row.name,
        tagline: row.tagline,
        emoji: row.emoji,
        iconUrl: row.iconUrl,
        locked: false,
      },
    ]);
  }

  remove(agentId: string): void {
    this.staged.update((current) => current.filter((pin) => pin.agentId !== agentId));
  }

  toggleLock(agentId: string): void {
    this.staged.update((current) =>
      current.map((pin) =>
        pin.agentId === agentId ? { ...pin, locked: !pin.locked } : pin,
      ),
    );
  }

  moveUp(index: number): void {
    this.swap(index, index - 1);
  }

  moveDown(index: number): void {
    this.swap(index, index + 1);
  }

  private swap(from: number, to: number): void {
    this.staged.update((current) => {
      if (to < 0 || to >= current.length) return current;
      const next = [...current];
      [next[from], next[to]] = [next[to], next[from]];
      return next;
    });
  }

  missingLabels(row: RoleAgentPinRow): string {
    return row.missing.map((item) => item.label).join(', ');
  }

  /**
   * Save the whole list.
   *
   * A save that *removes* seeds is confirmed first, because that is the one that reaches
   * other people: role pins resolve live, so removing one unpins for everyone in the role
   * who has not pinned it themselves (D9.1).
   */
  async save(): Promise<void> {
    const removed = this.removedNames();
    if (removed.length) {
      const ref = this.dialog.open<RolePinSaveDialogResult>(RolePinSaveDialogComponent, {
        data: { roleLabel: this.roleLabel(), removed },
      });
      const confirmed = await firstValueFrom(ref.closed);
      if (!confirmed) return;
    }

    this.busy.set(true);
    this.error.set(null);
    try {
      await this.service.saveRolePins(this.roleId(), {
        pins: this.staged().map((pin) => ({ agentId: pin.agentId, locked: pin.locked })),
      });
      await this.reloadPins();
    } catch (err) {
      // The refusal names the offending list, so the server's message beats a generic one.
      this.error.set(this.messageFor(err, 'Failed to save the default pins.'));
    } finally {
      this.busy.set(false);
    }
  }

  /** Names, not ids — the dialog is read by a person deciding whether to unpin for a cohort. */
  private removedNames(): string[] {
    const stagedIds = new Set(this.staged().map((pin) => pin.agentId));
    return this.savedPins()
      .filter((row) => !stagedIds.has(row.agentId))
      .map((row) => row.name);
  }

  private messageFor(err: unknown, fallback: string): string {
    const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
    return typeof detail === 'string' ? detail : fallback;
  }
}
