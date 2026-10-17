export interface AuthProvider {
  provider_id: string;
  display_name: string;
  provider_type: string;
  enabled: boolean;
  issuer_url: string;
  client_id: string;
  authorization_endpoint?: string | null;
  token_endpoint?: string | null;
  jwks_uri?: string | null;
  userinfo_endpoint?: string | null;
  end_session_endpoint?: string | null;
  scopes: string;
  response_type: string;
  pkce_enabled: boolean;
  redirect_uri?: string | null;
  user_id_claim: string;
  email_claim: string;
  name_claim: string;
  roles_claim: string;
  picture_claim?: string | null;
  first_name_claim?: string | null;
  last_name_claim?: string | null;
  user_id_pattern?: string | null;
  required_scopes?: string[] | null;
  allowed_audiences?: string[] | null;
  logo_url?: string | null;
  button_color?: string | null;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
  // Runtime tracking fields
  agentcore_runtime_arn?: string | null;
  agentcore_runtime_id?: string | null;
  agentcore_runtime_endpoint_url?: string | null;
  agentcore_runtime_status?: RuntimeStatus;
  agentcore_runtime_error?: string | null;
  agentcore_runtime_image_tag?: string | null;
}

export type RuntimeStatus = 'PENDING' | 'CREATING' | 'READY' | 'UPDATING' | 'FAILED' | 'UPDATE_FAILED';

export interface AuthProviderListResponse {
  providers: AuthProvider[];
  total: number;
}

export interface AuthProviderCreateRequest {
  provider_id: string;
  display_name: string;
  provider_type?: string;
  enabled?: boolean;
  issuer_url: string;
  client_id: string;
  client_secret: string;
  authorization_endpoint?: string | null;
  token_endpoint?: string | null;
  jwks_uri?: string | null;
  userinfo_endpoint?: string | null;
  end_session_endpoint?: string | null;
  scopes?: string;
  response_type?: string;
  pkce_enabled?: boolean;
  redirect_uri?: string | null;
  user_id_claim?: string;
  email_claim?: string;
  name_claim?: string;
  roles_claim?: string;
  picture_claim?: string | null;
  first_name_claim?: string | null;
  last_name_claim?: string | null;
  user_id_pattern?: string | null;
  required_scopes?: string[] | null;
  allowed_audiences?: string[] | null;
  logo_url?: string | null;
  button_color?: string | null;
}

export interface AuthProviderUpdateRequest {
  display_name?: string;
  enabled?: boolean;
  issuer_url?: string;
  client_id?: string;
  client_secret?: string;
  authorization_endpoint?: string | null;
  token_endpoint?: string | null;
  jwks_uri?: string | null;
  userinfo_endpoint?: string | null;
  end_session_endpoint?: string | null;
  scopes?: string;
  response_type?: string;
  pkce_enabled?: boolean;
  redirect_uri?: string | null;
  user_id_claim?: string;
  email_claim?: string;
  name_claim?: string;
  roles_claim?: string;
  picture_claim?: string | null;
  first_name_claim?: string | null;
  last_name_claim?: string | null;
  user_id_pattern?: string | null;
  required_scopes?: string[] | null;
  allowed_audiences?: string[] | null;
  logo_url?: string | null;
  button_color?: string | null;
}

export interface OIDCDiscoveryResponse {
  issuer: string;
  authorization_endpoint?: string | null;
  token_endpoint?: string | null;
  jwks_uri?: string | null;
  userinfo_endpoint?: string | null;
  end_session_endpoint?: string | null;
  scopes_supported?: string[] | null;
  response_types_supported?: string[] | null;
  claims_supported?: string[] | null;
}

/** Sign-in page settings, from GET/PUT /admin/auth-providers/login-page-settings. */
export interface LoginPageSettings {
  /** What a system admin asked for. */
  hide_cognito_login: boolean;
  /** Whether the sign-in page shows the Cognito button right now. Always true
   *  while no provider is enabled, whatever `hide_cognito_login` says. */
  cognito_login_visible: boolean;
  enabled_provider_count: number;
  updated_at: string | null;
  updated_by: string | null;
}

export interface LoginPageSettingsUpdateRequest {
  hide_cognito_login: boolean;
}
