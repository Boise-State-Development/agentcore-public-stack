import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowPath,
  heroCheckCircle,
  heroMagnifyingGlass,
} from '@ng-icons/heroicons/outline';
import { ServerTool, Tool, ToolService } from '../../services/tool/tool.service';
import { ToolCapabilityService } from '../../services/tool-capability/tool-capability.service';
import { ConnectorStatusService } from '../../settings/connectors/services/connector-status.service';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';
import { splitToolDescription } from '../../shared/utils/tool-description';
import { monogramFor } from '../../shared/utils/monogram';
import { SpinnerComponent } from '../../components/spinner/spinner.component';

/** A server's tool with its docstring already split for display. */
interface SubToolRow extends ServerTool {
  summary: string;
  detail: string;
}

/** Above this many sub-tools the list gets its own filter box. */
const FILTER_THRESHOLD = 8;

/**
 * Customize → Tools → one tool. Everything the browse card had no room for:
 * what the tool is, what an MCP server exposes tool-by-tool, and the prompts
 * and resources it offers besides tools.
 *
 * ⚠️ Since step 5 deleted the composer drawer (#1079) this is the **only**
 * per-sub-tool surface in the app. A user who wants 3 of Canvas's 48 tools has
 * nowhere else to say so.
 *
 * It descends from the drawer's `ToolDetailComponent` but deliberately did not
 * inherit two of its traits:
 *
 * - The drawer was **conversation-scoped** — it read `isToolShownEnabled()` and
 *   wrote through the Agent lock. Customize is **global**, so this page reads
 *   `tool.isEnabled` / `sub.enabled` and writes with `respectAgentLock: false`,
 *   exactly like the list it drills in from. See
 *   `docs/specs/customize-surface.md` §"The agent-lock seam".
 * - The drawer was 320px wide, which is why it had a tab strip. A page does not
 *   need one: tools, prompts and resources are stacked sections, so browser
 *   find-in-page works across all of them and nothing is hidden behind a tab a
 *   user has to guess at. A server with dozens of tools gets a filter box
 *   instead, which is the real fix for that list's length.
 *
 * Prompts and resources come from the stored capability snapshot, never a live
 * probe: probing opens an MCP session per server, and a 3LO server cannot be
 * reached without a consent token the browser does not hold. An admin refreshes
 * the snapshot; this page reads it. They stay read-only because acting on one
 * needs `prompts/get` / `resources/read`, which the backend does not expose to
 * this surface yet.
 */
@Component({
  selector: 'app-customize-tool-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, SpinnerComponent],
  providers: [
    provideIcons({ heroArrowLeft, heroArrowPath, heroCheckCircle, heroMagnifyingGlass }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <a
          routerLink="/customize/tools"
          class="inline-flex items-center gap-1.5 text-sm/6 font-medium text-gray-500 transition-colors hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          Tools
        </a>

        @if (tool(); as t) {
          <!-- Identity -->
          <div class="mt-6 flex items-start gap-4">
            <span
              aria-hidden="true"
              class="grid size-12 shrink-0 place-items-center rounded-2xl border border-gray-200 bg-gray-50 font-mono text-base font-semibold text-gray-600 dark:border-white/10 dark:bg-white/5 dark:text-gray-300"
              >{{ monogram() }}</span
            >
            <div class="min-w-0 flex-1">
              <h1 class="text-2xl/8 font-bold break-words text-gray-900 dark:text-white">
                {{ t.displayName }}
              </h1>
              <p class="mt-0.5 font-mono text-xs/5 break-all text-gray-500 dark:text-gray-400">
                {{ t.toolId }}
              </p>
              <div class="mt-2 flex flex-wrap items-center gap-1.5">
                @for (chip of chips(); track chip) {
                  <span
                    class="rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 font-medium text-gray-600 dark:bg-white/10 dark:text-gray-300"
                    >{{ chip }}</span
                  >
                }
              </div>
            </div>
          </div>

          @if (summary()) {
            <p class="mt-4 text-sm/6 text-gray-600 dark:text-gray-300">{{ summary() }}</p>
          }
          @if (detail()) {
            <button
              type="button"
              (click)="showDescriptionDetail.set(!showDescriptionDetail())"
              [attr.aria-expanded]="showDescriptionDetail()"
              aria-controls="tool-description-detail"
              class="mt-2 text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
            >
              {{ showDescriptionDetail() ? 'Hide reference' : 'Show reference' }}
            </button>
            @if (showDescriptionDetail()) {
              <!-- Keeps its line structure: an Args: block is a list, and
                   unwrapping it into a paragraph would read worse. -->
              <pre
                id="tool-description-detail"
                class="mt-2 overflow-x-auto rounded-2xl bg-gray-50 p-3 font-mono text-xs/5 whitespace-pre text-gray-600 dark:bg-white/5 dark:text-gray-300"
                >{{ detail() }}</pre
              >
            }
          }

          <!--
            Connection. Only drawn for a tool that actually needs consent, and
            only once we know the answer — an unknown state means the probe failed, and
            telling someone to reconnect on that basis sends them through a flow
            they may not need. Turning the tool on without connecting is still
            allowed: the agent surfaces its own consent prompt mid-turn, and
            blocking the switch here would be a second, contradictory gate.
          -->
          @switch (connection()) {
            @case ('disconnected') {
              <div
                class="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl border border-state-warning-200 bg-state-warning-50 px-4 py-3 dark:border-state-warning-800 dark:bg-state-warning-900/20"
              >
                <span class="min-w-0 text-sm/6 text-state-warning-800 dark:text-state-warning-200">
                  Needs your {{ t.requiresOauthProvider }} account before it can run.
                </span>
                <button
                  type="button"
                  (click)="connect()"
                  [disabled]="connecting()"
                  class="shrink-0 rounded-2xl bg-primary-accessible px-3.5 py-1.5 text-sm/6 font-semibold text-white transition-[filter] hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {{ connecting() ? 'Waiting…' : 'Connect' }}
                </button>
              </div>
            }
            @case ('connected') {
              <p
                class="mt-4 flex items-center gap-1.5 text-sm/6 text-state-success-700 dark:text-state-success-300"
              >
                <ng-icon name="heroCheckCircle" class="size-4 shrink-0" aria-hidden="true" />
                Connected as you
              </p>
            }
          }

          <!-- Master switch -->
          <div
            class="mt-4 flex items-center justify-between gap-4 rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-white/10 dark:bg-white/5"
          >
            <div class="min-w-0">
              <span
                id="tool-detail-state"
                class="block text-sm/6 font-medium text-gray-900 dark:text-white"
                >{{ t.isEnabled ? 'On' : 'Off' }}</span
              >
              <span class="block text-xs/5 text-gray-500 dark:text-gray-400">
                Applies to every conversation, including ones already open.
              </span>
            </div>
            <button
              type="button"
              role="switch"
              [attr.aria-checked]="t.isEnabled"
              aria-labelledby="tool-detail-state"
              [disabled]="pending().has(t.toolId)"
              (click)="onToggle(t)"
              class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
              [class]="t.isEnabled ? 'bg-primary-600 dark:bg-primary-500' : 'bg-gray-200 dark:bg-gray-700'"
            >
              <span
                aria-hidden="true"
                class="pointer-events-none inline-block size-5 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-in-out"
                [class.translate-x-5]="t.isEnabled"
                [class.translate-x-0]="!t.isEnabled"
              ></span>
            </button>
          </div>

          @if (saveError()) {
            <div
              role="alert"
              class="mt-4 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
            >
              {{ saveError() }}
            </div>
          }

          <!-- Tools -->
          @if (isMcpServer()) {
            <section class="mt-8">
              <h2 class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white">
                Tools
                <span class="font-mono text-xs/5 font-normal text-gray-500 tabular-nums dark:text-gray-400">
                  {{ subToolsLabel() }}
                </span>
              </h2>

              @if (subTools().length > 0) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  Turn off the ones you don’t need — only the tools left on are sent to the
                  model.
                </p>

                <!-- Dozens of tools is the normal case for a real MCP server, and
                     scrolling is not a way to find one by name. -->
                @if (subTools().length > FILTER_THRESHOLD) {
                  <div class="relative mt-3 max-w-sm">
                    <ng-icon
                      name="heroMagnifyingGlass"
                      class="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                      aria-hidden="true"
                    />
                    <label for="sub-tool-filter" class="sr-only">Filter this server’s tools</label>
                    <input
                      type="search"
                      id="sub-tool-filter"
                      [value]="subToolQuery()"
                      (input)="onSubToolFilter($event)"
                      placeholder="Filter tools…"
                      class="block w-full rounded-full border border-gray-300 bg-white py-2 pl-10 pr-4 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                    />
                  </div>
                }

                @if (visibleSubTools().length === 0) {
                  <p class="mt-3 text-sm/6 text-gray-500 dark:text-gray-400">
                    No tools match “{{ subToolQuery() }}”.
                  </p>
                } @else {
                  <ul class="mt-3 flex flex-col gap-2">
                    @for (sub of visibleSubTools(); track sub.name) {
                      <li
                        class="flex items-start justify-between gap-4 rounded-2xl border border-gray-200 px-4 py-3 dark:border-white/10"
                      >
                        <div class="min-w-0 flex-1">
                          <span
                            [id]="'subtool-' + sub.name"
                            class="block font-mono text-sm/6 font-medium text-gray-900 dark:text-white"
                            >{{ sub.name }}</span
                          >
                          @if (sub.summary) {
                            <span class="mt-0.5 block text-sm/6 text-gray-500 dark:text-gray-400">{{
                              sub.summary
                            }}</span>
                          }
                          @if (sub.detail) {
                            <button
                              type="button"
                              (click)="toggleDetail(sub.name)"
                              [attr.aria-expanded]="isDetailExpanded(sub.name)"
                              [attr.aria-controls]="'subtool-detail-' + sub.name"
                              class="mt-1 text-xs/5 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
                            >
                              {{ isDetailExpanded(sub.name) ? 'Hide details' : 'Show details' }}
                            </button>
                            @if (isDetailExpanded(sub.name)) {
                              <pre
                                [id]="'subtool-detail-' + sub.name"
                                class="mt-1.5 overflow-x-auto rounded-lg bg-gray-50 p-2 font-mono text-[11px]/5 whitespace-pre text-gray-600 dark:bg-white/5 dark:text-gray-300"
                                >{{ sub.detail }}</pre
                              >
                            }
                          }
                        </div>
                        <button
                          type="button"
                          role="switch"
                          [attr.aria-checked]="sub.enabled"
                          [attr.aria-labelledby]="'subtool-' + sub.name"
                          [disabled]="pending().has(sub.name)"
                          (click)="onToggleSubTool(t, sub)"
                          class="relative mt-0.5 inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
                          [class]="sub.enabled ? 'bg-primary-600 dark:bg-primary-500' : 'bg-gray-200 dark:bg-gray-700'"
                        >
                          <span
                            aria-hidden="true"
                            class="pointer-events-none inline-block size-4 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-in-out"
                            [class.translate-x-4]="sub.enabled"
                            [class.translate-x-0]="!sub.enabled"
                          ></span>
                        </button>
                      </li>
                    }
                  </ul>
                }
              } @else {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  This server hasn’t been asked what it offers yet.
                </p>
                <button
                  type="button"
                  (click)="discover()"
                  [disabled]="discovering()"
                  class="mt-3 inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 px-3.5 py-1.5 text-sm/6 font-semibold text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
                >
                  <ng-icon
                    name="heroArrowPath"
                    class="size-4"
                    [class.animate-spin]="discovering()"
                    aria-hidden="true"
                  />
                  {{ discovering() ? 'Listing tools…' : 'List this server’s tools' }}
                </button>
                @if (discoverError(); as message) {
                  <p class="mt-2 text-sm/6 text-state-danger-600 dark:text-state-danger-400" role="alert">
                    {{ message }}
                  </p>
                }
              }
            </section>
          }

          <!-- Prompts and resources: external MCP servers only. A local tool has
               no server to ask, and a Gateway target exposes tools only. -->
          @if (offersCapabilities()) {
            <section class="mt-8">
              <h2 class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white">
                Prompts
                @if (prompts().length > 0) {
                  <span class="font-mono text-xs/5 font-normal text-gray-500 tabular-nums dark:text-gray-400">
                    {{ prompts().length }}
                  </span>
                }
              </h2>
              @if (capabilitiesLoading()) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">Loading…</p>
              } @else if (neverDiscovered()) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  Nobody has asked this server what prompts it offers yet. An administrator can
                  refresh it.
                </p>
              } @else if (!capabilities()?.supportsPrompts) {
                <!-- Distinct from "has none": this server answered prompts/list with
                     "method not found", so prompts are not part of what it does. -->
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  This server doesn’t offer prompts.
                </p>
              } @else if (prompts().length === 0) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  This server offers prompts, but none right now.
                </p>
              } @else {
                <ul class="mt-3 flex flex-col gap-2">
                  @for (prompt of prompts(); track prompt.name) {
                    <li class="rounded-2xl border border-gray-200 px-4 py-3 dark:border-white/10">
                      <span class="block font-mono text-sm/6 font-medium text-gray-900 dark:text-white">{{
                        prompt.title || prompt.name
                      }}</span>
                      @if (prompt.description) {
                        <span class="mt-0.5 block text-sm/6 text-gray-500 dark:text-gray-400">{{
                          prompt.description
                        }}</span>
                      }
                      @if (prompt.arguments.length > 0) {
                        <span class="mt-1 block font-mono text-xs/5 text-gray-400 dark:text-gray-500"
                          >arguments: {{ prompt.arguments.join(', ') }}</span
                        >
                      }
                    </li>
                  }
                </ul>
              }
            </section>

            <section class="mt-8">
              <h2 class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white">
                Resources
                @if (resources().length > 0) {
                  <span class="font-mono text-xs/5 font-normal text-gray-500 tabular-nums dark:text-gray-400">
                    {{ resources().length }}
                  </span>
                }
              </h2>
              @if (capabilitiesLoading()) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">Loading…</p>
              } @else if (neverDiscovered()) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  Nobody has asked this server what resources it offers yet. An administrator can
                  refresh it.
                </p>
              } @else if (!capabilities()?.supportsResources) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  This server doesn’t offer resources.
                </p>
              } @else if (resources().length === 0) {
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                  This server offers resources, but none right now.
                </p>
              } @else {
                <ul class="mt-3 flex flex-col gap-2">
                  @for (resource of resources(); track resource.uri) {
                    <li class="rounded-2xl border border-gray-200 px-4 py-3 dark:border-white/10">
                      <span class="flex items-start gap-1.5">
                        <span
                          class="min-w-0 flex-1 font-mono text-sm/6 font-medium break-all text-gray-900 dark:text-white"
                          >{{ resource.uri }}</span
                        >
                        @if (resource.uriTemplate) {
                          <!-- A template is a pattern, not a readable URI — the
                               placeholders have to be filled before anything can
                               read it. -->
                          <span
                            class="shrink-0 rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 text-gray-600 dark:bg-white/10 dark:text-gray-300"
                            >template</span
                          >
                        }
                      </span>
                      @if (resource.name || resource.description) {
                        <span class="mt-0.5 block text-sm/6 text-gray-500 dark:text-gray-400">{{
                          resource.description || resource.name
                        }}</span>
                      }
                      @if (resource.mimeType) {
                        <span class="mt-1 block font-mono text-xs/5 text-gray-400 dark:text-gray-500">{{
                          resource.mimeType
                        }}</span>
                      }
                    </li>
                  }
                </ul>
              }
              @if (capabilities()?.truncated) {
                <p class="mt-3 text-xs/5 text-gray-400 dark:text-gray-500">
                  This server exposes more than we store; the list above is capped.
                </p>
              }
            </section>
          }

          <!-- About -->
          <section class="mt-8">
            <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">About</h2>
            <dl class="mt-3 flex flex-col gap-2.5">
              @for (fact of facts(); track fact.label) {
                <div
                  class="flex items-baseline justify-between gap-4 border-b border-gray-100 pb-2.5 last:border-0 dark:border-white/5"
                >
                  <dt class="text-sm/6 text-gray-500 dark:text-gray-400">{{ fact.label }}</dt>
                  <dd
                    class="min-w-0 text-right font-mono text-sm/6 break-words text-gray-700 dark:text-gray-200"
                  >
                    {{ fact.value }}
                  </dd>
                </div>
              }
            </dl>
          </section>
        } @else if (toolService.loading() || !toolService.initialized()) {
          <div class="mt-8 flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
            <app-spinner size="sm" label="Loading tool" />
            Loading…
          </div>
        } @else {
          <!-- Loaded, and this id is not in the catalog: either it was retired or
               the user's roles no longer grant it. Both read the same to them. -->
          <div
            class="mt-8 rounded-2xl border border-dashed border-gray-300 p-8 text-center dark:border-gray-700"
          >
            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">Tool not found</p>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              This tool no longer exists, or your roles don’t grant it.
            </p>
            <a
              routerLink="/customize/tools"
              class="mt-4 inline-block text-sm/6 font-semibold text-primary-accessible hover:underline dark:text-primary-accessible-dark"
              >Back to Tools</a
            >
          </div>
        }
      </div>
    </div>
  `,
})
export class CustomizeToolDetailPage {
  protected readonly toolService = inject(ToolService);
  private readonly capabilityService = inject(ToolCapabilityService);
  private readonly connectorStatus = inject(ConnectorStatusService);
  private readonly consent = inject(OAuthConsentService);

  /** Bound from the `:toolId` route param by `withComponentInputBinding()`. */
  readonly toolId = input.required<string>();

  protected readonly FILTER_THRESHOLD = FILTER_THRESHOLD;

  protected readonly saveError = signal<string | null>(null);
  protected readonly discovering = signal(false);
  protected readonly discoverError = signal<string | null>(null);
  protected readonly showDescriptionDetail = signal(false);
  protected readonly subToolQuery = signal('');
  /** Keyed by tool id for the master switch, by sub-tool name for the rest. */
  protected readonly pending = signal<ReadonlySet<string>>(new Set());

  private readonly expandedDetails = signal<ReadonlySet<string>>(new Set());

  constructor() {
    // A deep link to this page is a legitimate first paint; the catalog is
    // usually already in flight from ToolService's own constructor.
    if (!this.toolService.initialized() && !this.toolService.loading()) {
      void this.toolService.loadTools();
    }

    effect(() => {
      const provider = this.tool()?.requiresOauthProvider;
      if (provider) void this.connectorStatus.ensure([provider]);
    });

    // Only external MCP servers have prompts and resources to show, so only
    // they are worth a request. `ensure` is a no-op for an id already cached.
    effect(() => {
      if (this.offersCapabilities()) void this.capabilityService.ensure(this.toolId());
    });
  }

  protected readonly tool = computed<Tool | null>(
    () => this.toolService.tools().find(t => t.toolId === this.toolId()) ?? null,
  );

  protected readonly monogram = computed(() => monogramFor(this.tool()?.displayName ?? ''));

  private readonly description = computed(() =>
    splitToolDescription(this.tool()?.description),
  );
  protected readonly summary = computed(() => this.description().summary);
  protected readonly detail = computed(() => this.description().detail);

  /** Category, protocol, and status only when it is worth saying out loud. */
  protected readonly chips = computed(() => {
    const t = this.tool();
    if (!t) return [];
    const chips: string[] = [t.category, t.protocol];
    if (t.status !== 'active') chips.push(t.status);
    return chips;
  });

  protected readonly isMcpServer = computed(() => {
    const protocol = this.tool()?.protocol;
    return protocol === 'mcp' || protocol === 'mcp_external';
  });

  protected readonly offersCapabilities = computed(
    () => this.tool()?.protocol === 'mcp_external',
  );

  /**
   * The server's tools with their docstrings split into a readable summary and
   * the reference detail below it.
   */
  protected readonly subTools = computed<SubToolRow[]>(() =>
    (this.tool()?.serverTools ?? []).map(sub => ({
      ...sub,
      ...splitToolDescription(sub.description),
    })),
  );

  protected readonly visibleSubTools = computed(() => {
    const q = this.subToolQuery().trim().toLowerCase();
    if (!q) return this.subTools();
    return this.subTools().filter(sub =>
      [sub.name, sub.summary].some(field => field.toLowerCase().includes(q)),
    );
  });

  /** "3 of 17 on" while partly selected, so the heading carries the real state. */
  protected readonly subToolsLabel = computed(() => {
    const subs = this.subTools();
    if (subs.length === 0) return '';
    const on = subs.filter(s => s.enabled).length;
    return on === subs.length ? `${subs.length}` : `${on} of ${subs.length} on`;
  });

  protected readonly capabilities = computed(() =>
    this.capabilityService.capabilitiesFor(this.toolId()),
  );

  protected readonly capabilitiesLoading = computed(() =>
    this.capabilityService.isLoading(this.toolId()),
  );

  protected readonly prompts = computed(() => this.capabilities()?.prompts ?? []);
  protected readonly resources = computed(() => this.capabilities()?.resources ?? []);

  /** Never discovered — distinct from "discovered and offers nothing". */
  protected readonly neverDiscovered = computed(() => {
    const snapshot = this.capabilities();
    return !!snapshot && !snapshot.discoveredAt;
  });

  /** The OAuth provider this tool needs, or null when it needs none. */
  private readonly providerId = computed(() => this.tool()?.requiresOauthProvider ?? null);

  /** Connection state for that provider; 'none' when the tool needs no consent. */
  protected readonly connection = computed(() => {
    const provider = this.providerId();
    return provider ? this.connectorStatus.stateFor(provider) : 'none';
  });

  protected readonly connecting = computed(() => {
    const provider = this.providerId();
    return provider ? this.consent.inFlightProviders().has(provider) : false;
  });

  /** Catalog facts the card has no room for. */
  protected readonly facts = computed(() => {
    const t = this.tool();
    if (!t) return [];
    const rows: { label: string; value: string }[] = [
      { label: 'Tool ID', value: t.toolId },
      { label: 'Protocol', value: t.protocol },
      { label: 'Category', value: t.category },
      { label: 'Status', value: t.status },
      { label: 'On by default', value: t.enabledByDefault ? 'yes' : 'no' },
    ];
    if (t.requiresOauthProvider) {
      rows.push({ label: 'Requires account', value: t.requiresOauthProvider });
    }
    if (t.grantedBy.length > 0) {
      rows.push({ label: 'Granted by', value: t.grantedBy.join(', ') });
    }
    return rows;
  });

  protected isDetailExpanded(name: string): boolean {
    return this.expandedDetails().has(name);
  }

  protected toggleDetail(name: string): void {
    this.expandedDetails.update(set => {
      const next = new Set(set);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  protected onSubToolFilter(event: Event): void {
    this.subToolQuery.set((event.target as HTMLInputElement).value);
  }

  /**
   * Open the provider's consent popup. Reuses the same service the chat layer
   * and the connectors page use, so popup blocking, COOP-severed openers and
   * the completion broadcast are all already handled — and
   * `ConnectorStatusService` flips the state when that broadcast lands.
   *
   * `requestConsent` is called with no authorization URL on purpose: the
   * service then fetches a fresh one, because AgentCore's URLs expire quickly.
   */
  protected connect(): void {
    const provider = this.providerId();
    if (!provider) return;
    this.consent.requestConsent(provider, undefined);
    void this.consent.openConsentPopup(provider);
  }

  protected async onToggle(tool: Tool): Promise<void> {
    await this.withPending(tool.toolId, tool.displayName, () =>
      // `respectAgentLock: false` — see the class comment.
      this.toolService.toggleTool(tool.toolId, { respectAgentLock: false }),
    );
  }

  protected async onToggleSubTool(tool: Tool, sub: SubToolRow): Promise<void> {
    await this.withPending(sub.name, sub.name, () =>
      this.toolService.toggleServerTool(tool.toolId, sub.name, { respectAgentLock: false }),
    );
  }

  /**
   * A switch that snaps back on its own looks like a bug in the switch, so a
   * failed save says so. The service has already reverted its optimistic
   * update by the time this runs.
   */
  private async withPending(
    key: string,
    label: string,
    save: () => Promise<void>,
  ): Promise<void> {
    if (this.pending().has(key)) return;
    this.saveError.set(null);
    this.pending.update(set => new Set(set).add(key));
    try {
      await save();
    } catch {
      this.saveError.set(`Couldn't save the change to ${label}. Please try again.`);
    } finally {
      this.pending.update(set => {
        const next = new Set(set);
        next.delete(key);
        return next;
      });
    }
  }

  protected async discover(): Promise<void> {
    this.discovering.set(true);
    this.discoverError.set(null);
    try {
      await this.toolService.discoverServerTools(this.toolId());
    } catch {
      this.discoverError.set('Could not list this server’s tools.');
    } finally {
      this.discovering.set(false);
    }
  }
}
