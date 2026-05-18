import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
  effect,
} from '@angular/core';
import { Router, ActivatedRoute, RouterLink } from '@angular/router';
import { FormArray, FormBuilder, FormGroup, Validators, ReactiveFormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroServer,
  heroUserGroup,
  heroLink,
  heroShieldCheck,
  heroPlus,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import { AdminToolService } from '../services/admin-tool.service';
import { ConnectorsService } from '../../connectors/services/connectors.service';
import {
  TOOL_CATEGORIES,
  TOOL_PROTOCOLS,
  TOOL_STATUSES,
  MCP_TRANSPORTS,
  MCP_AUTH_TYPES,
  A2A_AUTH_TYPES,
  MCPServerConfig,
  MCPToolEntry,
  A2AAgentConfig,
  ToolProtocol,
} from '../models/admin-tool.model';

@Component({
  selector: 'app-tool-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReactiveFormsModule, NgIcon],
  providers: [provideIcons({ heroArrowLeft, heroServer, heroUserGroup, heroLink, heroShieldCheck, heroPlus, heroTrash })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Back link -->
        <a
          routerLink="/admin/tools"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          Back to Tools
        </a>

        <!-- Page Header -->
        <div class="mb-8">
          <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">
            {{ isEditMode() ? 'Edit Tool' : 'Create Tool' }}
          </h1>
          <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
            {{ isEditMode() ? 'Update tool metadata and settings.' : 'Add a new tool to the catalog.' }}
          </p>
        </div>

        <!-- Loading State -->
        @if (loading()) {
          <div class="flex h-64 items-center justify-center">
            <div class="size-10 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-700 dark:border-t-blue-500"></div>
          </div>
        } @else {
          <!-- Form -->
          <form [formGroup]="form" (ngSubmit)="onSubmit()" class="space-y-8">
            <!-- Basic Information -->
            <section class="space-y-4">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Basic information</h2>

              <!-- Tool ID (only for create) -->
              @if (!isEditMode()) {
                <div>
                  <label for="toolId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Tool ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    id="toolId"
                    type="text"
                    formControlName="toolId"
                    placeholder="e.g., my_custom_tool"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="form.get('toolId')?.invalid && form.get('toolId')?.touched"
                  />
                  @if (form.get('toolId')?.invalid && form.get('toolId')?.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      Tool ID must be 3-50 characters, lowercase letters, numbers, and underscores only.
                    </p>
                  }
                </div>
              }

              <!-- Display Name -->
              <div>
                <label for="displayName" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Display Name <span class="text-red-600">*</span>
                </label>
                <input
                  id="displayName"
                  type="text"
                  formControlName="displayName"
                  placeholder="e.g., My Custom Tool"
                  class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  [class.border-red-500]="form.get('displayName')?.invalid && form.get('displayName')?.touched"
                />
                @if (form.get('displayName')?.invalid && form.get('displayName')?.touched) {
                  <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                    Display name is required (1-100 characters).
                  </p>
                }
              </div>

              <!-- Description -->
              <div>
                <label for="description" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Description <span class="text-red-600">*</span>
                </label>
                <textarea
                  id="description"
                  formControlName="description"
                  rows="3"
                  placeholder="Describe what this tool does..."
                  class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  [class.border-red-500]="form.get('description')?.invalid && form.get('description')?.touched"
                ></textarea>
                @if (form.get('description')?.invalid && form.get('description')?.touched) {
                  <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                    Description is required (max 500 characters).
                  </p>
                }
              </div>

              <!-- Category and Protocol Row -->
              <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <label for="category" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Category
                  </label>
                  <select
                    id="category"
                    formControlName="category"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  >
                    @for (cat of categories; track cat.value) {
                      <option [value]="cat.value">{{ cat.label }}</option>
                    }
                  </select>
                </div>

                <div>
                  <label for="protocol" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Protocol
                  </label>
                  <select
                    id="protocol"
                    formControlName="protocol"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  >
                    @for (proto of protocols; track proto.value) {
                      <option [value]="proto.value">{{ proto.label }}</option>
                    }
                  </select>
                  @if (selectedProtocol()) {
                    <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                      {{ getProtocolDescription(selectedProtocol()) }}
                    </p>
                  }
                </div>
              </div>
            </section>

            <!-- MCP External Server Configuration -->
            @if (selectedProtocol() === 'mcp_external') {
              <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
                <div class="flex items-center gap-2">
                  <ng-icon name="heroServer" class="size-5 text-blue-600 dark:text-blue-400" aria-hidden="true" />
                  <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">MCP server configuration</h2>
                </div>

                <!-- Server URL -->
                <div>
                  <label for="mcpServerUrl" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Server URL <span class="text-red-600">*</span>
                  </label>
                  <input
                    id="mcpServerUrl"
                    type="url"
                    formControlName="mcpServerUrl"
                    placeholder="https://xxx.lambda-url.us-west-2.on.aws/"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Lambda Function URL or API Gateway endpoint
                  </p>
                </div>

                <!-- Transport and Auth Row -->
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label for="mcpTransport" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Transport
                    </label>
                    <select
                      id="mcpTransport"
                      formControlName="mcpTransport"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    >
                      @for (transport of mcpTransports; track transport.value) {
                        <option [value]="transport.value">{{ transport.label }}</option>
                      }
                    </select>
                  </div>

                  <div>
                    <label for="mcpAuthType" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Authentication
                    </label>
                    <select
                      id="mcpAuthType"
                      formControlName="mcpAuthType"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    >
                      @for (auth of mcpAuthTypes; track auth.value) {
                        <option [value]="auth.value">{{ auth.label }}</option>
                      }
                    </select>
                  </div>
                </div>

                <!-- AWS Region (shown for aws-iam auth) -->
                @if (form.get('mcpAuthType')?.value === 'aws-iam') {
                  <div>
                    <label for="mcpAwsRegion" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      AWS Region
                    </label>
                    <input
                      id="mcpAwsRegion"
                      type="text"
                      formControlName="mcpAwsRegion"
                      placeholder="us-west-2 (auto-detected from URL if blank)"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                    />
                  </div>
                }

                <!-- API Key Header (shown for api-key auth) -->
                @if (form.get('mcpAuthType')?.value === 'api-key') {
                  <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                    <div>
                      <label for="mcpApiKeyHeader" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                        API Key Header
                      </label>
                      <input
                        id="mcpApiKeyHeader"
                        type="text"
                        formControlName="mcpApiKeyHeader"
                        placeholder="x-api-key"
                        class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                      />
                    </div>
                    <div>
                      <label for="mcpSecretArn" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                        Secret ARN
                      </label>
                      <input
                        id="mcpSecretArn"
                        type="text"
                        formControlName="mcpSecretArn"
                        placeholder="arn:aws:secretsmanager:..."
                        class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                      />
                    </div>
                  </div>
                }

                <!-- MCP Tools -->
                <div formArrayName="mcpTools">
                  <div class="mb-2 flex items-center justify-between">
                    <span class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Available Tools
                    </span>
                    <div class="flex items-center gap-1">
                      <button
                        type="button"
                        (click)="discoverMcpTools()"
                        [disabled]="discovering() || !form.get('mcpServerUrl')?.value"
                        class="inline-flex items-center gap-1 rounded-2xl px-2.5 py-1 text-sm/6 font-medium text-blue-600 hover:bg-blue-50 hover:text-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-blue-400 dark:hover:bg-blue-900/20"
                      >
                        {{ discovering() ? 'Discovering…' : 'Discover from server' }}
                      </button>
                      <button
                        type="button"
                        (click)="addMcpTool()"
                        class="inline-flex items-center gap-1 rounded-2xl px-2.5 py-1 text-sm/6 font-medium text-blue-600 hover:bg-blue-50 hover:text-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-blue-400 dark:hover:bg-blue-900/20"
                      >
                        <ng-icon name="heroPlus" class="size-4" aria-hidden="true" />
                        Add Tool
                      </button>
                    </div>
                  </div>
                  @if (discoverError()) {
                    <p class="mb-2 text-sm/6 text-red-600 dark:text-red-400">
                      {{ discoverError() }}
                    </p>
                  }

                  @if (mcpToolsArray.length === 0) {
                    <p class="text-xs/5 italic text-gray-500 dark:text-gray-400">
                      No tools listed. Leave empty to discover tools at runtime — per-tool approval flags will not apply.
                    </p>
                  } @else {
                    <div class="space-y-2">
                      @for (row of mcpToolsArray.controls; track $index) {
                        <div [formGroupName]="$index" class="flex items-start gap-2 rounded-2xl border border-gray-200 bg-white p-2 dark:border-gray-700 dark:bg-gray-800">
                          <div class="flex-1">
                            <input
                              type="text"
                              formControlName="name"
                              placeholder="tool_name"
                              [attr.aria-label]="'Tool name ' + ($index + 1)"
                              class="block w-full rounded-2xl border border-gray-300 bg-white px-3 py-1.5 font-mono text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                            />
                          </div>
                          <label class="flex items-center gap-1.5 whitespace-nowrap pt-1.5 text-xs/5 text-gray-700 dark:text-gray-300">
                            <input
                              type="checkbox"
                              formControlName="needsApproval"
                              class="size-4 rounded border-gray-300 text-amber-600 focus:ring-2 focus:ring-amber-500 dark:border-gray-600 dark:bg-gray-800"
                            />
                            <span>Needs approval</span>
                          </label>
                          <button
                            type="button"
                            (click)="removeMcpTool($index)"
                            [attr.aria-label]="'Remove tool ' + ($index + 1)"
                            class="flex size-8 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 dark:text-gray-500 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                          >
                            <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                          </button>
                        </div>
                      }
                    </div>
                  }
                  <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
                    Tools flagged "Needs approval" will pause the agent for user confirmation before invocation.
                  </p>
                </div>

                <!-- Health Check -->
                <label class="flex items-center gap-3">
                  <input
                    type="checkbox"
                    formControlName="mcpHealthCheckEnabled"
                    class="size-4 rounded border-gray-300 text-blue-600 focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800"
                  />
                  <span class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Enable health checks
                  </span>
                </label>
              </section>

              <!-- Forward App Authentication Token -->
              <section class="space-y-3 border-t border-gray-200 pt-8 dark:border-gray-700">
                <div class="flex items-center gap-2">
                  <ng-icon name="heroShieldCheck" class="size-5 text-amber-600 dark:text-amber-400" aria-hidden="true" />
                  <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Forward app authentication token</h2>
                </div>

                <label class="flex items-start gap-3">
                  <input
                    type="checkbox"
                    formControlName="forwardAuthToken"
                    class="mt-0.5 size-4 rounded border-gray-300 text-amber-600 focus:ring-2 focus:ring-amber-500 dark:border-gray-600 dark:bg-gray-800"
                  />
                  <span class="flex-1">
                    <span class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Forward user's OIDC token to MCP server
                    </span>
                    <span class="mt-1 block text-sm/6 text-gray-600 dark:text-gray-400">
                      The user's authentication token from app login will be sent in the Authorization header.
                      The MCP server validates the JWT and extracts user identity from claims.
                    </span>
                  </span>
                </label>

                @if (form.get('forwardAuthToken')?.value) {
                  <div class="rounded-2xl border border-amber-300 bg-amber-50 p-4 dark:border-amber-700 dark:bg-amber-900/30">
                    <p class="mb-1 text-sm/6 font-medium text-amber-900 dark:text-amber-100">
                      Security notice
                    </p>
                    <p class="text-sm/6 text-amber-800 dark:text-amber-200">
                      Only enable this for MCP servers you control. The user's authentication token will be sent
                      in the Authorization header. The MCP server should validate the JWT signature and extract
                      user identity from the token claims. Set the MCP Authentication Type to "None" above.
                    </p>
                  </div>
                }
              </section>

              <!-- User OAuth Connector -->
              <section class="space-y-3 border-t border-gray-200 pt-8 dark:border-gray-700">
                <div class="flex items-center gap-2">
                  <ng-icon name="heroLink" class="size-5 text-emerald-600 dark:text-emerald-400" aria-hidden="true" />
                  <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">User OAuth connector</h2>
                </div>
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">
                  If this tool requires access to a user's external account (e.g., Google Workspace, Microsoft 365),
                  select the OAuth provider. The user's access token will be passed to the MCP server.
                </p>
                <div>
                  <label for="requiresOauthProvider" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Required OAuth provider
                  </label>
                  <select
                    id="requiresOauthProvider"
                    formControlName="requiresOauthProvider"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                  >
                    <option [value]="''">None - No user OAuth required</option>
                    @for (provider of oauthProviders(); track provider.providerId) {
                      <option [value]="provider.providerId">{{ provider.displayName }}</option>
                    }
                  </select>
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Users must connect this connector before using the tool. Manage connectors in
                    <a routerLink="/admin/connectors" class="font-medium text-blue-600 hover:text-blue-700 dark:text-blue-400 dark:hover:text-blue-300">Connectors</a>.
                  </p>
                </div>
              </section>
            }

            <!-- A2A Agent Configuration -->
            @if (selectedProtocol() === 'a2a') {
              <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
                <div class="flex items-center gap-2">
                  <ng-icon name="heroUserGroup" class="size-5 text-purple-600 dark:text-purple-400" aria-hidden="true" />
                  <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Agent-to-agent configuration</h2>
                </div>

                <!-- Agent URL -->
                <div>
                  <label for="a2aAgentUrl" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Agent URL <span class="text-red-600">*</span>
                  </label>
                  <input
                    id="a2aAgentUrl"
                    type="url"
                    formControlName="a2aAgentUrl"
                    placeholder="https://agent-endpoint.example.com/"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <!-- Agent ID and Auth Row -->
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label for="a2aAgentId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Agent ID
                    </label>
                    <input
                      id="a2aAgentId"
                      type="text"
                      formControlName="a2aAgentId"
                      placeholder="AgentCore Runtime ID (optional)"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                    />
                  </div>

                  <div>
                    <label for="a2aAuthType" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Authentication
                    </label>
                    <select
                      id="a2aAuthType"
                      formControlName="a2aAuthType"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    >
                      @for (auth of a2aAuthTypes; track auth.value) {
                        <option [value]="auth.value">{{ auth.label }}</option>
                      }
                    </select>
                  </div>
                </div>

                <!-- AWS Region (shown for aws-iam or agentcore auth) -->
                @if (form.get('a2aAuthType')?.value === 'aws-iam' || form.get('a2aAuthType')?.value === 'agentcore') {
                  <div>
                    <label for="a2aAwsRegion" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      AWS Region
                    </label>
                    <input
                      id="a2aAwsRegion"
                      type="text"
                      formControlName="a2aAwsRegion"
                      placeholder="us-west-2"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                    />
                  </div>
                }

                <!-- Capabilities -->
                <div>
                  <label for="a2aCapabilities" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Capabilities
                  </label>
                  <textarea
                    id="a2aCapabilities"
                    formControlName="a2aCapabilities"
                    rows="3"
                    placeholder="report_generation&#10;data_analysis&#10;document_creation"
                    class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  ></textarea>
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    One capability per line
                  </p>
                </div>

                <!-- Timeout and Retries -->
                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <div>
                    <label for="a2aTimeoutSeconds" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Timeout (seconds)
                    </label>
                    <input
                      id="a2aTimeoutSeconds"
                      type="number"
                      formControlName="a2aTimeoutSeconds"
                      min="1"
                      max="600"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    />
                  </div>
                  <div>
                    <label for="a2aMaxRetries" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Max Retries
                    </label>
                    <input
                      id="a2aMaxRetries"
                      type="number"
                      formControlName="a2aMaxRetries"
                      min="0"
                      max="10"
                      class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    />
                  </div>
                </div>
              </section>
            }

            <!-- Status & Visibility -->
            <section class="space-y-6 border-t border-gray-200 pt-8 dark:border-gray-700">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Status &amp; visibility</h2>

              <div>
                <label for="status" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Status
                </label>
                <select
                  id="status"
                  formControlName="status"
                  class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 sm:max-w-xs dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                >
                  @for (stat of statuses; track stat.value) {
                    <option [value]="stat.value">{{ stat.label }}</option>
                  }
                </select>
              </div>

              <div>
                <label class="flex items-center gap-3">
                  <input
                    type="checkbox"
                    formControlName="isPublic"
                    class="size-4 rounded border-gray-300 text-blue-600 focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800"
                  />
                  <span class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Public tool
                  </span>
                </label>
                <p class="ml-7 mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                  Available to all authenticated users.
                </p>
              </div>

              <div>
                <label class="flex items-center gap-3">
                  <input
                    type="checkbox"
                    formControlName="enabledByDefault"
                    class="size-4 rounded border-gray-300 text-blue-600 focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800"
                  />
                  <span class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Enabled by default
                  </span>
                </label>
                <p class="ml-7 mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                  Tool is enabled when a user first accesses it.
                </p>
              </div>
            </section>

            <!-- Form Actions -->
            <div class="flex flex-col gap-4 border-t border-gray-200 pt-6 dark:border-gray-700">
              @if (error()) {
                <div class="rounded-2xl border border-red-200 bg-red-50 p-4 text-sm/6 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200">
                  {{ error() }}
                </div>
              }

              @if (form.invalid) {
                <div class="rounded-2xl border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-900/20">
                  <p class="text-sm/6 font-medium text-amber-800 dark:text-amber-200">
                    Please fix the following before saving:
                  </p>
                  <ul class="mt-1 list-inside list-disc text-sm/6 text-amber-700 dark:text-amber-300">
                    @if (form.get('toolId')?.invalid && !isEditMode()) {
                      <li>Tool ID is required (3-50 chars, lowercase, numbers, underscores)</li>
                    }
                    @if (form.get('displayName')?.invalid) {
                      <li>Display name is required (1-100 characters)</li>
                    }
                    @if (form.get('description')?.invalid) {
                      <li>Description is required (max 500 characters)</li>
                    }
                  </ul>
                </div>
              }

              <div class="flex gap-2">
                <button
                  type="submit"
                  [disabled]="form.invalid || saving()"
                  class="inline-flex items-center justify-center rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  {{ saving() ? 'Saving…' : (isEditMode() ? 'Update Tool' : 'Create Tool') }}
                </button>
                <a
                  routerLink="/admin/tools"
                  class="inline-flex items-center justify-center rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
                >
                  Cancel
                </a>
              </div>
            </div>
          </form>
        }
      </div>
    </div>
  `,
})
export class ToolFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private adminToolService = inject(AdminToolService);
  private connectorsService = inject(ConnectorsService);

  readonly categories = TOOL_CATEGORIES;
  readonly protocols = TOOL_PROTOCOLS;
  readonly statuses = TOOL_STATUSES;
  readonly mcpTransports = MCP_TRANSPORTS;
  readonly mcpAuthTypes = MCP_AUTH_TYPES;
  readonly a2aAuthTypes = A2A_AUTH_TYPES;

  loading = signal(false);
  saving = signal(false);
  error = signal<string | null>(null);
  toolId = signal<string | null>(null);
  discovering = signal(false);
  discoverError = signal<string | null>(null);

  readonly isEditMode = computed(() => !!this.toolId());
  readonly selectedProtocol = signal<ToolProtocol>('local');

  /** Available connectors for dropdown */
  readonly oauthProviders = computed(() => this.connectorsService.getEnabledConnectors());

  form: FormGroup = this.fb.group({
    toolId: ['', [Validators.required, Validators.pattern(/^[a-z][a-z0-9_]{2,49}$/)]],
    displayName: ['', [Validators.required, Validators.minLength(1), Validators.maxLength(100)]],
    description: ['', [Validators.required, Validators.maxLength(500)]],
    category: ['utility'],
    protocol: ['local'],
    status: ['active'],
    isPublic: [false],
    enabledByDefault: [false],
    requiresOauthProvider: [''],
    forwardAuthToken: [false],
    // MCP External Server configuration
    mcpServerUrl: [''],
    mcpTransport: ['streamable-http'],
    mcpAuthType: ['aws-iam'],
    mcpAwsRegion: [''],
    mcpApiKeyHeader: [''],
    mcpSecretArn: [''],
    mcpTools: this.fb.array([] as FormGroup[]),
    mcpHealthCheckEnabled: [false],
    // A2A Agent configuration
    a2aAgentUrl: [''],
    a2aAgentId: [''],
    a2aAuthType: ['agentcore'],
    a2aAwsRegion: [''],
    a2aSecretArn: [''],
    a2aCapabilities: [''],
    a2aTimeoutSeconds: [120],
    a2aMaxRetries: [3],
  });

  constructor() {
    // Track protocol changes to show/hide configuration sections
    effect(() => {
      const protocol = this.form.get('protocol')?.value;
      if (protocol) {
        this.selectedProtocol.set(protocol);
      }
    });
  }

  getProtocolDescription(protocol: ToolProtocol | null): string {
    if (!protocol) return '';
    const found = this.protocols.find(p => p.value === protocol);
    return found?.description || '';
  }

  get mcpToolsArray(): FormArray<FormGroup> {
    return this.form.get('mcpTools') as FormArray<FormGroup>;
  }

  private buildMcpToolRow(entry?: MCPToolEntry): FormGroup {
    return this.fb.group({
      name: [entry?.name ?? '', [Validators.required]],
      needsApproval: [entry?.needsApproval ?? false],
      description: [entry?.description ?? ''],
    });
  }

  addMcpTool(): void {
    this.mcpToolsArray.push(this.buildMcpToolRow());
  }

  removeMcpTool(index: number): void {
    this.mcpToolsArray.removeAt(index);
  }

  async discoverMcpTools(): Promise<void> {
    const formValue = this.form.getRawValue();
    if (!formValue.mcpServerUrl) {
      return;
    }

    this.discovering.set(true);
    this.discoverError.set(null);
    try {
      const response = await this.adminToolService.discoverMCPTools({
        serverUrl: formValue.mcpServerUrl,
        transport: formValue.mcpTransport,
        authType: formValue.mcpAuthType,
        awsRegion: formValue.mcpAwsRegion || null,
        apiKeyHeader: formValue.mcpApiKeyHeader || null,
        secretArn: formValue.mcpSecretArn || null,
      });

      // Merge: keep existing rows (and their needsApproval flag), append any
      // newly-discovered names. Update descriptions on existing rows when the
      // server returned one and the row is empty.
      const existingByName = new Map<string, FormGroup>();
      for (const ctrl of this.mcpToolsArray.controls) {
        const name = (ctrl.get('name')?.value ?? '').trim();
        if (name) {
          existingByName.set(name, ctrl);
        }
      }

      for (const tool of response.tools) {
        const existing = existingByName.get(tool.name);
        if (existing) {
          if (tool.description && !existing.get('description')?.value) {
            existing.get('description')?.setValue(tool.description);
          }
        } else {
          this.mcpToolsArray.push(this.buildMcpToolRow({
            name: tool.name,
            needsApproval: false,
            description: tool.description ?? null,
          }));
        }
      }
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Discovery failed.';
      this.discoverError.set(message);
    } finally {
      this.discovering.set(false);
    }
  }

  async ngOnInit(): Promise<void> {
    // Listen for protocol changes to update the signal
    this.form.get('protocol')?.valueChanges.subscribe(value => {
      this.selectedProtocol.set(value);
    });

    // Mutual exclusivity: forwardAuthToken and requiresOauthProvider
    this.form.get('forwardAuthToken')?.valueChanges.subscribe(checked => {
      if (checked && this.form.get('requiresOauthProvider')?.value) {
        this.form.get('requiresOauthProvider')?.setValue('');
      }
    });
    this.form.get('requiresOauthProvider')?.valueChanges.subscribe(value => {
      if (value && this.form.get('forwardAuthToken')?.value) {
        this.form.get('forwardAuthToken')?.setValue(false);
      }
    });

    const id = this.route.snapshot.paramMap.get('toolId');
    if (id) {
      this.toolId.set(id);
      await this.loadTool(id);
    }
  }

  async loadTool(toolId: string): Promise<void> {
    this.loading.set(true);
    try {
      const tool = await this.adminToolService.fetchTool(toolId);

      // Basic fields
      this.form.patchValue({
        toolId: tool.toolId,
        displayName: tool.displayName,
        description: tool.description,
        category: tool.category,
        protocol: tool.protocol,
        status: tool.status,
        isPublic: tool.isPublic,
        enabledByDefault: tool.enabledByDefault,
        requiresOauthProvider: tool.requiresOauthProvider || '',
        forwardAuthToken: tool.forwardAuthToken || false,
      });

      // Update protocol signal
      this.selectedProtocol.set(tool.protocol);

      // MCP configuration
      if (tool.mcpConfig) {
        this.form.patchValue({
          mcpServerUrl: tool.mcpConfig.serverUrl,
          mcpTransport: tool.mcpConfig.transport,
          mcpAuthType: tool.mcpConfig.authType,
          mcpAwsRegion: tool.mcpConfig.awsRegion || '',
          mcpApiKeyHeader: tool.mcpConfig.apiKeyHeader || '',
          mcpSecretArn: tool.mcpConfig.secretArn || '',
          mcpHealthCheckEnabled: tool.mcpConfig.healthCheckEnabled,
        });
        this.mcpToolsArray.clear();
        for (const entry of tool.mcpConfig.tools) {
          this.mcpToolsArray.push(this.buildMcpToolRow(entry));
        }
      }

      // A2A configuration
      if (tool.a2aConfig) {
        this.form.patchValue({
          a2aAgentUrl: tool.a2aConfig.agentUrl,
          a2aAgentId: tool.a2aConfig.agentId || '',
          a2aAuthType: tool.a2aConfig.authType,
          a2aAwsRegion: tool.a2aConfig.awsRegion || '',
          a2aSecretArn: tool.a2aConfig.secretArn || '',
          a2aCapabilities: tool.a2aConfig.capabilities.join('\n'),
          a2aTimeoutSeconds: tool.a2aConfig.timeoutSeconds,
          a2aMaxRetries: tool.a2aConfig.maxRetries,
        });
      }

      // Disable toolId in edit mode
      this.form.get('toolId')?.disable();
    } catch (err: unknown) {
      console.error('Error loading tool:', err);
      this.error.set('Failed to load tool.');
    } finally {
      this.loading.set(false);
    }
  }

  async onSubmit(): Promise<void> {
    if (this.form.invalid) return;

    this.saving.set(true);
    this.error.set(null);

    try {
      const formValue = this.form.getRawValue();

      // Build MCP config if protocol is mcp_external
      let mcpConfig: MCPServerConfig | undefined;
      if (formValue.protocol === 'mcp_external' && formValue.mcpServerUrl) {
        const mcpTools: MCPToolEntry[] = (formValue.mcpTools ?? [])
          .map((row: { name?: string; needsApproval?: boolean; description?: string | null }) => ({
            name: (row.name ?? '').trim(),
            needsApproval: !!row.needsApproval,
            description: row.description?.trim() || null,
          }))
          .filter((row: MCPToolEntry) => row.name.length > 0);

        mcpConfig = {
          serverUrl: formValue.mcpServerUrl,
          transport: formValue.mcpTransport,
          authType: formValue.mcpAuthType,
          awsRegion: formValue.mcpAwsRegion || null,
          apiKeyHeader: formValue.mcpApiKeyHeader || null,
          secretArn: formValue.mcpSecretArn || null,
          tools: mcpTools,
          healthCheckEnabled: formValue.mcpHealthCheckEnabled,
          healthCheckIntervalSeconds: 300,
        };
      }

      // Build A2A config if protocol is a2a
      let a2aConfig: A2AAgentConfig | undefined;
      if (formValue.protocol === 'a2a' && formValue.a2aAgentUrl) {
        a2aConfig = {
          agentUrl: formValue.a2aAgentUrl,
          agentId: formValue.a2aAgentId || null,
          authType: formValue.a2aAuthType,
          awsRegion: formValue.a2aAwsRegion || null,
          secretArn: formValue.a2aSecretArn || null,
          capabilities: formValue.a2aCapabilities ? formValue.a2aCapabilities.split('\n').map((c: string) => c.trim()).filter((c: string) => c) : [],
          timeoutSeconds: formValue.a2aTimeoutSeconds,
          maxRetries: formValue.a2aMaxRetries,
        };
      }

      // Get OAuth provider value (empty string becomes null)
      const requiresOauthProvider = formValue.requiresOauthProvider || null;

      if (this.isEditMode()) {
        // Update existing tool
        await this.adminToolService.updateTool(this.toolId()!, {
          displayName: formValue.displayName,
          description: formValue.description,
          category: formValue.category,
          protocol: formValue.protocol,
          status: formValue.status,
          isPublic: formValue.isPublic,
          enabledByDefault: formValue.enabledByDefault,
          requiresOauthProvider: requiresOauthProvider,
          forwardAuthToken: formValue.forwardAuthToken || false,
          mcpConfig: mcpConfig,
          a2aConfig: a2aConfig,
        });
      } else {
        // Create new tool
        await this.adminToolService.createTool({
          toolId: formValue.toolId,
          displayName: formValue.displayName,
          description: formValue.description,
          category: formValue.category,
          protocol: formValue.protocol,
          status: formValue.status,
          isPublic: formValue.isPublic,
          enabledByDefault: formValue.enabledByDefault,
          requiresOauthProvider: requiresOauthProvider,
          forwardAuthToken: formValue.forwardAuthToken || false,
          mcpConfig: mcpConfig,
          a2aConfig: a2aConfig,
        });
      }

      await this.router.navigate(['/admin/tools']);
    } catch (err: unknown) {
      console.error('Error saving tool:', err);
      const message = err instanceof Error ? err.message : 'Failed to save tool.';
      this.error.set(message);
    } finally {
      this.saving.set(false);
    }
  }
}
