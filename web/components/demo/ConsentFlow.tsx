// ConsentFlow — the compliance gate that fronts the demo (R33/R40/R41). On mount it calls
// POST /api/consent/start to fetch the AI-disclosure + recording notice, collects (a) AI ack,
// (b) recording Allow/Decline, (c) a minor checkbox, then POSTs /api/consent/respond and handles
// every returned state. On `need_parental` it shows a parental step that re-submits with
// parental_consent:true. It NEVER enables the call itself — it reports the resolved ConsentState up
// to the demo page, which owns gating; this component just drives the conversation to a resolution.
// Styled in the dark "Cadence" design system (rendered inside the page's `.cadence` scope) — uses
// the shared .btn-*/.tag/.card tokens, not raw light Tailwind, so it matches the operator console.
'use client';

import { useCallback, useEffect, useState } from 'react';
import { consentRespond, consentStart, ApiError } from '@/lib/api';
import type { ConsentChannel, ConsentState } from '@/lib/api-types';
import { isEnded, needsParental } from '@/lib/consent';

interface ConsentFlowProps {
  jurisdiction: string;
  channel: ConsentChannel;
  /** Called whenever the backend resolves a consent state and once a session_id exists. */
  onResolved: (sessionId: string, state: ConsentState, message?: string) => void;
}

type Phase = 'loading' | 'collect' | 'parental' | 'submitting' | 'error';

export default function ConsentFlow({ jurisdiction, channel, onResolved }: ConsentFlowProps) {
  const [phase, setPhase] = useState<Phase>('loading');
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [disclosure, setDisclosure] = useState<string>('');

  // Collected answers.
  const [aiAck, setAiAck] = useState(false);
  const [recordingConsent, setRecordingConsent] = useState<boolean | null>(null);
  const [isMinor, setIsMinor] = useState(false);

  const start = useCallback(async () => {
    setPhase('loading');
    setError(null);
    try {
      const res = await consentStart({ jurisdiction, channel });
      setSessionId(res.session_id);
      setDisclosure(res.disclosure_text);
      setPhase('collect');
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to start the consent flow.');
      setPhase('error');
    }
  }, [jurisdiction, channel]);

  useEffect(() => {
    void start();
  }, [start]);

  const submit = useCallback(
    async (parental?: boolean) => {
      if (!sessionId || recordingConsent === null) return;
      setPhase('submitting');
      setError(null);
      try {
        const res = await consentRespond({
          session_id: sessionId,
          ai_acknowledged: aiAck,
          recording_consent: recordingConsent,
          is_minor: isMinor,
          ...(parental !== undefined ? { parental_consent: parental } : {}),
        });
        // Report up first so the page can gate the call UI.
        onResolved(res.session_id, res.state, res.message);
        if (needsParental(res.state)) {
          setPhase('parental');
        } else if (isEnded(res.state)) {
          setPhase('collect'); // page renders the end screen; keep our form inert behind it
        } else {
          setPhase('collect');
        }
      } catch (e) {
        setError(e instanceof ApiError ? e.message : 'Failed to submit consent.');
        setPhase('error');
      }
    },
    [sessionId, aiAck, recordingConsent, isMinor, onResolved],
  );

  if (phase === 'loading') {
    return <p className="muted" style={{ fontSize: 13 }}>Loading disclosure…</p>;
  }

  if (phase === 'error') {
    return (
      <div className="col gap12">
        <p style={{ fontSize: 13, color: 'var(--danger)' }}>{error}</p>
        <button type="button" onClick={() => void start()} className="btn btn-primary" style={{ alignSelf: 'flex-start' }}>
          Retry
        </button>
      </div>
    );
  }

  if (phase === 'parental') {
    return (
      <div className="col gap14">
        <h3 style={{ fontSize: 16 }}>Parental consent required</h3>
        <p className="muted" style={{ fontSize: 13, lineHeight: 1.5 }}>
          Because this call involves someone under 18, a parent or guardian must consent before we
          continue.
        </p>
        <div className="row gap8 wrap">
          <button type="button" onClick={() => void submit(true)} className="btn btn-primary">
            I am the parent/guardian and I consent
          </button>
          <button type="button" onClick={() => void submit(false)} className="btn btn-ghost">
            Decline
          </button>
        </div>
      </div>
    );
  }

  // collect / submitting
  const canSubmit = aiAck && recordingConsent !== null && phase !== 'submitting';

  return (
    <div className="col gap16">
      {/* Disclosure text shown verbatim from the backend (AI disclosure + recording notice). */}
      <div
        className="muted"
        style={{
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--r-sm)',
          padding: 14,
          fontSize: 13,
          lineHeight: 1.6,
          color: 'var(--text-2)',
          whiteSpace: 'pre-wrap',
        }}
      >
        {disclosure}
      </div>

      {/* (a) Acknowledge AI */}
      <label className="row gap8" style={{ alignItems: 'flex-start', fontSize: 13.5, cursor: 'pointer' }}>
        <input
          type="checkbox"
          style={{ marginTop: 2, accentColor: 'var(--accent)' }}
          checked={aiAck}
          onChange={(e) => setAiAck(e.target.checked)}
        />
        <span>I understand I am speaking with an AI assistant.</span>
      </label>

      {/* (b) Recording consent: Allow / Decline */}
      <fieldset className="col gap8" style={{ border: 0, padding: 0, margin: 0 }}>
        <legend style={{ fontSize: 13, fontWeight: 600, padding: 0, marginBottom: 4 }}>Recording</legend>
        <div className="row gap8 wrap">
          <button
            type="button"
            onClick={() => setRecordingConsent(true)}
            className={`btn ${recordingConsent === true ? 'btn-primary' : 'btn-ghost'}`}
          >
            Allow recording
          </button>
          <button
            type="button"
            onClick={() => setRecordingConsent(false)}
            className={`btn ${recordingConsent === false ? 'btn-primary' : 'btn-ghost'}`}
          >
            Decline recording
          </button>
        </div>
      </fieldset>

      {/* (c) Minor checkbox */}
      <label className="row gap8" style={{ alignItems: 'flex-start', fontSize: 13.5, cursor: 'pointer' }}>
        <input
          type="checkbox"
          style={{ marginTop: 2, accentColor: 'var(--accent)' }}
          checked={isMinor}
          onChange={(e) => setIsMinor(e.target.checked)}
        />
        <span>I am calling about a student under 18, or I am under 18.</span>
      </label>

      {error ? <p style={{ fontSize: 13, color: 'var(--danger)' }}>{error}</p> : null}

      <button
        type="button"
        disabled={!canSubmit}
        onClick={() => void submit()}
        className="btn btn-primary btn-lg"
        style={{ alignSelf: 'flex-start', opacity: canSubmit ? 1 : 0.4, cursor: canSubmit ? 'pointer' : 'not-allowed' }}
      >
        {phase === 'submitting' ? 'Submitting…' : 'Continue'}
      </button>
    </div>
  );
}
