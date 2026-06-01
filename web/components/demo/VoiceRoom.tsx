// VoiceRoom — the LiveKit room internals, rendered ONLY after a token is fetched and a Room is
// connected by the parent (VoiceCall). Uses @livekit/components-react's RoomContext so hooks like
// useConnectionState / useTracks work, and renders the live transcript from the agent's published
// transcription segments. Exposes mute + disconnect controls. This component assumes a connected
// Room is passed in; it never fetches tokens or holds secrets (the parent does that).
// Styled in the dark "Cadence" design system (rendered inside the page's `.cadence` scope): token
// status dot, .btn-ghost/.btn-danger controls, a surface transcript panel — not raw light Tailwind.
'use client';

import { useEffect, useMemo, useState } from 'react';
import { Room, RoomEvent, Track, TranscriptionSegment } from 'livekit-client';
import {
  RoomContext,
  useConnectionState,
  useLocalParticipant,
} from '@livekit/components-react';

interface VoiceRoomProps {
  room: Room;
  onDisconnect: () => void;
}

/** Inner UI — must live under RoomContext so the LiveKit hooks resolve. */
function RoomInner({ onDisconnect }: { onDisconnect: () => void }) {
  const connectionState = useConnectionState();
  const { localParticipant } = useLocalParticipant();
  const [muted, setMuted] = useState(false);

  async function toggleMute() {
    const next = !muted;
    await localParticipant.setMicrophoneEnabled(!next);
    setMuted(next);
  }

  return (
    <div className="col gap12">
      <div className="row gap8" style={{ fontSize: 13 }}>
        <span
          style={{
            display: 'inline-block',
            width: 8,
            height: 8,
            borderRadius: '50%',
            background: connectionState === 'connected' ? 'var(--ok)' : 'var(--warn)',
          }}
        />
        <span className="muted" style={{ textTransform: 'capitalize' }}>{connectionState}</span>
      </div>

      <div className="row gap8">
        <button type="button" onClick={() => void toggleMute()} className="btn btn-ghost btn-sm">
          {muted ? 'Unmute' : 'Mute'}
        </button>
        <button type="button" onClick={onDisconnect} className="btn btn-danger btn-sm">
          End call
        </button>
      </div>
    </div>
  );
}

export default function VoiceRoom({ room, onDisconnect }: VoiceRoomProps) {
  // Live transcript: accumulate transcription segments (deduped by id) as they arrive.
  const [segments, setSegments] = useState<Map<string, TranscriptionSegment>>(new Map());

  useEffect(() => {
    function onTranscription(received: TranscriptionSegment[]) {
      setSegments((prev) => {
        const next = new Map(prev);
        for (const seg of received) next.set(seg.id, seg);
        return next;
      });
    }
    room.on(RoomEvent.TranscriptionReceived, onTranscription);
    return () => {
      room.off(RoomEvent.TranscriptionReceived, onTranscription);
    };
  }, [room]);

  // Auto-attach any subscribed audio track so the agent's TTS plays.
  useEffect(() => {
    function onTrackSubscribed(track: Track) {
      if (track.kind === Track.Kind.Audio) {
        const el = track.attach();
        el.style.display = 'none';
        document.body.appendChild(el);
      }
    }
    room.on(RoomEvent.TrackSubscribed, onTrackSubscribed);
    return () => {
      room.off(RoomEvent.TrackSubscribed, onTrackSubscribed);
    };
  }, [room]);

  const orderedSegments = useMemo(
    () =>
      [...segments.values()].sort(
        (a, b) => (a.firstReceivedTime ?? 0) - (b.firstReceivedTime ?? 0),
      ),
    [segments],
  );

  return (
    <RoomContext.Provider value={room}>
      <RoomInner onDisconnect={onDisconnect} />

      <div
        style={{
          marginTop: 16,
          background: 'var(--surface)',
          border: '1px solid var(--border)',
          borderRadius: 'var(--r-sm)',
          padding: 12,
        }}
      >
        <h3
          style={{
            marginBottom: 8,
            fontSize: 10.5,
            fontWeight: 700,
            textTransform: 'uppercase',
            letterSpacing: '0.06em',
            color: 'var(--text-3)',
          }}
        >
          Live transcript
        </h3>
        {orderedSegments.length === 0 ? (
          <p className="faint" style={{ fontSize: 13 }}>Transcript will appear here as you talk.</p>
        ) : (
          <ul className="col gap6" style={{ fontSize: 13, listStyle: 'none', margin: 0, padding: 0 }}>
            {orderedSegments.map((seg) => (
              <li key={seg.id} style={{ color: 'var(--text-2)' }}>
                {seg.text}
              </li>
            ))}
          </ul>
        )}
      </div>
    </RoomContext.Provider>
  );
}
