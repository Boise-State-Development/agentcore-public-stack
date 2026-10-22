import { Tool } from '../../services/tool/tool.service';

/**
 * Result of reconciling a list of template tool refs against the live tool
 * catalog.
 *
 * - `apply`   — every ref that will be set on the form. This is the union of
 *               active refs and present-but-non-active refs (deprecated /
 *               disabled / coming_soon). Non-active refs are still applied so
 *               the author does not silently lose a binding a template asked
 *               for; they are additionally surfaced in `flagged`.
 * - `flagged` — the subset of `apply` that is present in the catalog but not
 *               `active`, paired with the offending status so the UI can warn.
 * - `dropped` — refs with no matching tool in the catalog. These are removed
 *               entirely (not applied) because there is nothing to bind to.
 *
 * Input order is preserved across all three arrays.
 */
export interface ToolRefReconcileResult {
  apply: string[];
  flagged: { ref: string; status: string }[];
  dropped: string[];
}

/**
 * Reconcile template-supplied tool refs against the live tool catalog.
 *
 * Pure: no side effects, no Angular dependencies. A ref matches a `Tool` by its
 * `toolId`.
 *
 * @param refs    the tool refs a template wants to set on the form, in order
 * @param catalog the live tool catalog (e.g. from ToolService.loadTools())
 */
export function reconcileToolRefs(
  refs: string[],
  catalog: Tool[],
): ToolRefReconcileResult {
  const byId = new Map<string, Tool>();
  for (const tool of catalog) {
    byId.set(tool.toolId, tool);
  }

  const result: ToolRefReconcileResult = {
    apply: [],
    flagged: [],
    dropped: [],
  };

  for (const ref of refs) {
    const tool = byId.get(ref);
    if (!tool) {
      // Unknown ref: nothing to bind to, so drop it entirely.
      result.dropped.push(ref);
      continue;
    }

    // Present in the catalog: always applied.
    result.apply.push(ref);

    // Non-active but present: still applied, but warn.
    if (tool.status !== 'active') {
      result.flagged.push({ ref, status: tool.status });
    }
  }

  return result;
}
