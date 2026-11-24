import { WorkflowStepPin } from './workflow.models';

/**
 * A pinned Agent step's line, on the run sheet and in the designer (workflow-designer §23.2). The
 * pin runs for everyone, so the Agent's owner is told they run it too, in place of the draft line.
 * A newer published version is named, beside Use version.
 */
export function pinnedVersionLine(pin: WorkflowStepPin, ownsAgent: boolean): string {
  const head = ownsAgent ? `Runs version ${pin.version} for everyone, including you.` : `Uses version ${pin.version}.`;
  return pin.offerVersion !== undefined ? `${head} Version ${pin.offerVersion} is the current published version.` : head;
}
