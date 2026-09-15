/**
 * Client mirror of the skill field length limits.
 *
 * Canonical source: `backend/src/apis/shared/skills/models.py`
 * (`SKILL_DESCRIPTION_MAX_LENGTH`). **The server is the control** — it rejects
 * an over-length description with a 422 whatever the client does. This module
 * exists so both skill forms agree on one number and the user sees the limit
 * before a failed round-trip.
 *
 * Keep this in sync with the Python constant when either changes.
 */

/**
 * Max length of a skill `description`, in characters.
 *
 * This is the Agent Skills spec limit for the SKILL.md frontmatter
 * `description` field, so a skill authored for Claude imports unchanged. It is
 * a real bound, not a formality: the description is the Level-1 catalog line
 * injected into the cacheable system prompt for every enabled skill, on every
 * turn.
 */
export const SKILL_DESCRIPTION_MAX_LENGTH = 1024;
