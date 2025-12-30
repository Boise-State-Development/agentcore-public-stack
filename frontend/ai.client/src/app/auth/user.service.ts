import { inject, Injectable, signal } from '@angular/core';
import { AuthService } from './auth.service';
import { User, JWTPayload } from './user.model';
import { environment } from '../../environments/environment';

/**
 * Service for managing current user information decoded from JWT tokens.
 * Automatically syncs with AuthService to update user data when tokens change.
 */
@Injectable({
  providedIn: 'root'
})
export class UserService {
  private authService = inject(AuthService);

  /**
   * Current user signal. Null if not authenticated or token cannot be decoded.
   */
  readonly currentUser = signal<User | null>(null);

  /**
   * Anonymous user object used when authentication is disabled.
   * Matches the backend anonymous user structure.
   */
  private readonly anonymousUser: User = {
    email: 'anonymous@local.dev',
    empl_id: 'anonymous',
    firstName: 'Anonymous',
    lastName: 'User',
    fullName: 'Anonymous User',
    roles: [],
    picture: undefined
  };

  constructor() {
    // Initialize user from current token or set anonymous user if auth disabled
    this.refreshUser();

    if (typeof window !== 'undefined') {
      // Listen for storage events to sync when tokens change in other tabs/windows
      window.addEventListener('storage', (event) => {
        if (event.key === 'access_token' || event.key === null) {
          this.refreshUser();
        }
      });

      // Listen for custom events to sync when tokens change in same tab
      window.addEventListener('token-stored', () => {
        this.refreshUser();
      });

      window.addEventListener('token-cleared', () => {
        this.currentUser.set(null);
      });
    }
  }

  /**
   * Decode and update user information from JWT token.
   * @param token JWT access token
   */
  private updateUserFromToken(token: string): void {
    try {
      const user = this.decodeToken(token);
      this.currentUser.set(user);
    } catch (error) {
      console.error('Failed to decode user from token:', error);
      this.currentUser.set(null);
    }
  }

  /**
   * Decode JWT token and extract user information.
   * @param token JWT access token
   * @returns User object with decoded information
   * @throws Error if token is invalid or missing required claims
   */
  private decodeToken(token: string): User {
    try {
      // JWT tokens have three parts: header.payload.signature
      const parts = token.split('.');
      if (parts.length !== 3) {
        throw new Error('Invalid token format');
      }

      // Decode the payload (middle part)
      const payload = this.base64UrlDecode(parts[1]);
      const jwtPayload: JWTPayload = JSON.parse(payload);
      
      // Extract user information matching backend User model
      const email = jwtPayload.email || jwtPayload.preferred_username;
      if (!email) {
        throw new Error('Token missing email or preferred_username');
      }

      const emplId = jwtPayload['http://schemas.boisestate.edu/claims/employeenumber'];
      if (!emplId || !emplId.toString().match(/^\d{9}$/)) {
        throw new Error('Token missing valid empl_id (9-digit employee number)');
      }

      // Build full name from available claims
      const fullName = jwtPayload.name ||
        `${jwtPayload.given_name || ''} ${jwtPayload.family_name || ''}`.trim() ||
        email;

      // Extract first and last name - prefer JWT claims, fall back to parsing fullName
      let firstName = jwtPayload.given_name || '';
      let lastName = jwtPayload.family_name || '';

      // If given_name/family_name not available, parse from fullName
      if (!firstName && fullName) {
        const nameParts = fullName.split(' ');
        firstName = nameParts[0] || '';
        lastName = nameParts.slice(1).join(' ') || '';
      }

      const roles = jwtPayload.roles || [];

      const user: User = {
        email,
        empl_id: emplId.toString(),
        firstName,
        lastName,
        fullName,
        roles,
        picture: jwtPayload.picture
      };

      return user;
    } catch (error) {
      if (error instanceof Error) {
        throw new Error(`Failed to decode token: ${error.message}`);
      }
      throw new Error('Failed to decode token: Unknown error');
    }
  }

  /**
   * Decode base64url encoded string.
   * @param str Base64url encoded string
   * @returns Decoded string
   */
  private base64UrlDecode(str: string): string {
    // Convert base64url to base64
    let base64 = str.replace(/-/g, '+').replace(/_/g, '/');
    
    // Add padding if needed
    while (base64.length % 4) {
      base64 += '=';
    }

    // Decode base64
    try {
      const decoded = atob(base64);
      return decoded;
    } catch (error) {
      throw new Error('Invalid base64 encoding');
    }
  }

  /**
   * Get the current user synchronously.
   * @returns Current user or null if not authenticated
   */
  getUser(): User | null {
    return this.currentUser();
  }

  /**
   * Check if user has a specific role.
   * @param role Role to check
   * @returns True if user has the role
   */
  hasRole(role: string): boolean {
    const user = this.currentUser();
    return user?.roles.includes(role) ?? false;
  }

  /**
   * Check if user has any of the specified roles.
   * @param roles Roles to check
   * @returns True if user has at least one of the roles
   */
  hasAnyRole(roles: string[]): boolean {
    const user = this.currentUser();
    if (!user) return false;
    return roles.some(role => user.roles.includes(role));
  }

  /**
   * Manually refresh user data from current token.
   * Useful when token has been updated externally.
   */
  refreshUser(): void {
    // If authentication is disabled, set anonymous user
    if (!environment.enableAuthentication) {
      this.currentUser.set(this.anonymousUser);
      return;
    }

    const token = this.authService.getAccessToken();
    if (token) {
      this.updateUserFromToken(token);
    } else {
      this.currentUser.set(null);
    }
  }
}

