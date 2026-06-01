/* CADENCE — shared UI: icon set, small components, and mock data.
   Exposes to window: Icon, Spark, Gauge, mock data tables. */
(function () {
  const { useState, useEffect, useRef } = React;

  /* ---------------------------- ICONS ---------------------------- */
  const PATHS = {
    pulse: 'M3 12h3.5l2-7 4 14 2.5-9 1.5 4H21',
    broadcast: 'M4.9 4.9a10 10 0 000 14.2M19.1 4.9a10 10 0 010 14.2M7.8 7.8a6 6 0 000 8.4M16.2 7.8a6 6 0 010 8.4M12 11a1 1 0 100 2 1 1 0 000-2z',
    list: 'M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01',
    chart: 'M3 3v18h18M7 15l3-4 3 2 4-6',
    alert: 'M12 3l9 16H3l9-16zM12 10v4M12 17h.01',
    flask: 'M9 3h6M10 3v6l-5 9.5A1.5 1.5 0 006.3 21h11.4a1.5 1.5 0 001.3-2.5L14 9V3M7.5 14h9',
    badge: 'M9 12l2 2 4-4M12 3l2.4 1.7 2.9-.3 1 2.8 2.5 1.5-.9 2.8.9 2.8-2.5 1.5-1 2.8-2.9-.3L12 21l-2.4-1.7-2.9.3-1-2.8L3.2 15l.9-2.8L3.2 9.4l2.5-1.5 1-2.8 2.9.3z',
    book: 'M4 5a2 2 0 012-2h13v15H6a2 2 0 00-2 2V5zM4 18a2 2 0 012-2h13',
    branch: 'M6 4v9M6 20a2 2 0 100-4 2 2 0 000 4zM6 9a2 2 0 100-4 2 2 0 000 4zM18 7a2 2 0 100-4 2 2 0 000 4zM18 7a8 8 0 01-8 8',
    mic: 'M12 3a3 3 0 00-3 3v6a3 3 0 006 0V6a3 3 0 00-3-3zM5 11a7 7 0 0014 0M12 18v3',
    spark: 'M12 3v3M12 18v3M4.2 7.5l2.1 1.2M17.7 15.3l2.1 1.2M4.2 16.5l2.1-1.2M17.7 8.7l2.1-1.2M12 8a4 4 0 100 8 4 4 0 000-8z',
    pivot: 'M3.5 12a8.5 8.5 0 0114.6-6M20.5 12a8.5 8.5 0 01-14.6 6M18 3v3.5h-3.5M6 21v-3.5h3.5',
    hand: 'M8 11V5.6a1.4 1.4 0 012.8 0V11m0-1.2V4.4a1.4 1.4 0 012.8 0V11m0-.6V6a1.4 1.4 0 012.8 0v7a6 6 0 01-6 6h-1a4 4 0 01-2.9-1.2l-3-3.2a1.5 1.5 0 012.2-2.1L8 13.4V8',
    chevron: 'M9 6l6 6-6 6',
    chevDown: 'M6 9l6 6 6-6',
    chevUp: 'M6 15l6-6 6 6',
    shield: 'M12 3l8 3v6c0 4.4-3.2 7.6-8 9-4.8-1.4-8-4.6-8-9V6l8-3z',
    search: 'M11 4a7 7 0 105.6 11.2L21 19M11 4a7 7 0 014.9 12',
    filter: 'M3 5h18l-7 8v6l-4-2v-4L3 5z',
    arrowR: 'M5 12h14M13 6l6 6-6 6',
    arrowUR: 'M7 17L17 7M9 7h8v8',
    check: 'M5 12l5 5L20 6',
    x: 'M6 6l12 12M18 6L6 18',
    clock: 'M12 7v5l3.5 2M12 21a9 9 0 100-18 9 9 0 000 18z',
    user: 'M12 12a4 4 0 100-8 4 4 0 000 8zM5 20a7 7 0 0114 0',
    play: 'M7 5l12 7-12 7V5z',
    scrub: 'M3 12h18M7 8v8M12 6v12M17 9v6',
    flag: 'M5 21V4M5 4h12l-2 4 2 4H5',
    diff: 'M12 3v18M5 8H3m0 8h2m14-8h2m-2 8h2M8 6L5 8l3 2M16 14l3 2-3 2',
    promote: 'M12 19V6M6 12l6-6 6 6',
    rollback: 'M3 12a9 9 0 109-9 9 9 0 00-6.4 2.6L3 8M3 3v5h5',
    plus: 'M12 5v14M5 12h14',
    edit: 'M4 20h4L19 9l-4-4L4 16v4zM14 6l4 4',
    save: 'M5 4h11l3 3v13H5V4zM8 4v5h7V4M8 20v-6h8v6',
    sliders: 'M4 8h10M18 8h2M4 16h2M10 16h10M14 6v4M6 14v4',
    target: 'M12 12m-2 0a2 2 0 104 0 2 2 0 10-4 0M12 3v3M12 18v3M3 12h3M18 12h3M12 7a5 5 0 100 10 5 5 0 000-10z',
    layers: 'M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5M3 17l9 5 9-5',
    clipboard: 'M9 4h6v3H9zM9 5H6v15h12V5h-3M9 12h6M9 16h4',
    bolt: 'M13 3L4 14h6l-1 7 9-11h-6l1-7z',
    eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7zM12 15a3 3 0 100-6 3 3 0 000 6z',
    dots: 'M5 12h.01M12 12h.01M19 12h.01',
    phone: 'M5 4h4l2 5-3 2a12 12 0 005 5l2-3 5 2v4a2 2 0 01-2 2A16 16 0 013 6a2 2 0 012-2z',
    grid: 'M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z',
    download: 'M12 4v10m-4-4l4 4 4-4M5 20h14',
    sigma: 'M6 5h12l-7 7 7 7H6v-2l6-5-6-5V5z',
    gauge: 'M12 14a2 2 0 100-4 2 2 0 000 4zM12 14l4-4M5.6 18a9 9 0 1112.8 0',
    seedling: 'M12 21V9m0 4C12 8 8 6 4 7c0 5 4 6 8 6zm0-1c0-4 4-6 8-5 0 4-4 6-8 6z',
    note: 'M5 4h14v10l-5 5H5V4zM14 19v-5h5',
  };
  function Icon({ d, size, sw, style, className }) {
    return React.createElement('svg', {
      viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor',
      strokeWidth: sw || 1.7, strokeLinecap: 'round', strokeLinejoin: 'round',
      width: size || 18, height: size || 18, style, className,
    }, React.createElement('path', { d: PATHS[d] || '' }));
  }

  /* ---------------------------- SPARKLINE ---------------------------- */
  function Spark({ data, w = 78, h = 30, color = 'var(--accent)', fill = true }) {
    const max = Math.max(...data), min = Math.min(...data), rng = max - min || 1;
    const pts = data.map((v, i) => [(i / (data.length - 1)) * w, h - 3 - ((v - min) / rng) * (h - 6)]);
    const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const area = line + ` L${w} ${h} L0 ${h} Z`;
    const gid = 'sg' + Math.random().toString(36).slice(2, 7);
    return React.createElement('svg', { width: w, height: h, style: { display: 'block', overflow: 'visible' } },
      React.createElement('defs', null,
        React.createElement('linearGradient', { id: gid, x1: 0, y1: 0, x2: 0, y2: 1 },
          React.createElement('stop', { offset: '0%', stopColor: color, stopOpacity: .28 }),
          React.createElement('stop', { offset: '100%', stopColor: color, stopOpacity: 0 }))),
      fill && React.createElement('path', { d: area, fill: `url(#${gid})` }),
      React.createElement('path', { d: line, fill: 'none', stroke: color, strokeWidth: 1.8, strokeLinecap: 'round', strokeLinejoin: 'round' }));
  }

  /* ---------------------------- RING GAUGE ---------------------------- */
  function Ring({ value, size = 54, stroke = 6, color = 'var(--accent)', label }) {
    const r = (size - stroke) / 2, c = 2 * Math.PI * r;
    return React.createElement('div', { style: { position: 'relative', width: size, height: size, flex: '0 0 auto' } },
      React.createElement('svg', { width: size, height: size, style: { transform: 'rotate(-90deg)' } },
        React.createElement('circle', { cx: size / 2, cy: size / 2, r, fill: 'none', stroke: 'var(--surface-3)', strokeWidth: stroke }),
        React.createElement('circle', { cx: size / 2, cy: size / 2, r, fill: 'none', stroke: color, strokeWidth: stroke, strokeLinecap: 'round', strokeDasharray: c, strokeDashoffset: c * (1 - value) })),
      React.createElement('div', { style: { position: 'absolute', inset: 0, display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '13px', fontWeight: 700, fontVariantNumeric: 'tabular-nums' } }, label != null ? label : Math.round(value * 100) + '%'));
  }

  Object.assign(window, { Icon, Spark, Ring, CIcons: PATHS });
})();
