// TextConsole — the text-chat panel (R1). POSTs /api/chat {session_id, text} and renders {reply}.
// Gated: the input/send are disabled until consent is satisfied (controlled by `enabled`); a 409
// from the backend surfaces as "consent required". The internal `decision_act` policy label is
// debug-only and rendered ONLY when `showDebug` is on — it is NEVER shown to the prospect by
// default (house rule: internal act labels never render in the prospect-facing surface).
'use client';

import { useRef, useState } from 'react';
import { sendChat, ApiError } from '@/lib/api';

interface TextConsoleProps {
  sessionId: string | null;
  enabled: boolean;
  /** Debug toggle: when true, render the internal decision_act tag on agent turns. */
  showDebug: boolean;
}

interface ChatTurn {
  role: 'prospect' | 'agent';
  text: string;
  decisionAct?: string;
}

export default function TextConsole({ sessionId, enabled, showDebug }: TextConsoleProps) {
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const canSend = enabled && !!sessionId && draft.trim().length > 0 && !sending;

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
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setError('Consent required before chatting.');
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
    <section className="flex h-full flex-col rounded-lg border border-neutral-200 bg-white">
      <header className="border-b border-neutral-200 px-4 py-2.5">
        <h2 className="text-sm font-semibold">Text chat</h2>
      </header>

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto p-4">
        {turns.length === 0 ? (
          <p className="text-sm text-neutral-400">
            {enabled ? 'Say hello to start the conversation.' : 'Complete consent to begin.'}
          </p>
        ) : (
          turns.map((turn, i) => (
            <div
              key={i}
              className={turn.role === 'prospect' ? 'flex justify-end' : 'flex justify-start'}
            >
              <div
                className={`max-w-[80%] rounded-2xl px-3 py-2 text-sm ${
                  turn.role === 'prospect'
                    ? 'bg-neutral-900 text-white'
                    : 'bg-neutral-100 text-neutral-900'
                }`}
              >
                <p className="whitespace-pre-wrap">{turn.text}</p>
                {/* decision_act is internal/debug-only — gated behind showDebug, never default. */}
                {showDebug && turn.role === 'agent' && turn.decisionAct ? (
                  <span className="mt-1 inline-block rounded bg-amber-100 px-1.5 py-0.5 font-mono text-[10px] text-amber-800">
                    act: {turn.decisionAct}
                  </span>
                ) : null}
              </div>
            </div>
          ))
        )}
      </div>

      {error ? <p className="px-4 pb-1 text-xs text-red-600">{error}</p> : null}

      <form
        className="flex gap-2 border-t border-neutral-200 p-3"
        onSubmit={(e) => {
          e.preventDefault();
          void handleSend();
        }}
      >
        <input
          type="text"
          value={draft}
          disabled={!enabled || sending}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={enabled ? 'Type a message…' : 'Consent required'}
          className="flex-1 rounded-md border border-neutral-300 px-3 py-2 text-sm outline-none focus:border-neutral-500 disabled:bg-neutral-100 disabled:text-neutral-400"
        />
        <button
          type="submit"
          disabled={!canSend}
          className="rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {sending ? '…' : 'Send'}
        </button>
      </form>
    </section>
  );
}
