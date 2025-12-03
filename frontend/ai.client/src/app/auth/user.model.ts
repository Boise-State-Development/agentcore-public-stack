/**
 * User model representing decoded JWT token data.
 * Matches the backend User model structure.
 */
export interface User {
  email: string;
  empl_id: string;
  name: string;
  roles: string[];
  picture?: string;
}

/**
 * Decoded JWT payload structure from Entra ID.
 * This represents the raw JWT claims before mapping to User.
 */
export interface JWTPayload {
  email?: string;
  preferred_username?: string;
  name?: string;
  given_name?: string;
  family_name?: string;
  'http://schemas.boisestate.edu/claims/employeenumber'?: string;
  roles?: string[];
  picture?: string;
  exp?: number;
  iat?: number;
  aud?: string | string[];
  iss?: string;
  sub?: string;
}


