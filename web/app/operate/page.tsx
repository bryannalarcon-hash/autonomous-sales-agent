// /operate → redirect to the live monitor (U15). The handoff's default Operate landing is the Live
// Call Monitor (falls back to the most-recent call when none is active); the per-screen pages live
// under /operate/{live,calls,kpi,escalations,review}. This bare index just forwards there so the
// nav/route map has a clean entry point.
import { redirect } from 'next/navigation';

export default function OperateIndex() {
  redirect('/operate/live');
}
