/* P4 — KPI Views : single-version dashboard + compare mode */
(function () {
  const { useState } = React;
  const { Icon, DB, Spark } = window;
  const K = DB.KPI;

  function BarRow({ t, v, max, color, suffix }) {
    return React.createElement('div', { style: { marginBottom: 11 } },
      React.createElement('div', { className: 'row', style: { justifyContent: 'space-between', marginBottom: 5 } },
        React.createElement('span', { style: { fontSize: 12.5, color: 'var(--text-2)', fontWeight: 550 } }, t),
        React.createElement('span', { className: 'mono', style: { fontSize: 12, fontWeight: 650 } }, suffix === '%' ? Math.round(v * 100) + '%' : v)),
      React.createElement('div', { className: 'bar', style: { height: 8 } }, React.createElement('i', { style: { width: ((suffix === '%' ? v : v / max) * 100) + '%', background: color || 'var(--accent)' } })));
  }

  function Compare() {
    const rows = [
      ['Weighted-ladder score', '3.05', '3.42', '+0.37', 'up'],
      ['Same-call enrollment', '52%', '61%', '+9 pts', 'up'],
      ['Qualification accuracy', '89%', '93%', '+4 pts', 'up'],
      ['Objection recovery', '61%', '68%', '+7 pts', 'up'],
      ['Escalation rate', '5.8%', '4.2%', '−1.6 pts', 'up'],
      ['Avg duration', '5:44', '5:21', '−23s', 'up'],
      ['Pushiness (guardrail)', '0.19', '0.21', '+0.02', 'flat'],
    ];
    return React.createElement('div', { className: 'card', style: { padding: '6px 8px' } },
      React.createElement('table', { className: 'tbl' },
        React.createElement('thead', null, React.createElement('tr', null,
          React.createElement('th', null, 'Metric'),
          React.createElement('th', { className: 'num' }, 'v10 (baseline)'),
          React.createElement('th', { className: 'num' }, 'v12 (champion)'),
          React.createElement('th', { className: 'num' }, 'Δ'))),
        React.createElement('tbody', null, rows.map((r, i) =>
          React.createElement('tr', { key: i, style: { cursor: 'default' } },
            React.createElement('td', { className: 'b' }, r[0]),
            React.createElement('td', { className: 'num muted mono' }, r[1]),
            React.createElement('td', { className: 'num mono b' }, r[2]),
            React.createElement('td', { className: 'num' }, React.createElement('span', { className: 'tag ' + (r[4] === 'up' ? 'ok' : r[4] === 'down' ? 'danger' : ''), style: { fontVariantNumeric: 'tabular-nums' } }, r[3])))))));
  }

  function Kpi() {
    const [mode, setMode] = useState('overview');
    const [range, setRange] = useState('7d');

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('div', { className: 'page-scroll scroll' },
          React.createElement('div', { className: 'pad' },
            React.createElement('div', { className: 'row', style: { marginBottom: 16, gap: 12 } },
              React.createElement('div', { className: 'seg' },
                React.createElement('button', { className: mode === 'overview' ? 'on' : '', onClick: () => setMode('overview') }, 'Overview'),
                React.createElement('button', { className: mode === 'compare' ? 'on' : '', onClick: () => setMode('compare') }, 'Compare versions')),
              React.createElement('div', { className: 'grow' }),
              React.createElement('div', { className: 'seg' },
                ['24h', '7d', '30d'].map(r => React.createElement('button', { key: r, className: range === r ? 'on' : '', onClick: () => setRange(r) }, r))),
              React.createElement('button', { className: 'gctl' }, React.createElement(Icon, { d: 'mic', size: 15 }), 'All archetypes', React.createElement(Icon, { d: 'chevDown', className: 'gctl-chev' }))),

            mode === 'compare'
              ? React.createElement('div', { className: 'col', style: { gap: 14 } },
                  React.createElement('div', { className: 'sec-head' }, React.createElement('h2', null, 'Champion vs. baseline'), React.createElement('span', { className: 'sub' }, 'Same population · last ' + range)),
                  React.createElement(Compare, null))
              : React.createElement(React.Fragment, null,
                /* primary tiles */
                React.createElement('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 14, marginBottom: 16 } },
                  K.primary.map((m, i) =>
                    React.createElement('div', { className: 'kpi', key: i },
                      React.createElement('div', { className: 'k-lbl' }, React.createElement(Icon, { d: m.ic, size: 14 }), m.k),
                      React.createElement('div', { className: 'k-val' }, m.good ? React.createElement('span', { style: { color: 'var(--ok)' } }, m.v) : m.v, m.u ? React.createElement('span', { className: 'u' }, m.u) : null),
                      React.createElement('div', { className: 'k-delta ' + m.dir }, m.dir === 'up' ? '↑' : m.dir === 'down' ? '↓' : '→', ' ', m.delta),
                      React.createElement('div', { className: 'k-spark' }, React.createElement(Spark, { data: m.spark, w: 70, h: 28, color: m.good ? 'var(--ok)' : 'var(--accent)' }))))),

                /* mid panels */
                React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1.1fr 1fr', gap: 14, marginBottom: 16 } },
                  React.createElement('div', { className: 'card' },
                    React.createElement('div', { className: 'card-head' }, React.createElement(Icon, { d: 'sigma', size: 16, style: { color: 'var(--accent)' } }), React.createElement('h3', null, 'Outcome ladder distribution'), React.createElement('span', { className: 'sub', style: { marginLeft: 'auto' } }, 'weighted score 3.42 / 5')),
                    React.createElement('div', { className: 'card-pad' }, K.ladder.map((l, i) => React.createElement(BarRow, { key: i, t: l.t, v: l.v / 100, color: l.c, suffix: '%' })))),
                  React.createElement('div', { className: 'card' },
                    React.createElement('div', { className: 'card-head' }, React.createElement(Icon, { d: 'shield', size: 16, style: { color: 'var(--accent)' } }), React.createElement('h3', null, 'Objection recovery by type')),
                    React.createElement('div', { className: 'card-pad' }, K.objection.map((o, i) => React.createElement(BarRow, { key: i, t: o.t, v: o.rate, color: o.rate < 0.6 ? 'var(--warn)' : 'var(--accent)', suffix: '%' }))))),

                /* archetype + dwell */
                React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14, marginBottom: 16 } },
                  React.createElement('div', { className: 'card' },
                    React.createElement('div', { className: 'card-head' }, React.createElement(Icon, { d: 'user', size: 16, style: { color: 'var(--accent)' } }), React.createElement('h3', null, 'Conversion by archetype')),
                    React.createElement('div', { className: 'card-pad' }, K.archetype.map((a, i) => React.createElement(BarRow, { key: i, t: a.t, v: a.conv, color: a.conv < 0.4 ? 'var(--danger)' : a.conv < 0.65 ? 'var(--warn)' : 'var(--ok)', suffix: '%' })))),
                  React.createElement('div', { className: 'card' },
                    React.createElement('div', { className: 'card-head' }, React.createElement(Icon, { d: 'clock', size: 16, style: { color: 'var(--accent)' } }), React.createElement('h3', null, 'Avg dwell per stage'), React.createElement('span', { className: 'sub', style: { marginLeft: 'auto' } }, 'turns')),
                    React.createElement('div', { className: 'card-pad' }, K.dwell.map((d, i) => React.createElement(BarRow, { key: i, t: d.t, v: d.v, max: 7, color: 'var(--violet)' }))))),

                /* secondary grid */
                React.createElement('div', { className: 'sec-head' }, React.createElement('h2', null, 'Secondary metrics'), React.createElement('span', { className: 'sub' }, 'diagnostic signal')),
                React.createElement('div', { style: { display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 12 } },
                  K.secondary.map((m, i) =>
                    React.createElement('div', { className: 'card solid card-pad', key: i },
                      React.createElement('div', { className: 'muted', style: { fontSize: 12, fontWeight: 600 } }, m.k),
                      React.createElement('div', { className: 'row', style: { justifyContent: 'space-between', alignItems: 'baseline', marginTop: 4 } },
                        React.createElement('span', { style: { fontFamily: 'var(--font-display)', fontSize: 22, fontWeight: 600, letterSpacing: '-0.02em' } }, m.v),
                        React.createElement('span', { className: 'faint', style: { fontSize: 11 } }, m.sub)
                      )
                    )
                  )
                )
              )
          )
        )
      )
    );
  }

  (window.CadencePages = window.CadencePages || {}).kpi = Kpi;
})();
