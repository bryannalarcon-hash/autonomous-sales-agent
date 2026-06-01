// Sparkline + ring-gauge helpers ported from the design handoff (ui.jsx Spark / Ring). Spark draws a
// gradient area + line over a small data series (KPI tiles); Ring is a circular progress gauge
// (Call Review outcome). The prototype seeded its gradient id with Math.random() — that would break
// SSR hydration in Next, so we use React.useId() for a stable, unique gradient id instead.
'use client';

import { useId } from 'react';

export function Spark({
  data,
  w = 78,
  h = 30,
  color = 'var(--accent)',
  fill = true,
}: {
  data: number[];
  w?: number;
  h?: number;
  color?: string;
  fill?: boolean;
}) {
  const gid = useId().replace(/[^a-zA-Z0-9_-]/g, '');
  if (!data || data.length === 0) return <svg width={w} height={h} />;
  const max = Math.max(...data);
  const min = Math.min(...data);
  const rng = max - min || 1;
  const denom = data.length - 1 || 1;
  const pts = data.map((v, i) => [(i / denom) * w, h - 3 - ((v - min) / rng) * (h - 6)] as const);
  const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
  const area = `${line} L${w} ${h} L0 ${h} Z`;
  return (
    <svg width={w} height={h} style={{ display: 'block', overflow: 'visible' }}>
      <defs>
        <linearGradient id={gid} x1={0} y1={0} x2={0} y2={1}>
          <stop offset="0%" stopColor={color} stopOpacity={0.28} />
          <stop offset="100%" stopColor={color} stopOpacity={0} />
        </linearGradient>
      </defs>
      {fill && <path d={area} fill={`url(#${gid})`} />}
      <path d={line} fill="none" stroke={color} strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Ring({
  value,
  size = 54,
  stroke = 6,
  color = 'var(--accent)',
  label,
}: {
  value: number;
  size?: number;
  stroke?: number;
  color?: string;
  label?: string;
}) {
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  return (
    <div style={{ position: 'relative', width: size, height: size, flex: '0 0 auto' }}>
      <svg width={size} height={size} style={{ transform: 'rotate(-90deg)' }}>
        <circle cx={size / 2} cy={size / 2} r={r} fill="none" stroke="var(--surface-3)" strokeWidth={stroke} />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={r}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={c}
          strokeDashoffset={c * (1 - value)}
        />
      </svg>
      <div
        style={{
          position: 'absolute',
          inset: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: '13px',
          fontWeight: 700,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {label != null ? label : `${Math.round(value * 100)}%`}
      </div>
    </div>
  );
}
