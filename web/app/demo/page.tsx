// /demo — the prospect-facing call console (plan U13), restyled into the dark "Cadence" design
// system (web/app/cadence.css, applied via the `.cadence` scope from this page + demo/layout.tsx).
// Three stages on the same brain: a consent gate (ConsentFlow) that MUST resolve to ready/unrecorded
// before the call UI unlocks, then a text console (R1) and a LiveKit web-voice control (R3) side by
// side. This page owns the session: it holds the session_id + resolved ConsentState and derives the
// gate (lib/consent.isCallEnabled) so the call UI stays disabled until consent is satisfied. `ended`
// shows a polite end screen; `unrecorded` shows a visible "not being recorded" banner. A debug toggle
// reveals internal decision_act labels for engineers only — never shown to the prospect by default.
// CB-54: `handleConsentRequired` resets the consent state so the gate re-appears if TextConsole's
// chat returns a 409 (session expired / orphaned by double-start); honest re-consent prompt instead
// of a dead error message. TextConsole also calls /end on done=true to finalize the episode.
// Unlike /operate this is a LIGHTER prospect layout (no nav rail / DashboardShell) but uses the SAME
// dark aurora + tokens + card/tag/button classes — not raw Tailwind bg-white/neutral-*.
'use client';

import Link from 'next/link';
import { useCallback, useState } from 'react';
import ConsentFlow from '@/components/demo/ConsentFlow';
import TextConsole from '@/components/demo/TextConsole';
import VoiceCall from '@/components/demo/VoiceCall';
import { Icon } from '@/components/cadence/Icon';
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

  // CB-54: TextConsole calls this when /api/chat returns 409 (session not consented, e.g. orphaned
  // by the React StrictMode double-start). Reset the gate so ConsentFlow re-appears and the user
  // can re-consent without a dead-end error message.
  const handleConsentRequired = useCallback(() => {
    setConsentState(null);
    setSessionId(null);
  }, []);

  const callEnabled = isCallEnabled(consentState);
  const recorded = isRecorded(consentState);

  return (
    // `.cadence` paints the full-viewport dark aurora + sets the token/font context; the prospect
    // content scrolls inside a centered .page-scroll (no operator nav rail).
    <div className="cadence">
      <div className="page">
        <header className="topbar">
          {/* Prospect brand lockup — the same gradient wordmark the operator shell uses. */}
          <div className="brand-mark" style={{ width: 30, height: 30, borderRadius: 9 }}>
            <Icon name="pulse" size={17} sw={2.4} />
          </div>
          <div className="top-title" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 1 }}>
            <span className="glow-text" style={{ lineHeight: 1.1 }}>
              Talk to our tutoring advisor
            </span>
            <span className="top-sub">An AI assistant — text or voice.</span>
          </div>
          <div className="top-spacer" />
          {/* Operator-facing: jump to the Cadence Operate/Improve dashboard (live monitor, KPIs, lab). */}
          <Link href="/operate" className="gctl" title="Open the operator dashboard">
            <Icon name="chart" size={15} />
            <span>Operator dashboard</span>
            <Icon name="arrowR" className="gctl-chev" />
          </Link>
          {/* Debug toggle — engineer-only; controls visibility of internal decision_act labels. */}
          <label className="row gap6 faint" style={{ fontSize: 12, fontWeight: 600, cursor: 'pointer' }}>
            <button
              type="button"
              className={`toggle${showDebug ? ' on' : ''}`}
              aria-pressed={showDebug}
              onClick={() => setShowDebug((v) => !v)}
            >
              <i />
            </button>
            debug
          </label>
        </header>

        <div className="page-scroll">
          <div className="pad" style={{ maxWidth: 1040, margin: '0 auto', width: '100%' }}>
            {/* End screen takes over the whole surface. */}
            {isEnded(consentState) ? (
              <div className="card card-pad" style={{ textAlign: 'center', padding: '48px 32px' }}>
                <h2 style={{ fontSize: 18 }}>Thanks for stopping by.</h2>
                <p className="muted" style={{ marginTop: 8, fontSize: 13.5 }}>
                  {endMessage ?? 'We can’t proceed without the required consent. Take care!'}
                </p>
              </div>
            ) : (
              <>
                {/* Stage 1: consent gate. Hidden once the call is enabled. */}
                {!callEnabled ? (
                  <div className="card" style={{ maxWidth: 620, margin: '0 auto' }}>
                    <div className="card-head">
                      <Icon name="shield" size={16} />
                      <h3>Before we begin</h3>
                    </div>
                    <div className="card-pad">
                      <ConsentFlow
                        jurisdiction={DEMO_JURISDICTION}
                        channel={DEMO_CHANNEL}
                        onResolved={handleResolved}
                      />
                    </div>
                  </div>
                ) : (
                  <>
                    {/* "Not being recorded" banner for the unrecorded state. */}
                    {!recorded ? (
                      <div
                        className="tag warn tag-lg"
                        style={{ display: 'flex', width: '100%', marginBottom: 16, padding: '9px 14px' }}
                      >
                        This conversation is&nbsp;<strong>not being recorded</strong>.
                      </div>
                    ) : null}

                    <div
                      style={{
                        display: 'grid',
                        gap: 18,
                        gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
                      }}
                    >
                      <div style={{ height: '28rem' }}>
                        <TextConsole
                          sessionId={sessionId}
                          enabled={callEnabled}
                          showDebug={showDebug}
                          onConsentRequired={handleConsentRequired}
                        />
                      </div>
                      {/* Equal-height wrapper so the voice panel matches the text console (N8 parity). */}
                      <div style={{ height: '28rem' }}>
                        <VoiceCall sessionId={sessionId} identity="prospect" enabled={callEnabled} />
                      </div>
                    </div>
                  </>
                )}
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
