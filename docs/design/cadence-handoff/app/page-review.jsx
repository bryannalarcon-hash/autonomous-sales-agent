/* P2 — Call Review (post-call) : transcript + decision trace + belief replay scrubber */
(function () {
  const { useState } = React;
  const { Icon, DB, Ring } = window;

  const css = `
  .rv{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) minmax(340px,416px);grid-template-rows:auto minmax(0,1fr);}
  .rv-strip{grid-column:1 / -1;display:flex;align-items:center;gap:16px;padding:13px 20px;border-bottom:1px solid var(--border);flex-wrap:wrap;}
  .rv-av{width:40px;height:40px;border-radius:11px;background:var(--accent-soft);color:var(--accent-strong);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:14px;flex:0 0 auto;}
  .rv-name{font-family:var(--font-display);font-weight:600;font-size:15.5px;letter-spacing:-0.02em;}
  .rv-co{font-size:12px;color:var(--text-3);}
  .rv-stat{display:flex;flex-direction:column;gap:2px;padding:0 15px;border-left:1px solid var(--border);}
  .rv-stat .l{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text-3);}
  .rv-stat .v{font-size:13.5px;font-weight:650;}
  .rv-left{min-height:0;overflow:auto;padding:18px 20px;display:flex;flex-direction:column;gap:11px;}
  .rv-turn{display:flex;gap:12px;border-radius:13px;padding:11px 13px;border:1px solid transparent;transition:.12s;cursor:pointer;}
  .rv-turn:hover{background:var(--surface-2);}
  .rv-turn.cur{background:var(--surface-2);border-color:var(--accent-border);box-shadow:0 0 0 3px var(--accent-soft);}
  .rv-tnum{flex:0 0 auto;width:26px;height:26px;border-radius:8px;background:var(--surface-2);border:1px solid var(--border);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:var(--text-3);font-variant-numeric:tabular-nums;}
  .rv-turn.a .rv-tnum{background:var(--accent-soft);color:var(--accent-strong);border-color:var(--accent-border);}
  .rv-tc{flex:1;min-width:0;}
  .rv-twho{font-size:11px;font-weight:700;color:var(--text-3);display:flex;gap:7px;align-items:center;}
  .rv-ttext{font-size:13.5px;margin-top:3px;line-height:1.5;}
  .rv-trace{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap;}
  .rv-dchip{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;padding:3px 8px;border-radius:7px;background:var(--accent-soft);color:var(--accent-strong);border:1px solid var(--accent-border);}
  .rv-dchip svg{width:12px;height:12px;}
  .rv-rat{font-size:11.5px;color:var(--text-3);font-style:italic;}
  .rv-lat{font-size:10.5px;color:var(--text-3);font-weight:600;font-variant-numeric:tabular-nums;}
  .rv-right{min-height:0;border-left:1px solid var(--border);display:flex;flex-direction:column;overflow:auto;padding:18px;gap:14px;}
  .rv-replay{display:flex;flex-direction:column;gap:12px;}
  .rv-scrub{display:flex;align-items:center;gap:11px;}
  .rv-track{flex:1;height:7px;border-radius:5px;background:var(--surface-3);position:relative;cursor:pointer;}
  .rv-trackfill{position:absolute;left:0;top:0;bottom:0;border-radius:5px;background:var(--accent-grad);}
  .rv-knob{position:absolute;top:50%;width:15px;height:15px;border-radius:50%;background:#fff;transform:translate(-50%,-50%);box-shadow:0 2px 6px rgba(0,0,0,.4);}
  .rv-mk{position:absolute;top:50%;width:6px;height:6px;border-radius:50%;background:var(--warn);transform:translate(-50%,-50%);box-shadow:0 0 0 2px var(--panel);}
  .rv-gg{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
  .rv-g{background:var(--surface-2);border:1px solid var(--border);border-radius:13px;padding:11px 12px;}
  .rv-gt{font-size:11.5px;color:var(--text-3);font-weight:600;display:flex;justify-content:space-between;}
  .rv-gv{font-family:var(--font-display);font-size:22px;font-weight:600;letter-spacing:-0.03em;font-variant-numeric:tabular-nums;margin-top:2px;}
  .rv-slot{display:grid;grid-template-columns:78px 1fr 32px;align-items:center;gap:10px;font-size:12px;margin-bottom:7px;}
  .rv-slot .n{color:var(--text-2);font-weight:600;}
  .rv-slot .v{text-align:right;font-weight:650;font-size:11.5px;font-variant-numeric:tabular-nums;}
  .rv-outcome{display:flex;align-items:center;gap:12px;padding:13px;border-radius:13px;background:var(--ok-soft);border:1px solid var(--ok-border);}
  `;

  function Review({ params }) {
    const call = (params && params.call) || 'CALL-4820';
    const T = DB.TRANSCRIPT;
    const [cur, setCur] = useState(T.length - 1);
    const aTurns = T.map((t, i) => ({ ...t, i })).filter(t => t.who === 'a');
    const active = T[cur];
    // nearest belief snapshot up to cur
    let snap = null;
    for (let i = cur; i >= 0; i--) { if (T[i].trust != null) { snap = T[i]; break; } }
    snap = snap || { trust: .42, bail: .28, stage: 'Discovery' };

    const SLOTS = [{ n: 'Budget', v: .58 }, { n: 'Authority', v: .82 }, { n: 'Need', v: .9 }, { n: 'Timeline', v: .55 }];

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('style', null, css),
        React.createElement('div', { className: 'rv' },
          /* outcome strip */
          React.createElement('div', { className: 'rv-strip' },
            React.createElement('div', { className: 'rv-av' }, 'PN'),
            React.createElement('div', null,
              React.createElement('div', { className: 'rv-name' }, 'Priya Nair'),
              React.createElement('div', { className: 'rv-co' }, 'Cedar & Co · Busy Decider')),
            React.createElement('div', { className: 'rv-stat' }, React.createElement('div', { className: 'l' }, 'Outcome'), React.createElement('div', { className: 'v', style: { color: 'var(--ok)' } }, 'Enrolled · T3')),
            React.createElement('div', { className: 'rv-stat' }, React.createElement('div', { className: 'l' }, 'Duration'), React.createElement('div', { className: 'v' }, '5:48')),
            React.createElement('div', { className: 'rv-stat' }, React.createElement('div', { className: 'l' }, 'Turns'), React.createElement('div', { className: 'v' }, '18')),
            React.createElement('div', { className: 'rv-stat' }, React.createElement('div', { className: 'l' }, 'Version'), React.createElement('div', { className: 'v' }, 'v12 · kb-37')),
            React.createElement('div', { style: { marginLeft: 'auto', display: 'flex', gap: 9 } },
              React.createElement('button', { className: 'btn btn-ghost btn-sm' }, React.createElement(Icon, { d: 'flag', size: 14 }), 'Flag'),
              React.createElement('button', { className: 'btn btn-ghost btn-sm' }, React.createElement(Icon, { d: 'download', size: 14 }), 'Export'),
              React.createElement('button', { className: 'btn btn-ghost btn-sm', onClick: () => window.cadenceGo('versions') }, React.createElement(Icon, { d: 'flask', size: 14 }), 'Use in experiment'))),

          /* transcript + trace */
          React.createElement('div', { className: 'rv-left scroll' },
            T.map((t, i) =>
              React.createElement('div', { key: i, className: 'rv-turn ' + t.who + (i === cur ? ' cur' : ''), onClick: () => setCur(i) },
                React.createElement('div', { className: 'rv-tnum' }, i + 1),
                React.createElement('div', { className: 'rv-tc' },
                  React.createElement('div', { className: 'rv-twho' }, t.who === 'a' ? 'Ava (agent)' : 'Priya', '·', t.t, t.stage ? React.createElement('span', { className: 'tag', style: { padding: '1px 7px' } }, t.stage) : null),
                  React.createElement('div', { className: 'rv-ttext' }, t.text),
                  t.dec && React.createElement('div', { className: 'rv-trace' },
                    React.createElement('span', { className: 'rv-dchip' }, React.createElement(Icon, { d: 'spark', size: 12 }), t.dec),
                    React.createElement('span', { className: 'rv-rat' }, '“' + t.rat + '”'),
                    React.createElement('span', { className: 'rv-lat' }, t.lat))))),

          /* belief replay */
          React.createElement('div', { className: 'rv-right scroll' },
            React.createElement('div', { className: 'card card-pad rv-replay' },
              React.createElement('div', { className: 'row', style: { justifyContent: 'space-between' } },
                React.createElement('h3', { style: { fontSize: 14 } }, 'Belief replay'),
                React.createElement('span', { className: 'tag accent' }, 'Turn ' + (cur + 1) + ' · ' + active.t)),
              React.createElement('div', { className: 'rv-scrub' },
                React.createElement(Icon, { d: 'play', size: 16, style: { color: 'var(--accent)' } }),
                React.createElement('div', { className: 'rv-track', onClick: (e) => { const r = e.currentTarget.getBoundingClientRect(); setCur(Math.round(((e.clientX - r.left) / r.width) * (T.length - 1))); } },
                  React.createElement('div', { className: 'rv-trackfill', style: { width: (cur / (T.length - 1)) * 100 + '%' } }),
                  aTurns.filter(t => t.dec && /pivot|close|de-risk|reframe/i.test(t.dec)).map(t => React.createElement('div', { key: t.i, className: 'rv-mk', style: { left: (t.i / (T.length - 1)) * 100 + '%' }, title: t.dec })),
                  React.createElement('div', { className: 'rv-knob', style: { left: (cur / (T.length - 1)) * 100 + '%' } })),
                React.createElement('span', { className: 'mono', style: { fontSize: 11, color: 'var(--text-3)' } }, active.t)),
              React.createElement('div', { className: 'faint', style: { fontSize: 11 } }, 'Drag the scrubber or click a turn — belief state below reflects that moment.')),

            React.createElement('div', { className: 'card card-pad' },
              React.createElement('div', { className: 'rv-gg' },
                React.createElement('div', { className: 'rv-g' },
                  React.createElement('div', { className: 'rv-gt' }, React.createElement('span', null, 'Trust')),
                  React.createElement('div', { className: 'rv-gv' }, snap.trust.toFixed(2)),
                  React.createElement('div', { className: 'bar', style: { marginTop: 8 } }, React.createElement('i', { style: { width: snap.trust * 100 + '%', background: 'var(--ok)' } }))),
                React.createElement('div', { className: 'rv-g' },
                  React.createElement('div', { className: 'rv-gt' }, React.createElement('span', null, 'Bail risk')),
                  React.createElement('div', { className: 'rv-gv' }, snap.bail.toFixed(2)),
                  React.createElement('div', { className: 'bar', style: { marginTop: 8 } }, React.createElement('i', { style: { width: snap.bail * 100 + '%', background: 'var(--warn)' } })))),
              React.createElement('div', { className: 'row', style: { margin: '14px 0 12px', gap: 10 } },
                React.createElement('span', { className: 'tag accent' }, 'Stage · ' + snap.stage),
                active.dec ? React.createElement('span', { className: 'tag violet' }, active.dec) : null),
              SLOTS.map((s, i) =>
                React.createElement('div', { className: 'rv-slot', key: i },
                  React.createElement('span', { className: 'n' }, s.n),
                  React.createElement('span', { className: 'bar' }, React.createElement('i', { style: { width: s.v * 100 + '%', background: s.v < 0.6 ? 'var(--warn)' : 'var(--accent)' } })),
                  React.createElement('span', { className: 'v' }, s.v.toFixed(2))
                )
              )
            ),

            React.createElement('div', { className: 'rv-outcome' },
              React.createElement(Ring, { value: 0.93, size: 46, color: 'var(--ok)', label: '✓' }),
              React.createElement('div', null,
                React.createElement('div', { className: 'b', style: { fontSize: 13.5 } }, 'Qualified correctly'),
                React.createElement('div', { className: 'muted', style: { fontSize: 12 } }, 'Reached T3 same-call enroll · pilot booked')))))
        )
      )
    );
  }

  (window.CadencePages = window.CadencePages || {}).review = Review;
})();
