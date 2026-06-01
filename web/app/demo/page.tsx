// /demo — the prospect-facing call console (plan U13). Three stages on the same brain: a consent
// gate (ConsentFlow) that MUST resolve to ready/unrecorded before the call UI unlocks, then a text
// console (R1) and a LiveKit web-voice control (R3) side by side. This page owns the session: it
// holds the session_id + resolved ConsentState and derives the gate (lib/consent.isCallEnabled) so
// the call UI stays disabled until consent is satisfied. `ended` shows a polite end screen;
// `unrecorded` shows a visible "not being recorded" banner. A debug toggle reveals internal
// decision_act labels for engineers only — never shown to the prospect by default.
'use client';

import Link from 'next/link';
import { useCallback, useState } from 'react';
import ConsentFlow from '@/components/demo/ConsentFlow';
import TextConsole from '@/components/demo/TextConsole';
import VoiceCall from '@/components/demo/VoiceCall';
import type { ConsentState } from '@/lib/api-types';
import { isCallEnabled, isEnded, isRecorded } from '@/lib/consent';

// Demo defaults — a real deployment would derive jurisdiction from the prospect; the channel is
// "voice" so the disclosure covers the recording notice the voice path needs.
const DEMO_JURISDICTION = 'US-CA';
const DEMO_CHANNEL = 'voice' as const;

export default function DemoPage() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [consentState, setConsentState] = useState<ConsentState | null>(null);
  const [endMessage, setEndMessage] = useState<string | undefined>();
  const [showDebug, setShowDebug] = useState(false);

  const handleResolved = useCallback(
    (id: string, state: ConsentState, message?: string) => {
      setSessionId(id);
      setConsentState(state);
      if (state === 'ended') setEndMessage(message);
    },
    [],
  );

  const callEnabled = isCallEnabled(consentState);
  const recorded = isRecorded(consentState);

  return (
    <main className="mx-auto max-w-5xl p-6">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Talk to our tutoring advisor</h1>
          <p className="text-sm text-neutral-500">An AI assistant — text or voice.</p>
        </div>
        <div className="flex items-center gap-4">
          {/* Operator-facing: jump to the Cadence Operate/Improve dashboard (live monitor, KPIs, lab). */}
          <Link
            href="/operate"
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 hover:bg-neutral-50"
          >
            Operator dashboard →
          </Link>
          {/* Debug toggle — engineer-only; controls visibility of internal decision_act labels. */}
          <label className="flex items-center gap-1.5 text-xs text-neutral-400">
            <input
              type="checkbox"
              checked={showDebug}
              onChange={(e) => setShowDebug(e.target.checked)}
            />
            debug
          </label>
        </div>
      </header>

      {/* End screen takes over the whole surface. */}
      {isEnded(consentState) ? (
        <div className="rounded-lg border border-neutral-200 bg-white p-8 text-center">
          <h2 className="text-base font-semibold">Thanks for stopping by.</h2>
          <p className="mt-2 text-sm text-neutral-600">
            {endMessage ?? 'We can’t proceed without the required consent. Take care!'}
          </p>
        </div>
      ) : (
        <>
          {/* Stage 1: consent gate. Hidden once the call is enabled. */}
          {!callEnabled ? (
            <div className="mb-6 rounded-lg border border-neutral-200 bg-white p-6">
              <h2 className="mb-4 text-base font-semibold">Before we begin</h2>
              <ConsentFlow
                jurisdiction={DEMO_JURISDICTION}
                channel={DEMO_CHANNEL}
                onResolved={handleResolved}
              />
            </div>
          ) : (
            <>
              {/* "Not being recorded" banner for the unrecorded state. */}
              {!recorded ? (
                <div className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-800">
                  This conversation is <strong>not being recorded</strong>.
                </div>
              ) : null}

              <div className="grid gap-6 md:grid-cols-2">
                <div className="h-[28rem]">
                  <TextConsole
                    sessionId={sessionId}
                    enabled={callEnabled}
                    showDebug={showDebug}
                  />
                </div>
                <VoiceCall sessionId={sessionId} identity="prospect" enabled={callEnabled} />
              </div>
            </>
          )}
        </>
      )}
    </main>
  );
}
