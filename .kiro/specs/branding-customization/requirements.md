# Requirements Document

## Introduction

This feature establishes a plug-n-play, build-time / deploy-time branding foundation for the AgentCore Public Stack Angular frontend. The stack is open source and intended to be forked and rebranded. Today, branding values (logo asset paths, logo alt text / app name, chat greeting text, and brand colors) are scattered and hardcoded across component templates, TypeScript source, and the global stylesheet. This makes rebranding error-prone and undocumented.

This feature centralizes the rebrandable surface into a single, well-documented source of truth so that a forker can rebrand the application by (a) replacing a documented set of logo asset files and (b) editing brand values (hex colors, greeting text, app name) in one config location, from which the full derived Tailwind color scales regenerate. The change must preserve the existing light/dark theme behavior and must not alter the current appearance when the default (current) branding values are used.

The configuration shape is deliberately designed to be forward-compatible with a future runtime admin customization page ("Option 2"), so that a future runtime writer can populate the same values without rework.

### Scope

In scope — the following are the ONLY rebrandable elements:
1. Logo images (sidenav top-left and chat greeting block), including light and dark variants.
2. Logo alt text / app name (currently hardcoded to "Boise State University Logo").
3. Chat greeting text (greeting templates and fallback greetings shown on a new/empty chat).
4. Brand colors used across light and dark themes (primary, secondary, tertiary), including the full derived color scales.

Out of scope (explicit non-goals — see Requirement 9):
- Any admin dashboard or in-app UI for editing branding.
- Backend persistence of branding values.
- S3 or runtime logo uploads.
- Runtime color or branding overrides.

## Glossary

- **Branding_System**: The frontend branding foundation delivered by this feature, comprising the branding configuration source of truth, the color scale derivation, and the components/styles that consume branding values.
- **Brand_Config**: The single source-of-truth configuration artifact that holds all rebrandable branding values (logo asset references, app name / logo alt text, greeting text, and brand hex colors).
- **Brand_Color**: A single hex color value provided by a forker for one of the named brand roles (primary, secondary, tertiary).
- **Color_Scale**: The set of eleven derived color steps (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, 950) generated from a single Brand_Color, where step 500 is the literal Brand_Color hex and the remaining steps are derived by adjusting lightness while holding chroma and hue.
- **Color_Scale_Generator**: The build-time mechanism that produces the Tailwind `@theme` color scale declarations from the Brand_Color values.
- **Logo_Asset**: A logo image file referenced by the Brand_Config. Each logo has a light-theme variant and a dark-theme variant.
- **App_Name**: The human-readable product/organization name used as the logo alt text and any name-based branding string.
- **Greeting_Template**: A greeting string shown on a new/empty chat that contains the `{name}` placeholder for the current user's first name.
- **Fallback_Greeting**: A greeting string shown on a new/empty chat when the current user's first name is not available.
- **Greeting_Provider**: The frontend logic that selects and renders a greeting from the Greeting_Templates or Fallback_Greetings defined in the Brand_Config.
- **Default_Branding**: The current, shipped branding values (Boise State University logos, alt text, greeting arrays, and the primary `#0033a0`, secondary `#d64309`, tertiary `#0072ce` colors).
- **Forker**: A developer who clones/forks the repository to deploy a rebranded instance of the application.
- **Sidenav_Component**: The frontend navigation sidebar component that renders the logo top-left (`frontend/ai.client/src/app/components/sidenav/sidenav.html`).
- **Chat_Greeting_Block**: The empty-chat greeting area that renders the logo and greeting message (`frontend/ai.client/src/app/session/components/chat-container/chat-container.component.html`).
- **Rebranding_Documentation**: The README/docs content that explains the rebranding process to a Forker.

## Requirements

### Requirement 1: Single Source of Truth for Branding Values

**User Story:** As a Forker, I want all rebrandable branding values collected in one documented location, so that I can rebrand the application without hunting through templates and source files.

#### Acceptance Criteria

1. THE Branding_System SHALL define all rebrandable branding values in a single Brand_Config location.
2. THE Brand_Config SHALL include the Logo_Asset references, the App_Name, the Greeting_Templates, the Fallback_Greetings, and the Brand_Color values for the primary, secondary, and tertiary roles.
3. WHERE a rebrandable value is consumed by the Sidenav_Component, the Chat_Greeting_Block, or the Greeting_Provider, THE Branding_System SHALL read that value from the Brand_Config rather than from a hardcoded literal in a component template or component TypeScript source.
4. THE Brand_Config SHALL define each of the primary, secondary, and tertiary brand color roles as a single valid 6-digit hexadecimal Brand_Color value.
5. THE Brand_Config SHALL define the App_Name as a non-empty text value between 1 and 50 characters in length.
6. THE Brand_Config SHALL provide at least one Greeting_Templates entry and at least one Fallback_Greetings entry.
7. IF the Greeting_Provider cannot read a valid Greeting_Templates value from the Brand_Config, THEN THE Greeting_Provider SHALL render a Fallback_Greetings value in the Chat_Greeting_Block.

### Requirement 2: Configurable Logo Assets

**User Story:** As a Forker, I want to replace the logo images by swapping documented files, so that my organization's logo appears in the sidenav and the chat greeting block.

#### Acceptance Criteria

1. THE Brand_Config SHALL reference exactly one light-theme Logo_Asset and exactly one dark-theme Logo_Asset, each identified by a documented file path.
2. WHEN the application renders the Sidenav_Component logo, THE Branding_System SHALL use the Logo_Asset references from the Brand_Config.
3. WHEN the application renders the Chat_Greeting_Block logo, THE Branding_System SHALL use the Logo_Asset references from the Brand_Config.
4. WHILE the active theme is light, THE Branding_System SHALL display the light-theme Logo_Asset in both the Sidenav_Component and the Chat_Greeting_Block.
5. WHILE the active theme is dark, THE Branding_System SHALL display the dark-theme Logo_Asset in both the Sidenav_Component and the Chat_Greeting_Block.
6. WHEN the active theme changes between light and dark, THE Branding_System SHALL update the displayed Logo_Asset to match the newly active theme within 1 second and without a full page reload.
7. WHERE a Forker replaces the documented Logo_Asset files at the documented paths without editing component templates, THE Branding_System SHALL display the replacement logos in the Sidenav_Component and the Chat_Greeting_Block.
8. IF a referenced Logo_Asset file is absent at its documented path or cannot be loaded, THEN THE Branding_System SHALL render the Sidenav_Component and the Chat_Greeting_Block with their existing layout dimensions preserved and SHALL surface a visible indication that the logo failed to load, without blocking rendering of surrounding content.

### Requirement 3: Configurable App Name and Logo Alt Text

**User Story:** As a Forker, I want the app name and logo alt text to come from configuration, so that accessibility labels reflect my brand instead of the original organization.

#### Acceptance Criteria

1. THE Brand_Config SHALL define exactly one App_Name value as a text string containing 1 to 100 characters with at least one non-whitespace character.
2. WHEN the Sidenav_Component renders a logo image, THE Branding_System SHALL set the image alt text to a value equal to the App_Name.
3. WHEN the Chat_Greeting_Block renders a logo image, THE Branding_System SHALL set the image alt text to a value equal to the App_Name.
4. THE Branding_System SHALL set identical alt text equal to the App_Name on every branding logo image rendered by the Sidenav_Component and the Chat_Greeting_Block.
5. IF the App_Name is absent or contains only whitespace when a branding logo image is rendered, THEN THE Branding_System SHALL set the image alt text to a non-empty default label and render the image without error.

### Requirement 4: Configurable Chat Greeting Text

**User Story:** As a Forker, I want the chat greeting text to come from configuration, so that new/empty chats greet users with my brand's messaging.

#### Acceptance Criteria

1. THE Brand_Config SHALL define the Greeting_Templates as an ordered list of 1 to 50 greeting strings, each containing 1 to 500 characters.
2. THE Brand_Config SHALL define the Fallback_Greetings as an ordered list of 1 to 50 greeting strings, each containing 1 to 500 characters.
3. WHEN the Greeting_Provider renders a greeting and the current user's first name is present and contains at least one non-whitespace character, THE Greeting_Provider SHALL select one Greeting_Template and render it with every `{name}` placeholder replaced by the current user's first name.
4. WHEN the Greeting_Provider renders a greeting and the current user's first name is absent, null, empty, or whitespace-only, THE Greeting_Provider SHALL render one Fallback_Greeting selected from the Brand_Config.
5. WHERE a Greeting_Template contains the `{name}` placeholder, THE Greeting_Provider SHALL replace every occurrence of `{name}` with the current user's first name.
6. THE Greeting_Provider SHALL read the Greeting_Templates and Fallback_Greetings from the Brand_Config rather than from hardcoded arrays in component TypeScript source.
7. IF the Greeting_Templates list is empty or undefined, THEN THE Greeting_Provider SHALL render a Fallback_Greeting.
8. IF both the Greeting_Templates and Fallback_Greetings lists are empty or undefined, THEN THE Greeting_Provider SHALL render a built-in default greeting that contains no `{name}` placeholder.

### Requirement 5: Configurable Brand Colors with Derived Scales

**User Story:** As a Forker, I want to set brand colors from single hex values, so that the full light and dark theme color scales update to match my brand.

#### Acceptance Criteria

1. THE Brand_Config SHALL define one Brand_Color value for each of the primary, secondary, and tertiary roles as a 6-digit hexadecimal value (#RRGGBB, digits 0-9 and A-F, case-insensitive).
2. WHEN the Color_Scale_Generator produces a Color_Scale for a role, THE Color_Scale_Generator SHALL set the step-500 value to the literal Brand_Color hex for that role, unchanged.
3. WHEN the Color_Scale_Generator produces a Color_Scale for a role, THE Color_Scale_Generator SHALL derive the non-500 steps from the Brand_Color by adjusting lightness while holding chroma and hue equal to the Brand_Color, such that steps 50 through 400 are progressively lighter and steps 600 through 950 are progressively darker.
4. THE Color_Scale_Generator SHALL produce exactly 11 Color_Scale steps (50, 100, 200, 300, 400, 500, 600, 700, 800, 900, and 950) for each of the primary, secondary, and tertiary roles.
5. WHEN a Forker edits a Brand_Color hex value in the Brand_Config and the value is saved, THE Branding_System SHALL regenerate the full Color_Scale for the corresponding role used by the Tailwind theme.
6. THE Branding_System SHALL apply the generated Color_Scale values to both the light theme and the dark theme.
7. IF a Brand_Color value is not a valid 6-digit hexadecimal value, THEN THE Branding_System SHALL reject the value, retain the prior Color_Scale for that role, and surface an error indication identifying the offending Brand_Color and role.

### Requirement 6: Rebranding Documentation

**User Story:** As a Forker, I want clear documentation of the rebranding process, so that I can rebrand the application quickly without reading the source code.

#### Acceptance Criteria

1. THE Rebranding_Documentation SHALL provide the ordered steps to replace the Logo_Asset files and the file paths for both the light-theme variant and the dark-theme variant, such that each variant is individually identified.
2. THE Rebranding_Documentation SHALL provide the ordered steps to edit each of the App_Name, the Greeting_Templates, the Fallback_Greetings, and the Brand_Color values in the Brand_Config, listing each value by name.
3. THE Rebranding_Documentation SHALL identify the single Brand_Config location as the one place to edit all branding values, and SHALL state that no other file requires editing to complete rebranding.
4. THE Rebranding_Documentation SHALL state that the `{name}` placeholder in a Greeting_Template is replaced at runtime with the current user's first name, and SHALL state the displayed result when no first name is available.
5. THE Rebranding_Documentation SHALL state that editing a Brand_Color hex value regenerates the derived Color_Scale for that role, and SHALL state the accepted hex value format for a Brand_Color entry.
6. THE Rebranding_Documentation SHALL provide the observable steps a Forker performs to verify that each edited branding value appears in the running application after rebranding.

### Requirement 7: Preserve Existing Appearance with Default Branding

**User Story:** As a maintainer of the upstream stack, I want the default branding to render exactly as it does today, so that centralizing branding does not regress the current appearance.

#### Acceptance Criteria

1. WHERE the Brand_Config holds the Default_Branding values, THE Branding_System SHALL render logos in the Sidenav_Component and the Chat_Greeting_Block that are byte-for-byte identical to the logo assets rendered by the current application for the same theme.
2. WHERE the Brand_Config holds the Default_Branding values, THE Branding_System SHALL produce primary, secondary, and tertiary Color_Scale values that are character-for-character identical to the corresponding values defined in the current `@theme` block.
3. WHEN the active theme changes and the Brand_Config holds the Default_Branding values, THE Branding_System SHALL perform the logo and color theme switching with no additional or missing switch events compared to the current behavior.
4. WHERE the Brand_Config holds the Default_Branding values, THE Greeting_Provider SHALL render greetings that are character-for-character identical to an entry contained in the current Default_Branding greeting text set.
5. IF the Brand_Config is absent, empty, or unparseable, THEN THE Branding_System SHALL render the application using the Default_Branding values and surface an error indication that the Brand_Config could not be read, without blocking application rendering.

### Requirement 8: Forward-Compatible Configuration Shape

**User Story:** As a maintainer planning a future runtime admin customization page (Option 2), I want the branding configuration shape to be reusable by a future runtime writer, so that Option 2 can populate the same values without reworking the branding foundation.

#### Acceptance Criteria

1. THE Brand_Config SHALL represent branding values in a structured shape that contains one distinct named field for each of the Logo_Asset references, the App_Name, the Greeting_Templates, the Fallback_Greetings, and the Brand_Color values.
2. WHEN a consuming component reads a branding value, THE Branding_System SHALL provide that value through a single defined access boundary such that the value source can change without modifying the consuming component.
3. THE Brand_Config shape SHALL represent each Brand_Color role as a single hexadecimal color input value in 6-digit form (with an optional leading "#"), so that a future runtime writer can supply the same input the Color_Scale_Generator consumes.
4. IF a Brand_Config field is absent or empty when read through the access boundary, THEN THE Branding_System SHALL supply the defined default value for that field so that every consuming component receives a usable branding value.
5. IF a Brand_Color role value is not a valid 6-digit hexadecimal color input value, THEN THE Branding_System SHALL reject the invalid value, apply the default Brand_Color for that role, and record an indication that the value was rejected.

### Requirement 9: Documented Non-Goals for Deferred Runtime Customization

**User Story:** As a maintainer, I want the deferred runtime customization capabilities documented as non-goals, so that the scope of this feature is unambiguous and Option 2 is clearly separated.

#### Acceptance Criteria

1. THE Rebranding_Documentation SHALL contain a section, identified by a heading that includes the term "Non-Goals", that lists all capabilities deferred from this feature.
2. WHERE the Non-Goals section is present, THE Rebranding_Documentation SHALL state that an in-app admin UI for editing branding is out of scope for this feature and deferred to a future capability explicitly labeled "Option 2".
3. WHERE the Non-Goals section is present, THE Rebranding_Documentation SHALL state that backend persistence of branding values is out of scope for this feature.
4. WHERE the Non-Goals section is present, THE Rebranding_Documentation SHALL state that runtime logo uploads are out of scope for this feature.
5. WHERE the Non-Goals section is present, THE Rebranding_Documentation SHALL state that runtime color overrides and runtime branding overrides are out of scope for this feature.
