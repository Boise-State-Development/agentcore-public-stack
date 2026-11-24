import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import {
  ScriptReviewSheetComponent,
  ScriptReviewSheetData,
  ScriptReviewSheetResult,
} from './components/script-review-sheet.component';
import { ScriptReviewAnswers, UnreviewedScriptSkill, unreviewedScriptsFrom } from './script-review.model';

/** Where the scripts would run, for the sheet's copy. */
export interface ScriptReviewScope {
  name: string;
  kind: 'agent' | 'project';
  /** Reads a skill file for View; omit to offer no View. */
  readFile?: (skillId: string, path: string) => Promise<string>;
}

/** A save that raced an author's edit asks again with the new set; this bounds that loop. */
const MAX_ROUNDS = 3;

/** Open the sheet for one skill; undefined means Cancel. */
export async function openScriptReviewSheet(
  dialog: Dialog,
  skill: UnreviewedScriptSkill,
  scope: ScriptReviewScope,
  isUpdate = false,
): Promise<ScriptReviewSheetResult | undefined> {
  const ref = dialog.open<ScriptReviewSheetResult, ScriptReviewSheetData>(ScriptReviewSheetComponent, {
    data: { skill, scopeName: scope.name, scope: scope.kind, isUpdate, readFile: scope.readFile },
  });
  return firstValueFrom(ref.closed);
}

/**
 * Run a save, answering any `skill_scripts_unreviewed` 409 with the review sheet
 * (skill-code-sandbox §8.5), and resend it with what the binder chose.
 *
 * Resolves to the save's result, or `null` when the binder cancelled (or, on an Update,
 * kept the reviewed version). Any other error is rethrown for the caller's own handling.
 * `isUpdate` is for a pin move: there "decline" means don't move it.
 */
export async function saveWithScriptReview<T>(
  dialog: Dialog,
  scope: ScriptReviewScope,
  attempt: (answers: ScriptReviewAnswers) => Promise<T>,
  options: { isUpdate?: boolean; initial?: ScriptReviewAnswers } = {},
): Promise<T | null> {
  const reviewed: Record<string, string> = { ...(options.initial?.scriptsReviewed ?? {}) };
  const declined = new Set<string>(options.initial?.scriptsDeclined ?? []);
  for (let round = 0; ; round++) {
    try {
      return await attempt(answers(reviewed, declined));
    } catch (err) {
      const skills = unreviewedScriptsFrom(err);
      if (!skills || round >= MAX_ROUNDS) throw err;
      for (const skill of skills) {
        const choice = await openScriptReviewSheet(dialog, skill, scope, options.isUpdate === true);
        if (choice === undefined) return null;
        if (choice === 'allow') {
          reviewed[skill.skillId] = skill.scriptsHash;
          declined.delete(skill.skillId);
        } else if (options.isUpdate) {
          return null;
        } else {
          declined.add(skill.skillId);
          delete reviewed[skill.skillId];
        }
      }
    }
  }
}

function answers(reviewed: Record<string, string>, declined: Set<string>): ScriptReviewAnswers {
  const out: ScriptReviewAnswers = {};
  if (Object.keys(reviewed).length) out.scriptsReviewed = { ...reviewed };
  if (declined.size) out.scriptsDeclined = [...declined].sort();
  return out;
}

/** A skill file's path as the My Skills resource route takes it: below its kind folder. */
export function resourcePathBelowKind(path: string): string {
  const slash = path.indexOf('/');
  return slash === -1 ? path : path.slice(slash + 1);
}
