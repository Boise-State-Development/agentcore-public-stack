import { Component, ChangeDetectionStrategy, signal, computed, inject, Pipe, PipeTransform } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroSparkles,
  heroLightBulb,
  heroMagnifyingGlass,
  heroArrowPath,
  heroExclamationTriangle,
  heroInformationCircle,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { MemoryService } from './services/memory.service';
import { MemoryRecord, MemoriesResponse } from './models/memory.model';

/**
 * Represents a parsed preference with structured display
 */
interface ParsedPreference {
  /** The main preference text to display prominently */
  mainText: string;
  /** Optional categories/tags for the preference */
  categories?: string[];
}

/**
 * Pipe to parse and format preference content for display.
 * Extracts the main preference text and optional categories,
 * hiding verbose context/metadata fields.
 */
@Pipe({
  name: 'parsePreference',
  pure: true
})
export class ParsePreferencePipe implements PipeTransform {
  transform(content: string): ParsedPreference {
    if (!content) {
      return { mainText: '' };
    }

    // Try to parse as JSON
    try {
      const parsed = JSON.parse(content);

      if (typeof parsed === 'object' && parsed !== null) {
        // Look for the main preference/value text
        const mainTextKeys = ['preference', 'value', 'text', 'content', 'description', 'setting', 'summary'];
        let mainText: string | undefined;

        // Look for categories
        let categories: string[] | undefined;

        for (const [key, val] of Object.entries(parsed)) {
          if (val === null || val === undefined) continue;

          const normalizedKey = key.toLowerCase();

          // Extract main text
          if (!mainText && mainTextKeys.some(k => normalizedKey === k)) {
            mainText = typeof val === 'string' ? val : JSON.stringify(val);
            continue;
          }

          // Extract categories (can be array or string)
          if (normalizedKey === 'categories' || normalizedKey === 'category' || normalizedKey === 'tags') {
            if (Array.isArray(val)) {
              categories = val.map(v => String(v));
            } else if (typeof val === 'string') {
              categories = [val];
            }
          }
        }

        // If we found a main text, use it
        if (mainText) {
          return { mainText, categories };
        }

        // Fallback: if no main text found but has categories, show the raw content
        // without the categories (to avoid duplicate info)
        if (categories) {
          return { mainText: content, categories };
        }
      }
    } catch {
      // Not valid JSON, return as plain text
    }

    return { mainText: content };
  }
}

@Component({
  selector: 'app-memory-dashboard-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, NgIcon, ParsePreferencePipe],
  providers: [
    provideIcons({
      heroSparkles,
      heroLightBulb,
      heroMagnifyingGlass,
      heroArrowPath,
      heroExclamationTriangle,
      heroInformationCircle,
      heroTrash,
    })
  ],
  template: `
    <div class="min-h-dvh bg-gray-100 dark:bg-gray-900">
      <div class="mx-auto max-w-3xl px-4 py-8">
        <!-- Header -->
        <div class="mb-8">
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">Memories</h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            View what the AI has learned about you across conversations
          </p>
        </div>

        <!-- Status Check -->
        @if (memoryStatus.isLoading()) {
          <div class="flex items-center justify-center py-12">
            <div class="text-center">
              <div class="mb-4 inline-block size-8 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent"></div>
              <p class="text-base/7 text-gray-600 dark:text-gray-400">Checking memory status...</p>
            </div>
          </div>
        } @else if (!isMemoryAvailable()) {
          <!-- Memory Unavailable State -->
          <div class="rounded-lg border border-yellow-200 bg-yellow-50 p-6 dark:border-yellow-800 dark:bg-yellow-900/20">
            <div class="flex items-start gap-4">
              <ng-icon name="heroExclamationTriangle" size="24" color="var(--color-yellow-600)" class="shrink-0" />
              <div>
                <h3 class="text-base/7 font-semibold text-yellow-800 dark:text-yellow-200">Memory Not Available</h3>
                <p class="mt-2 text-sm/6 text-yellow-700 dark:text-yellow-300">
                  AgentCore Memory is not configured. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured.
                </p>
                <p class="mt-2 text-sm/6 text-yellow-600 dark:text-yellow-400">
                  Current mode: {{ memoryStatus.value()?.mode || 'unknown' }}
                </p>
              </div>
            </div>
          </div>
        } @else {
          <!-- Memory Available - Show Content -->

          <!-- Info Banner -->
          <div class="mb-6 rounded-lg border border-blue-200 bg-blue-50 p-4 dark:border-blue-800 dark:bg-blue-900/20">
            <div class="flex items-start gap-3">
              <ng-icon name="heroInformationCircle" size="20" color="var(--color-blue-600)" class="shrink-0" />
              <p class="text-sm/6 text-blue-700 dark:text-blue-300">
                These memories are automatically extracted from your conversations to personalize responses.
                They help the AI remember your preferences and context across sessions.
              </p>
            </div>
          </div>

          <!-- Search and Refresh Controls -->
          <div class="mb-6 flex flex-wrap items-center gap-4">
            <div class="relative grow">
              <ng-icon
                name="heroMagnifyingGlass"
                size="20"
                color="var(--color-gray-400)"
                class="absolute left-3 top-1/2 -translate-y-1/2"
              />
              <input
                type="text"
                [value]="searchQuery()"
                (input)="searchQuery.set($any($event.target).value)"
                (keyup.enter)="performSearch()"
                placeholder="Search your memories..."
                class="w-full rounded-lg border border-gray-300 bg-white py-2 pl-10 pr-4 text-sm/6 text-gray-900 placeholder-gray-500 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder-gray-400 dark:focus:border-blue-400 dark:focus:ring-blue-400"
              />
            </div>
            <button
              type="button"
              (click)="performSearch()"
              class="rounded-lg bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white transition-colors hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600"
            >
              Search
            </button>
            <button
              type="button"
              (click)="refresh()"
              class="flex items-center gap-2 rounded-lg border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              <ng-icon name="heroArrowPath" size="16" />
              Refresh
            </button>
          </div>

          <!-- Tab Navigation -->
          <div class="mb-6 border-b border-gray-200 dark:border-gray-700">
            <nav class="-mb-px flex gap-6">
              <button
                type="button"
                (click)="activeTab.set('all')"
                [class.border-blue-500]="activeTab() === 'all'"
                [class.text-blue-600]="activeTab() === 'all'"
                [class.dark:text-blue-400]="activeTab() === 'all'"
                [class.border-transparent]="activeTab() !== 'all'"
                [class.text-gray-500]="activeTab() !== 'all'"
                [class.dark:text-gray-400]="activeTab() !== 'all'"
                class="border-b-2 px-1 pb-4 text-sm/6 font-medium transition-colors hover:border-gray-300 hover:text-gray-700 dark:hover:text-gray-300"
              >
                All Memories
              </button>
              <button
                type="button"
                (click)="activeTab.set('preferences')"
                [class.border-blue-500]="activeTab() === 'preferences'"
                [class.text-blue-600]="activeTab() === 'preferences'"
                [class.dark:text-blue-400]="activeTab() === 'preferences'"
                [class.border-transparent]="activeTab() !== 'preferences'"
                [class.text-gray-500]="activeTab() !== 'preferences'"
                [class.dark:text-gray-400]="activeTab() !== 'preferences'"
                class="border-b-2 px-1 pb-4 text-sm/6 font-medium transition-colors hover:border-gray-300 hover:text-gray-700 dark:hover:text-gray-300"
              >
                Preferences ({{ preferencesCount() }})
              </button>
              <button
                type="button"
                (click)="activeTab.set('facts')"
                [class.border-blue-500]="activeTab() === 'facts'"
                [class.text-blue-600]="activeTab() === 'facts'"
                [class.dark:text-blue-400]="activeTab() === 'facts'"
                [class.border-transparent]="activeTab() !== 'facts'"
                [class.text-gray-500]="activeTab() !== 'facts'"
                [class.dark:text-gray-400]="activeTab() !== 'facts'"
                class="border-b-2 px-1 pb-4 text-sm/6 font-medium transition-colors hover:border-gray-300 hover:text-gray-700 dark:hover:text-gray-300"
              >
                Facts ({{ factsCount() }})
              </button>
            </nav>
          </div>

          <!-- Search Results (shown at top when searching) -->
          @if (searchResults()) {
            <div class="mb-6">
              <div class="mb-4 flex items-center justify-between">
                <h2 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
                  Search Results for "{{ lastSearchQuery() }}"
                </h2>
                <button
                  type="button"
                  (click)="clearSearch()"
                  class="text-sm/6 font-medium text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300"
                >
                  Clear search
                </button>
              </div>
              @if (searchResults()!.memories.length > 0) {
                <div class="overflow-hidden rounded-lg border border-blue-200 bg-blue-50 dark:border-blue-800 dark:bg-blue-900/20">
                  <ul class="divide-y divide-blue-200 dark:divide-blue-800">
                    @for (memory of searchResults()!.memories; track memory.recordId || $index) {
                      <li class="group flex items-start gap-3 px-4 py-3">
                        <div class="min-w-0 grow">
                          @if (memory.createdAt) {
                            <p class="mb-1 text-xs/4 text-gray-400 dark:text-gray-500">{{ formatRelativeTime(memory.createdAt) }}</p>
                          }
                          <p class="text-sm/6 text-gray-900 dark:text-white">{{ memory.content }}</p>
                          @if (memory.relevanceScore) {
                            <p class="mt-1 text-xs/5 text-blue-600 dark:text-blue-400">
                              {{ formatScore(memory.relevanceScore) }} match
                            </p>
                          }
                        </div>
                        @if (memory.recordId) {
                          <button
                            type="button"
                            (click)="deleteMemory(memory.recordId)"
                            [disabled]="deletingMemoryId() === memory.recordId"
                            class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-blue-100 hover:text-red-500 group-hover:opacity-100 dark:hover:bg-blue-800 dark:hover:text-red-400"
                            [class.opacity-100]="deletingMemoryId() === memory.recordId"
                          >
                            @if (deletingMemoryId() === memory.recordId) {
                              <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-red-500"></div>
                            } @else {
                              <ng-icon name="heroTrash" size="16" />
                            }
                          </button>
                        }
                      </li>
                    }
                  </ul>
                </div>
              } @else {
                <div class="rounded-lg border border-gray-200 bg-white p-8 text-center dark:border-gray-700 dark:bg-gray-800">
                  <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                    No memories found matching your search.
                  </p>
                </div>
              }
            </div>
          }

          <!-- Loading State -->
          @if (allMemories.isLoading() || isSearching()) {
            <div class="flex items-center justify-center py-12">
              <div class="text-center">
                <div class="mb-4 inline-block size-8 animate-spin rounded-full border-4 border-solid border-blue-600 border-r-transparent"></div>
                <p class="text-base/7 text-gray-600 dark:text-gray-400">
                  {{ isSearching() ? 'Searching memories...' : 'Loading memories...' }}
                </p>
              </div>
            </div>
          } @else if (allMemories.error()) {
            <!-- Error State -->
            <div class="rounded-lg bg-red-50 p-6 dark:bg-red-900/20">
              <h3 class="text-base/7 font-semibold text-red-800 dark:text-red-200">Error Loading Memories</h3>
              <p class="mt-2 text-sm/6 text-red-700 dark:text-red-300">
                {{ allMemories.error() }}
              </p>
            </div>
          } @else {
            <!-- Memory Content -->
            @if (activeTab() === 'all') {
              <!-- All Memories View -->
              <div class="space-y-8">
                <!-- Preferences Section -->
                @if (preferences().length > 0) {
                  <section>
                    <h2 class="mb-4 flex items-center gap-2 text-lg/7 font-semibold text-gray-900 dark:text-white">
                      <ng-icon name="heroSparkles" size="20" color="var(--color-purple-500)" />
                      Preferences
                    </h2>
                    <div class="overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                      <ul class="divide-y divide-gray-200 dark:divide-gray-700">
                        @for (memory of preferences(); track memory.recordId || $index) {
                          @let parsed = memory.content | parsePreference;
                          <li class="group flex items-start gap-3 px-4 py-3">
                            <div class="min-w-0 grow">
                              @if (memory.createdAt) {
                                <p class="mb-1 text-xs/4 text-gray-400 dark:text-gray-500">{{ formatRelativeTime(memory.createdAt) }}</p>
                              }
                              <p class="text-sm/6 text-gray-900 dark:text-white">{{ parsed.mainText }}</p>
                              @if ((parsed.categories && parsed.categories.length > 0) || memory.relevanceScore) {
                                <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                                  @if (parsed.categories && parsed.categories.length > 0) {
                                    @for (cat of parsed.categories; track cat) {
                                      @let color = getCategoryColor(cat);
                                      <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs/5 font-medium" [class]="color.bg + ' ' + color.text">
                                        {{ cat }}
                                      </span>
                                    }
                                  }
                                  @if (memory.relevanceScore) {
                                    <span class="text-xs/5 text-gray-400 dark:text-gray-500">
                                      {{ formatScore(memory.relevanceScore) }} match
                                    </span>
                                  }
                                </div>
                              }
                            </div>
                            @if (memory.recordId) {
                              <button
                                type="button"
                                (click)="deleteMemory(memory.recordId)"
                                [disabled]="deletingMemoryId() === memory.recordId"
                                class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-red-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-red-400"
                                [class.opacity-100]="deletingMemoryId() === memory.recordId"
                              >
                                @if (deletingMemoryId() === memory.recordId) {
                                  <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-red-500"></div>
                                } @else {
                                  <ng-icon name="heroTrash" size="16" />
                                }
                              </button>
                            }
                          </li>
                        }
                      </ul>
                    </div>
                  </section>
                }

                <!-- Facts Section -->
                @if (facts().length > 0) {
                  <section>
                    <h2 class="mb-4 flex items-center gap-2 text-lg/7 font-semibold text-gray-900 dark:text-white">
                      <ng-icon name="heroLightBulb" size="20" color="var(--color-yellow-500)" />
                      Facts
                    </h2>
                    <div class="overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                      <ul class="divide-y divide-gray-200 dark:divide-gray-700">
                        @for (memory of facts(); track memory.recordId || $index) {
                          <li class="group flex items-start gap-3 px-4 py-3">
                            <div class="min-w-0 grow">
                              @if (memory.createdAt) {
                                <p class="mb-1 text-xs/4 text-gray-400 dark:text-gray-500">{{ formatRelativeTime(memory.createdAt) }}</p>
                              }
                              <p class="text-sm/6 text-gray-900 dark:text-white">{{ memory.content }}</p>
                              @if (memory.relevanceScore) {
                                <p class="mt-1 text-xs/5 text-gray-400 dark:text-gray-500">
                                  {{ formatScore(memory.relevanceScore) }} match
                                </p>
                              }
                            </div>
                            @if (memory.recordId) {
                              <button
                                type="button"
                                (click)="deleteMemory(memory.recordId)"
                                [disabled]="deletingMemoryId() === memory.recordId"
                                class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-red-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-red-400"
                                [class.opacity-100]="deletingMemoryId() === memory.recordId"
                              >
                                @if (deletingMemoryId() === memory.recordId) {
                                  <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-red-500"></div>
                                } @else {
                                  <ng-icon name="heroTrash" size="16" />
                                }
                              </button>
                            }
                          </li>
                        }
                      </ul>
                    </div>
                  </section>
                }

                <!-- Empty State -->
                @if (preferences().length === 0 && facts().length === 0) {
                  <div class="rounded-lg border border-gray-200 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
                    <ng-icon name="heroSparkles" size="48" color="var(--color-gray-400)" class="mx-auto" />
                    <h3 class="mt-4 text-base/7 font-semibold text-gray-900 dark:text-white">No memories yet</h3>
                    <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
                      Start having conversations and the AI will learn about your preferences and context.
                    </p>
                  </div>
                }
              </div>
            } @else if (activeTab() === 'preferences') {
              <!-- Preferences Only View -->
              @if (preferences().length > 0) {
                <div class="overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                  <ul class="divide-y divide-gray-200 dark:divide-gray-700">
                    @for (memory of preferences(); track memory.recordId || $index) {
                      @let parsed = memory.content | parsePreference;
                      <li class="group flex items-start gap-3 px-4 py-3">
                        <ng-icon name="heroSparkles" size="16" color="var(--color-purple-500)" class="mt-0.5 shrink-0" />
                        <div class="min-w-0 grow">
                          @if (memory.createdAt) {
                            <p class="mb-1 text-xs/4 text-gray-400 dark:text-gray-500">{{ formatRelativeTime(memory.createdAt) }}</p>
                          }
                          <p class="text-sm/6 text-gray-900 dark:text-white">{{ parsed.mainText }}</p>
                          @if ((parsed.categories && parsed.categories.length > 0) || memory.relevanceScore) {
                            <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1.5">
                              @if (parsed.categories && parsed.categories.length > 0) {
                                @for (cat of parsed.categories; track cat) {
                                  @let color = getCategoryColor(cat);
                                  <span class="inline-flex items-center rounded-full px-2 py-0.5 text-xs/5 font-medium" [class]="color.bg + ' ' + color.text">
                                    {{ cat }}
                                  </span>
                                }
                              }
                              @if (memory.relevanceScore) {
                                <span class="text-xs/5 text-gray-400 dark:text-gray-500">
                                  {{ formatScore(memory.relevanceScore) }} match
                                </span>
                              }
                            </div>
                          }
                        </div>
                        @if (memory.recordId) {
                          <button
                            type="button"
                            (click)="deleteMemory(memory.recordId)"
                            [disabled]="deletingMemoryId() === memory.recordId"
                            class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-red-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-red-400"
                            [class.opacity-100]="deletingMemoryId() === memory.recordId"
                          >
                            @if (deletingMemoryId() === memory.recordId) {
                              <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-red-500"></div>
                            } @else {
                              <ng-icon name="heroTrash" size="16" />
                            }
                          </button>
                        }
                      </li>
                    }
                  </ul>
                </div>
              } @else {
                <div class="rounded-lg border border-gray-200 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
                  <ng-icon name="heroSparkles" size="48" color="var(--color-gray-400)" class="mx-auto" />
                  <h3 class="mt-4 text-base/7 font-semibold text-gray-900 dark:text-white">No preferences learned yet</h3>
                  <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
                    The AI will learn your preferences as you have more conversations.
                  </p>
                </div>
              }
            } @else {
              <!-- Facts Only View -->
              @if (facts().length > 0) {
                <div class="overflow-hidden rounded-lg border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
                  <ul class="divide-y divide-gray-200 dark:divide-gray-700">
                    @for (memory of facts(); track memory.recordId || $index) {
                      <li class="group flex items-start gap-3 px-4 py-3">
                        <ng-icon name="heroLightBulb" size="16" color="var(--color-yellow-500)" class="mt-0.5 shrink-0" />
                        <div class="min-w-0 grow">
                          @if (memory.createdAt) {
                            <p class="mb-1 text-xs/4 text-gray-400 dark:text-gray-500">{{ formatRelativeTime(memory.createdAt) }}</p>
                          }
                          <p class="text-sm/6 text-gray-900 dark:text-white">{{ memory.content }}</p>
                          @if (memory.relevanceScore) {
                            <p class="mt-1 text-xs/5 text-gray-400 dark:text-gray-500">
                              {{ formatScore(memory.relevanceScore) }} match
                            </p>
                          }
                        </div>
                        @if (memory.recordId) {
                          <button
                            type="button"
                            (click)="deleteMemory(memory.recordId)"
                            [disabled]="deletingMemoryId() === memory.recordId"
                            class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-red-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-red-400"
                            [class.opacity-100]="deletingMemoryId() === memory.recordId"
                          >
                            @if (deletingMemoryId() === memory.recordId) {
                              <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-red-500"></div>
                            } @else {
                              <ng-icon name="heroTrash" size="16" />
                            }
                          </button>
                        }
                      </li>
                    }
                  </ul>
                </div>
              } @else {
                <div class="rounded-lg border border-gray-200 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
                  <ng-icon name="heroLightBulb" size="48" color="var(--color-gray-400)" class="mx-auto" />
                  <h3 class="mt-4 text-base/7 font-semibold text-gray-900 dark:text-white">No facts learned yet</h3>
                  <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400">
                    The AI will learn facts about you as you have more conversations.
                  </p>
                </div>
              }
            }

          }
        }
      </div>
    </div>
  `
})
export class MemoryDashboardPage {
  private memoryService = inject(MemoryService);

  // Resources from service
  readonly memoryStatus = this.memoryService.memoryStatus;
  readonly allMemories = this.memoryService.allMemories;

  // UI State
  readonly activeTab = signal<'all' | 'preferences' | 'facts'>('all');
  readonly searchQuery = signal('');
  readonly lastSearchQuery = signal('');
  readonly isSearching = signal(false);
  readonly searchResults = signal<MemoriesResponse | null>(null);
  readonly deletingMemoryId = signal<string | null>(null);

  // Computed values
  readonly isMemoryAvailable = computed(() => {
    const status = this.memoryStatus.value();
    return status?.available === true;
  });

  readonly preferences = computed(() => {
    const data = this.allMemories.value();
    return data?.preferences?.memories ?? [];
  });

  readonly facts = computed(() => {
    const data = this.allMemories.value();
    return data?.facts?.memories ?? [];
  });

  readonly preferencesCount = computed(() => this.preferences().length);
  readonly factsCount = computed(() => this.facts().length);

  /**
   * Perform semantic search across memories
   */
  async performSearch(): Promise<void> {
    const query = this.searchQuery().trim();
    if (!query) {
      this.searchResults.set(null);
      return;
    }

    this.isSearching.set(true);
    this.lastSearchQuery.set(query);

    try {
      const results = await this.memoryService.searchMemories({
        query,
        topK: 20
      });
      this.searchResults.set(results);
    } catch (error) {
      console.error('Search failed:', error);
      this.searchResults.set(null);
    } finally {
      this.isSearching.set(false);
    }
  }

  /**
   * Refresh all memory data
   */
  refresh(): void {
    this.searchResults.set(null);
    this.searchQuery.set('');
    this.memoryService.reload();
  }

  /**
   * Clear search results and return to normal view
   */
  clearSearch(): void {
    this.searchResults.set(null);
    this.searchQuery.set('');
  }

  /**
   * Delete a memory record
   */
  async deleteMemory(recordId: string): Promise<void> {
    if (this.deletingMemoryId()) return;

    this.deletingMemoryId.set(recordId);

    try {
      await this.memoryService.deleteMemory(recordId);
      // Reload memories after successful deletion
      this.memoryService.reload();
    } catch (error) {
      console.error('Failed to delete memory:', error);
    } finally {
      this.deletingMemoryId.set(null);
    }
  }

  /**
   * Format relevance score as percentage
   */
  formatScore(score: number): string {
    return `${(score * 100).toFixed(0)}%`;
  }

  /**
   * Color palette for category badges - works well in both light and dark modes
   */
  private readonly categoryColors = [
    { bg: 'bg-purple-100 dark:bg-purple-900/30', text: 'text-purple-700 dark:text-purple-300' },
    { bg: 'bg-blue-100 dark:bg-blue-900/30', text: 'text-blue-700 dark:text-blue-300' },
    { bg: 'bg-green-100 dark:bg-green-900/30', text: 'text-green-700 dark:text-green-300' },
    { bg: 'bg-amber-100 dark:bg-amber-900/30', text: 'text-amber-700 dark:text-amber-300' },
    { bg: 'bg-rose-100 dark:bg-rose-900/30', text: 'text-rose-700 dark:text-rose-300' },
    { bg: 'bg-cyan-100 dark:bg-cyan-900/30', text: 'text-cyan-700 dark:text-cyan-300' },
    { bg: 'bg-indigo-100 dark:bg-indigo-900/30', text: 'text-indigo-700 dark:text-indigo-300' },
    { bg: 'bg-teal-100 dark:bg-teal-900/30', text: 'text-teal-700 dark:text-teal-300' },
    { bg: 'bg-orange-100 dark:bg-orange-900/30', text: 'text-orange-700 dark:text-orange-300' },
    { bg: 'bg-pink-100 dark:bg-pink-900/30', text: 'text-pink-700 dark:text-pink-300' },
  ];

  /**
   * Get a consistent color for a category based on its name hash
   */
  getCategoryColor(category: string): { bg: string; text: string } {
    // Simple hash function to get consistent color for same category
    let hash = 0;
    for (let i = 0; i < category.length; i++) {
      hash = ((hash << 5) - hash) + category.charCodeAt(i);
      hash = hash & hash; // Convert to 32-bit integer
    }
    const index = Math.abs(hash) % this.categoryColors.length;
    return this.categoryColors[index];
  }

  /**
   * Format a date string as relative time (e.g., "Learned 2 days ago", "Learned just now")
   */
  formatRelativeTime(dateString: string | undefined): string {
    if (!dateString) return '';

    try {
      const date = new Date(dateString);
      const now = new Date();
      const diffMs = now.getTime() - date.getTime();
      const diffSecs = Math.floor(diffMs / 1000);
      const diffMins = Math.floor(diffSecs / 60);
      const diffHours = Math.floor(diffMins / 60);
      const diffDays = Math.floor(diffHours / 24);
      const diffWeeks = Math.floor(diffDays / 7);
      const diffMonths = Math.floor(diffDays / 30);

      if (diffSecs < 60) return 'Learned just now';
      if (diffMins < 60) return diffMins === 1 ? 'Learned 1 minute ago' : `Learned ${diffMins} minutes ago`;
      if (diffHours < 24) return diffHours === 1 ? 'Learned 1 hour ago' : `Learned ${diffHours} hours ago`;
      if (diffDays < 7) return diffDays === 1 ? 'Learned 1 day ago' : `Learned ${diffDays} days ago`;
      if (diffWeeks < 4) return diffWeeks === 1 ? 'Learned 1 week ago' : `Learned ${diffWeeks} weeks ago`;
      if (diffMonths < 12) return diffMonths === 1 ? 'Learned 1 month ago' : `Learned ${diffMonths} months ago`;

      const diffYears = Math.floor(diffMonths / 12);
      return diffYears === 1 ? 'Learned 1 year ago' : `Learned ${diffYears} years ago`;
    } catch {
      return '';
    }
  }
}
