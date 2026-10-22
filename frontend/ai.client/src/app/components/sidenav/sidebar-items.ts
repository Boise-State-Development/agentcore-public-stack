import { FeatureFlags } from '../../../environments/feature-flags';
import { SidebarItemPreference } from '../../services/user-settings.service';

/**
 * A navigation entry the user can show, hide and reorder in the sidebar.
 *
 * New Session and Search are not here: they are the sidebar's fixed controls,
 * not destinations, and a user who hid New Session would have no way back to
 * a blank conversation.
 */
export interface SidebarItemDef {
  /** Stable key in the saved layout. Never rename one: a saved layout would lose it. */
  id: string;
  label: string;
  route: string;
  /** A heroicons outline name, registered by every component that renders it. */
  icon: string;
  /** Where a user with no saved layout sees it: in the sidebar, or under More. */
  defaultVisible: boolean;
  /** The compile-time feature switch this entry rides, if any (`FEATURES`). */
  feature?: keyof FeatureFlags;
}

/**
 * Every customizable entry, in the default order.
 *
 * The backend checks only a saved layout's shape, so an entry is added here
 * alone. Add one hidden by default unless it should change what every
 * existing user sees: a user with a saved layout gets a new entry where the
 * default order puts it, with its default visibility.
 */
export const SIDEBAR_ITEMS: readonly SidebarItemDef[] = [
  { id: 'agents', label: 'Agents', route: '/agents', icon: 'heroSparkles', defaultVisible: true },
  // Shared Projects (shared-projects §6), only in a build that turns them on.
  { id: 'projects', label: 'Projects', route: '/projects', icon: 'heroFolderOpen', defaultVisible: true, feature: 'projects' },
  { id: 'artifacts', label: 'Artifacts', route: '/artifacts', icon: 'heroDocumentText', defaultVisible: true },
  // The capabilities hub; see `docs/specs/customize-surface.md`.
  { id: 'customize', label: 'Customize', route: '/customize', icon: 'heroAdjustmentsHorizontal', defaultVisible: true },
  // Open to every signed-in user. The only control is the environment's
  // SCHEDULED_RUNS_ENABLED kill switch (default on); with it off the page
  // shows its own "not available" state, the same trade Agents makes rather
  // than hiding the entry behind a request.
  { id: 'schedules', label: 'Schedules', route: '/schedules', icon: 'heroClock', defaultVisible: true },
];

/** The layout of a user who has never customized it. */
export function defaultSidebarLayout(
  defs: readonly SidebarItemDef[] = SIDEBAR_ITEMS,
): SidebarItemPreference[] {
  return defs.map(def => ({ id: def.id, visible: def.defaultVisible }));
}

/**
 * A saved layout made whole against the entries this build defines.
 *
 * - An id this build does not define is dropped: the entry was removed, or
 *   the layout was saved by a newer build.
 * - An entry the layout does not mention goes where the default order puts
 *   it (after the nearest earlier entry that is placed), with its default
 *   visibility, so a new entry does not land at the bottom of the list.
 *
 * Entries behind a feature switch that is off are kept, so their place
 * survives a build that hides them; callers filter them for display.
 */
export function resolveSidebarLayout(
  saved: readonly SidebarItemPreference[] | null | undefined,
  defs: readonly SidebarItemDef[] = SIDEBAR_ITEMS,
): SidebarItemPreference[] {
  if (!saved?.length) {
    return defaultSidebarLayout(defs);
  }

  const known = new Set(defs.map(def => def.id));
  const seen = new Set<string>();
  const layout: SidebarItemPreference[] = [];
  for (const pref of saved) {
    if (known.has(pref.id) && !seen.has(pref.id)) {
      seen.add(pref.id);
      layout.push({ id: pref.id, visible: pref.visible === true });
    }
  }

  defs.forEach((def, index) => {
    if (seen.has(def.id)) {
      return;
    }
    let insertAt = 0;
    for (let earlier = index - 1; earlier >= 0; earlier--) {
      const placed = layout.findIndex(pref => pref.id === defs[earlier].id);
      if (placed >= 0) {
        insertAt = placed + 1;
        break;
      }
    }
    layout.splice(insertAt, 0, { id: def.id, visible: def.defaultVisible });
    seen.add(def.id);
  });

  return layout;
}

/** Whether two layouts put the same entries in the same places. */
export function sameSidebarLayout(
  a: readonly SidebarItemPreference[],
  b: readonly SidebarItemPreference[],
): boolean {
  return a.length === b.length && a.every((pref, i) => pref.id === b[i].id && pref.visible === b[i].visible);
}
