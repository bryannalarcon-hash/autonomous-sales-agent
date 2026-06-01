// The consent gate — the single rule that decides whether the call UI (text chat + voice) may be
// used. R33/R40/R41: the call surface MUST stay disabled until the backend reports `ready`
// (recorded) or `unrecorded` (allowed, not recorded). `need_parental` and `ended` keep it blocked.
// Pure + framework-free so the gate is trivially correct and reusable across the demo components.
import type { ConsentState } from './api-types';

/** True only for states where the prospect may actually talk to the agent. */
export function isCallEnabled(state: ConsentState | null): boolean {
  return state === 'ready' || state === 'unrecorded';
}

/** True when the conversation is being recorded (drives the "recorded" vs "not recorded" banner). */
export function isRecorded(state: ConsentState | null): boolean {
  return state === 'ready';
}

/** True when the backend asked for the parental-consent step (minor flagged). */
export function needsParental(state: ConsentState | null): boolean {
  return state === 'need_parental';
}

/** True when the session is over and the polite end screen should show. */
export function isEnded(state: ConsentState | null): boolean {
  return state === 'ended';
}
