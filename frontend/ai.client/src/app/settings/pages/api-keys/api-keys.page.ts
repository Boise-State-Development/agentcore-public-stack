import {
  Component,
  ChangeDetectionStrategy,
  signal,
  computed,
  inject,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroKey, heroPlus, heroTrash, heroClipboardDocument,
  heroCheck, heroChevronDown, heroChevronUp, heroShieldCheck,
  heroExclamationTriangle, heroInformationCircle, heroCpuChip,
  heroCodeBracket, heroCommandLine, heroClock, heroXMark,
  heroQuestionMarkCircle,
} from '@ng-icons/heroicons/outline';
import { ToastService } from '../../../services/toast/toast.service';
import { ConfigService } from '../../../services/config.service';
import { ModelService } from '../../../session/services/model/model.service';
import { ApiKeyService, ApiKey, CreateApiKeyResponse } from './api-key.service';

type ResponseType = 'non-streaming' | 'streaming';
type ExampleFormat = 'simple' | 'conversation';
type ExampleLanguage = 'curl' | 'python' | 'javascript';
type TooltipField = 'responseType' | 'exampleFormat' | 'optionalParams' | null;

@Component({
  selector: 'app-api-keys',
  imports: [NgIcon],
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [
    provideIcons({
      heroKey, heroPlus, heroTrash, heroClipboardDocument,
      heroCheck, heroChevronDown, heroChevronUp, heroShieldCheck,
      heroExclamationTriangle, heroInformationCircle, heroCpuChip,
      heroCodeBracket, heroCommandLine, heroClock, heroXMark,
      heroQuestionMarkCircle,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div>
      <div>
        <div class="mb-6">
          <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">API Keys</h2>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">Programmatic access to the chat API</p>
        </div>

        <div class="grid grid-cols-1 gap-8 xl:grid-cols-2">
          <!-- LEFT COLUMN -->
          <div class="flex flex-col gap-5">

            @if (loading()) {
              <!-- Loading skeleton -->
              <div class="animate-pulse">
                <div class="rounded-sm border-2 border-dashed border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-700 dark:bg-gray-800/50">
                  <div class="mx-auto h-5 w-40 rounded-xs bg-gray-200 dark:bg-gray-700"></div>
                </div>
                <div class="mt-5 rounded-sm border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                  <div class="p-4">
                    <div class="h-5 w-32 rounded-xs bg-gray-200 dark:bg-gray-700"></div>
                    <div class="mt-2 h-4 w-24 rounded-xs bg-gray-200 dark:bg-gray-700"></div>
                  </div>
                  <div class="border-t border-gray-100 px-4 py-3 dark:border-gray-800">
                    <div class="flex flex-col gap-2">
                      <div class="h-4 w-48 rounded-xs bg-gray-200 dark:bg-gray-700"></div>
                      <div class="h-4 w-44 rounded-xs bg-gray-200 dark:bg-gray-700"></div>
                      <div class="h-4 w-36 rounded-xs bg-gray-200 dark:bg-gray-700"></div>
                    </div>
                  </div>
                </div>
              </div>
            } @else if (!showCreateDialog()) {
              <button (click)="handleCreateClick()"
                class="flex items-center justify-center gap-2 rounded-sm border-2 border-dashed border-orange-300 bg-white px-4 py-3 text-sm/6 font-semibold text-orange-600 transition-colors hover:border-orange-400 hover:bg-orange-50 focus-visible:ring-2 focus-visible:ring-orange-500 focus-visible:ring-offset-2 dark:border-orange-600/40 dark:bg-gray-800 dark:text-orange-400 dark:hover:border-orange-500 dark:hover:bg-orange-950/30">
                <ng-icon name="heroPlus" class="size-4" /> Create New API Key
              </button>
            } @else {
              <div class="rounded-sm border border-orange-300 bg-white p-5 shadow-sm dark:border-orange-700/50 dark:bg-gray-800">
                <div class="flex items-center justify-between">
                  <h3 class="text-sm/6 font-semibold text-gray-900 dark:text-white">New API Key</h3>
                  <button (click)="closeCreateDialog()" class="rounded-xs p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200" aria-label="Close">
                    <ng-icon name="heroXMark" class="size-4" />
                  </button>
                </div>
                @if (!createdKeySecret()) {
                  <div class="mt-3 flex gap-2">
                    <input id="key-name" type="text" placeholder="Key name, e.g. my-app"
                      [value]="newKeyName()" (input)="newKeyName.set($any($event.target).value)" (keydown.enter)="createKey()"
                      [disabled]="creating()"
                      class="block flex-1 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder-gray-400 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder-gray-500" />
                    <button (click)="createKey()" [disabled]="!newKeyName().trim() || creating()"
                      class="inline-flex shrink-0 items-center gap-2 rounded-sm bg-orange-500 px-4 py-2 text-sm/6 font-semibold text-white transition-colors hover:bg-orange-600 focus-visible:ring-2 focus-visible:ring-orange-500 focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50">
                      @if (creating()) {
                        <div class="size-4 animate-spin rounded-full border-2 border-white/30 border-t-white"></div>
                        Generating...
                      } @else {
                        Generate
                      }
                    </button>
                  </div>
                } @else {
                  <div class="mt-3 rounded-sm border border-green-300 bg-green-50 p-4 dark:border-green-700/50 dark:bg-green-950/30">
                    <div class="flex items-start gap-2">
                      <ng-icon name="heroShieldCheck" class="mt-0.5 size-5 shrink-0 text-green-600 dark:text-green-400" />
                      <div class="min-w-0 flex-1">
                        <p class="text-sm/6 font-semibold text-green-800 dark:text-green-200">Copy your key now — you won't see it again</p>
                        <div class="mt-2 flex items-center gap-2">
                          <code class="block flex-1 truncate rounded-xs bg-green-100 px-2 py-1 font-mono text-xs text-green-900 dark:bg-green-900/40 dark:text-green-100">{{ createdKeySecret() }}</code>
                          <button (click)="copyToClipboard(createdKeySecret()!, 'secret')"
                            class="shrink-0 rounded-xs p-1.5 text-green-700 hover:bg-green-200 focus-visible:ring-2 focus-visible:ring-green-500 dark:text-green-300 dark:hover:bg-green-800" aria-label="Copy API key">
                            <ng-icon [name]="copiedField() === 'secret' ? 'heroCheck' : 'heroClipboardDocument'" class="size-4" />
                          </button>
                        </div>
                      </div>
                    </div>
                  </div>
                  <div class="mt-3 flex justify-end">
                    <button (click)="closeCreateDialog()" class="rounded-sm bg-gray-100 px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-200 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700">Done</button>
                  </div>
                }
              </div>
            }

            <!-- Confirm replace modal -->
            @if (showConfirmReplace()) {
              <div class="fixed inset-0 z-50 flex items-center justify-center bg-black/50" (click)="showConfirmReplace.set(false)">
                <div class="mx-4 w-full max-w-sm rounded-sm border border-gray-200 bg-white p-6 shadow-xl dark:border-gray-700 dark:bg-gray-800" (click)="$event.stopPropagation()">
                  <div class="flex items-start gap-3">
                    <div class="flex size-10 shrink-0 items-center justify-center rounded-full bg-orange-100 dark:bg-orange-900/30">
                      <ng-icon name="heroExclamationTriangle" class="size-5 text-orange-600 dark:text-orange-400" />
                    </div>
                    <div>
                      <h3 class="text-base/6 font-semibold text-gray-900 dark:text-white">Replace existing key?</h3>
                      <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">
                        Creating a new API key will <span class="font-semibold text-red-600 dark:text-red-400">permanently delete</span> your current key. Any integrations using the old key will stop working immediately.
                      </p>
                    </div>
                  </div>
                  <div class="mt-5 flex justify-end gap-3">
                    <button (click)="showConfirmReplace.set(false)" class="rounded-sm px-3 py-1.5 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white">Cancel</button>
                    <button (click)="confirmCreateKey()" class="rounded-sm bg-orange-500 px-4 py-1.5 text-sm/6 font-semibold text-white transition-colors hover:bg-orange-600 focus-visible:ring-2 focus-visible:ring-orange-500 focus-visible:ring-offset-2">Replace Key</button>
                  </div>
                </div>
              </div>
            }

            <!-- Current key -->
            @if (loading()) {
              <!-- handled above -->
            } @else if (apiKey(); as key) {
              <div class="rounded-sm border bg-white shadow-xs dark:bg-gray-800"
                [class]="!isExpired() ? 'border-gray-200 dark:border-gray-700' : 'border-red-200 dark:border-red-900/50'">
                <div class="flex items-start justify-between p-4">
                  <div class="min-w-0">
                    <h3 class="truncate text-base/6 font-semibold text-gray-900 dark:text-white">{{ key.name }}</h3>
                    @if (!isExpired()) {
                      <div class="mt-1 flex items-center gap-1.5">
                        <span class="inline-block size-2 rounded-full bg-green-500"></span>
                        <span class="text-sm/5 font-medium text-green-600 dark:text-green-400">Expires {{ daysUntil(key.expires_at) }}</span>
                      </div>
                    } @else {
                      <div class="mt-1 flex items-center gap-1.5">
                        <span class="inline-block size-2 rounded-full bg-red-500"></span>
                        <span class="text-sm/5 font-medium text-red-600 dark:text-red-400">Expired</span>
                      </div>
                    }
                  </div>
                  <button (click)="deleteKey()" [disabled]="deleting()"
                    class="shrink-0 rounded-sm p-2 text-gray-400 transition-colors hover:bg-red-50 hover:text-red-600 focus-visible:ring-2 focus-visible:ring-red-500 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:bg-red-950/30 dark:hover:text-red-400"
                    [attr.aria-label]="'Delete key ' + key.name">
                    @if (deleting()) {
                      <div class="size-5 animate-spin rounded-full border-2 border-gray-300 border-t-red-500"></div>
                    } @else {
                      <ng-icon name="heroTrash" class="size-5" />
                    }
                  </button>
                </div>
                <div class="border-t border-gray-100 px-4 py-3 dark:border-gray-800">
                  <dl class="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm/6">
                    <dt class="text-gray-500 dark:text-gray-400">Created</dt>
                    <dd class="text-gray-900 dark:text-gray-200">{{ formatDate(key.created_at) }}</dd>
                    <dt class="text-gray-500 dark:text-gray-400">Expires</dt>
                    <dd class="text-gray-900 dark:text-gray-200">{{ formatDate(key.expires_at) }}</dd>
                    <dt class="text-gray-500 dark:text-gray-400">Last Used</dt>
                    <dd class="text-gray-900 dark:text-gray-200">{{ key.last_used_at ? formatDate(key.last_used_at) : 'Never' }}</dd>
                  </dl>
                </div>
              </div>
            } @else if (!showCreateDialog()) {
              <div class="rounded-sm border border-dashed border-gray-300 bg-white p-8 text-center dark:border-gray-700 dark:bg-gray-800">
                <ng-icon name="heroKey" class="mx-auto size-10 text-gray-300 dark:text-gray-600" />
                <p class="mt-3 text-sm/6 font-medium text-gray-900 dark:text-white">No API key yet</p>
                <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">Create your first key to get started with the API.</p>
              </div>
            }

            <!-- Available Models -->
            <div class="overflow-hidden rounded-sm border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
              <button (click)="modelsExpanded.set(!modelsExpanded())"
                class="flex w-full items-center justify-between p-4 text-left focus-visible:ring-2 focus-visible:ring-orange-500 focus-visible:ring-offset-2"
                [attr.aria-expanded]="modelsExpanded()">
                <div class="flex items-center gap-3">
                  <div class="flex size-8 items-center justify-center rounded-xs bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400">
                    <ng-icon name="heroCpuChip" class="size-4" />
                  </div>
                  <div>
                    <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Available Models</h2>
                    <p class="text-xs/5 text-gray-500 dark:text-gray-400">{{ availableModels().length }} model{{ availableModels().length === 1 ? '' : 's' }} you can use via API</p>
                  </div>
                </div>
                <ng-icon [name]="modelsExpanded() ? 'heroChevronUp' : 'heroChevronDown'" class="size-5 text-gray-400" />
              </button>
              @if (modelsExpanded()) {
                <div class="border-t border-gray-200 dark:border-gray-700">
                  @if (modelService.modelsLoading()) {
                    <div class="flex items-center justify-center p-6">
                      <div class="size-6 animate-spin rounded-full border-2 border-gray-300 border-t-orange-500"></div>
                    </div>
                  } @else {
                    <div class="divide-y divide-gray-100 dark:divide-gray-800">
                      @for (model of availableModels(); track model.id) {
                        <div class="flex items-center justify-between gap-3 px-4 py-3">
                          <div class="min-w-0">
                            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ model.modelName }}</p>
                            <p class="text-xs/5 text-gray-500 dark:text-gray-400">{{ model.providerName }}</p>
                          </div>
                          <div class="flex shrink-0 items-center gap-1.5">
                            <code class="max-w-48 truncate rounded-xs bg-gray-100 px-2 py-0.5 font-mono text-xs text-gray-600 dark:bg-gray-800 dark:text-gray-300">{{ model.modelId }}</code>
                            <button (click)="copyToClipboard(model.modelId, model.modelId)"
                              class="rounded-xs p-1 text-gray-400 hover:text-gray-600 focus-visible:ring-2 focus-visible:ring-orange-500 dark:hover:text-gray-200"
                              [attr.aria-label]="'Copy model ID ' + model.modelId">
                              <ng-icon [name]="copiedField() === model.modelId ? 'heroCheck' : 'heroClipboardDocument'" class="size-3.5" />
                            </button>
                          </div>
                        </div>
                      }
                    </div>
                  }
                </div>
              }
            </div>

            <!-- Important Information -->
            <div class="overflow-hidden rounded-sm border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
              <button (click)="infoExpanded.set(!infoExpanded())"
                class="flex w-full items-center justify-between p-4 text-left focus-visible:ring-2 focus-visible:ring-orange-500 focus-visible:ring-offset-2"
                [attr.aria-expanded]="infoExpanded()">
                <div class="flex items-center gap-3">
                  <div class="flex size-8 items-center justify-center rounded-xs bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400">
                    <ng-icon name="heroInformationCircle" class="size-4" />
                  </div>
                  <div>
                    <h2 class="text-sm/6 font-semibold text-gray-900 dark:text-white">Important Information</h2>
                    <p class="text-xs/5 text-gray-500 dark:text-gray-400">Key policies and usage guidelines</p>
                  </div>
                </div>
                <ng-icon [name]="infoExpanded() ? 'heroChevronUp' : 'heroChevronDown'" class="size-5 text-gray-400" />
              </button>
              @if (infoExpanded()) {
                <div class="border-t border-gray-200 px-4 pb-4 pt-3 dark:border-gray-700">
                  <div class="space-y-3 text-sm/6 text-gray-600 dark:text-gray-300">
                    <div class="flex gap-3">
                      <ng-icon name="heroClock" class="mt-0.5 size-4 shrink-0 text-orange-500" />
                      <div><span class="font-semibold text-gray-900 dark:text-white">Expiration — </span>Keys expire 90 days after creation. Generate a new key before expiry to maintain access.</div>
                    </div>
                    <div class="flex gap-3">
                      <ng-icon name="heroCpuChip" class="mt-0.5 size-4 shrink-0 text-purple-500" />
                      <div><span class="font-semibold text-gray-900 dark:text-white">Quota &amp; Rate Limits — </span>API usage counts against your monthly quota. The same limits apply as the web interface.</div>
                    </div>
                    <div class="flex gap-3">
                      <ng-icon name="heroShieldCheck" class="mt-0.5 size-4 shrink-0 text-green-500" />
                      <div><span class="font-semibold text-gray-900 dark:text-white">Security — </span>Treat your API key like a password. Never commit it to source control or share it publicly.</div>
                    </div>
                    <div class="flex gap-3">
                      <ng-icon name="heroKey" class="mt-0.5 size-4 shrink-0 text-blue-500" />
                      <div><span class="font-semibold text-gray-900 dark:text-white">Authentication — </span>Include your key in the <code class="rounded-xs bg-gray-100 px-1 font-mono text-xs dark:bg-gray-800">X-API-Key</code> header with every request.</div>
                    </div>
                  </div>
                </div>
              }
            </div>
          </div>

          <!-- RIGHT COLUMN -->
          <div class="flex flex-col gap-5">
            <div class="rounded-sm border border-gray-200 bg-white p-5 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-semibold uppercase tracking-wider text-orange-500 dark:text-orange-400">Interactive examples with your available models</p>
              <div class="mt-2 flex items-center gap-3">
                <ng-icon name="heroCodeBracket" class="size-6 text-orange-500" />
                <h2 class="text-xl/7 font-bold tracking-tight text-gray-900 dark:text-white">Usage Examples</h2>
              </div>
              <div class="mt-4 flex flex-col gap-3">
                <div>
                  <label for="model-select" class="block text-xs/5 font-medium text-gray-500 dark:text-gray-400">Select Model</label>
                  <select id="model-select" (change)="selectedModelId.set($any($event.target).value)"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white">
                    @for (model of availableModels(); track model.id) {
                      <option [value]="model.modelId" [selected]="model.modelId === selectedModelId()">{{ model.modelName }} ({{ model.modelId }})</option>
                    }
                  </select>
                </div>
                <!-- Response Type -->
                <div>
                  <div class="flex items-center gap-1.5">
                    <label for="response-type" class="text-xs/5 font-medium text-gray-500 dark:text-gray-400">Response Type</label>
                    <div class="relative">
                      <button (mouseenter)="activeTooltip.set('responseType')" (mouseleave)="activeTooltip.set(null)"
                        (focus)="activeTooltip.set('responseType')" (blur)="activeTooltip.set(null)"
                        class="rounded-full text-gray-400 hover:text-orange-500 focus-visible:ring-2 focus-visible:ring-orange-500 dark:hover:text-orange-400"
                        aria-label="Response type information" type="button">
                        <ng-icon name="heroQuestionMarkCircle" class="size-3.5" />
                      </button>
                      @if (activeTooltip() === 'responseType') {
                        <div class="absolute bottom-full left-1/2 z-10 mb-2 w-64 -translate-x-1/2 rounded-sm border border-gray-200 bg-white p-3 shadow-lg dark:border-gray-600 dark:bg-gray-800" role="tooltip">
                          <p class="text-xs/5 font-semibold text-gray-900 dark:text-white">Non-Streaming</p>
                          <p class="text-xs/4 text-gray-600 dark:text-gray-300">Returns the complete response at once as JSON. Best for batch processing and simple integrations.</p>
                          <p class="mt-2 text-xs/5 font-semibold text-gray-900 dark:text-white">Streaming (SSE)</p>
                          <p class="text-xs/4 text-gray-600 dark:text-gray-300">Returns the response as Server-Sent Events. Ideal for real-time UIs and chat interfaces.</p>
                          <div class="absolute left-1/2 top-full -translate-x-1/2 border-4 border-transparent border-t-white dark:border-t-gray-800"></div>
                        </div>
                      }
                    </div>
                  </div>
                  <select id="response-type" (change)="selectedResponseType.set($any($event.target).value)"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white">
                    <option value="non-streaming" [selected]="selectedResponseType() === 'non-streaming'">Non-Streaming (Complete)</option>
                    <option value="streaming" [selected]="selectedResponseType() === 'streaming'">Streaming (SSE)</option>
                  </select>
                </div>
                <!-- Example Format -->
                <div>
                  <div class="flex items-center gap-1.5">
                    <label for="example-format" class="text-xs/5 font-medium text-gray-500 dark:text-gray-400">Example Format</label>
                    <div class="relative">
                      <button (mouseenter)="activeTooltip.set('exampleFormat')" (mouseleave)="activeTooltip.set(null)"
                        (focus)="activeTooltip.set('exampleFormat')" (blur)="activeTooltip.set(null)"
                        class="rounded-full text-gray-400 hover:text-orange-500 focus-visible:ring-2 focus-visible:ring-orange-500 dark:hover:text-orange-400"
                        aria-label="Example format information" type="button">
                        <ng-icon name="heroQuestionMarkCircle" class="size-3.5" />
                      </button>
                      @if (activeTooltip() === 'exampleFormat') {
                        <div class="absolute bottom-full left-1/2 z-10 mb-2 w-64 -translate-x-1/2 rounded-sm border border-gray-200 bg-white p-3 shadow-lg dark:border-gray-600 dark:bg-gray-800" role="tooltip">
                          <p class="text-xs/5 font-semibold text-gray-900 dark:text-white">Simple Message</p>
                          <p class="text-xs/4 text-gray-600 dark:text-gray-300">Single message without history. Perfect for one-off Q&A requests.</p>
                          <p class="mt-2 text-xs/5 font-semibold text-gray-900 dark:text-white">Conversation History</p>
                          <p class="text-xs/4 text-gray-600 dark:text-gray-300">Includes previous messages for multi-turn conversations with full context.</p>
                          <div class="absolute left-1/2 top-full -translate-x-1/2 border-4 border-transparent border-t-white dark:border-t-gray-800"></div>
                        </div>
                      }
                    </div>
                  </div>
                  <select id="example-format" (change)="selectedFormat.set($any($event.target).value)"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white">
                    <option value="simple" [selected]="selectedFormat() === 'simple'">Simple Message</option>
                    <option value="conversation" [selected]="selectedFormat() === 'conversation'">Conversation History</option>
                  </select>
                </div>
                <!-- Optional Parameters -->
                <div>
                  <div class="relative flex items-center py-1">
                    <div class="grow border-t border-gray-200 dark:border-gray-700"></div>
                    <button (click)="optionalParamsExpanded.set(!optionalParamsExpanded())"
                      class="mx-3 flex shrink-0 items-center gap-1.5 text-xs/5 font-medium text-gray-400 transition-colors hover:text-orange-500 focus-visible:ring-2 focus-visible:ring-orange-500 focus-visible:ring-offset-2 dark:text-gray-500 dark:hover:text-orange-400"
                      [attr.aria-expanded]="optionalParamsExpanded()" type="button">
                      {{ optionalParamsExpanded() ? 'Hide' : 'Show' }} optional parameters
                      <ng-icon [name]="optionalParamsExpanded() ? 'heroChevronUp' : 'heroChevronDown'" class="size-3.5" />
                    </button>
                    <div class="grow border-t border-gray-200 dark:border-gray-700"></div>
                  </div>
                  @if (optionalParamsExpanded()) {
                    <div class="flex flex-col gap-3 pt-2">
                      <div class="grid grid-cols-2 gap-3">
                        <div>
                          <label for="param-temperature" class="block text-xs/5 font-medium text-gray-500 dark:text-gray-400">Temperature</label>
                          <input id="param-temperature" type="text" placeholder="0.7"
                            [value]="paramTemperature()" (input)="paramTemperature.set($any($event.target).value)"
                            class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-1.5 font-mono text-sm/6 text-gray-900 placeholder-gray-400 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder-gray-500" />
                          <p class="mt-0.5 text-xs/4 text-gray-400 dark:text-gray-500">0.0 – 1.0</p>
                        </div>
                        <div>
                          <label for="param-max-tokens" class="block text-xs/5 font-medium text-gray-500 dark:text-gray-400">Max Tokens</label>
                          <input id="param-max-tokens" type="text" placeholder="4096"
                            [value]="paramMaxTokens()" (input)="paramMaxTokens.set($any($event.target).value)"
                            class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-1.5 font-mono text-sm/6 text-gray-900 placeholder-gray-400 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder-gray-500" />
                          <p class="mt-0.5 text-xs/4 text-gray-400 dark:text-gray-500">1 – model max</p>
                        </div>
                      </div>
                      <div>
                        <label for="param-top-p" class="block text-xs/5 font-medium text-gray-500 dark:text-gray-400">Top P</label>
                        <input id="param-top-p" type="text" placeholder="(default)"
                          [value]="paramTopP()" (input)="paramTopP.set($any($event.target).value)"
                          class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-1.5 font-mono text-sm/6 text-gray-900 placeholder-gray-400 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder-gray-500" />
                        <p class="mt-0.5 text-xs/4 text-gray-400 dark:text-gray-500">Nucleus sampling, 0.0 – 1.0</p>
                      </div>
                      <div>
                        <label for="param-system-prompt" class="block text-xs/5 font-medium text-gray-500 dark:text-gray-400">System Prompt</label>
                        <textarea id="param-system-prompt" rows="2" placeholder="(none)"
                          [value]="paramSystemPrompt()" (input)="paramSystemPrompt.set($any($event.target).value)"
                          class="mt-1 block w-full resize-y rounded-sm border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 placeholder-gray-400 focus:border-orange-500 focus:outline-hidden focus:ring-2 focus:ring-orange-500/30 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder-gray-500"></textarea>
                        <p class="mt-0.5 text-xs/4 text-gray-400 dark:text-gray-500">Instructions for the model's behavior</p>
                      </div>
                    </div>
                  }
                </div>
              </div>
            </div>

            <!-- Code examples -->
            <div class="flex flex-col gap-3">
              <div class="flex gap-1 rounded-sm border border-gray-200 bg-white p-1 dark:border-gray-700 dark:bg-gray-800">
                @for (lang of languages; track lang.id) {
                  <button (click)="selectedLanguage.set(lang.id)"
                    [class]="selectedLanguage() === lang.id
                      ? 'flex-1 rounded-xs bg-orange-500 px-3 py-2 text-sm/6 font-medium text-white shadow-sm'
                      : 'flex-1 rounded-xs px-3 py-2 text-sm/6 font-medium text-gray-600 transition-colors hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'">
                    <div class="flex items-center justify-center gap-2">
                      <ng-icon [name]="lang.icon" class="size-4" /> {{ lang.label }}
                    </div>
                  </button>
                }
              </div>
              <div class="overflow-hidden rounded-sm border border-gray-700 shadow-sm">
                <div class="flex items-center justify-between bg-gray-800 px-4 py-2">
                  <span class="text-xs/5 font-medium text-gray-400">{{ currentLanguageLabel() }}</span>
                  <button (click)="copyToClipboard(codeExample(), 'code')"
                    class="flex items-center gap-1.5 rounded-xs px-2 py-1 text-xs/5 font-medium text-orange-400 hover:bg-gray-700 focus-visible:ring-2 focus-visible:ring-orange-500">
                    <ng-icon [name]="copiedField() === 'code' ? 'heroCheck' : 'heroClipboardDocument'" class="size-3.5" />
                    {{ copiedField() === 'code' ? 'Copied' : 'Copy' }}
                  </button>
                </div>
                <pre class="overflow-x-auto bg-gray-950 p-4 text-sm/6"><code class="font-mono text-gray-300">{{ codeExample() }}</code></pre>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  `,
})
export class ApiKeysPage {
  private toast = inject(ToastService);
  private apiKeyService = inject(ApiKeyService);
  private configService = inject(ConfigService);
  readonly modelService = inject(ModelService);

  readonly showCreateDialog = signal(false);
  readonly showConfirmReplace = signal(false);
  readonly newKeyName = signal('');
  readonly createdKeySecret = signal<string | null>(null);
  readonly copiedField = signal<string | null>(null);
  readonly modelsExpanded = signal(false);
  readonly infoExpanded = signal(false);
  readonly activeTooltip = signal<TooltipField>(null);

  readonly selectedModelId = signal('');
  readonly selectedResponseType = signal<ResponseType>('non-streaming');
  readonly selectedFormat = signal<ExampleFormat>('simple');
  readonly selectedLanguage = signal<ExampleLanguage>('curl');
  readonly optionalParamsExpanded = signal(false);
  readonly paramTemperature = signal('');
  readonly paramMaxTokens = signal('');
  readonly paramTopP = signal('');
  readonly paramSystemPrompt = signal('');

  readonly apiKey = signal<ApiKey | null>(null);
  readonly loading = signal(false);
  readonly creating = signal(false);
  readonly deleting = signal(false);

  readonly isExpired = computed(() => {
    const key = this.apiKey();
    if (!key) return false;
    return new Date(key.expires_at).getTime() < Date.now();
  });

  constructor() {
    this.loadKey();
  }

  private loadKey(): void {
    this.loading.set(true);
    this.apiKeyService.getKey().subscribe({
      next: (key: ApiKey | null) => {
        this.apiKey.set(key);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  readonly availableModels = computed(() => this.modelService.availableModels());

  readonly selectedModelForExample = computed(() => {
    const id = this.selectedModelId();
    const models = this.availableModels();
    if (id) return id;
    return models.length > 0 ? models[0].modelId : 'us.anthropic.claude-sonnet-4-20250514-v1:0';
  });

  readonly languages = [
    { id: 'curl' as const, label: 'cURL', icon: 'heroCommandLine' },
    { id: 'python' as const, label: 'Python', icon: 'heroCodeBracket' },
    { id: 'javascript' as const, label: 'JavaScript', icon: 'heroCodeBracket' },
  ];

  readonly currentLanguageLabel = computed(() =>
    this.languages.find(l => l.id === this.selectedLanguage())?.label ?? ''
  );

  readonly apiBaseUrl = computed(() => {
    const appApiUrl = this.configService.appApiUrl();
    if (appApiUrl) {
      return appApiUrl.replace(/\/+$/, '');
    }
    return window.location.origin;
  });

  readonly codeExample = computed(() => {
    const model = this.selectedModelForExample();
    const streaming = this.selectedResponseType() === 'streaming';
    const conversation = this.selectedFormat() === 'conversation';
    const lang = this.selectedLanguage();
    const baseUrl = this.apiBaseUrl();
    const opts = {
      temperature: this.paramTemperature().trim(),
      maxTokens: this.paramMaxTokens().trim(),
      topP: this.paramTopP().trim(),
      systemPrompt: this.paramSystemPrompt().trim(),
    };
    if (lang === 'curl') return this.buildCurlExample(model, conversation, streaming, opts, baseUrl);
    if (lang === 'python') return this.buildPythonExample(model, conversation, streaming, opts, baseUrl);
    return this.buildJsExample(model, conversation, streaming, opts, baseUrl);
  });

  handleCreateClick(): void {
    const current = this.apiKey();
    if (current && !this.isExpired()) {
      this.showConfirmReplace.set(true);
    } else {
      this.openCreateDialog();
    }
  }

  confirmCreateKey(): void {
    this.showConfirmReplace.set(false);
    this.openCreateDialog();
  }

  openCreateDialog(): void {
    this.showCreateDialog.set(true);
    this.newKeyName.set('');
    this.createdKeySecret.set(null);
  }

  closeCreateDialog(): void {
    this.showCreateDialog.set(false);
    this.newKeyName.set('');
    this.createdKeySecret.set(null);
  }

  createKey(): void {
    const name = this.newKeyName().trim();
    if (!name || this.creating()) return;
    this.creating.set(true);
    this.apiKeyService.createKey(name).subscribe({
      next: (res: CreateApiKeyResponse) => {
        this.apiKey.set({
          key_id: res.key_id,
          name: res.name,
          created_at: res.created_at,
          expires_at: res.expires_at,
          last_used_at: null,
        });
        this.createdKeySecret.set(res.key);
        this.creating.set(false);
        this.toast.success('API Key Created', `Key "${name}" has been generated.`);
      },
      error: () => {
        this.creating.set(false);
        this.toast.error('Failed to create API key.');
      },
    });
  }

  deleteKey(): void {
    const key = this.apiKey();
    if (!key || this.deleting()) return;
    if (!confirm(`Delete API key "${key.name}"? This action cannot be undone.`)) return;
    this.deleting.set(true);
    this.apiKeyService.deleteKey(key.key_id).subscribe({
      next: () => {
        this.apiKey.set(null);
        this.deleting.set(false);
        this.toast.success('Key Deleted', `API key "${key.name}" has been removed.`);
      },
      error: () => {
        this.deleting.set(false);
        this.toast.error('Failed to delete API key.');
      },
    });
  }

  copyToClipboard(text: string, field: string = 'secret'): void {
    navigator.clipboard.writeText(text).then(() => {
      this.copiedField.set(field);
      setTimeout(() => this.copiedField.set(null), 2000);
    });
  }

  daysUntil(dateStr: string): string {
    const diff = Math.ceil((new Date(dateStr).getTime() - Date.now()) / (1000 * 60 * 60 * 24));
    if (diff <= 0) return 'today';
    return `in ${diff} day${diff === 1 ? '' : 's'}`;
  }

  formatDate(dateStr: string): string {
    return new Date(dateStr).toLocaleDateString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit',
    });
  }



  private buildOptionalFields(prefix: string, opts: { temperature: string; maxTokens: string; topP: string; systemPrompt: string }, format: 'json' | 'python' | 'js'): string {
    const lines: string[] = [];
    if (opts.temperature && opts.temperature !== '0.7') {
      const key = format === 'json' ? '"temperature"' : 'temperature';
      lines.push(`${prefix}${key}: ${opts.temperature}`);
    }
    if (opts.maxTokens && opts.maxTokens !== '4096') {
      const key = format === 'json' ? '"max_tokens"' : format === 'js' ? 'max_tokens' : '"max_tokens"';
      lines.push(`${prefix}${key}: ${opts.maxTokens}`);
    }
    if (opts.topP) {
      const key = format === 'json' ? '"top_p"' : format === 'js' ? 'top_p' : '"top_p"';
      lines.push(`${prefix}${key}: ${opts.topP}`);
    }
    if (opts.systemPrompt) {
      const escaped = opts.systemPrompt.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      const key = format === 'json' ? '"system_prompt"' : format === 'js' ? 'system_prompt' : '"system_prompt"';
      lines.push(`${prefix}${key}: "${escaped}"`);
    }
    return lines.length ? ',\n' + lines.join(',\n') : '';
  }

  private buildCurlExample(model: string, conversation: boolean, streaming: boolean, opts: { temperature: string; maxTokens: string; topP: string; systemPrompt: string }, baseUrl: string): string {
    const messages = conversation
      ? `[
      {"role": "user", "content": "What is quantum computing?"},
      {"role": "assistant", "content": "Quantum computing uses quantum bits..."},
      {"role": "user", "content": "How does it differ from classical computing?"}
    ]`
      : `[
      {"role": "user", "content": "Hello, what can you help me with?"}
    ]`;
    const streamFlag = streaming ? `,\n    "stream": true` : '';
    const optFields = this.buildOptionalFields('    ', opts, 'json');
    const curlStream = streaming ? ' \\\n  --no-buffer' : '';
    return `curl -X POST "${baseUrl}/chat/api-converse"${curlStream} \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: YOUR_API_KEY" \\
  -d '{
    "model_id": "${model}",
    "messages": ${messages}${streamFlag}${optFields}
  }'`;
  }

  private buildPythonExample(model: string, conversation: boolean, streaming: boolean, opts: { temperature: string; maxTokens: string; topP: string; systemPrompt: string }, baseUrl: string): string {
    const messages = conversation
      ? `[
        {"role": "user", "content": "What is quantum computing?"},
        {"role": "assistant", "content": "Quantum computing uses quantum bits..."},
        {"role": "user", "content": "How does it differ from classical computing?"},
    ]`
      : `[
        {"role": "user", "content": "Hello, what can you help me with?"},
    ]`;

    const optLines: string[] = [];
    if (opts.temperature && opts.temperature !== '0.7') optLines.push(`    "temperature": ${opts.temperature},`);
    if (opts.maxTokens && opts.maxTokens !== '4096') optLines.push(`    "max_tokens": ${opts.maxTokens},`);
    if (opts.topP) optLines.push(`    "top_p": ${opts.topP},`);
    if (opts.systemPrompt) {
      const escaped = opts.systemPrompt.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      optLines.push(`    "system_prompt": "${escaped}",`);
    }
    const optBlock = optLines.length ? '\n' + optLines.join('\n') : '';

    if (streaming) {
      return `import requests
import json

url = "${baseUrl}/chat/api-converse"
headers = {
    "Content-Type": "application/json",
    "X-API-Key": "YOUR_API_KEY",
}
payload = {
    "model_id": "${model}",
    "messages": ${messages},
    "stream": True,${optBlock}
}

with requests.post(url, json=payload, headers=headers, stream=True) as resp:
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if line.startswith("data: "):
            event = json.loads(line[6:])
            # Text content arrives in content_block_delta events
            if event.get("type") == "text":
                print(event["text"], end="", flush=True)
            # Reasoning models emit reasoning_delta events
            elif event.get("text") and "reasoning" in str(event):
                print(f"[thinking] {event['text']}", end="")`;
    }
    return `import requests

url = "${baseUrl}/chat/api-converse"
headers = {
    "Content-Type": "application/json",
    "X-API-Key": "YOUR_API_KEY",
}
payload = {
    "model_id": "${model}",
    "messages": ${messages},${optBlock}
}

response = requests.post(url, json=payload, headers=headers)
response.raise_for_status()
data = response.json()

print(data["content"])

# For reasoning models, the thinking process is also available:
if data.get("reasoning"):
    print(f"Reasoning: {data['reasoning']}")

# Token usage
if data.get("usage"):
    u = data["usage"]
    print(f"Tokens — in: {u.get('inputTokens')}, out: {u.get('outputTokens')}")`;
  }

  private buildJsExample(model: string, conversation: boolean, streaming: boolean, opts: { temperature: string; maxTokens: string; topP: string; systemPrompt: string }, baseUrl: string): string {
    const messages = conversation
      ? `[
      { role: "user", content: "What is quantum computing?" },
      { role: "assistant", content: "Quantum computing uses quantum bits..." },
      { role: "user", content: "How does it differ from classical computing?" },
    ]`
      : `[
      { role: "user", content: "Hello, what can you help me with?" },
    ]`;

    const optLines: string[] = [];
    if (opts.temperature && opts.temperature !== '0.7') optLines.push(`    temperature: ${opts.temperature},`);
    if (opts.maxTokens && opts.maxTokens !== '4096') optLines.push(`    max_tokens: ${opts.maxTokens},`);
    if (opts.topP) optLines.push(`    top_p: ${opts.topP},`);
    if (opts.systemPrompt) {
      const escaped = opts.systemPrompt.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
      optLines.push(`    system_prompt: "${escaped}",`);
    }
    const optBlock = optLines.length ? '\n' + optLines.join('\n') : '';

    if (streaming) {
      return `const response = await fetch("${baseUrl}/chat/api-converse", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": "YOUR_API_KEY",
  },
  body: JSON.stringify({
    model_id: "${model}",
    messages: ${messages},
    stream: true,${optBlock}
  }),
});

const reader = response.body.getReader();
const decoder = new TextDecoder();
let buffer = "";

while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });

  // Parse SSE events from the buffer
  const lines = buffer.split("\\n");
  buffer = lines.pop() ?? "";

  for (const line of lines) {
    if (line.startsWith("event: ")) {
      const eventType = line.slice(7);
      // Handle event types: message_start, content_block_delta,
      // reasoning_delta, message_stop, metadata, done
    }
    if (line.startsWith("data: ")) {
      const data = JSON.parse(line.slice(6));
      if (data.text) process.stdout.write(data.text);
    }
  }
}`;
    }
    return `const response = await fetch("${baseUrl}/chat/api-converse", {
  method: "POST",
  headers: {
    "Content-Type": "application/json",
    "X-API-Key": "YOUR_API_KEY",
  },
  body: JSON.stringify({
    model_id: "${model}",
    messages: ${messages},${optBlock}
  }),
});

const data = await response.json();
console.log(data.content);

// For reasoning models, the thinking process is also available:
if (data.reasoning) {
  console.log("Reasoning:", data.reasoning);
}

// Token usage
if (data.usage) {
  console.log(\`Tokens — in: \${data.usage.inputTokens}, out: \${data.usage.outputTokens}\`);
}`;
  }
}
