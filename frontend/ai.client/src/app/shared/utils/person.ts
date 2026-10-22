/**
 * What to call someone the API identifies by email: Shared Projects, notifications
 * and memory spaces.
 *
 * Those APIs send a display name (from the users table) beside each email when they
 * know one, and null when they don't — someone who has never signed in, or an
 * account with no name. The email stays the key; it is what shows when there is no
 * name, and the secondary line or tooltip when there is.
 */
export function personLabel(name: string | null | undefined, email: string | null | undefined): string {
  return name?.trim() || email || '';
}
