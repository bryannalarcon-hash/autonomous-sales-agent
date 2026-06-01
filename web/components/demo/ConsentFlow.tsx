// ConsentFlow — the compliance gate that fronts the demo (R33/R40/R41). On mount it calls
// POST /api/consent/start to fetch the AI-disclosure + recording notice, collects (a) AI ack,
// (b) recording Allow/Decline, (c) a minor checkbox, then POSTs /api/consent/respond and handles
// every returned state. On `need_parental` it shows a parental step that re-submits with
// parental_consent:true. It NEVER enables the call itself — it reports the resolved ConsentState up
// to the demo page, which owns gating; this component just drives the conversation to a resolution.
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
    return <p className="text-sm text-neutral-500">Loading disclosure…</p>;
  }

  if (phase === 'error') {
    return (
      <div className="space-y-3">
        <p className="text-sm text-red-600">{error}</p>
        <button
          type="button"
          onClick={() => void start()}
          className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700"
        >
          Retry
        </button>
      </div>
    );
  }

  if (phase === 'parental') {
    return (
      <div className="space-y-4">
        <h3 className="text-base font-semibold">Parental consent required</h3>
        <p className="text-sm text-neutral-600">
          Because this call involves someone under 18, a parent or guardian must consent before we
          continue.
        </p>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => void submit(true)}
            className="rounded-md bg-neutral-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-neutral-700"
          >
            I am the parent/guardian and I consent
          </button>
          <button
            type="button"
            onClick={() => void submit(false)}
            className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 hover:bg-neutral-100"
          >
            Decline
          </button>
        </div>
      </div>
    );
  }

  // collect / submitting
  const canSubmit = aiAck && recordingConsent !== null && phase !== 'submitting';

  return (
    <div className="space-y-5">
      {/* Disclosure text shown verbatim from the backend (AI disclosure + recording notice). */}
      <div className="rounded-md border border-neutral-200 bg-white p-4 text-sm leading-relaxed text-neutral-700 whitespace-pre-wrap">
        {disclosure}
      </div>

      {/* (a) Acknowledge AI */}
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={aiAck}
          onChange={(e) => setAiAck(e.target.checked)}
        />
        <span>I understand I am speaking with an AI assistant.</span>
      </label>

      {/* (b) Recording consent: Allow / Decline */}
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Recording</legend>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={() => setRecordingConsent(true)}
            className={`rounded-md border px-3 py-1.5 text-sm font-medium ${
              recordingConsent === true
                ? 'border-neutral-900 bg-neutral-900 text-white'
                : 'border-neutral-300 text-neutral-700 hover:bg-neutral-100'
            }`}
          >
            Allow recording
          </button>
          <button
            type="button"
            onClick={() => setRecordingConsent(false)}
            className={`rounded-md border px-3 py-1.5 text-sm font-medium ${
              recordingConsent === false
                ? 'border-neutral-900 bg-neutral-900 text-white'
                : 'border-neutral-300 text-neutral-700 hover:bg-neutral-100'
            }`}
          >
            Decline recording
          </button>
        </div>
      </fieldset>

      {/* (c) Minor checkbox */}
      <label className="flex items-start gap-2 text-sm">
        <input
          type="checkbox"
          className="mt-0.5"
          checked={isMinor}
          onChange={(e) => setIsMinor(e.target.checked)}
        />
        <span>I am calling about a student under 18, or I am under 18.</span>
      </label>

      {error ? <p className="text-sm text-red-600">{error}</p> : null}

      <button
        type="button"
        disabled={!canSubmit}
        onClick={() => void submit()}
        className="rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {phase === 'submitting' ? 'Submitting…' : 'Continue'}
      </button>
    </div>
  );
}
