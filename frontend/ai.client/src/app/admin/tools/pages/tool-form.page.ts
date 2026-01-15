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
import { FormBuilder, FormGroup, Validators, ReactiveFormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroCheck,
  heroServer,
  heroUserGroup,
} from '@ng-icons/heroicons/outline';
import { AdminToolService } from '../services/admin-tool.service';
import {
  AdminTool,
  ToolFormData,
  TOOL_CATEGORIES,
  TOOL_PROTOCOLS,
  TOOL_STATUSES,
  MCP_TRANSPORTS,
  MCP_AUTH_TYPES,
  A2A_AUTH_TYPES,
  MCPServerConfig,
  A2AAgentConfig,
  ToolProtocol,
} from '../models/admin-tool.model';

@Component({
  selector: 'app-tool-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, ReactiveFormsModule, NgIcon],
  providers: [provideIcons({ heroArrowLeft, heroCheck, heroServer, heroUserGroup })],
  host: {
    class: 'block p-6',
  },
  template: `
    <div class="max-w-2xl">
      <!-- Header -->
      <div class="mb-6">
        <a
          routerLink="/admin/tools"
          class="inline-flex items-center gap-1 text-sm text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 mb-4"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to Tools
        </a>
        <h1 class="text-3xl/9 font-bold">
          {{ isEditMode() ? 'Edit Tool' : 'Create Tool' }}
        </h1>
        <p class="text-gray-600 dark:text-gray-400">
          {{ isEditMode() ? 'Update tool metadata and settings.' : 'Add a new tool to the catalog.' }}
        </p>
      </div>

      <!-- Loading State -->
      @if (loading()) {
        <div class="flex items-center justify-center h-64">
          <div class="animate-spin rounded-full size-12 border-4 border-gray-300 dark:border-gray-600 border-t-blue-600"></div>
        </div>
      } @else {
        <!-- Form -->
        <form [formGroup]="form" (ngSubmit)="onSubmit()" class="space-y-6">
          <!-- Tool ID (only for create) -->
          @if (!isEditMode()) {
            <div>
              <label for="toolId" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Tool ID
              </label>
              <input
                id="toolId"
                type="text"
                formControlName="toolId"
                class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                placeholder="e.g., my_custom_tool"
              />
              @if (form.get('toolId')?.invalid && form.get('toolId')?.touched) {
                <p class="mt-1 text-sm text-red-600 dark:text-red-400">
                  Tool ID must be 3-50 characters, lowercase letters, numbers, and underscores only.
                </p>
              }
            </div>
          }

          <!-- Display Name -->
          <div>
            <label for="displayName" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Display Name
            </label>
            <input
              id="displayName"
              type="text"
              formControlName="displayName"
              class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
              placeholder="e.g., My Custom Tool"
            />
            @if (form.get('displayName')?.invalid && form.get('displayName')?.touched) {
              <p class="mt-1 text-sm text-red-600 dark:text-red-400">
                Display name is required (1-100 characters).
              </p>
            }
          </div>

          <!-- Description -->
          <div>
            <label for="description" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Description
            </label>
            <textarea
              id="description"
              formControlName="description"
              rows="3"
              class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
              placeholder="Describe what this tool does..."
            ></textarea>
            @if (form.get('description')?.invalid && form.get('description')?.touched) {
              <p class="mt-1 text-sm text-red-600 dark:text-red-400">
                Description is required (max 500 characters).
              </p>
            }
          </div>

          <!-- Category and Protocol Row -->
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label for="category" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Category
              </label>
              <select
                id="category"
                formControlName="category"
                class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
              >
                @for (cat of categories; track cat.value) {
                  <option [value]="cat.value">{{ cat.label }}</option>
                }
              </select>
            </div>

            <div>
              <label for="protocol" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Protocol
              </label>
              <select
                id="protocol"
                formControlName="protocol"
                class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
              >
                @for (proto of protocols; track proto.value) {
                  <option [value]="proto.value">{{ proto.label }}</option>
                }
              </select>
              @if (selectedProtocol()) {
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  {{ getProtocolDescription(selectedProtocol()) }}
                </p>
              }
            </div>
          </div>

          <!-- MCP External Server Configuration -->
          @if (selectedProtocol() === 'mcp_external') {
            <div class="border border-blue-200 dark:border-blue-800 rounded-lg p-4 bg-blue-50/50 dark:bg-blue-900/20">
              <div class="flex items-center gap-2 mb-4">
                <ng-icon name="heroServer" class="size-5 text-blue-600 dark:text-blue-400" />
                <h3 class="text-lg font-semibold text-blue-900 dark:text-blue-100">MCP Server Configuration</h3>
              </div>

              <!-- Server URL -->
              <div class="mb-4">
                <label for="mcpServerUrl" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Server URL <span class="text-red-500">*</span>
                </label>
                <input
                  id="mcpServerUrl"
                  type="url"
                  formControlName="mcpServerUrl"
                  class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                  placeholder="https://xxx.lambda-url.us-west-2.on.aws/"
                />
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  Lambda Function URL or API Gateway endpoint
                </p>
              </div>

              <!-- Transport and Auth Row -->
              <div class="grid grid-cols-2 gap-4 mb-4">
                <div>
                  <label for="mcpTransport" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Transport
                  </label>
                  <select
                    id="mcpTransport"
                    formControlName="mcpTransport"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                  >
                    @for (transport of mcpTransports; track transport.value) {
                      <option [value]="transport.value">{{ transport.label }}</option>
                    }
                  </select>
                </div>

                <div>
                  <label for="mcpAuthType" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Authentication
                  </label>
                  <select
                    id="mcpAuthType"
                    formControlName="mcpAuthType"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                  >
                    @for (auth of mcpAuthTypes; track auth.value) {
                      <option [value]="auth.value">{{ auth.label }}</option>
                    }
                  </select>
                </div>
              </div>

              <!-- AWS Region (shown for aws-iam auth) -->
              @if (form.get('mcpAuthType')?.value === 'aws-iam') {
                <div class="mb-4">
                  <label for="mcpAwsRegion" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    AWS Region
                  </label>
                  <input
                    id="mcpAwsRegion"
                    type="text"
                    formControlName="mcpAwsRegion"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                    placeholder="us-west-2 (auto-detected from URL if blank)"
                  />
                </div>
              }

              <!-- API Key Header (shown for api-key auth) -->
              @if (form.get('mcpAuthType')?.value === 'api-key') {
                <div class="grid grid-cols-2 gap-4 mb-4">
                  <div>
                    <label for="mcpApiKeyHeader" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      API Key Header
                    </label>
                    <input
                      id="mcpApiKeyHeader"
                      type="text"
                      formControlName="mcpApiKeyHeader"
                      class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                      placeholder="x-api-key"
                    />
                  </div>
                  <div>
                    <label for="mcpSecretArn" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                      Secret ARN
                    </label>
                    <input
                      id="mcpSecretArn"
                      type="text"
                      formControlName="mcpSecretArn"
                      class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                      placeholder="arn:aws:secretsmanager:..."
                    />
                  </div>
                </div>
              }

              <!-- MCP Tools -->
              <div class="mb-4">
                <label for="mcpTools" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Available Tools
                </label>
                <textarea
                  id="mcpTools"
                  formControlName="mcpTools"
                  rows="3"
                  class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600 font-mono text-sm"
                  placeholder="search_policies&#10;get_policy_by_number&#10;list_policy_categories"
                ></textarea>
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  One tool name per line. Leave empty to discover tools at runtime.
                </p>
              </div>

              <!-- Health Check -->
              <label class="flex items-center gap-2">
                <input
                  type="checkbox"
                  formControlName="mcpHealthCheckEnabled"
                  class="size-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                />
                <span class="text-sm font-medium text-gray-700 dark:text-gray-300">
                  Enable Health Checks
                </span>
              </label>
            </div>
          }

          <!-- A2A Agent Configuration -->
          @if (selectedProtocol() === 'a2a') {
            <div class="border border-purple-200 dark:border-purple-800 rounded-lg p-4 bg-purple-50/50 dark:bg-purple-900/20">
              <div class="flex items-center gap-2 mb-4">
                <ng-icon name="heroUserGroup" class="size-5 text-purple-600 dark:text-purple-400" />
                <h3 class="text-lg font-semibold text-purple-900 dark:text-purple-100">Agent-to-Agent Configuration</h3>
              </div>

              <!-- Agent URL -->
              <div class="mb-4">
                <label for="a2aAgentUrl" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Agent URL <span class="text-red-500">*</span>
                </label>
                <input
                  id="a2aAgentUrl"
                  type="url"
                  formControlName="a2aAgentUrl"
                  class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                  placeholder="https://agent-endpoint.example.com/"
                />
              </div>

              <!-- Agent ID and Auth Row -->
              <div class="grid grid-cols-2 gap-4 mb-4">
                <div>
                  <label for="a2aAgentId" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Agent ID
                  </label>
                  <input
                    id="a2aAgentId"
                    type="text"
                    formControlName="a2aAgentId"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                    placeholder="AgentCore Runtime ID (optional)"
                  />
                </div>

                <div>
                  <label for="a2aAuthType" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Authentication
                  </label>
                  <select
                    id="a2aAuthType"
                    formControlName="a2aAuthType"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                  >
                    @for (auth of a2aAuthTypes; track auth.value) {
                      <option [value]="auth.value">{{ auth.label }}</option>
                    }
                  </select>
                </div>
              </div>

              <!-- AWS Region (shown for aws-iam or agentcore auth) -->
              @if (form.get('a2aAuthType')?.value === 'aws-iam' || form.get('a2aAuthType')?.value === 'agentcore') {
                <div class="mb-4">
                  <label for="a2aAwsRegion" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    AWS Region
                  </label>
                  <input
                    id="a2aAwsRegion"
                    type="text"
                    formControlName="a2aAwsRegion"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                    placeholder="us-west-2"
                  />
                </div>
              }

              <!-- Capabilities -->
              <div class="mb-4">
                <label for="a2aCapabilities" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                  Capabilities
                </label>
                <textarea
                  id="a2aCapabilities"
                  formControlName="a2aCapabilities"
                  rows="3"
                  class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600 font-mono text-sm"
                  placeholder="report_generation&#10;data_analysis&#10;document_creation"
                ></textarea>
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  One capability per line
                </p>
              </div>

              <!-- Timeout and Retries -->
              <div class="grid grid-cols-2 gap-4">
                <div>
                  <label for="a2aTimeoutSeconds" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Timeout (seconds)
                  </label>
                  <input
                    id="a2aTimeoutSeconds"
                    type="number"
                    formControlName="a2aTimeoutSeconds"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                    min="1"
                    max="600"
                  />
                </div>
                <div>
                  <label for="a2aMaxRetries" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                    Max Retries
                  </label>
                  <input
                    id="a2aMaxRetries"
                    type="number"
                    formControlName="a2aMaxRetries"
                    class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                    min="0"
                    max="10"
                  />
                </div>
              </div>
            </div>
          }

          <!-- Status and Icon Row -->
          <div class="grid grid-cols-2 gap-4">
            <div>
              <label for="status" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Status
              </label>
              <select
                id="status"
                formControlName="status"
                class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
              >
                @for (stat of statuses; track stat.value) {
                  <option [value]="stat.value">{{ stat.label }}</option>
                }
              </select>
            </div>

            <div>
              <label for="icon" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
                Icon
              </label>
              <input
                id="icon"
                type="text"
                formControlName="icon"
                class="w-full px-3 py-2 border border-gray-300 rounded-sm focus:ring-2 focus:ring-blue-500 focus:border-blue-500 dark:bg-gray-800 dark:border-gray-600"
                placeholder="e.g., heroCodeBracket"
              />
            </div>
          </div>

          <!-- Checkboxes -->
          <div class="space-y-3">
            <label class="flex items-center gap-2">
              <input
                type="checkbox"
                formControlName="isPublic"
                class="size-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <span class="text-sm font-medium text-gray-700 dark:text-gray-300">
                Public Tool
              </span>
              <span class="text-sm text-gray-500 dark:text-gray-400">
                (Available to all authenticated users)
              </span>
            </label>

            <label class="flex items-center gap-2">
              <input
                type="checkbox"
                formControlName="enabledByDefault"
                class="size-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <span class="text-sm font-medium text-gray-700 dark:text-gray-300">
                Enabled by Default
              </span>
              <span class="text-sm text-gray-500 dark:text-gray-400">
                (Tool is enabled when user first accesses it)
              </span>
            </label>

            <label class="flex items-center gap-2">
              <input
                type="checkbox"
                formControlName="requiresApiKey"
                class="size-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              />
              <span class="text-sm font-medium text-gray-700 dark:text-gray-300">
                Requires API Key
              </span>
              <span class="text-sm text-gray-500 dark:text-gray-400">
                (Tool requires external API key)
              </span>
            </label>
          </div>

          <!-- Error Message -->
          @if (error()) {
            <div class="p-4 bg-red-50 border border-red-200 rounded-sm text-red-800 dark:bg-red-900/20 dark:border-red-800 dark:text-red-200">
              {{ error() }}
            </div>
          }

          <!-- Actions -->
          <div class="flex items-center justify-end gap-3 pt-4">
            <a
              routerLink="/admin/tools"
              class="px-4 py-2 border border-gray-300 rounded-sm hover:bg-gray-100 dark:border-gray-600 dark:hover:bg-gray-700"
            >
              Cancel
            </a>
            <button
              type="submit"
              [disabled]="form.invalid || saving()"
              class="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-sm hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <ng-icon name="heroCheck" class="size-5" />
              {{ saving() ? 'Saving...' : (isEditMode() ? 'Update Tool' : 'Create Tool') }}
            </button>
          </div>
        </form>
      }
    </div>
  `,
})
export class ToolFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private adminToolService = inject(AdminToolService);

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

  readonly isEditMode = computed(() => !!this.toolId());
  readonly selectedProtocol = signal<ToolProtocol>('local');

  form: FormGroup = this.fb.group({
    toolId: ['', [Validators.required, Validators.pattern(/^[a-z][a-z0-9_]{2,49}$/)]],
    displayName: ['', [Validators.required, Validators.minLength(1), Validators.maxLength(100)]],
    description: ['', [Validators.required, Validators.maxLength(500)]],
    category: ['utility'],
    protocol: ['local'],
    status: ['active'],
    icon: [''],
    isPublic: [false],
    enabledByDefault: [false],
    requiresApiKey: [false],
    // MCP External Server configuration
    mcpServerUrl: [''],
    mcpTransport: ['streamable-http'],
    mcpAuthType: ['aws-iam'],
    mcpAwsRegion: [''],
    mcpApiKeyHeader: [''],
    mcpSecretArn: [''],
    mcpTools: [''],
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

  async ngOnInit(): Promise<void> {
    // Listen for protocol changes to update the signal
    this.form.get('protocol')?.valueChanges.subscribe(value => {
      this.selectedProtocol.set(value);
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
        icon: tool.icon || '',
        isPublic: tool.isPublic,
        enabledByDefault: tool.enabledByDefault,
        requiresApiKey: tool.requiresApiKey,
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
          mcpTools: tool.mcpConfig.tools.join('\n'),
          mcpHealthCheckEnabled: tool.mcpConfig.healthCheckEnabled,
        });
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
        mcpConfig = {
          serverUrl: formValue.mcpServerUrl,
          transport: formValue.mcpTransport,
          authType: formValue.mcpAuthType,
          awsRegion: formValue.mcpAwsRegion || null,
          apiKeyHeader: formValue.mcpApiKeyHeader || null,
          secretArn: formValue.mcpSecretArn || null,
          tools: formValue.mcpTools ? formValue.mcpTools.split('\n').map((t: string) => t.trim()).filter((t: string) => t) : [],
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

      if (this.isEditMode()) {
        // Update existing tool
        await this.adminToolService.updateTool(this.toolId()!, {
          displayName: formValue.displayName,
          description: formValue.description,
          category: formValue.category,
          protocol: formValue.protocol,
          status: formValue.status,
          icon: formValue.icon || undefined,
          isPublic: formValue.isPublic,
          enabledByDefault: formValue.enabledByDefault,
          requiresApiKey: formValue.requiresApiKey,
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
          icon: formValue.icon || undefined,
          isPublic: formValue.isPublic,
          enabledByDefault: formValue.enabledByDefault,
          requiresApiKey: formValue.requiresApiKey,
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
