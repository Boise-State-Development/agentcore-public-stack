import { Injectable } from '@angular/core';
import { ManagedModelFormData } from '../models/managed-model.model';

/**
 * One-shot handoff for a curated template between the catalog page and the
 * model form. The catalog calls `set()` before navigating; the form calls
 * `consume()` once on init, which returns the pending template and clears it
 * so a refresh of the form route doesn't re-apply stale data.
 *
 * Kept deliberately tiny — query params can't carry the full template
 * (pricing + supportedParams shape would have to be re-serialized), and
 * the alternative of pushing a full template through Router state is
 * fragile across navigations the user might not expect.
 */
@Injectable({ providedIn: 'root' })
export class CuratedModelPrefillService {
  private pending: ManagedModelFormData | null = null;

  set(template: ManagedModelFormData): void {
    this.pending = template;
  }

  /** Returns the pending template (if any) and clears it. */
  consume(): ManagedModelFormData | null {
    const value = this.pending;
    this.pending = null;
    return value;
  }
}
