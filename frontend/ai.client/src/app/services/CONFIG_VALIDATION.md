# Frontend Configuration Validation

## Overview

Runtime configuration validation has been implemented to catch configuration errors early in the application lifecycle. This prevents the app from starting with invalid or missing configuration, providing clear error messages to help users resolve issues.

## Components

### 1. ConfigValidatorService (`config-validator.service.ts`)

**Purpose**: Validates environment configuration at runtime

**Key Features**:
- Validates required fields (appApiUrl)
- Validates URL format
- Checks for localhost URLs in production mode
- Returns validation results with detailed error messages
- Exposes validation errors via Angular signal for reactive UI updates

**Validation Rules**:
1. **Required Fields**: `appApiUrl` must be present
2. **Valid URLs**: Both URLs must be valid URL format
3. **Production Mode**: In production, URLs cannot be localhost/127.0.0.1/::1
4. **Localhost Detection**: Detects localhost, 127.0.0.1, ::1, and *.localhost domains

**API**:
```typescript
interface ConfigValidationResult {
  valid: boolean;
  errors: string[];
}

class ConfigValidatorService {
  readonly validationErrors: Signal<string[]>;
  validateConfig(): ConfigValidationResult;
}
```

### 2. ConfigErrorComponent (`config-error/config-error.component.ts`)

**Purpose**: Displays full-page error when configuration is invalid

**Features**:
- Professional, accessible error display
- Lists all configuration errors
- Provides common causes and solutions
- Separate guidance for production vs local development
- Fully responsive design
- Dark mode support
- WCAG 2.1 AA compliant

**Design**:
- Full-page centered layout
- Card-based design with clear visual hierarchy
- Color-coded sections (error header, info boxes)
- Icon-based visual cues
- Monospace font for technical details

### 3. App Initialization Integration

**APP_INITIALIZER**: Validation runs before app bootstrap
- Configured in `app.config.ts`
- Runs synchronously before Angular starts
- Populates validation errors signal

**App Component**: Conditionally displays error or normal app
- Checks `configValidator.validationErrors()` signal
- Shows `ConfigErrorComponent` if errors exist
- Shows normal app UI if validation passes

## Usage

### For Developers

The validation runs automatically on app startup. No manual invocation needed.

### For Production Deployments

Set these environment variables during build:
```bash
export APP_API_URL="https://api.example.com"
export INFERENCE_API_URL="https://inference.example.com"
export PRODUCTION="true"
export ENABLE_AUTHENTICATION="true"
```

### For Local Development

No configuration needed. Default localhost URLs work automatically:
- `appApiUrl`: http://localhost:8000
- `production`: false

## Error Messages

### Missing Configuration
```
appApiUrl is required but not configured
```

### Invalid URL Format
```
appApiUrl is not a valid URL: "not-a-url"
```

### Localhost in Production
```
appApiUrl cannot be localhost in production mode. 
Set APP_API_URL environment variable to your production API URL.
```

## Testing

Comprehensive unit tests in `config-validator.service.spec.ts`:
- ✅ Valid localhost URLs in development mode
- ✅ Missing required fields detection
- ✅ Invalid URL format detection
- ✅ Localhost detection in production mode
- ✅ Production URLs in production mode
- ✅ IPv4 localhost (127.0.0.1) detection
- ✅ IPv6 localhost (::1) detection
- ✅ Signal updates
- ✅ Multiple error accumulation

## Architecture Decisions

### Why APP_INITIALIZER?
- Runs before app bootstrap
- Prevents app from starting with invalid config
- Synchronous validation ensures errors are caught early

### Why Signal-Based Errors?
- Reactive updates to UI
- Angular's modern state management
- Efficient change detection with OnPush

### Why Full-Page Error?
- Configuration errors are critical startup failures
- Full-page display ensures visibility
- Prevents confusing partial app states
- Provides comprehensive troubleshooting guidance

### Why Not Throw Errors?
- Throwing would show generic Angular error page
- Custom error component provides better UX
- Actionable guidance helps users resolve issues
- Professional appearance maintains brand quality

## Integration with Build Process

The validation works with the build-time variable injection:

1. **Build Script** (`scripts/stack-frontend/build.sh`):
   - Injects environment variables into `environment.ts`
   - Sets production flag

2. **Runtime Validation**:
   - Validates injected values
   - Catches injection failures
   - Detects misconfiguration

3. **Error Display**:
   - Shows clear error messages
   - Guides users to fix configuration
   - Prevents silent failures

## Future Enhancements

Potential improvements:
- [ ] Validate authentication configuration
- [ ] Check API endpoint connectivity
- [ ] Validate feature flags
- [ ] Add configuration health check endpoint
- [ ] Support configuration reload without restart
- [ ] Add telemetry for configuration errors

## Related Files

- `src/environments/environment.ts` - Configuration values
- `src/app/app.config.ts` - APP_INITIALIZER setup
- `src/app/app.ts` - App component with error handling
- `src/app/app.html` - Conditional error display
- `scripts/stack-frontend/build.sh` - Build-time injection

## Spec Reference

This implementation satisfies:
- **Requirement 14.5**: Frontend runtime validation
- **Task 12.2**: Add runtime configuration validation to frontend

Validates:
- Required configuration values are present
- Configuration is valid (URL format)
- Production builds don't use localhost URLs
- Clear error messages for troubleshooting
