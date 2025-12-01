# Claude Code Instructions for AgentCore Public Stack

## Project Overview

This is a production-ready multi-agent conversational AI system built with AWS Bedrock AgentCore and Strands Agents. The application combines agent orchestration with MCP-based tool integration, multimodal I/O, financial analysis tools, and deep research capabilities.

**Tech Stack:**
- **Frontend**: Angular v21, TypeScript, Tailwind v4.1 CSS
- **Backend**: Python 3.13+, FastAPI
- **Agent Framework**: Strands Agents (AWS Bedrock)
- **Cloud Services**: AWS Bedrock AgentCore (Runtime, Memory, Gateway, Code Interpreter, Browser)

## Project Structure

```
/
├── backend/
│   └── src/
│       ├── agentcore/              # Core agent implementation
│       │   ├── agent/              # Agent orchestration & session management
│       │   ├── builtin_tools/      # AWS SDK tools (Code Interpreter, Browser)
│       │   ├── local_tools/        # Direct function call tools
│       │   └── config.py           # Environment configuration
│       └── api/                    # FastAPI routes
│           ├── chat/               # Chat endpoints & SSE streaming
│           ├── health/             # Health check endpoints
│           └── utils/              # Event processing utilities
│
└── frontend/
    └── ai.client/
        └── src/
            ├── app/
            │   ├── components/     # Shared UI components
            │   ├── conversations/  # Chat UI & services
            │   └── services/       # State management, HTTP services
            └── environments/       # Environment configuration
```

## Key Architecture Patterns

### 1. Multi-Protocol Tool Architecture

Tools communicate via different protocols:

| Protocol | Location | Examples | Auth |
|----------|----------|----------|------|
| Direct call | `backend/src/agentcore/local_tools/` | Weather, Calculator, Visualization | N/A |
| AWS SDK | `backend/src/agentcore/builtin_tools/` | Code Interpreter, Browser | IAM |
| MCP + SigV4 | Cloud Lambda (Gateway) | Wikipedia, ArXiv, Finance | AWS SigV4 |
| A2A | Cloud Runtime (WIP) | Report Writer | AgentCore auth |

### 2. Turn-based Session Management

**File:** `backend/src/agentcore/agent/turn_based_session_manager.py`

Buffers messages within a turn to reduce AgentCore Memory API calls (75% reduction):
- Buffers: User → Assistant → Tool → Result
- Flushes: Single merged API call per turn
- Detects turn completion automatically

### 3. Dynamic Tool Filtering

**File:** `backend/src/agentcore/agent/agent.py:277-318`

Users can enable/disable tools via UI. The agent filters tool definitions before each invocation to reduce token usage.

### 4. Prompt Caching Strategy

**File:** `backend/src/agentcore/agent/agent.py:67-142`

Two-level caching:
- System prompt caching (static)
- Conversation history caching (dynamic, last 2 messages)
- Max 4 cache points with rotation

### 5. Multimodal I/O

**Input:** Images (PNG, JPEG, GIF, WebP), Documents (PDF, CSV, DOCX)
**Output:** Charts from Code Interpreter, screenshots from Browser
**Implementation:** `backend/src/agentcore/agent/agent.py:483-549`

## Development Guidelines

### Python Backend

**Style:**
- Use type hints for all functions
- Follow PEP 8 conventions
- Use Pydantic models for data validation
- Async/await for I/O operations

**Key Files:**
- `backend/src/agentcore/agent/agent.py` - Main ChatbotAgent implementation
- `backend/src/api/chat/routes.py` - SSE streaming endpoints
- `backend/src/api/chat/service.py` - Business logic layer

**Tool Development:**
- Local tools: Add to `backend/src/agentcore/local_tools/`
- Use `@tool` decorator from Strands framework
- Return `ToolResult` with multimodal content support
- Follow existing tool patterns (see `weather.py`, `visualization.py`)

**Testing Tools:**
```bash
# From backend/src
python -m pytest
python -m pytest tests/test_agent.py -v
```

### Angular Frontend

This project uses Angular v21+ with modern best practices.

#### TypeScript Best Practices

- **Use strict type checking** - Enable TypeScript strict mode
- **Prefer type inference** when the type is obvious
- **Avoid `any` type** - Use `unknown` when type is uncertain

#### Angular Component Guidelines

**CRITICAL - Standalone Components:**
- Always use standalone components (Angular v21+)
- **Do NOT set `standalone: true`** - It's the default in Angular v20+
- Components are standalone by default

**Component Structure:**
- Keep components small and focused on a single responsibility
- Use `input()` and `output()` functions instead of `@Input()`/`@Output()` decorators
- Use `computed()` for derived state
- Set `changeDetection: ChangeDetectionStrategy.OnPush` in `@Component` decorator
- Prefer inline templates for small components
- When using external templates/styles, use paths relative to the component TS file

**Host Bindings:**
- **Do NOT use `@HostBinding` and `@HostListener` decorators**
- Put host bindings inside the `host` object of the `@Component` or `@Directive` decorator instead

Example:
```typescript
import { Component, ChangeDetectionStrategy, input, output, computed } from '@angular/core';

@Component({
  selector: 'app-my-component',
  // standalone: true,  ❌ DO NOT include this
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(click)': 'handleClick()',
    '[class.active]': 'isActive()'
  },
  template: `<div>{{ displayText() }}</div>`,
})
export class MyComponent {
  // Use input/output functions
  value = input<string>();
  changed = output<string>();

  // Use computed for derived state
  displayText = computed(() => this.value()?.toUpperCase() ?? '');

  handleClick() {
    this.changed.emit(this.value());
  }
}
```

#### Template Best Practices

- Keep templates simple and avoid complex logic
- **Use native control flow** instead of structural directives:
  - Use `@if` instead of `*ngIf`
  - Use `@for` instead of `*ngFor`
  - Use `@switch` instead of `*ngSwitch`
- Use the async pipe to handle observables
- **Do NOT use `ngClass`** - Use `class` bindings instead
- **Do NOT use `ngStyle`** - Use `style` bindings instead
- **Do NOT assume globals** like `new Date()` are available in templates
- **Do NOT write arrow functions in templates** - They are not supported

Example:
```typescript
template: `
  <!-- ✅ Use native control flow -->
  @if (user()) {
    <div>{{ user().name }}</div>
  }

  @for (item of items(); track item.id) {
    <div>{{ item.name }}</div>
  }

  <!-- ✅ Use class bindings -->
  <div [class.active]="isActive()"></div>

  <!-- ✅ Use style bindings -->
  <div [style.color]="textColor()"></div>
`
```

#### Forms

- **Prefer Reactive forms** instead of Template-driven forms
- Use FormBuilder with typed forms
- Validate forms using Validators

#### Images

- **Use `NgOptimizedImage`** for all static images
- Note: `NgOptimizedImage` does not work for inline base64 images

#### State Management

- **Use signals** for local component state
- **Use `computed()`** for derived state
- Keep state transformations pure and predictable
- **Do NOT use `mutate()` on signals** - Use `update()` or `set()` instead

Example:
```typescript
import { signal, computed } from '@angular/core';

export class MyComponent {
  count = signal(0);
  doubleCount = computed(() => this.count() * 2);

  increment() {
    this.count.update(value => value + 1);  // ✅
    // this.count.mutate(value => value++);  // ❌
  }
}
```

#### Services

- Design services around a single responsibility
- **Use `providedIn: 'root'`** for singleton services
- **Use `inject()` function** instead of constructor injection

Example:
```typescript
import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';

@Injectable({
  providedIn: 'root'
})
export class DataService {
  private http = inject(HttpClient);  // ✅ Use inject()

  // constructor(private http: HttpClient) {}  // ❌ Avoid constructor injection
}
```

#### Accessibility Requirements

- **MUST pass all AXE checks**
- **MUST follow WCAG AA minimums**:
  - Focus management
  - Color contrast
  - ARIA attributes

#### Key Files

- `frontend/ai.client/src/app/conversations/conversation.page.ts` - Main chat interface
- `frontend/ai.client/src/app/conversations/services/chat/` - Chat state & HTTP services
- `frontend/ai.client/src/app/components/` - Shared components

#### State Management Architecture

- `chat-state.service.ts` - Message state using signals and RxJS BehaviorSubject
- `chat-http.service.ts` - API communication
- `stream-parser.service.ts` - SSE event parsing

#### Testing

```bash
# From frontend/ai.client
npm test
ng test --watch=false
```

#### Tailwind CSS Best Practices

This project uses **Tailwind CSS v4.1+** for styling. Follow these critical guidelines:

**Core Principles:**
- **Always use Tailwind CSS v4.1+** utilities
- **Never use `@apply`** - Use CSS variables, the `--spacing()` function, or framework components instead
- **Always use predefined scale values** - Avoid arbitrary values like `ml-[16px]`, use `ml-4` instead
- **Always use `min-h-dvh`** instead of `min-h-screen` (Safari mobile bug fix)

**Removed/Renamed Utilities (v4):**

| ❌ Never Use (v3) | ✅ Always Use (v4) |
|-------------------|-------------------|
| `bg-opacity-*` | `bg-black/50` (opacity modifier) |
| `text-opacity-*` | `text-black/50` |
| `flex-shrink-*` | `shrink-*` |
| `flex-grow-*` | `grow-*` |
| `overflow-ellipsis` | `text-ellipsis` |
| `bg-gradient-*` | `bg-linear-*` |
| `shadow-sm` | `shadow-xs` |
| `shadow` | `shadow-sm` |
| `rounded-sm` | `rounded-xs` |
| `rounded` | `rounded-sm` |
| `outline-none` | `outline-hidden` |
| `ring` | `ring-3` |

**Layout & Spacing:**

```html
<!-- ✅ ALWAYS use gap for flex/grid spacing -->
<div class="flex gap-4">
  <div>Item 1</div>
  <div>Item 2</div>
</div>

<!-- ❌ NEVER use space-x/space-y in flex/grid -->
<div class="flex space-x-4">...</div>

<!-- ✅ Use size-* for equal width/height -->
<div class="size-8">Icon</div>

<!-- ❌ Don't use separate w-* and h-* when equal -->
<div class="w-8 h-8">Icon</div>
```

**Typography:**

```html
<!-- ✅ ALWAYS use line height modifiers with text size -->
<p class="text-base/7">Text with line height</p>
<h1 class="text-2xl/9">Heading</h1>

<!-- ❌ NEVER use separate leading-* classes -->
<p class="text-base leading-7">Wrong pattern</p>
```

**Colors & Opacity:**

```html
<!-- ✅ Use opacity modifier syntax -->
<div class="bg-red-500/60 text-white/90">Content</div>

<!-- ❌ Don't use old opacity utilities -->
<div class="bg-red-500 bg-opacity-60 text-white text-opacity-90">Wrong</div>
```

**Gradients:**

```html
<!-- ✅ Use bg-linear-* (v4) -->
<div class="bg-linear-to-br from-violet-500 to-fuchsia-500"></div>

<!-- ❌ Don't use bg-gradient-* (deprecated) -->
<div class="bg-gradient-to-br from-violet-500 to-fuchsia-500"></div>
```

**Responsive Design:**

```html
<!-- ✅ Only specify breakpoint when value changes -->
<div class="px-4 lg:px-8">Content</div>

<!-- ❌ Don't repeat unchanged values across breakpoints -->
<div class="px-4 md:px-4 lg:px-4">Content</div>
```

**Dark Mode:**

```html
<!-- ✅ Light mode first, then dark mode -->
<div class="bg-white text-black dark:bg-black dark:text-white">
  <button class="hover:bg-gray-100 dark:hover:bg-gray-800">Click</button>
</div>
```

**New v4 Features:**

```html
<!-- Container Queries -->
<article class="@container">
  <div class="flex flex-col @md:flex-row @lg:gap-8">
    <img class="w-full @md:w-48" />
  </div>
</article>

<!-- Text Shadows (v4.1) -->
<h1 class="text-shadow-lg">Large shadow</h1>

<!-- Masking (v4.1) -->
<div class="mask-t-from-50%">Top fade</div>
<div class="mask-linear-from-white mask-linear-to-black/60">Gradient mask</div>
```

**CSS Variables & Theme Extension:**

```css
/* Access theme values */
.custom-element {
  background: var(--color-red-500);
  border-radius: var(--radius-lg);
  margin-top: calc(100vh - --spacing(16));
}

/* Extend theme */
@theme {
  --color-mint-500: oklch(0.72 0.11 178);
}
```

## Running the Project

### Local Development

```bash
# Initial setup
./setup.sh

# Configure environment
cd backend/src
cp .env.example .env
# Edit .env with AWS credentials

# Start all services
cd ../..
./start.sh
```

**Access:**
- Frontend: http://localhost:4200
- Backend API: http://localhost:8000
- Health check: http://localhost:8000/health

### Docker Services

**Frontend Container:**
- Port: 4200
- Hot reload enabled
- Node v18+

**Backend Container:**
- Port: 8000
- Python 3.13
- Auto-reload with uvicorn

## Environment Configuration

### Backend (.env)

Required variables:
```bash
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=<your-key>
AWS_SECRET_ACCESS_KEY=<your-secret>

# AgentCore
AGENTCORE_MEMORY_ID=<memory-id>
AGENTCORE_RUNTIME_ID=<runtime-id>

# Optional API keys
TAVILY_API_KEY=<key>
GOOGLE_SEARCH_API_KEY=<key>
```

### Frontend (environment.ts)

```typescript
export const environment = {
  production: false,
  apiUrl: 'http://localhost:8000',
};
```

## Common Tasks

### Adding a New Local Tool

1. Create tool file in `backend/src/agentcore/local_tools/`
2. Implement with `@tool` decorator
3. Add to `__init__.py` exports
4. Register in `agent.py` tool list
5. Update frontend tool configuration

Example:
```python
from strands_agents import tool, ToolResult

@tool
async def my_new_tool(param: str) -> ToolResult:
    """Tool description for the model."""
    result = perform_operation(param)
    return {
        "content": [{"text": result}],
        "status": "success"
    }
```

### Adding a UI Component

1. Create component in `frontend/ai.client/src/app/components/`
2. Use modern Angular patterns (signals, native control flow, OnPush)
3. Export from `index.ts`
4. Import in parent component

Example:
```typescript
import { Component, ChangeDetectionStrategy, input, output, computed, inject, signal } from '@angular/core';
import { CommonModule } from '@angular/common';

@Component({
  selector: 'app-my-component',
  // Do NOT set standalone: true (it's the default in Angular 20+)
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    @if (isVisible()) {
      <div [class.active]="isActive()">
        {{ displayText() }}
      </div>
    }

    @for (item of items(); track item.id) {
      <div>{{ item.name }}</div>
    }
  `,
  styles: [`
    .active {
      color: blue;
    }
  `]
})
export class MyComponent {
  // Use input/output functions
  title = input.required<string>();
  items = input<Item[]>([]);
  clicked = output<void>();

  // Use signals for local state
  isActive = signal(false);

  // Use computed for derived state
  displayText = computed(() => this.title().toUpperCase());
  isVisible = computed(() => this.items().length > 0);

  // Use inject() for dependencies
  private dataService = inject(DataService);

  handleClick() {
    this.isActive.update(value => !value);
    this.clicked.emit();
  }
}
```

**What NOT to do:**
```typescript
// ❌ WRONG - Old patterns
import { Component, Input, Output, EventEmitter, HostBinding, HostListener } from '@angular/core';

@Component({
  selector: 'app-old-component',
  standalone: true,  // ❌ Don't set this
  template: `
    <!-- ❌ Don't use old structural directives -->
    <div *ngIf="isVisible">
      <div *ngFor="let item of items">{{ item }}</div>
    </div>

    <!-- ❌ Don't use ngClass or ngStyle -->
    <div [ngClass]="{'active': isActive}"></div>
    <div [ngStyle]="{'color': textColor}"></div>
  `
})
export class OldComponent {
  @Input() title!: string;  // ❌ Don't use @Input decorator
  @Output() clicked = new EventEmitter<void>();  // ❌ Don't use @Output decorator

  @HostBinding('class.active') isActive = true;  // ❌ Don't use @HostBinding

  @HostListener('click')  // ❌ Don't use @HostListener
  onClick() {
    this.clicked.emit();
  }

  constructor(private dataService: DataService) {}  // ❌ Don't use constructor injection
}
```

### Debugging SSE Streaming

**Backend:**
- Check logs in `agentcore.log`
- Add logging in `backend/src/api/utils/event_processor.py`
- Monitor FastAPI console output

**Frontend:**
- Check Network tab for SSE events
- Add logging in `stream-parser.service.ts`
- Monitor service state in Angular DevTools

## Code Reference Patterns

When discussing code, use this format for references:
- `filename:line` - e.g., `agent.py:277`
- `directory/filename` - e.g., `local_tools/weather.py`
- `class.method` - e.g., `ChatbotAgent.create_agent`

## Important Constraints

1. **No Over-engineering**: Only implement what's explicitly requested
2. **Security**: Watch for injection vulnerabilities (command, XSS, SQL)
3. **Token Optimization**: Consider token usage when modifying agent prompts
4. **Multimodal Support**: Maintain compatibility with image/document handling
5. **Session Management**: Respect turn-based buffering patterns
6. **Tool Protocols**: Use correct protocol for tool type (Direct/AWS SDK/MCP/A2A)

## Known Limitations

- AgentCore Gateway requires cloud deployment (not available locally)
- AgentCore Memory uses local file storage in development
- A2A Report Writer tools are work in progress
- Browser automation requires Nova Act model access

## Testing Guidelines

### Backend Tests
- Unit tests for tools in `backend/tests/`
- Integration tests for agent orchestration
- Mock AWS services in tests

### Frontend Tests
- Component tests with TestBed
- Service tests with RxJS marble testing
- E2E tests for critical flows

## Documentation References

- Main README: `README.md` - Full project documentation
- Tool Specifications: `docs/TOOLS.md` - Detailed tool list
- Architecture Diagrams: `docs/images/` - Visual references

## When Making Changes

1. Read existing code first before suggesting modifications
2. Maintain consistency with existing patterns
3. Update tests if changing functionality
4. Consider impact on token usage (prompt caching)
5. Preserve multimodal content handling
6. Test locally before suggesting cloud deployment changes

## Quick Reference Commands

```bash
# Backend
cd backend/src && python -m uvicorn api.main:app --reload

# Frontend
cd frontend/ai.client && ng serve

# Docker
docker-compose up --build

# Testing
cd backend/src && pytest
cd frontend/ai.client && npm test

# Logs
tail -f agentcore.log
docker-compose logs -f backend
```

## Git Workflow

- Main branch: `main`
- Feature branches: `feature/description`
- Current worktree: `wonderful-wiles`
- Always check status before commits

## Contact & Support

- GitHub Issues: https://github.com/aws-samples/sample-strands-agent-with-agentcore/issues
- License: MIT
