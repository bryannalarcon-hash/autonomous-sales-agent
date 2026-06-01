/* P1 — Live Call Monitor */
(function () {
  const { useState, useEffect, useRef } = React;
  const { Icon, DB } = window;

  const css = `
  .lv{flex:1;min-height:0;display:flex;flex-direction:column;}
  .lv-body{flex:1;min-height:0;display:grid;grid-template-columns:1fr 388px;grid-template-rows:minmax(0,1fr);gap:16px;padding:18px 20px;}
  .lv-col{display:flex;flex-direction:column;gap:14px;min-height:0;}
  .lv-pros{padding:15px 17px;display:flex;align-items:center;gap:14px;}
  .lv-pav{width:46px;height:46px;border-radius:13px;background:var(--accent-soft);color:var(--accent-strong);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px;flex:0 0 auto;}
  .lv-pname{font-family:var(--font-display);font-weight:600;font-size:16px;letter-spacing:-0.02em;}
  .lv-pco{color:var(--text-3);font-size:12.5px;margin-top:1px;}
  .lv-pright{margin-left:auto;text-align:right;align-self:flex-start;}
  .lv-dur{font-family:var(--font-display);font-size:22px;font-weight:600;font-variant-numeric:tabular-nums;letter-spacing:-0.02em;}
  .lv-vtag{font-size:11px;color:var(--text-3);margin-top:2px;}
  .lv-tcard{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;position:relative;}
  .lv-stream{flex:1;min-height:0;overflow:hidden;padding:16px 18px 20px;display:flex;flex-direction:column;gap:15px;justify-content:flex-end;}
  .lv-turn{display:flex;flex-direction:column;gap:5px;max-width:88%;}
  .lv-turn.p{align-self:flex-start;}
  .lv-turn.a{align-self:flex-end;align-items:flex-end;max-width:92%;}
  .lv-th{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--text-3);font-weight:600;}
  .lv-bub{padding:10px 13px;border-radius:14px;font-size:13.5px;line-height:1.5;}
  .lv-turn.p .lv-bub{background:var(--surface-2);border:1px solid var(--border);border-top-left-radius:5px;color:var(--text);}
  .lv-turn.a .lv-bub{background:var(--accent-grad);color:var(--accent-ink);border-top-right-radius:5px;font-weight:500;}
  .lv-dec{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end;}
  .lv-dchip{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;padding:3px 8px;border-radius:7px;background:var(--accent-soft);color:var(--accent-strong);border:1px solid var(--accent-border);}
  .lv-dchip svg{width:12px;height:12px;}
  .lv-rat{font-size:11.5px;color:var(--text-3);font-style:italic;text-align:right;}
  .lv-lat{font-size:10.5px;color:var(--text-3);font-variant-numeric:tabular-nums;font-weight:600;}
  .lv-speak{display:inline-flex;gap:4px;padding:11px 15px;}
  .lv-speak i{width:7px;height:7px;border-radius:50%;background:var(--accent-ink);opacity:.7;animation:lvd 1.2s infinite;}
  .lv-speak i:nth-child(2){animation-delay:.2s;}.lv-speak i:nth-child(3){animation-delay:.4s;}
  @keyframes lvd{0%,60%,100%{transform:translateY(0);opacity:.4;}30%{transform:translateY(-4px);opacity:1;}}
  .lv-fade{position:absolute;left:1px;right:1px;top:49px;height:40px;background:linear-gradient(to bottom,rgba(34,36,78,.85),transparent);pointer-events:none;}
  .lv-belief{flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden;}
  .lv-bb{padding:13px 16px;display:flex;flex-direction:column;gap:10px;overflow:hidden;}
  .lv-gg{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
  .lv-g{background:var(--surface-2);border:1px solid var(--border);border-radius:13px;padding:10px 12px;}
  .lv-gt{display:flex;align-items:center;justify-content:space-between;font-size:11.5px;color:var(--text-3);font-weight:600;}
  .lv-gv{font-family:var(--font-display);font-size:23px;font-weight:600;letter-spacing:-0.03em;font-variant-numeric:tabular-nums;margin-top:2px;}
  .lv-box{background:var(--surface-2);border:1px solid var(--border);border-radius:13px;padding:10px 13px;display:flex;gap:14px;}
  .lv-box .l{font-size:10.5px;color:var(--text-3);font-weight:650;text-transform:uppercase;letter-spacing:0.06em;}
  .lv-box .v{font-size:13px;font-weight:600;margin-top:3px;}
  .lv-escal{display:flex;align-items:center;gap:11px;padding:10px 13px;border-radius:13px;background:var(--warn-soft);border:1px solid var(--warn-border);}
  .lv-escal .i{width:30px;height:30px;border-radius:9px;background:var(--warn);display:flex;align-items:center;justify-content:center;flex:0 0 auto;color:var(--warn-ink);}
  .lv-dcard{border-radius:13px;padding:12px 13px;background:var(--accent-strong-bg);border:1px solid var(--accent-border);}
  .lv-dcard .l{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--accent-strong);}
  .lv-dcard .big{font-family:var(--font-display);font-size:16px;font-weight:600;letter-spacing:-0.02em;margin-top:4px;color:var(--accent-strong);display:flex;align-items:center;gap:8px;}
  .lv-dcard .r{font-size:12px;color:var(--text-2);margin-top:6px;line-height:1.45;}
  .lv-conf{display:inline-flex;align-items:center;gap:7px;margin-top:9px;font-size:11px;color:var(--text-3);font-weight:600;}
  .lv-exp{border-top:1px solid var(--border);padding:11px 16px 13px;display:flex;flex-direction:column;gap:9px;flex:1;min-height:0;overflow:hidden;}
  .lv-eh{display:flex;align-items:center;gap:8px;font-size:11px;font-weight:700;color:var(--text-2);text-transform:uppercase;letter-spacing:0.05em;}
  .lv-slot{display:grid;grid-template-columns:84px 1fr 34px;align-items:center;gap:10px;font-size:12px;}
  .lv-slot .n{color:var(--text-2);font-weight:600;}
  .lv-slot .v{text-align:right;font-variant-numeric:tabular-nums;font-weight:650;font-size:11.5px;}
  .lv-drivers{display:flex;flex-wrap:wrap;gap:6px;}
  .lv-driver{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:4px 8px;border-radius:7px;background:var(--surface-2);border:1px solid var(--border);color:var(--text-2);}
  .lv-driver .ar.up{color:var(--danger);}.lv-driver .ar.dn{color:var(--ok);}.lv-driver .ar.fl{color:var(--text-3);}
  .lv-actions{flex:0 0 auto;height:66px;border-top:1px solid var(--border);background:var(--panel-2);display:flex;align-items:center;gap:18px;padding:0 22px;}
  .lv-rec{display:inline-flex;align-items:center;gap:8px;font-size:12.5px;color:var(--text-2);font-weight:550;}
  .lv-rec .rd{width:9px;height:9px;border-radius:50%;background:var(--danger);animation:blink 1.4s infinite;}
  .lv-tl{display:flex;align-items:center;gap:10px;min-width:280px;}
  .lv-tlbar{flex:1;height:9px;border-radius:5px;overflow:hidden;display:flex;background:var(--surface-3);}
  .lv-tlbar .ag{background:var(--accent);}.lv-tlbar .pr{background:var(--ok);}
  `;

  const SLOTS = [{ n: 'Budget', v: .58 }, { n: 'Authority', v: .82 }, { n: 'Need', v: .9 }, { n: 'Timeline', v: .55 }, { n: 'Team size', v: .95 }];
  const DRIVERS = [{ n: 'Price sensitivity', ar: 'up' }, { n: 'Skepticism', ar: 'dn', note: 'easing' }, { n: 'Urgency', ar: 'fl' }, { n: 'Rapport', ar: 'up' }];

  function Live() {
    const turns = DB.TRANSCRIPT.slice(0, 8);
    const callMeta = '#CALL-4821 · 04:12';
    return (
      React.createElement('div', { className: 'lv' },
        React.createElement('style', null, css),
        React.createElement('div', { className: 'lv-body' },
          /* left */
          React.createElement('div', { className: 'lv-col' },
            React.createElement('div', { className: 'card lv-pros' },
              React.createElement('div', { className: 'lv-pav' }, 'JA'),
              React.createElement('div', null,
                React.createElement('div', { className: 'lv-pname' }, 'Jordan Avery'),
                React.createElement('div', { className: 'lv-pco' }, 'Northwind Logistics'),
                React.createElement('div', { className: 'row gap6', style: { marginTop: 8 } },
                  React.createElement('span', { className: 'tag accent' }, 'Skeptical Analyzer'),
                  React.createElement('span', { className: 'tag' }, 'Inbound · SMB'),
                  React.createElement('span', { className: 'tag' }, 'Web-voice'))),
              React.createElement('div', { className: 'lv-pright' },
                React.createElement('div', { className: 'lv-dur' }, '04:12'),
                React.createElement('div', { className: 'lv-vtag' }, 'v12 · kb-37'))),

            React.createElement('div', { className: 'card lv-tcard' },
              React.createElement('div', { className: 'card-head' },
                React.createElement('h3', null, 'Transcript'),
                React.createElement('span', { className: 'row gap8', style: { marginLeft: 'auto', fontSize: 11.5, color: 'var(--text-3)', fontWeight: 550 } },
                  'Auto-scroll', React.createElement('span', { className: 'toggle on' }, React.createElement('i', null)))),
              React.createElement('div', { className: 'lv-stream' },
                turns.map((t, i) =>
                  React.createElement('div', { className: 'lv-turn ' + t.who, key: i },
                    React.createElement('div', { className: 'lv-th' }, t.who === 'a' ? 'Ava (agent)' : 'Jordan', '·', t.t),
                    React.createElement('div', { className: 'lv-bub' }, t.text),
                    t.dec && React.createElement('div', { className: 'lv-dec' },
                      React.createElement('span', { className: 'lv-dchip' }, React.createElement(Icon, { d: 'spark', size: 12 }), t.dec),
                      React.createElement('span', { className: 'lv-lat' }, t.lat)),
                    t.rat && React.createElement('div', { className: 'lv-rat' }, '“' + t.rat + '”'))),
                React.createElement('div', { className: 'lv-turn a' },
                  React.createElement('div', { className: 'lv-th' }, 'Ava (agent)', '·', 'now'),
                  React.createElement('div', { className: 'lv-bub lv-speak' }, React.createElement('i', null), React.createElement('i', null), React.createElement('i', null)),
                  React.createElement('div', { className: 'lv-dec' },
                    React.createElement('span', { className: 'lv-dchip' }, React.createElement(Icon, { d: 'pivot', size: 12 }), 'Trial-close · pilot')))),
              React.createElement('div', { className: 'lv-fade' }))),

          /* right — belief */
          React.createElement('div', { className: 'lv-col' },
            React.createElement('div', { className: 'card lv-belief' },
              React.createElement('div', { className: 'card-head' },
                React.createElement(Icon, { d: 'spark', size: 16, style: { color: 'var(--accent)' } }),
                React.createElement('h3', null, 'Belief State'),
                React.createElement('span', { className: 'tag', style: { marginLeft: 'auto' } }, 'Closing')),
              React.createElement('div', { className: 'lv-bb' },
                React.createElement('div', { className: 'lv-gg' },
                  React.createElement('div', { className: 'lv-g' },
                    React.createElement('div', { className: 'lv-gt' }, 'Trust', React.createElement('span', { style: { color: 'var(--ok)', fontWeight: 700 } }, '↑ +.08')),
                    React.createElement('div', { className: 'lv-gv' }, '0.66'),
                    React.createElement('div', { className: 'bar', style: { marginTop: 8 } }, React.createElement('i', { style: { width: '66%', background: 'var(--ok)' } }))),
                  React.createElement('div', { className: 'lv-g' },
                    React.createElement('div', { className: 'lv-gt' }, 'Bail risk', React.createElement('span', { style: { color: 'var(--ok)', fontWeight: 700 } }, '↓ −.07')),
                    React.createElement('div', { className: 'lv-gv' }, '0.34'),
                    React.createElement('div', { className: 'bar', style: { marginTop: 8 } }, React.createElement('i', { style: { width: '34%', background: 'var(--warn)' } })))),
                React.createElement('div', { className: 'lv-box' },
                  React.createElement('div', null, React.createElement('div', { className: 'l' }, 'Stage'), React.createElement('div', { className: 'v' }, 'Closing')),
                  React.createElement('div', { style: { borderLeft: '1px solid var(--border)', paddingLeft: 14 } }, React.createElement('div', { className: 'l' }, 'Last act'), React.createElement('div', { className: 'v' }, 'Trial-close'))),
                React.createElement('div', { className: 'lv-escal' },
                  React.createElement('div', { className: 'i' }, React.createElement(Icon, { d: 'alert', size: 17 })),
                  React.createElement('div', null,
                    React.createElement('div', { style: { fontSize: 12.5, fontWeight: 700, color: 'var(--warn-strong)' } }, 'Escalation · Armed'),
                    React.createElement('div', { style: { fontSize: 11, color: 'var(--warn-strong)', opacity: .8 } }, 'Watching concession pressure'))),
                React.createElement('div', { className: 'lv-dcard' },
                  React.createElement('div', { className: 'l' }, 'Current decision'),
                  React.createElement('div', { className: 'big' }, React.createElement(Icon, { d: 'pivot', size: 16 }), 'Trial-close → pilot'),
                  React.createElement('div', { className: 'r' }, 'Buying signal detected (“how fast can we start”); trust recovering — move to low-commitment close.'),
                  React.createElement('div', { className: 'lv-conf' }, 'Confidence',
                    React.createElement('span', { className: 'bar', style: { width: 54 } }, React.createElement('i', { style: { width: '83%' } })), '0.83'))),
              React.createElement('div', { className: 'lv-exp' },
                React.createElement('div', { className: 'lv-eh' }, 'Full belief state', React.createElement(Icon, { d: 'chevDown', size: 14, style: { marginLeft: 'auto' } })),
                SLOTS.map((s, i) =>
                  React.createElement('div', { className: 'lv-slot', key: i },
                    React.createElement('span', { className: 'n' }, s.n),
                    React.createElement('span', { className: 'bar' }, React.createElement('i', { style: { width: (s.v * 100) + '%', background: s.v < 0.5 ? 'var(--warn)' : 'var(--accent)' } })),
                    React.createElement('span', { className: 'v' }, s.v.toFixed(2)))),
                React.createElement('div', { className: 'lv-drivers' },
                  DRIVERS.map((d, i) =>
                    React.createElement('span', { className: 'lv-driver', key: i }, d.n,
                      React.createElement('span', { className: 'ar ' + d.ar }, d.ar === 'up' ? '↑' : d.ar === 'dn' ? '↓' : '→'),
                      d.note ? React.createElement('span', { className: 'faint', style: { fontWeight: 500 } }, d.note) : null)
                  )
                )
              )
            )
          )
        ),

        React.createElement('div', { className: 'lv-actions' },
          React.createElement('span', { className: 'lv-rec' }, React.createElement('span', { className: 'rd' }), 'Recording · Turn 14 · synced 0.8s ago'),
          React.createElement('div', { className: 'lv-tl' },
            React.createElement('span', { style: { fontSize: 11, fontWeight: 650, color: 'var(--text-3)' } }, 'Talk / Listen'),
            React.createElement('div', { className: 'lv-tlbar' }, React.createElement('div', { className: 'ag', style: { width: '44%' } }), React.createElement('div', { className: 'pr', style: { width: '56%' } })),
            React.createElement('span', { style: { fontSize: 11, fontWeight: 650, color: 'var(--text-3)' } }, '44 / 56')),
          React.createElement('div', { style: { marginLeft: 'auto', display: 'flex', gap: 10 } },
            React.createElement('button', { className: 'btn btn-ghost', onClick: () => window.cadenceGo('review', { call: 'CALL-4821' }) }, React.createElement(Icon, { d: 'eye', size: 16 }), 'Open review'),
            React.createElement('button', { className: 'btn btn-primary btn-lg' }, React.createElement(Icon, { d: 'hand', size: 17 }), 'Take over')))
      )
    );
  }

  (window.CadencePages = window.CadencePages || {}).live = Live;
})();
