# Assistants Module

This module handles the management of AI assistants within the application.

## Structure

```
assistants/
├── assistants.page.ts          # Main page component
├── assistants.page.html
├── assistants.page.css
├── components/                 # Reusable UI components
│   ├── assistant-list.component.*
│   ├── assistant-card.component.*
│   └── assistant-form.component.*
├── services/                   # Business logic and API services
│   ├── assistant.service.ts        # State management with signals
│   └── assistant-api.service.ts    # HTTP API calls
└── models/                     # TypeScript interfaces and types
    └── assistant.model.ts
```

## Components

### AssistantListComponent
Displays a list of assistants with Tailwind CSS styling.

**Inputs:**
- `assistants`: Array of Assistant objects

**Outputs:**
- `assistantSelected`: Emits when an assistant is clicked

### AssistantCardComponent
Displays a single assistant in a card format with action buttons.

**Inputs:**
- `assistant`: Single Assistant object

**Outputs:**
- `editClicked`: Emits when edit button is clicked
- `deleteClicked`: Emits when delete button is clicked

### AssistantFormComponent
Reactive form for creating or editing an assistant.

**Inputs:**
- `mode`: 'create' or 'edit'

**Outputs:**
- `formSubmitted`: Emits form data when submitted
- `formCancelled`: Emits when form is cancelled

## Services

### AssistantService
Main service for managing assistant state using Angular signals. Provides reactive state management for the assistants module.

### AssistantApiService
Service for making HTTP API calls to the backend. Handles all network communication for assistant operations.

## Models

### Assistant
Main interface representing an assistant entity.

### AssistantConfig
Configuration options for an assistant.

### CreateAssistantRequest
Payload for creating a new assistant.

### UpdateAssistantRequest
Payload for updating an existing assistant.

## Usage

Import the `AssistantsPage` component in your routes configuration:

```typescript
{
  path: 'assistants',
  loadComponent: () => import('./assistants/assistants.page').then(m => m.AssistantsPage)
}
```

## Testing Locally

### Prerequisites
1. Backend server running on `http://localhost:8000`
2. Valid JWT token (logged in user)
3. No `ASSISTANTS_TABLE_NAME` env var set (uses local file storage)

### Test Flow

1. **Navigate to Assistants Page**
   - Go to `/assistants`
   - Page loads and fetches assistants from backend
   - Shows loading state while fetching

2. **Create New Assistant (Draft)**
   - Click "Create New" button
   - Backend creates draft assistant with auto-generated ID
   - Redirects to `/assistants/{id}/edit`
   - Form loads with draft data (status: DRAFT)

3. **Complete the Assistant**
   - Fill in all required fields:
     - Name (min 3 characters)
     - Description (min 10 characters)
     - Instructions (min 20 characters - the system prompt)
     - Vector Index ID (defaults to `idx_assistants`)
   - Click "Create Assistant" or "Update Assistant"
   - Status transitions from DRAFT to COMPLETE
   - Redirects back to `/assistants` list

4. **Verify Backend Storage**
   - Check `backend/src/assistants/` directory
   - Should see `assistant_AST-{id}.json` files

### Data Flow

```
User clicks "Create New"
  ↓
POST /api/assistants/draft
  ↓
Draft created (status: DRAFT)
  ↓
Navigate to /assistants/{id}/edit
  ↓
User fills form
  ↓
PUT /api/assistants/{id} (status: COMPLETE)
  ↓
Assistant saved
  ↓
Navigate to /assistants
  ↓
List refreshes showing new assistant
```

### Browser Console Testing

```javascript
// Check service is loaded
const service = document.querySelector('app-assistants').__ngContext__[8];

// Check assistants signal
console.log(service.assistants());

// Check loading state
console.log(service.loading());

// Check error state
console.log(service.error());
```

## Status

✅ HTTP calls implemented  
✅ Business logic in AssistantService  
✅ Reactive forms initialized  
✅ Form validation added  
✅ Error handling implemented  
✅ Loading states implemented  
⏳ Unit tests pending
