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
import { MemoriesResponse } from './models/memory.model';
import { parseIso } from '../utils/date';
import { SpinnerComponent } from '../components/spinner/spinner.component';

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
  imports: [FormsModule, NgIcon, ParsePreferencePipe, SpinnerComponent],
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
    <div class="min-h-dvh">
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
              <app-spinner size="lg" label="Checking memory status" />
              <p class="text-base/7 text-gray-600 dark:text-gray-400">Checking memory status...</p>
            </div>
          </div>
        } @else if (!isMemoryAvailable()) {
          <!-- Memory Unavailable State -->
          <div class="rounded-lg border border-state-warning-200 bg-state-warning-50 p-6 dark:border-state-warning-800 dark:bg-state-warning-900/20">
            <div class="flex items-start gap-4">
              <ng-icon name="heroExclamationTriangle" size="24" color="var(--color-state-warning-600)" class="shrink-0" />
              <div>
                <h3 class="text-base/7 font-semibold text-state-warning-800 dark:text-state-warning-200">Memory Not Available</h3>
                <p class="mt-2 text-sm/6 text-state-warning-700 dark:text-state-warning-300">
                  AgentCore Memory is not configured. Memory features require cloud mode with AGENTCORE_MEMORY_ID configured.
                </p>
                <p class="mt-2 text-sm/6 text-state-warning-600 dark:text-state-warning-400">
                  Current mode: {{ memoryStatus.value()?.mode || 'unknown' }}
                </p>
              </div>
            </div>
          </div>
        } @else {
          <!-- Memory Available - Show Content -->

          <!-- Info Banner -->
          <div class="mb-6 rounded-lg border border-state-info-200 bg-state-info-50 p-4 dark:border-state-info-800 dark:bg-state-info-900/20">
            <div class="flex items-start gap-3">
              <ng-icon name="heroInformationCircle" size="20" color="var(--color-state-info-600)" class="shrink-0" />
              <p class="text-sm/6 text-state-info-700 dark:text-state-info-300">
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
                class="w-full rounded-lg border border-gray-300 bg-white py-2 pl-10 pr-4 text-sm/6 text-gray-900 placeholder-gray-500 focus:border-primary-500 focus:outline-none focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder-gray-400 dark:focus:border-primary-400 dark:focus:ring-primary-400"
              />
            </div>
            <button
              type="button"
              (click)="performSearch()"
              class="rounded-lg bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white transition-colors hover:brightness-95"
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
                [class.border-primary-500]="activeTab() === 'all'"
                [class.text-primary-accessible]="activeTab() === 'all'"
                [class.dark:text-primary-accessible-dark]="activeTab() === 'all'"
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
                [class.border-primary-500]="activeTab() === 'preferences'"
                [class.text-primary-accessible]="activeTab() === 'preferences'"
                [class.dark:text-primary-accessible-dark]="activeTab() === 'preferences'"
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
                [class.border-primary-500]="activeTab() === 'facts'"
                [class.text-primary-accessible]="activeTab() === 'facts'"
                [class.dark:text-primary-accessible-dark]="activeTab() === 'facts'"
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
                  class="text-sm/6 font-medium text-primary-accessible hover:underline dark:text-primary-accessible-dark"
                >
                  Clear search
                </button>
              </div>
              @if (searchResults()!.memories.length > 0) {
                <div class="overflow-hidden rounded-lg border border-state-info-200 bg-state-info-50 dark:border-state-info-800 dark:bg-state-info-900/20">
                  <ul class="divide-y divide-state-info-200 dark:divide-state-info-800">
                    @for (memory of searchResults()!.memories; track memory.recordId || $index) {
                      <li class="group flex items-start gap-3 px-4 py-3">
                        <div class="min-w-0 grow">
                          @if (memory.createdAt) {
                            <p class="mb-1 text-xs/4 text-gray-400 dark:text-gray-500">{{ formatRelativeTime(memory.createdAt) }}</p>
                          }
                          <p class="text-sm/6 text-gray-900 dark:text-white">{{ memory.content }}</p>
                          @if (memory.relevanceScore) {
                            <p class="mt-1 text-xs/5 text-state-info-600 dark:text-state-info-400">
                              {{ formatScore(memory.relevanceScore) }} match
                            </p>
                          }
                        </div>
                        @if (memory.recordId) {
                          <button
                            type="button"
                            (click)="deleteMemory(memory.recordId)"
                            [disabled]="deletingMemoryId() === memory.recordId"
                            class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-state-info-100 hover:text-state-danger-500 group-hover:opacity-100 dark:hover:bg-state-info-800 dark:hover:text-state-danger-400"
                            [class.opacity-100]="deletingMemoryId() === memory.recordId"
                          >
                            @if (deletingMemoryId() === memory.recordId) {
                              <app-spinner size="sm" variant="danger" label="Deleting" />
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
                <app-spinner size="lg" label="Loading memories" />
                <p class="text-base/7 text-gray-600 dark:text-gray-400">
                  {{ isSearching() ? 'Searching memories...' : 'Loading memories...' }}
                </p>
              </div>
            </div>
          } @else if (allMemories.error()) {
            <!-- Error State -->
            <div class="rounded-lg bg-state-danger-50 p-6 dark:bg-state-danger-900/20">
              <h3 class="text-base/7 font-semibold text-state-danger-800 dark:text-state-danger-200">Error Loading Memories</h3>
              <p class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-300">
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
                      <ng-icon name="heroSparkles" size="20" color="var(--color-category-accent-skills-500)" />
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
                                class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-state-danger-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-state-danger-400"
                                [class.opacity-100]="deletingMemoryId() === memory.recordId"
                              >
                                @if (deletingMemoryId() === memory.recordId) {
                                  <app-spinner size="sm" variant="danger" label="Deleting" />
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
                      <ng-icon name="heroLightBulb" size="20" color="var(--color-accent-4-700)" />
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
                                class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-state-danger-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-state-danger-400"
                                [class.opacity-100]="deletingMemoryId() === memory.recordId"
                              >
                                @if (deletingMemoryId() === memory.recordId) {
                                  <app-spinner size="sm" variant="danger" label="Deleting" />
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
                        <ng-icon name="heroSparkles" size="16" color="var(--color-category-accent-skills-500)" class="mt-0.5 shrink-0" />
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
                            class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-state-danger-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-state-danger-400"
                            [class.opacity-100]="deletingMemoryId() === memory.recordId"
                          >
                            @if (deletingMemoryId() === memory.recordId) {
                              <app-spinner size="sm" variant="danger" label="Deleting" />
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
                        <ng-icon name="heroLightBulb" size="16" color="var(--color-accent-4-700)" class="mt-0.5 shrink-0" />
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
                            class="shrink-0 rounded p-1 text-gray-400 opacity-0 transition-opacity hover:bg-gray-100 hover:text-state-danger-500 group-hover:opacity-100 dark:hover:bg-gray-700 dark:hover:text-state-danger-400"
                            [class.opacity-100]="deletingMemoryId() === memory.recordId"
                          >
                            @if (deletingMemoryId() === memory.recordId) {
                              <app-spinner size="sm" variant="danger" label="Deleting" />
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
  // Generic accent-* identity tokens (styles/tokens/identity.css) — ten
  // hues with no fixed meaning, purely to keep hash-adjacent categories
  // visually distinct. Not vendor/state/filetype tokens: this rotation's
  // slots don't stand for anything, so it gets its own token family rather
  // than borrowing one whose name implies a meaning it doesn't carry.
  private readonly categoryColors = [
    { bg: 'bg-accent-1-100 dark:bg-accent-1-900/30', text: 'text-accent-1-700 dark:text-accent-1-300' },
    { bg: 'bg-accent-2-100 dark:bg-accent-2-900/30', text: 'text-accent-2-700 dark:text-accent-2-300' },
    { bg: 'bg-accent-3-100 dark:bg-accent-3-900/30', text: 'text-accent-3-700 dark:text-accent-3-300' },
    { bg: 'bg-accent-4-100 dark:bg-accent-4-900/30', text: 'text-accent-4-700 dark:text-accent-4-300' },
    { bg: 'bg-accent-5-100 dark:bg-accent-5-900/30', text: 'text-accent-5-700 dark:text-accent-5-300' },
    { bg: 'bg-accent-6-100 dark:bg-accent-6-900/30', text: 'text-accent-6-700 dark:text-accent-6-300' },
    { bg: 'bg-accent-7-100 dark:bg-accent-7-900/30', text: 'text-accent-7-700 dark:text-accent-7-300' },
    { bg: 'bg-accent-8-100 dark:bg-accent-8-900/30', text: 'text-accent-8-700 dark:text-accent-8-300' },
    { bg: 'bg-accent-9-100 dark:bg-accent-9-900/30', text: 'text-accent-9-700 dark:text-accent-9-300' },
    { bg: 'bg-accent-10-100 dark:bg-accent-10-900/30', text: 'text-accent-10-700 dark:text-accent-10-300' },
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
      const date = parseIso(dateString);
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
