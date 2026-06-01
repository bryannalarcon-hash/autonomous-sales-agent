// VoiceCall — the "Start voice call" control + LiveKit connection lifecycle (R3 web voice). Fetches
// POST /api/livekit/token, connects a livekit-client Room to the returned url, and renders VoiceRoom
// (state + mute/disconnect + transcript). Two failure modes are handled WITHOUT crashing: a 503 from
// the token endpoint (LiveKit creds absent) shows the "voice unavailable — use text console"
// fallback, and any other error shows a retryable message. Gated by `enabled` (consent). The token
// never persists anywhere; it lives only for the duration of room.connect.
'use client';

import { useCallback, useRef, useState } from 'react';
import { Room } from 'livekit-client';
import { livekitToken, ApiError } from '@/lib/api';
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

  return (
    <section className="rounded-lg border border-neutral-200 bg-white p-4">
      <h2 className="mb-3 text-sm font-semibold">Voice call</h2>

      {status === 'unavailable' ? (
        <div className="rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
          Voice is unavailable in this environment (LiveKit not configured). Please use the text
          console instead.
        </div>
      ) : status === 'connected' && room ? (
        <VoiceRoom room={room} onDisconnect={() => void disconnect()} />
      ) : (
        <div className="space-y-2">
          <button
            type="button"
            disabled={!enabled || !sessionId || status === 'connecting'}
            onClick={() => void start()}
            className="rounded-md bg-neutral-900 px-4 py-2 text-sm font-medium text-white hover:bg-neutral-700 disabled:cursor-not-allowed disabled:opacity-40"
          >
            {status === 'connecting' ? 'Connecting…' : 'Start voice call'}
          </button>
          {!enabled ? (
            <p className="text-xs text-neutral-400">Consent required before starting a call.</p>
          ) : null}
          {status === 'error' && error ? <p className="text-xs text-red-600">{error}</p> : null}
        </div>
      )}
    </section>
  );
}
