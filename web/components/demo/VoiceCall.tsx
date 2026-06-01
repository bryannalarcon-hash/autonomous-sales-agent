// VoiceCall — the "Start voice call" control + LiveKit connection lifecycle (R3 web voice). Fetches
// POST /api/livekit/token, connects a livekit-client Room to the returned url, and renders VoiceRoom
// (state + mute/disconnect + transcript). Two failure modes are handled WITHOUT crashing: a 503 from
// the token endpoint (LiveKit creds absent) shows the "voice unavailable — use text console"
// fallback, and any other error shows a retryable message. Gated by `enabled` (consent). The token
// never persists anywhere; it lives only for the duration of room.connect.
// N8 FIX: the idle panel was a near-empty box (title + lone button). It now carries an honest
// explainer of what "Start voice call" does (connects you to the AI advisor by voice in-browser;
// mic permission needed) plus a status/idle indicator, and fills the same column height as the text
// console beside it — so the panel reads as ready rather than broken. Button + behavior unchanged.
// Styled in the dark "Cadence" design system (rendered inside the page's `.cadence` scope): a .card
// shell, .tag warn fallback banner, .btn-primary start control — not raw light Tailwind.
'use client';

import { useCallback, useRef, useState } from 'react';
import { Room } from 'livekit-client';
import { livekitToken, ApiError } from '@/lib/api';
import { Icon } from '@/components/cadence/Icon';
import VoiceRoom from './VoiceRoom';

interface VoiceCallProps {
  sessionId: string | null;
  identity: string;
  enabled: boolean;
}

type Status = 'idle' | 'connecting' | 'connected' | 'unavailable' | 'error';

export default function VoiceCall({ sessionId, identity, enabled }: VoiceCallProps) {
  const [status, setStatus] = useState<Status>('idle');
  const [error, setError] = useState<string | null>(null);
  const [room, setRoom] = useState<Room | null>(null);
  const roomRef = useRef<Room | null>(null);

  const disconnect = useCallback(async () => {
    const r = roomRef.current;
    roomRef.current = null;
    setRoom(null);
    setStatus('idle');
    if (r) await r.disconnect();
  }, []);

  const start = useCallback(async () => {
    if (!sessionId) return;
    setStatus('connecting');
    setError(null);
    try {
      const { token, url } = await livekitToken({ session_id: sessionId, identity });
      const r = new Room({ adaptiveStream: true, dynacast: true });
      await r.connect(url, token);
      await r.localParticipant.setMicrophoneEnabled(true);
      roomRef.current = r;
      setRoom(r);
      setStatus('connected');
    } catch (e) {
      if (e instanceof ApiError && e.status === 503) {
        // LiveKit creds absent in this environment — graceful, non-crashing fallback.
        setStatus('unavailable');
      } else {
        setError(e instanceof ApiError ? e.message : 'Could not start the voice call.');
        setStatus('error');
      }
    }
  }, [sessionId, identity]);

  // Idle/connecting status dot + label — mirrors the connected VoiceRoom's status row so the panel
  // shows a clear state at every stage instead of reading as an empty box (N8). Dot color maps to a
  // Cadence token; connecting pulses via the shared cad-blink animation.
  const dotColor =
    status === 'connecting' ? 'var(--warn)' : status === 'error' ? 'var(--danger)' : 'var(--text-3)';
  const statusLabel =
    status === 'connecting'
      ? 'Connecting…'
      : status === 'error'
        ? 'Connection failed'
        : enabled
          ? 'Ready'
          : 'Awaiting consent';
  const canStart = enabled && !!sessionId && status !== 'connecting';

  return (
    <section className="card" style={{ display: 'flex', height: '100%', flexDirection: 'column', overflow: 'hidden' }}>
      <header className="card-head">
        <Icon name="phone" size={16} />
        <h3>Voice call</h3>
      </header>

      <div className="scroll" style={{ flex: 1, minHeight: 0, overflowY: 'auto', padding: 16 }}>
        {status === 'unavailable' ? (
          <div
            className="tag warn"
            style={{ display: 'flex', width: '100%', padding: '10px 13px', lineHeight: 1.5, whiteSpace: 'normal' }}
          >
            Voice is unavailable in this environment (LiveKit not configured). Please use the text
            console instead.
          </div>
        ) : status === 'connected' && room ? (
          <VoiceRoom room={room} onDisconnect={() => void disconnect()} />
        ) : (
          <div className="col gap16">
            {/* Idle status indicator — parity with the text console's filled panel. */}
            <div className="row gap8" style={{ fontSize: 13 }}>
              <span
                className={status === 'connecting' ? 'live-dot' : undefined}
                style={{
                  display: 'inline-block',
                  width: 8,
                  height: 8,
                  borderRadius: '50%',
                  background: dotColor,
                  margin: 0,
                }}
              />
              <span className="muted">{statusLabel}</span>
            </div>

            {/* Honest explainer of what the button does, so the panel never looks broken/empty. */}
            <p className="muted" style={{ fontSize: 13, lineHeight: 1.6 }}>
              <strong style={{ fontWeight: 600, color: 'var(--text)' }}>Start voice call</strong>{' '}
              connects you to the AI tutoring advisor by voice, right in your browser. Your browser
              will ask for microphone permission so the advisor can hear you — a live transcript
              appears once you&rsquo;re connected.
            </p>

            <button
              type="button"
              disabled={!canStart}
              onClick={() => void start()}
              className="btn btn-primary"
              style={{ alignSelf: 'flex-start', opacity: canStart ? 1 : 0.4, cursor: canStart ? 'pointer' : 'not-allowed' }}
            >
              {status === 'connecting' ? 'Connecting…' : 'Start voice call'}
            </button>
            {!enabled ? (
              <p className="faint" style={{ fontSize: 11.5 }}>Consent required before starting a call.</p>
            ) : null}
            {status === 'error' && error ? (
              <p style={{ fontSize: 11.5, color: 'var(--danger)' }}>{error}</p>
            ) : null}
          </div>
        )}
      </div>
    </section>
  );
}
