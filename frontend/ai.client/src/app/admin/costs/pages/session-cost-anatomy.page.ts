import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from '@angular/core';
import { DatePipe } from '@angular/common';
import { HttpErrorResponse } from '@angular/common/http';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroCheck,
  heroChevronDown,
  heroClipboardDocument,
} from '@ng-icons/heroicons/outline';
import { AdminCostHttpService } from '../services/admin-cost-http.service';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { ContextTrajectoryChartComponent } from '../components/context-trajectory-chart.component';
import { CacheStatus, DiagnosisSeverity, SessionDiagnosis,
  CompactionEvent,
  SessionCallRow,
} from '../models';
import {
  AnatomyRow,
  FINGERPRINT_KEYS,
  FINGERPRINT_LABELS,
  FingerprintKey,
  buildAnatomyRows,
  truncateHash,
} from './session-cost-anatomy.util';
import {
  SEVERITY_LABELS,
  buildDiagnosticJson,
  downRate,
  feedbackByTurnClassLine,
  feedbackRetryLine,
  formatBytes,
  formatEvidenceValue,
  humanizeKey,
  severityChipClass,
} from './session-profile.util';

/**
 * Admin drill-down: per-model-call cost anatomy for one session.
 *
 * The forensic view for prompt-cache diagnostics — each call row carries its
 * cache status and prefix-fingerprint hashes, and the hash that flipped
 * between consecutive calls names the cache-buster (the diagnosis on
 * `miss_avoidable` and `partial_miss` rows).
 *
 * A `partial_miss` row is the one to read carefully: it *did* read from cache,
 * so it looks healthy at a glance, but the read is a leading segment (tools +
 * system) against a re-write of everything after it. Its Read column stays
 * flat turn after turn while Write tracks the whole conversation.
 */
@Component({
  selector: 'app-session-cost-anatomy',
  imports: [RouterLink, NgIcon, DatePipe, SpinnerComponent, ContextTrajectoryChartComponent],
  providers: [provideIcons({ heroArrowLeft, heroCheck, heroChevronDown, heroClipboardDocument })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div>
      <!-- Back links: to the user when the profile knows them, else to analytics -->
      <div class="mb-6 flex flex-wrap items-center gap-x-6 gap-y-2">
        @if (profileUserId(); as userId) {
          <a
            [routerLink]="['/admin/users', userId]"
            class="inline-flex items-center gap-2 rounded-2xl text-sm/6 font-medium text-gray-600 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
          >
            <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
            Back to user
          </a>
        }
        <a
          routerLink="/admin/costs"
          class="inline-flex items-center gap-2 rounded-2xl text-sm/6 font-medium text-gray-600 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
        >
          @if (!profileUserId()) {
            <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          }
          Cost Analytics
        </a>
      </div>

      <!-- Page Header -->
      <div class="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Session Cost Anatomy</h1>
          <p class="mt-1 truncate font-mono text-sm/6 text-gray-600 dark:text-gray-400" [title]="id()">
            {{ id() }}
          </p>
        </div>
        @if (profileResource.hasValue()) {
          <!--
            The whole payload is content-free by construction (server-enforced),
            so it is safe to hand to a model for a second opinion — that is the
            literal purpose of this button.
          -->
          <button
            type="button"
            (click)="copyDiagnosticJson()"
            class="inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-200 dark:hover:bg-gray-700"
            [attr.aria-live]="'polite'"
          >
            <ng-icon [name]="copied() ? 'heroCheck' : 'heroClipboardDocument'" class="size-4" aria-hidden="true" />
            {{ copied() ? 'Copied' : 'Copy diagnostic JSON' }}
          </button>
        }
      </div>

      <!-- ── Profile: what the user was doing, content-free ── -->
      @if (profileResource.hasValue()) {
        @let profile = profileResource.value();
        <section class="mb-6" aria-labelledby="profile-heading">
          <h2 id="profile-heading" class="sr-only">Conversation profile</h2>
          <div class="grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-5">
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Messages</p>
              <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ profile.session.messageCount ?? '—' }}</p>
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Model calls</p>
              <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ profile.callCount }}</p>
              @if (modelMixLine(); as mix) {
                <p class="mt-1 truncate text-xs/5 text-gray-500 dark:text-gray-400" [title]="mix">{{ mix }}</p>
              }
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Tool calls</p>
              @if (profile.dataCoverage.toolCensus) {
                <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ profile.session.toolCallCount ?? 0 }}</p>
                @if ((profile.session.toolErrorCount ?? 0) > 0) {
                  <p class="mt-1 text-xs/5 text-state-danger-600 dark:text-state-danger-400">{{ profile.session.toolErrorCount }} failed</p>
                }
              } @else {
                <p class="mt-1 text-lg/7 font-semibold text-gray-400 dark:text-gray-500">—</p>
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">not tracked</p>
              }
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Attachments</p>
              <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ profile.attachments.count }}</p>
              @if (documentsLine(); as documents) {
                <!-- How the documents were actually consumed: calls with the
                     full document inline vs. a digest only, and what
                     document_read pulled back. The digest-vs-full split is
                     the quantity the offload work is judged on. -->
                <p class="mt-1 truncate text-xs/5 text-gray-500 dark:text-gray-400" [title]="documents">{{ documents }}</p>
              } @else if (profile.attachments.count > 0) {
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">{{ bytes(profile.attachments.totalBytes) }}</p>
              }
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Compactions</p>
              @if (profile.dataCoverage.compactionCount) {
                <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ profile.session.compactionCount ?? 0 }}</p>
                @if (compactionEventsLine(); as events) {
                  <p class="mt-1 truncate text-xs/5 text-gray-500 dark:text-gray-400" [title]="events">{{ events }}</p>
                }
              } @else {
                <p class="mt-1 text-lg/7 font-semibold text-gray-400 dark:text-gray-500">—</p>
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                  {{ profile.session.summarizedTurns ? profile.session.summarizedTurns + ' turns summarized' : 'not tracked' }}
                </p>
              }
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Peak context</p>
              <p
                class="mt-1 text-lg/7 font-semibold"
                [class]="
                  (profile.peakContextTokens ?? 0) > profile.compactionThreshold
                    ? 'text-state-danger-600 dark:text-state-danger-400'
                    : 'text-gray-900 dark:text-white'
                "
              >
                {{ profile.peakContextTokens != null ? formatTokens(profile.peakContextTokens) : '—' }}
              </p>
              @if (profile.session.contextWindow) {
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">of {{ formatTokens(profile.session.contextWindow) }} window</p>
              }
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Static prefix</p>
              @if (profile.dataCoverage.prefixTokens && profile.prefixTokens; as prefix) {
                <!-- Read on every call, re-written on every cold turn: the
                     part of the prompt the user never typed. Tool schemas are
                     the part that curation can shrink. -->
                <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ formatTokens(prefix.system + prefix.tools) }}</p>
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                  {{ formatTokens(prefix.system) }} system · {{ formatTokens(prefix.tools) }} tools
                </p>
              } @else {
                <p class="mt-1 text-lg/7 font-semibold text-gray-400 dark:text-gray-500">—</p>
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">not tracked</p>
              }
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Window trims</p>
              @if (profile.dataCoverage.windowTrim) {
                <!-- Each trim moves the front of the history, which re-writes
                     the cached prefix on the next call. -->
                <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">{{ profile.windowTrimCalls ?? 0 }}</p>
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                  {{ profile.windowRemovedMessages ?? 0 }} messages removed
                </p>
              } @else {
                <p class="mt-1 text-lg/7 font-semibold text-gray-400 dark:text-gray-500">—</p>
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">not tracked</p>
              }
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Write : read</p>
              <p
                class="mt-1 text-lg/7 font-semibold"
                [class]="
                  (profile.writeReadRatio ?? 0) > 3
                    ? 'text-state-danger-600 dark:text-state-danger-400'
                    : 'text-gray-900 dark:text-white'
                "
              >
                {{ profile.writeReadRatio != null ? profile.writeReadRatio.toFixed(2) : '—' }}
              </p>
              <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">healthy ≈ 0.1</p>
            </div>
            <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
              <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Feedback</p>
              @if (profile.dataCoverage.feedback && profile.feedback; as feedback) {
                <!-- The outcome signal: thumbs joined to the call they rate.
                     Headline is the down-thumb rate; the line under it splits
                     it by document turn class (offload spec §6.1) once the
                     cost rows carry one. -->
                <p
                  class="mt-1 text-lg/7 font-semibold"
                  [class]="
                    (feedbackDownRate() ?? 0) >= 50
                      ? 'text-state-danger-600 dark:text-state-danger-400'
                      : 'text-gray-900 dark:text-white'
                  "
                >
                  {{ feedbackDownRate() != null ? feedbackDownRate() + '% down' : '—' }}
                </p>
                <p class="mt-1 truncate text-xs/5 text-gray-500 dark:text-gray-400" [title]="feedbackTurnClassLine() ?? ''">
                  {{ feedback.up }} up · {{ feedback.down }} down
                  @if (feedbackTurnClassLine(); as byClass) {
                    · {{ byClass }}
                  } @else {
                    · turn class not tracked
                  }
                </p>
                @if (feedbackRetryLine(); as retries) {
                  <!-- Quality in dollars: the thumbed answer plus the retry it took. -->
                  <p class="mt-0.5 text-xs/5 text-gray-500 dark:text-gray-400">{{ retries }}</p>
                }
              } @else {
                <p class="mt-1 text-lg/7 font-semibold text-gray-400 dark:text-gray-500">—</p>
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">not tracked</p>
              }
            </div>
          </div>
        </section>

        <!-- ── Diagnoses ── -->
        <section class="mb-6" aria-labelledby="diagnoses-heading">
          <div class="rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800">
            <div class="flex items-center justify-between border-b border-gray-200 px-4 py-3 dark:border-gray-700">
              <h2 id="diagnoses-heading" class="text-sm/6 font-semibold text-gray-900 dark:text-white">
                Diagnoses
                <span class="ml-2 font-normal text-gray-500 dark:text-gray-400">{{ profile.diagnoses.length }}</span>
              </h2>
              <p class="text-xs/5 text-gray-500 dark:text-gray-400">Named, computable findings — evidence is the row's own numbers</p>
            </div>
            @if (profile.diagnoses.length === 0) {
              <p class="px-4 py-6 text-sm/6 text-gray-500 dark:text-gray-400">Nothing fired. This conversation looks healthy on every rule.</p>
            } @else {
              <ul class="divide-y divide-gray-200 dark:divide-gray-700">
                @for (d of profile.diagnoses; track d.code) {
                  <li>
                    <button
                      type="button"
                      (click)="toggleDiagnosis(d.code)"
                      [attr.aria-expanded]="isDiagnosisOpen(d.code)"
                      [attr.aria-controls]="'diag-' + d.code"
                      class="flex w-full items-start gap-3 px-4 py-3 text-left hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-[-2px] focus-visible:outline-primary-500 dark:hover:bg-gray-700/40"
                    >
                      <span [class]="chipClass(d.severity)">{{ severityLabel(d.severity) }}</span>
                      <span class="min-w-0 flex-1">
                        <span class="block text-sm/6 font-medium text-gray-900 dark:text-white">{{ d.headline }}</span>
                        <span class="block font-mono text-xs/5 text-gray-500 dark:text-gray-400">{{ d.code }}</span>
                      </span>
                      <ng-icon
                        name="heroChevronDown"
                        class="mt-1 size-4 shrink-0 text-gray-400 transition-transform duration-150"
                        [class.rotate-180]="isDiagnosisOpen(d.code)"
                        aria-hidden="true"
                      />
                    </button>
                    @if (isDiagnosisOpen(d.code)) {
                      <div [id]="'diag-' + d.code" class="bg-gray-50 px-4 py-3 dark:bg-gray-900/40">
                        <p class="text-sm/6 text-gray-700 dark:text-gray-300">{{ d.suggestion }}</p>
                        <dl class="mt-3 grid grid-cols-1 gap-x-8 gap-y-2 sm:grid-cols-2 lg:grid-cols-3">
                          @for (entry of evidenceEntries(d); track entry.key) {
                            <div>
                              <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">{{ entry.label }}</dt>
                              <dd class="mt-0.5 break-words font-mono text-xs/5 text-gray-700 dark:text-gray-300">{{ entry.value }}</dd>
                            </div>
                          }
                        </dl>
                        <p class="mt-3 font-mono text-xs/5 text-gray-500 dark:text-gray-400">ref: {{ d.ref }}</p>
                      </div>
                    }
                  </li>
                }
              </ul>
            }
          </div>
        </section>

        <!-- ── Context trajectory ── -->
        <section class="mb-6" aria-labelledby="trajectory-heading">
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <h2 id="trajectory-heading" class="text-sm/6 font-semibold text-gray-900 dark:text-white">Context per call</h2>
            <p class="mb-3 text-xs/5 text-gray-500 dark:text-gray-400">
              Tokens the model had to read on each call, against the compaction threshold. A plateau above the line painted as partial misses is the spiral.
            </p>
            <app-context-trajectory-chart
              [points]="profile.contextTrajectory"
              [threshold]="profile.compactionThreshold"
              [contextWindow]="profile.session.contextWindow"
            />
          </div>
        </section>
      } @else if (profileResource.error() && !profileNotFound()) {
        <div
          class="mb-6 rounded-2xl border border-state-warning-200 bg-state-warning-50 px-4 py-3 text-sm/6 text-state-warning-800 dark:border-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-200"
        >
          The conversation profile could not be loaded; the call anatomy below is unaffected.
          <button type="button" (click)="profileResource.reload()" class="ml-2 font-medium underline hover:no-underline">Retry</button>
        </div>
      }

      @if (anatomyResource.isLoading()) {
        <!-- Loading State -->
        <div class="flex h-64 items-center justify-center">
          <div class="flex flex-col items-center gap-4">
            <app-spinner size="xl" label="Loading session cost anatomy" />
            <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading session cost anatomy…</p>
          </div>
        </div>
      } @else if (notFound()) {
        <!-- 404: session has no cost rows -->
        <div
          class="rounded-2xl border border-dashed border-gray-300 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800"
        >
          <p class="text-sm/6 font-medium text-gray-900 dark:text-white">No cost rows for this session</p>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            Nothing has been recorded under this session ID — check the ID, or the session may predate
            cost tracking.
          </p>
        </div>
      } @else if (anatomyResource.error()) {
        <!-- Error State -->
        <div
          class="rounded-2xl border border-state-danger-200 bg-state-danger-50 p-4 text-state-danger-800 dark:border-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200"
        >
          <p class="text-sm/6">Failed to load session cost anatomy. Please try again.</p>
          <button
            type="button"
            (click)="anatomyResource.reload()"
            class="mt-2 text-sm/6 font-medium underline hover:no-underline"
          >
            Retry
          </button>
        </div>
      } @else if (anatomyResource.hasValue()) {
        <!-- Summary Header -->
        <div class="mb-6 grid grid-cols-2 gap-4 sm:grid-cols-3 xl:grid-cols-6">
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Total Cost
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatCurrency(anatomyResource.value().totalCost) }}
            </p>
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Cache Efficiency
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatEfficiency(anatomyResource.value().cacheEfficiency) }}
            </p>
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Avoidable Misses
            </p>
            <p
              class="mt-1 text-lg/7 font-semibold"
              [class]="
                unexplainedMisses() > 0
                  ? 'text-state-danger-600 dark:text-state-danger-400'
                  : 'text-gray-900 dark:text-white'
              "
            >
              {{ anatomyResource.value().avoidableMissCount }}
            </p>
            <!--
              #756 — an @-mention re-writes the prefix on purpose and looks exactly
              like the regression this page exists to find. The count stays whole (the
              spend was real); the explained part is named underneath so the red number
              above only means "unexplained".
            -->
            @if (anatomyResource.value().agentSwitchMissCount > 0) {
              <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                {{ anatomyResource.value().agentSwitchMissCount }} from an agent switch ·
                <span class="font-medium">{{ unexplainedMisses() }} unexplained</span>
              </p>
            }
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Wasted
            </p>
            <p
              class="mt-1 text-lg/7 font-semibold"
              [class]="
                anatomyResource.value().wastedUsd > 0
                  ? 'text-state-danger-600 dark:text-state-danger-400'
                  : 'text-gray-900 dark:text-white'
              "
            >
              {{ formatCurrency(anatomyResource.value().wastedUsd, 4) }}
            </p>
            <!--
              Partial misses read from cache, so they used to be invisible here
              (reported as hits, $0 wasted) while costing as much as a full
              miss. Named under the total rather than in a tile of their own:
              the dollars are the headline, the shape is the explanation.
            -->
            @if (anatomyResource.value().partialMissCount > 0) {
              <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                {{ formatCurrency(anatomyResource.value().partialMissUsd, 4) }} from
                <span class="font-medium"
                  >{{ anatomyResource.value().partialMissCount }} partial
                  {{ anatomyResource.value().partialMissCount === 1 ? 'miss' : 'misses' }}</span
                >
              </p>
            }
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Cache Read
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatTokens(anatomyResource.value().totalCacheReadTokens) }}
            </p>
          </div>
          <div class="rounded-2xl border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800">
            <p class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
              Cache Write
            </p>
            <p class="mt-1 text-lg/7 font-semibold text-gray-900 dark:text-white">
              {{ formatTokens(anatomyResource.value().totalCacheWriteTokens) }}
            </p>
          </div>
        </div>

        <!-- Calls Table -->
        <div
          class="overflow-hidden rounded-2xl border border-gray-200 bg-white dark:border-gray-700 dark:bg-gray-800"
        >
          <div class="overflow-x-auto">
            <table class="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead>
                <tr class="text-left text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                  <th scope="col" class="px-3 py-3 sm:pl-4"><span class="sr-only">Expand</span></th>
                  <th scope="col" class="px-3 py-3">Time</th>
                  <th scope="col" class="px-3 py-3">Model</th>
                  <th scope="col" class="px-3 py-3 text-right">In</th>
                  <th scope="col" class="px-3 py-3 text-right">Read</th>
                  <th scope="col" class="px-3 py-3 text-right">Write</th>
                  <th scope="col" class="px-3 py-3 text-right">Out</th>
                  <th scope="col" class="px-3 py-3 text-right">Cost</th>
                  <th scope="col" class="px-3 py-3">Cache Status</th>
                  <th scope="col" class="px-3 py-3 text-right">Gap</th>
                  <th scope="col" class="px-3 py-3 text-right">Wasted</th>
                  <th scope="col" class="px-3 py-3">Fingerprints</th>
                </tr>
              </thead>
              <tbody class="divide-y divide-gray-200 dark:divide-gray-700">
                @for (row of rows(); track row.index) {
                  <tr
                    class="text-sm/6 text-gray-700 dark:text-gray-300"
                    [class.bg-state-danger-50]="row.call.cacheStatus === 'miss_avoidable'"
                    [class.dark:bg-state-danger-900/10]="row.call.cacheStatus === 'miss_avoidable'"
                    [class.bg-category-accent-partial-miss-50]="row.call.cacheStatus === 'partial_miss'"
                    [class.dark:bg-category-accent-partial-miss-900/10]="row.call.cacheStatus === 'partial_miss'"
                  >
                    <td class="px-3 py-2 sm:pl-4">
                      <button
                        type="button"
                        (click)="toggleExpand(row.index)"
                        [attr.aria-expanded]="isExpanded(row.index)"
                        [attr.aria-controls]="'call-detail-' + row.index"
                        [attr.aria-label]="(isExpanded(row.index) ? 'Hide' : 'Show') + ' details for call ' + (row.index + 1)"
                        class="flex size-7 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon
                          name="heroChevronDown"
                          class="size-4 transition-transform duration-150"
                          [class.rotate-180]="isExpanded(row.index)"
                          aria-hidden="true"
                        />
                      </button>
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 tabular-nums" [title]="row.call.timestamp">
                      {{ row.call.timestamp | date: 'MMM d, HH:mm:ss' }}
                    </td>
                    <td class="max-w-48 truncate px-3 py-2 font-mono text-xs/5" [title]="row.call.modelId ?? ''">
                      {{ row.call.modelId ?? '—' }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatTokens(row.call.inputTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-state-success-700 dark:text-state-success-400">
                      {{ formatTokens(row.call.cacheReadTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums text-state-info-700 dark:text-state-info-400">
                      {{ formatTokens(row.call.cacheWriteTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatTokens(row.call.outputTokens) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatCurrency(row.call.cost, 4) }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2">
                      @if (row.call.cacheStatus; as status) {
                        <span [class]="getStatusClass(status)">{{ getStatusLabel(status) }}</span>
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500">—</span>
                      }
                      @if (row.call.windowTrimmed; as trimmed) {
                        <!-- The window slid before this call — the prefix changed. -->
                        <span
                          class="ml-1 rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 text-gray-600 dark:bg-white/10 dark:text-gray-300"
                          [title]="trimmed + ' messages trimmed from the window before this call'"
                          >trim −{{ trimmed }}</span
                        >
                      }
                      @for (event of row.call.compactionEvents ?? []; track $index) {
                        <span
                          class="ml-1 rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 text-gray-600 dark:bg-white/10 dark:text-gray-300"
                          [title]="compactionEventTitle(event)"
                          >{{ event.kind }}</span
                        >
                      }
                      @if (documentBadge(row.call); as badge) {
                        <!-- What the model had of the attachments on this call:
                             the full document inline, a digest only, or pages
                             it retrieved with document_read. -->
                        <span
                          class="ml-1 rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 text-gray-600 dark:bg-white/10 dark:text-gray-300"
                          [title]="documentDetail(row.call)"
                          >{{ badge }}</span
                        >
                      }
                    </td>
                    <td class="whitespace-nowrap px-3 py-2 text-right tabular-nums">
                      {{ formatGap(row.call.cacheGapSeconds) }}
                      @if (row.call.cachePrefixGapSeconds; as prefixGap) {
                        <!-- The gap that decided the verdict, when a call with a
                             different prefix ran in between (e.g. an @-mention).
                             Without it, a miss_ttl_expired beside a short gap
                             reads as a bug rather than as the correct answer. -->
                        <span
                          class="ml-1 text-xs text-gray-500 dark:text-gray-400"
                          [title]="'Last call with the same prefix was ' + formatGap(prefixGap) + ' ago — that is the entry this call could have hit'"
                          >({{ formatGap(prefixGap) }})</span
                        >
                      }
                    </td>
                    <td
                      class="whitespace-nowrap px-3 py-2 text-right tabular-nums"
                      [class.text-state-danger-600]="row.call.wastedUsd > 0"
                      [class.dark:text-state-danger-400]="row.call.wastedUsd > 0"
                    >
                      {{ row.call.wastedUsd > 0 ? formatCurrency(row.call.wastedUsd, 4) : '—' }}
                    </td>
                    <td class="whitespace-nowrap px-3 py-2">
                      @if (row.call.prefixFingerprints; as fp) {
                        <div class="flex items-center gap-1.5">
                          @for (key of fingerprintKeys; track key) {
                            <span
                              [class]="getFingerprintClass(row, key)"
                              [title]="
                                fingerprintLabels[key] +
                                ': ' +
                                (fp[key] ?? 'not recorded') +
                                (isChanged(row, key) ? ' — changed since previous call' : '')
                              "
                            >
                              {{ fingerprintLabels[key] }} {{ truncateHash(fp[key]) }}
                            </span>
                          }
                        </div>
                      } @else {
                        <span class="text-gray-400 dark:text-gray-500">—</span>
                      }
                    </td>
                  </tr>
                  @if (isExpanded(row.index)) {
                    <tr [id]="'call-detail-' + row.index" class="bg-gray-50 dark:bg-gray-900/40">
                      <td colspan="12" class="px-4 py-3 sm:pl-14">
                        <dl class="grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-2 lg:grid-cols-3">
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Timestamp
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ row.call.timestamp }}
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Message ID
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ row.call.messageId ?? '—' }}
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Message Count
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ row.call.prefixFingerprints?.messageCount ?? '—' }}
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Window Removed
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ row.call.windowRemovedMessages ?? '—' }}
                              @if (row.call.windowTrimmed) {
                                <span class="text-gray-500 dark:text-gray-400">(+{{ row.call.windowTrimmed }} before this call)</span>
                              }
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Static Prefix
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              @if (row.call.prefixTokens; as prefix) {
                                {{ formatTokens(prefix.system) }} system · {{ formatTokens(prefix.tools) }} tools
                              } @else {
                                —
                              }
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Compaction
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              @if (row.call.compactionEvents?.length) {
                                @for (event of row.call.compactionEvents; track $index) {
                                  <div>{{ compactionEventTitle(event) }}</div>
                                }
                              } @else {
                                —
                              }
                            </dd>
                          </div>
                          <div>
                            <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                              Documents
                            </dt>
                            <dd class="mt-0.5 font-mono text-xs/5 text-gray-700 dark:text-gray-300">
                              {{ documentDetail(row.call) || '—' }}
                            </dd>
                          </div>
                          @for (key of fingerprintKeys; track key) {
                            <div>
                              <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">
                                {{ fingerprintLabels[key] }} Hash
                                @if (isChanged(row, key)) {
                                  <span class="ml-1 normal-case text-state-danger-600 dark:text-state-danger-400">(changed)</span>
                                }
                              </dt>
                              <dd
                                class="mt-0.5 break-all font-mono text-xs/5"
                                [class]="
                                  isChanged(row, key)
                                    ? 'text-state-danger-600 dark:text-state-danger-400'
                                    : 'text-gray-700 dark:text-gray-300'
                                "
                              >
                                {{ row.call.prefixFingerprints?.[key] ?? '—' }}
                              </dd>
                            </div>
                          }
                        </dl>
                      </td>
                    </tr>
                  }
                } @empty {
                  <tr>
                    <td colspan="12" class="px-4 py-8 text-center text-sm/6 text-gray-500 dark:text-gray-400">
                      No model calls recorded for this session.
                    </td>
                  </tr>
                }
              </tbody>
            </table>
          </div>
        </div>
      }
    </div>
  `,
})
export class SessionCostAnatomyPage {
  private costHttp = inject(AdminCostHttpService);

  /** Session ID from the `costs/sessions/:id` route (component input binding). */
  readonly id = input.required<string>();

  readonly fingerprintKeys = FINGERPRINT_KEYS;
  readonly fingerprintLabels = FINGERPRINT_LABELS;
  readonly truncateHash = truncateHash;

  readonly anatomyResource = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => firstValueFrom(this.costHttp.getSessionCostAnatomy(params.id)),
  });

  /** Chronological call rows annotated with fingerprint diffs. */
  readonly rows = computed<AnatomyRow[]>(() =>
    this.anatomyResource.hasValue() ? buildAnatomyRows(this.anatomyResource.value().calls) : []
  );

  /** True when the backend returned 404 — the session has no cost rows. */
  readonly notFound = computed(() => this.errorStatus(this.anatomyResource.error()) === 404);

  /**
   * The content-free profile — what the user was doing. Loaded independently
   * of the anatomy: a session can have a metadata row and no cost rows (or,
   * for rows that predate cost tracking, the reverse), and one half failing
   * must not hide the other.
   */
  readonly profileResource = resource({
    params: () => ({ id: this.id() }),
    loader: ({ params }) => firstValueFrom(this.costHttp.getSessionProfile(params.id)),
  });

  readonly profileNotFound = computed(() => this.errorStatus(this.profileResource.error()) === 404);

  /** The owning user, for the back link — only the profile knows it. */
  readonly profileUserId = computed(() =>
    this.profileResource.hasValue() ? (this.profileResource.value().userId ?? null) : null,
  );

  /** "13 × gpt-5.4" or "9 × claude-haiku-4-5, 4 × gpt-5.4" — models by call count, desc. */
  readonly modelMixLine = computed(() => {
    if (!this.profileResource.hasValue()) return '';
    const mix = this.profileResource.value().modelMix;
    return Object.entries(mix)
      .sort((a, b) => b[1] - a[1])
      .map(([model, calls]) => `${calls} × ${model.split('.').pop()?.replace(/-\d{8}.*$/, '').replace(/:.*$/, '') ?? model}`)
      .join(', ');
  });

  private expandedDiagnoses = signal<ReadonlySet<string>>(new Set());
  readonly copied = signal(false);

  isDiagnosisOpen(code: string): boolean {
    return this.expandedDiagnoses().has(code);
  }

  toggleDiagnosis(code: string): void {
    this.expandedDiagnoses.update((current) => {
      const next = new Set(current);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  }

  chipClass(severity: DiagnosisSeverity): string {
    return severityChipClass(severity);
  }

  severityLabel(severity: DiagnosisSeverity): string {
    return SEVERITY_LABELS[severity] ?? severity;
  }

  evidenceEntries(d: SessionDiagnosis): Array<{ key: string; label: string; value: string }> {
    return Object.entries(d.evidence).map(([key, value]) => ({
      key,
      label: humanizeKey(key),
      value: formatEvidenceValue(value),
    }));
  }

  bytes(n: number): string {
    return formatBytes(n);
  }

  /** The profile + anatomy as one JSON document on the clipboard. */
  async copyDiagnosticJson(): Promise<void> {
    if (!this.profileResource.hasValue()) return;
    const anatomy = this.anatomyResource.hasValue() ? this.anatomyResource.value() : null;
    const json = buildDiagnosticJson(this.profileResource.value(), anatomy);
    try {
      await navigator.clipboard.writeText(json);
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2000);
    } catch {
      // Clipboard unavailable (insecure context, permissions) — leave the
      // button as-is; nothing to recover, the data is still on the page.
    }
  }

  /**
   * Avoidable misses with no explanation — the number that should never move (#756).
   *
   * `avoidableMissCount` includes deliberate `@`-mention prefix swaps, which cost real
   * money but are not a regression. The backend reports the explained subset rather than
   * deducting it, so the page does the subtraction where a reader can see both halves.
   */
  /** "3 applied · 1 forced · summary 2.3K" — the compaction decisions by kind. */
  readonly feedbackDownRate = computed(() => {
    if (!this.profileResource.hasValue()) return null;
    const feedback = this.profileResource.value().feedback;
    return feedback ? downRate(feedback) : null;
  });

  readonly feedbackRetryLine = computed(() =>
    this.profileResource.hasValue() ? feedbackRetryLine(this.profileResource.value().feedback) : null,
  );

  readonly feedbackTurnClassLine = computed(() =>
    this.profileResource.hasValue() ? feedbackByTurnClassLine(this.profileResource.value().feedback) : null,
  );

  readonly compactionEventsLine = computed(() => {
    if (!this.profileResource.hasValue()) return '';
    const p = this.profileResource.value();
    const counts = p.compactionEventCounts ?? {};
    const parts = Object.keys(counts)
      .sort()
      .map((kind) => `${counts[kind]} ${kind.replace('_', ' ')}`);
    if (p.lastSummaryTokens != null) parts.push(`summary ${this.formatTokens(p.lastSummaryTokens)}`);
    return parts.join(' · ');
  });

  compactionEventTitle(event: CompactionEvent): string {
    const parts = [event.kind.replace(/_/g, ' ')];
    if (event.checkpoint != null) parts.push(`checkpoint ${event.checkpoint}`);
    if (event.summaryTokens != null) parts.push(`summary ${this.formatTokens(event.summaryTokens)}`);
    if (event.summarizedTurns != null) parts.push(`${event.summarizedTurns} turns summarized`);
    if (event.retainedMessages != null) parts.push(`${event.retainedMessages} messages retained`);
    if (event.truncatedToolResults) parts.push(`${event.truncatedToolResults} tool results truncated`);
    if (event.documents != null) parts.push(`${event.documents} document${event.documents === 1 ? '' : 's'}`);
    if (event.documentTokens != null) parts.push(`~${this.formatTokens(event.documentTokens)} tokens`);
    if (event.digestTokens != null) parts.push(`→ ~${this.formatTokens(event.digestTokens)} digest`);
    if (event.slices) parts.push(`${event.slices} page slice${event.slices === 1 ? '' : 's'} aged`);
    if (event.cacheGapSeconds != null) parts.push(`cache gap ${this.formatGap(event.cacheGapSeconds)}`);
    return parts.join(' · ');
  }

  /**
   * "2 full · 1 digest-only · read 4 pages · peak ~12K" — how the session's
   * documents were consumed, call by call. Empty when the rows predate the
   * document fields, so the card falls back to the upload byte total.
   */
  readonly documentsLine = computed(() => {
    if (!this.profileResource.hasValue()) return '';
    const p = this.profileResource.value();
    if (!p.dataCoverage.documents) return '';
    const parts: string[] = [];
    if (p.fullDocumentCalls) parts.push(`${p.fullDocumentCalls} full`);
    if (p.digestOnlyCalls) parts.push(`${p.digestOnlyCalls} digest-only`);
    if (p.documentReadCalls) parts.push(`read ${p.documentReadPages ?? 0} pages in ${p.documentReadCalls} calls`);
    if (p.peakDocumentTokens != null) parts.push(`peak ~${this.formatTokens(p.peakDocumentTokens)}`);
    return parts.join(' · ');
  });

  /** Short row badge: `doc`, `digest`, or `+Np` for pages retrieved this call. */
  documentBadge(call: SessionCallRow): string {
    const reads = call.documentReads;
    if (reads && reads.pages > 0) return `+${reads.pages}p`;
    if (call.hasDocuments) return 'doc';
    if (call.documentDigests) return 'digest';
    return '';
  }

  /** The expanded-row line for the call's document context. */
  documentDetail(call: SessionCallRow): string {
    if (call.hasDocuments == null && !call.documentReads) return '';
    const parts: string[] = [];
    if (call.hasDocuments) {
      const mime = Object.entries(call.documentMime ?? {})
        .map(([fmt, n]) => `${fmt}×${n}`)
        .join(' ');
      parts.push(`${call.documentCount ?? 0} inline ~${this.formatTokens(call.documentTokens ?? 0)}${mime ? ` (${mime})` : ''}`);
    }
    if (call.documentDigests) parts.push(`${call.documentDigests} digest${call.documentDigests === 1 ? '' : 's'}`);
    if (call.documentsAttached) parts.push(`${call.documentsAttached} attached this turn`);
    if (call.documentSlices) parts.push(`${call.documentSlices} retrieved slice${call.documentSlices === 1 ? '' : 's'} ~${this.formatTokens(call.documentSliceTokens ?? 0)}`);
    if (call.documentReads?.calls) parts.push(`document_read ×${call.documentReads.calls} → ${call.documentReads.pages} pages`);
    return parts.length ? parts.join(' · ') : 'no documents in context';
  }

  readonly unexplainedMisses = computed(() => {
    const anatomy = this.anatomyResource.value();
    if (!anatomy) return 0;
    return Math.max(0, anatomy.avoidableMissCount - anatomy.agentSwitchMissCount);
  });

  private expandedRows = signal<ReadonlySet<number>>(new Set());

  isExpanded(index: number): boolean {
    return this.expandedRows().has(index);
  }

  toggleExpand(index: number): void {
    this.expandedRows.update((current) => {
      const next = new Set(current);
      if (next.has(index)) {
        next.delete(index);
      } else {
        next.add(index);
      }
      return next;
    });
  }

  isChanged(row: AnatomyRow, key: FingerprintKey): boolean {
    return row.changed.includes(key);
  }

  getFingerprintClass(row: AnatomyRow, key: FingerprintKey): string {
    const base = 'inline-flex items-center rounded-2xl px-2 py-0.5 font-mono text-xs/5';
    if (this.isChanged(row, key)) {
      return `${base} bg-state-danger-100 font-semibold text-state-danger-700 ring-1 ring-inset ring-state-danger-300 dark:bg-state-danger-900/40 dark:text-state-danger-300 dark:ring-state-danger-700`;
    }
    return `${base} bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400`;
  }

  getStatusClass(status: CacheStatus): string {
    const base = 'inline-flex items-center rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';
    switch (status) {
      case 'hit':
        return `${base} bg-state-success-100 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300`;
      case 'first_write':
        return `${base} bg-state-info-100 text-state-info-800 dark:bg-state-info-900/30 dark:text-state-info-300`;
      case 'miss_ttl_expired':
        return `${base} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
      case 'miss_avoidable':
        return `${base} bg-state-danger-100 text-state-danger-800 dark:bg-state-danger-900/30 dark:text-state-danger-300`;
      case 'partial_miss':
        return `${base} bg-category-accent-partial-miss-100 text-category-accent-partial-miss-800 dark:bg-category-accent-partial-miss-900/30 dark:text-category-accent-partial-miss-300`;
      case 'uncached':
      default:
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  }

  getStatusLabel(status: CacheStatus): string {
    switch (status) {
      case 'hit':
        return 'Hit';
      case 'first_write':
        return 'First Write';
      case 'miss_ttl_expired':
        return 'Miss (TTL)';
      case 'miss_avoidable':
        return 'Miss (Avoidable)';
      case 'partial_miss':
        return 'Miss (Partial)';
      case 'uncached':
        return 'Uncached';
      default:
        return status;
    }
  }

  formatCurrency(value: number, maxFractionDigits = 2): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: maxFractionDigits,
    }).format(value);
  }

  formatTokens(tokens: number): string {
    if (tokens >= 1_000_000) {
      return `${(tokens / 1_000_000).toFixed(1)}M`;
    } else if (tokens >= 1_000) {
      return `${(tokens / 1_000).toFixed(1)}K`;
    }
    return tokens.toString();
  }

  formatEfficiency(efficiency: number | null): string {
    if (efficiency === null) {
      return '—';
    }
    return `${(efficiency * 100).toFixed(1)}%`;
  }

  formatGap(seconds: number | null | undefined): string {
    if (seconds == null) {
      return '—';
    }
    if (seconds >= 60) {
      const minutes = Math.floor(seconds / 60);
      const rest = seconds % 60;
      return rest > 0 ? `${minutes}m ${rest}s` : `${minutes}m`;
    }
    return `${seconds}s`;
  }

  private errorStatus(error: unknown): number | undefined {
    if (error instanceof HttpErrorResponse) {
      return error.status;
    }
    // resource() may wrap loader errors; check the cause chain.
    const cause = (error as { cause?: unknown } | null | undefined)?.cause;
    if (cause instanceof HttpErrorResponse) {
      return cause.status;
    }
    return undefined;
  }
}
