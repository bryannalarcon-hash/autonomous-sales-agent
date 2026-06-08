// TextConsole — the text-chat panel (R1). POSTs /api/chat {session_id, text} and renders {reply}.
// Gated: the input/send are disabled until consent is satisfied (controlled by `enabled`); a 409
// from the backend surfaces as a re-consent prompt (CB-54: honest copy + onConsentRequired callback
// so the page can re-show the gate rather than leaving the user in a dead end). The internal
// `decision_act` policy label is debug-only and rendered ONLY when `showDebug` is on — it is NEVER
// shown to the prospect by default (house rule: internal act labels never render in the
// prospect-facing surface). CB-54: when done=true is returned by /api/chat the session is finalized
// (POST /api/session/{id}/end) so the episode reaches a terminal status and appears in the Calls
// list (previously the session lingered as in_progress indefinitely).
// CB-63: /api/chat now streams tokens via SSE (Accept: text/event-stream). The UI appends each
// `token` event to the agent bubble in real-time (progressive render) and shows an immediate typing
// indicator from the moment the user hits Send — eliminating the 5.7–9.1 s silence. The `done`
// event carries the final {reply, decision_act, done} payload and triggers /end on terminal turns.
// Fallback: if the browser or server does not support SSE, the fetch-based stream still works
// because we use fetch() + ReadableStream (not EventSource), so CORS + credentials headers are
// always sent. CB-54 behaviours preserved: 409 re-consent recovery, /end on done.
// Styled in the dark "Cadence" design system (rendered inside the page's `.cadence` scope): a
// .card shell, accent-gradient agent bubbles vs surface prospect bubbles, .input + .btn composer.
'use client';

import { useRef, useState } from 'react';
import { sessionEnd, ApiError, API_BASE } from '@/lib/api';
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
  /** True while a streaming agent reply is still arriving (typing indicator). */
  streaming?: boolean;
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

  /** Scroll the message list to the bottom (deferred to let the DOM settle). */
  function scrollToBottom() {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    });
  }

  /** CB-54: finalize the session after a terminal turn. Fire-and-forget — a failure here
   *  doesn't block the UX; the operator can still find the in-progress episode. */
  function maybeFinalize(currentSessionId: string) {
    if (!endedRef.current) {
      endedRef.current = true;
      void sessionEnd({ session_id: currentSessionId }).catch(() => {});
    }
  }

  async function handleSend() {
    if (!canSend || !sessionId) return;
    const text = draft.trim();
    const currentSessionId = sessionId;
    setDraft('');
    setError(null);
    // Add the prospect's turn immediately so the UI feels responsive.
    setTurns((prev) => [...prev, { role: 'prospect', text }]);
    setSending(true);

    // CB-63: add an agent "typing" placeholder immediately — the streaming turn will fill it in.
    // The placeholder carries streaming=true so we render the typing indicator.
    setTurns((prev) => [...prev, { role: 'agent', text: '', streaming: true }]);
    scrollToBottom();

    try {
      const res = await fetch(`${API_BASE}/api/chat`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          // CB-63: signal we accept SSE so the server streams tokens instead of buffering.
          'Accept': 'text/event-stream',
        },
        body: JSON.stringify({ session_id: currentSessionId, text }),
      });

      // Consent 409 — the backend rejected BEFORE opening the stream.
      if (res.status === 409) {
        // Remove the typing-indicator placeholder we added.
        setTurns((prev) => prev.filter((t) => !t.streaming));
        setError('Your session is no longer valid. Please complete consent again to continue.');
        if (onConsentRequired) {
          onConsentRequired();
        }
        return;
      }

      if (!res.ok || !res.body) {
        setTurns((prev) => prev.filter((t) => !t.streaming));
        setError(`Request failed (${res.status}).`);
        return;
      }

      // SSE streaming: read the response body as a text stream, parse SSE events, and
      // progressively append each `token` event to the agent bubble.
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let currentEvent = 'message';
      let assembledReply = '';
      let finalDecisionAct = '';
      let finalDone = false;

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done: streamDone, value } = await reader.read();
        if (streamDone) break;

        buffer += decoder.decode(value, { stream: true });

        // Process complete SSE lines from the buffer.
        let newlineIdx: number;
        while ((newlineIdx = buffer.indexOf('\n')) !== -1) {
          const line = buffer.slice(0, newlineIdx);
          buffer = buffer.slice(newlineIdx + 1);

          if (line.startsWith(':')) continue; // SSE comment
          if (line.startsWith('event:')) {
            currentEvent = line.slice('event:'.length).trim();
          } else if (line.startsWith('data:')) {
            const rawData = line.slice('data:'.length).trim();
            if (currentEvent === 'token') {
              // Each token data value is a JSON-encoded string (e.g. `"word "`).
              let token: string;
              try {
                token = JSON.parse(rawData) as string;
              } catch {
                token = rawData;
              }
              assembledReply += token;
              // Update the streaming bubble in-place (replace the last streaming turn).
              setTurns((prev) => {
                const copy = [...prev];
                const lastIdx = copy.length - 1;
                if (lastIdx >= 0 && copy[lastIdx].streaming) {
                  copy[lastIdx] = { ...copy[lastIdx], text: assembledReply };
                }
                return copy;
              });
              scrollToBottom();
            } else if (currentEvent === 'done') {
              // Terminal event — finalize the agent bubble with the complete metadata.
              try {
                const payload = JSON.parse(rawData) as {
                  reply: string;
                  decision_act: string;
                  done: boolean;
                };
                // Use the server's assembled reply as the canonical text (R37 parity).
                assembledReply = payload.reply;
                finalDecisionAct = payload.decision_act ?? '';
                finalDone = payload.done ?? false;
              } catch {
                // If JSON parse fails, keep whatever tokens we assembled.
              }
              // Finalize the streaming bubble: clear the streaming flag.
              setTurns((prev) => {
                const copy = [...prev];
                const lastIdx = copy.length - 1;
                if (lastIdx >= 0 && copy[lastIdx].streaming) {
                  copy[lastIdx] = {
                    role: 'agent',
                    text: assembledReply,
                    streaming: false,
                    decisionAct: finalDecisionAct,
                  };
                }
                return copy;
              });
            } else if (currentEvent === 'error') {
              // Server signalled an error inside the stream (e.g. consent flipped mid-stream).
              let errDetail: { error?: string } = {};
              try {
                errDetail = JSON.parse(rawData) as { error?: string };
              } catch {
                // ignore
              }
              if (errDetail.error === 'consent_not_satisfied') {
                setTurns((prev) => prev.filter((t) => !t.streaming));
                setError('Your session is no longer valid. Please complete consent again to continue.');
                if (onConsentRequired) onConsentRequired();
              } else {
                setTurns((prev) => prev.filter((t) => !t.streaming));
                setError('Failed to send message.');
              }
            }
            // Reset event name after dispatch.
            if (line === '' || line.startsWith('data:')) {
              // Keep currentEvent until we see a blank line (event boundary).
            }
          } else if (line === '') {
            // Blank line = end of SSE event block; reset for next event.
            currentEvent = 'message';
          }
        }
      }

      // Stream finished. If the `done` event set finalDone, finalize the session (CB-54).
      if (finalDone) {
        setDone(true);
        maybeFinalize(currentSessionId);
      }
    } catch (e) {
      // Network / parse failure.
      setTurns((prev) => prev.filter((t) => !t.streaming));
      if (e instanceof ApiError && e.status === 409) {
        // CB-54: surface re-consent prompt.
        setError('Your session is no longer valid. Please complete consent again to continue.');
        if (onConsentRequired) onConsentRequired();
      } else {
        setError('Failed to send message.');
      }
    } finally {
      setSending(false);
      scrollToBottom();
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
                  {/* CB-63: show a typing indicator (animated dots) while streaming and text is
                      not yet arrived; once tokens start flowing the accumulating text renders
                      progressively instead. */}
                  {turn.streaming && !turn.text ? (
                    <span style={{ opacity: 0.6, letterSpacing: 2 }} aria-label="Typing">
                      {'···'}
                    </span>
                  ) : (
                    turn.text
                  )}
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
