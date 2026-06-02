// demo-call.ts — client-side orchestrator for a self-driving DEMO call. Runs a scripted, realistic
// prospect conversation through the REAL backend endpoints (/api/consent/start + respond, then
// /api/chat per turn, then /api/session/{id}/end) so the Live monitor populates turn-by-turn exactly
// as a real phone/web call does — each committed chat turn fires the same per-turn live-heartbeat
// upsert. This lets an operator watch the live pipeline end-to-end WITHOUT a phone. No mock/stub: the
// agent replies are produced by the real brain; only the prospect's lines are scripted.
import { consentRespond, consentStart, sendChat, sessionEnd } from './api';

/** A realistic, increasingly-warm QUALIFIED-parent arc: discovery → objections → buying signal.
 *  Ends on an explicit move-forward so the agent's close gate (trust≥0.6, ≥4 turns) can fire and the
 *  call reaches a terminal outcome (enrolled / consult booked) rather than lingering in_progress. */
export const DEMO_PROSPECT_SCRIPT: readonly string[] = [
  "Hi — my daughter's a high-school sophomore and she's really struggling with Algebra 2 right before finals.",
  "She does all the homework but still gets C's and D's on the tests, and her confidence is shot.",
  "We tried one of those free tutoring apps and she just didn't stick with it. What makes you different?",
  "That actually sounds like what she needs. How does the pricing work — is it worth it?",
  "Okay, that's reasonable. How soon could she actually start with someone?",
  "Great — let's go ahead and get her set up. What's the next step?",
];

export type DemoPhase = 'consent' | 'turn' | 'ending' | 'done' | 'error';

export interface DemoProgress {
  phase: DemoPhase;
  /** 1-based turn index currently being sent (phase === 'turn'). */
  turn?: number;
  total?: number;
  /** Final episode id (phase === 'done'). */
  episodeId?: string;
  outcome?: string;
  error?: string;
}

export interface DemoHandle {
  /** Resolves when the demo finishes (or is aborted). Never rejects — errors arrive via onProgress. */
  done: Promise<DemoProgress>;
  /** The session id, available once consent completes (for best-effort cleanup on unmount). */
  sessionId: () => string | null;
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Run a scripted demo call. `onProgress` is invoked at each phase/turn so the UI can narrate it.
 *  `signal` (optional) aborts the run between turns and best-effort-ends the session. */
export function runDemoCall(
  onProgress: (p: DemoProgress) => void,
  signal?: AbortSignal,
  // Per-turn pacing so the operator can watch each turn land in the monitor (the brain itself adds
  // ~several seconds/turn). Small extra gap keeps the heartbeat fresh and the stream watchable.
  perTurnGapMs = 900,
): DemoHandle {
  let sessionId: string | null = null;

  const run = async (): Promise<DemoProgress> => {
    try {
      onProgress({ phase: 'consent' });
      const start = await consentStart({ jurisdiction: '', channel: 'text' });
      sessionId = start.session_id;
      // Accept the AI-disclosure + recording consent; explicitly not a minor (the CALLER is the parent).
      await consentRespond({
        session_id: sessionId,
        ai_acknowledged: true,
        recording_consent: true,
        is_minor: false,
      });

      const total = DEMO_PROSPECT_SCRIPT.length;
      for (let i = 0; i < total; i++) {
        if (signal?.aborted) break;
        onProgress({ phase: 'turn', turn: i + 1, total });
        const res = await sendChat({ session_id: sessionId, text: DEMO_PROSPECT_SCRIPT[i] });
        // The agent reached a terminal act (enrollment close / disqualify / escalate) — stop early.
        if (res.done) break;
        if (i < total - 1) await sleep(perTurnGapMs);
      }

      onProgress({ phase: 'ending' });
      const end = await sessionEnd({ session_id: sessionId });
      const result: DemoProgress = { phase: 'done', episodeId: end.episode_id, outcome: end.outcome };
      onProgress(result);
      return result;
    } catch (e) {
      // Best-effort: if a session was created, end it so we don't leave a dangling in-progress shell.
      if (sessionId) {
        try {
          await sessionEnd({ session_id: sessionId });
        } catch {
          /* swallow — the live monitor drops stale in-progress calls after the heartbeat window */
        }
      }
      const result: DemoProgress = {
        phase: 'error',
        error: e instanceof Error ? e.message : 'Demo call failed.',
      };
      onProgress(result);
      return result;
    }
  };

  return { done: run(), sessionId: () => sessionId };
}
