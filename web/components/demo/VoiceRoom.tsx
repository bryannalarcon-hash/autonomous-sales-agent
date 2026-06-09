// VoiceRoom — the LiveKit room internals, rendered ONLY after a token is fetched and a Room is
// connected by the parent (VoiceCall). Uses @livekit/components-react's RoomContext so hooks like
// useConnectionState / useTracks work, and renders the live transcript from the agent's published
// transcription segments — LABELED "You" (local mic STT) vs "Advisor" (remote agent TTS) off each
// segment's participant. Plays the agent's audio via <RoomAudioRenderer/> (robust: handles tracks
// subscribed before mount + autoplay resume — the old manual track.attach left the call SILENT).
// Exposes mute + disconnect controls. Assumes a connected Room is passed in; never fetches tokens or
// holds secrets (the parent does that). Styled in the dark "Cadence" design system (rendered inside
// the page's `.cadence` scope): status dot, .btn-ghost/.btn-danger controls, a surface transcript.
'use client';

import { useEffect, useMemo, useState } from 'react';
import { Participant, Room, RoomEvent, TranscriptionSegment } from 'livekit-client';
import {
  RoomAudioRenderer,
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
  // Live transcript: accumulate transcription segments (deduped by id) as they arrive, tagging each
  // with whether it came from the LOCAL participant (the caller's mic STT) or a REMOTE one (the
  // agent's TTS) so the panel can label "You" vs "Advisor".
  const [segments, setSegments] = useState<Map<string, { seg: TranscriptionSegment; isLocal: boolean }>>(
    new Map(),
  );

  useEffect(() => {
    // LiveKit attributes each segment to the participant it came from; isLocal => the caller.
    function onTranscription(received: TranscriptionSegment[], participant?: Participant) {
      const isLocal = !!participant?.isLocal;
      setSegments((prev) => {
        const next = new Map(prev);
        for (const seg of received) next.set(seg.id, { seg, isLocal });
        return next;
      });
    }
    room.on(RoomEvent.TranscriptionReceived, onTranscription);
    return () => {
      room.off(RoomEvent.TranscriptionReceived, onTranscription);
    };
  }, [room]);

  const orderedSegments = useMemo(
    () =>
      [...segments.values()].sort(
        (a, b) => (a.seg.firstReceivedTime ?? 0) - (b.seg.firstReceivedTime ?? 0),
      ),
    [segments],
  );

  return (
    <RoomContext.Provider value={room}>
      <RoomInner onDisconnect={onDisconnect} />
      {/* Plays every subscribed audio track (the agent's TTS) — handles tracks already subscribed
          before this mounts + browser autoplay resumption, which the prior manual track.attach()
          missed, leaving the call silent. */}
      <RoomAudioRenderer />

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
            {orderedSegments.map(({ seg, isLocal }) => (
              <li key={seg.id} style={{ color: 'var(--text-2)', display: 'flex', gap: 8 }}>
                <span
                  style={{
                    flexShrink: 0,
                    minWidth: 52,
                    fontSize: 10.5,
                    fontWeight: 700,
                    textTransform: 'uppercase',
                    letterSpacing: '0.04em',
                    color: isLocal ? 'var(--accent)' : 'var(--ok)',
                  }}
                >
                  {isLocal ? 'You' : 'Advisor'}
                </span>
                <span>{seg.text}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </RoomContext.Provider>
  );
}
