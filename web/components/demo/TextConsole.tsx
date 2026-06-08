// TextConsole — the text-chat panel (R1). POSTs /api/chat {session_id, text} and renders {reply}.
// Gated: the input/send are disabled until consent is satisfied (controlled by `enabled`); a 409
// from the backend surfaces as a re-consent prompt (CB-54: honest copy + onConsentRequired callback
// so the page can re-show the gate rather than leaving the user in a dead end). The internal
// `decision_act` policy label is debug-only and rendered ONLY when `showDebug` is on — it is NEVER
// shown to the prospect by default (house rule: internal act labels never render in the
// prospect-facing surface). CB-54: when done=true is returned by /api/chat the session is finalized
// (POST /api/session/{id}/end) so the episode reaches a terminal status and appears in the Calls
// list (previously the session lingered as in_progress indefinitely).
// Styled in the dark "Cadence" design system (rendered inside the page's `.cadence` scope): a
// .card shell, accent-gradient agent bubbles vs surface prospect bubbles, .input + .btn composer.
'use client';

import { useRef, useState } from 'react';
import { sendChat, sessionEnd, ApiError } from '@/lib/api';
import { Icon } from '@/components/cadence/Icon';

interface TextConsoleProps {
  sessionId: string | null;
  enabled: boolean;
  /** Debug toggle: when true, render the internal decision_act tag on agent turns. */
  showDebug: boolean;
  /**
   * CB-54: called when the backend returns a consent 409, so the demo page can re-show the
   * consent gate instead of leaving the user staring at a dead error message.
   */
  onConsentRequired?: () => void;
}

interface ChatTurn {
  role: 'prospect' | 'agent';
  text: string;
  decisionAct?: string;
}

export default function TextConsole({ sessionId, enabled, showDebug, onConsentRequired }: TextConsoleProps) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // CB-54: track whether /end has already been called for this session so we never double-finalize.
  const endedRef = useRef(false);

  const canSend = enabled && !!sessionId && draft.trim().length > 0 && !sending && !done;

  async function handleSend() {
    if (!canSend || !sessionId) return;
    const text = draft.trim();
    setDraft('');
    setError(null);
    setTurns((prev) => [...prev, { role: 'prospect', text }]);
    setSending(true);
    try {
      const res = await sendChat({ session_id: sessionId, text });
      setTurns((prev) => [
        ...prev,
        { role: 'agent', text: res.reply, decisionAct: res.decision_act },
      ]);
      if (res.done) {
        // CB-54: terminal turn — finalize the episode so it appears in the Calls list and
        // doesn't linger as in_progress. Fire-and-forget: a failure here doesn't block the UX.
        setDone(true);
        if (!endedRef.current && sessionId) {
          endedRef.current = true;
          void sessionEnd({ session_id: sessionId }).catch(() => {
            // Swallow: /end is best-effort; the operator can still find the in-progress episode.
          });
        }
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // CB-54: honest copy + actionable re-consent prompt instead of a dead error message.
        // The error shows a "re-consent" link only when the parent provided the callback.
        setError('Your session is no longer valid. Please complete consent again to continue.');
        if (onConsentRequired) {
          onConsentRequired();
        }
      } else {
        setError(e instanceof ApiError ? e.message : 'Failed to send message.');
      }
    } finally {
      setSending(false);
      // Defer scroll to after the DOM updates.
      requestAnimationFrame(() => {
        scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
      });
    }
  }

  return (
    <section className="card" style={{ display: 'flex', height: '100%', flexDirection: 'column', overflow: 'hidden' }}>
      <header className="card-head">
        <Icon name="dots" size={16} />
        <h3>Text chat</h3>
      </header>

      <div ref={scrollRef} className="scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: 16, display: 'flex', flexDirection: 'column', gap: 12 }}>
        {turns.length === 0 ? (
          <p className="faint" style={{ fontSize: 13 }}>
            {enabled ? 'Say hello to start the conversation.' : 'Complete consent to begin.'}
          </p>
        ) : (
          turns.map((turn, i) => (
            <div
              key={i}
              style={{ display: 'flex', justifyContent: turn.role === 'prospect' ? 'flex-end' : 'flex-start' }}
            >
              <div style={{ maxWidth: '82%' }}>
                <div
                  style={{
                    padding: '9px 13px',
                    borderRadius: 14,
                    fontSize: 13.5,
                    lineHeight: 1.5,
                    whiteSpace: 'pre-wrap',
                    ...(turn.role === 'prospect'
                      ? {
                          background: 'var(--surface-2)',
                          border: '1px solid var(--border)',
                          color: 'var(--text)',
                          borderTopRightRadius: 5,
                        }
                      : {
                          background: 'var(--accent-grad)',
                          color: 'var(--accent-ink)',
                          fontWeight: 500,
                          borderTopLeftRadius: 5,
                        }),
                  }}
                >
                  {turn.text}
                </div>
                {/* decision_act is internal/debug-only — gated behind showDebug, never default. */}
                {showDebug && turn.role === 'agent' && turn.decisionAct ? (
                  <span className="tag warn mono" style={{ marginTop: 5, fontSize: 10 }}>
                    act: {turn.decisionAct}
                  </span>
                ) : null}
              </div>
            </div>
          ))
        )}

        {/* CB-54: terminal state — polite end-of-call notice, input stays off. */}
        {done && !error ? (
          <p className="faint" style={{ fontSize: 13, textAlign: 'center', marginTop: 8 }}>
            The conversation has ended. Refresh the page to start a new one.
          </p>
        ) : null}
      </div>

      {error ? (
        <div style={{ padding: '0 16px 8px' }}>
          <p style={{ fontSize: 11.5, color: 'var(--danger)', marginBottom: onConsentRequired ? 6 : 0 }}>
            {error}
          </p>
          {/* CB-54: when onConsentRequired is provided, the parent will re-show the gate. */}
          {onConsentRequired ? (
            <button
              type="button"
              onClick={() => { setError(null); onConsentRequired(); }}
              className="btn btn-ghost"
              style={{ fontSize: 12, padding: '4px 10px' }}
            >
              Re-open consent
            </button>
          ) : null}
        </div>
      ) : null}

      <form
        style={{ display: 'flex', gap: 8, borderTop: '1px solid var(--border)', padding: 12 }}
        onSubmit={(e) => {
          e.preventDefault();
          void handleSend();
        }}
      >
        <input
          type="text"
          value={draft}
          disabled={!enabled || sending || done}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={enabled && !done ? 'Type a message…' : done ? 'Conversation ended' : 'Consent required'}
          className="input"
          style={{ flex: 1, opacity: !enabled || sending || done ? 0.55 : 1 }}
        />
        <button
          type="submit"
          disabled={!canSend}
          className="btn btn-primary"
          style={{ opacity: canSend ? 1 : 0.4, cursor: canSend ? 'pointer' : 'not-allowed' }}
        >
          {sending ? '…' : 'Send'}
        </button>
      </form>
    </section>
  );
}
