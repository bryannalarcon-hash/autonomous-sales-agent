// VoiceRoom — the LiveKit room internals, rendered ONLY after a token is fetched and a Room is
// connected by the parent (VoiceCall). Uses @livekit/components-react's RoomContext so hooks like
// useConnectionState / useTracks work, and renders the live transcript from the agent's published
// transcription segments. Exposes mute + disconnect controls. This component assumes a connected
// Room is passed in; it never fetches tokens or holds secrets (the parent does that).
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
    <div className="space-y-3">
      <div className="flex items-center gap-2 text-sm">
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            connectionState === 'connected' ? 'bg-green-500' : 'bg-amber-500'
          }`}
        />
        <span className="capitalize text-neutral-600">{connectionState}</span>
      </div>

      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => void toggleMute()}
          className="rounded-md border border-neutral-300 px-3 py-1.5 text-sm font-medium text-neutral-700 hover:bg-neutral-100"
        >
          {muted ? 'Unmute' : 'Mute'}
        </button>
        <button
          type="button"
          onClick={onDisconnect}
          className="rounded-md bg-red-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-red-500"
        >
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

      <div className="mt-4 rounded-md border border-neutral-200 bg-neutral-50 p-3">
        <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-500">
          Live transcript
        </h3>
        {orderedSegments.length === 0 ? (
          <p className="text-sm text-neutral-400">Transcript will appear here as you talk.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {orderedSegments.map((seg) => (
              <li key={seg.id} className="text-neutral-700">
                {seg.text}
              </li>
            ))}
          </ul>
        )}
      </div>
    </RoomContext.Provider>
  );
}
