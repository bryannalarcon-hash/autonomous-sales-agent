/* P6 — Experiment Lab : champion/challenger experiments + before-after detail */
(function () {
  const { useState } = React;
  const { Icon, DB } = window;

  const STATE = {
    running: { c: 'info', l: 'Running' }, 'result-ready': { c: 'ok', l: 'Result ready' },
    blocked: { c: 'danger', l: 'Guardrail blocked' }, failed: { c: '', l: 'Failed' }, retired: { c: '', l: 'Retired' },
  };

  function Lab() {
    const [sel, setSel] = useState(null);
    const [tab, setTab] = useState('active');
    const active = DB.EXPERIMENTS.filter(e => ['running', 'result-ready', 'blocked'].includes(e.state));
    const past = DB.EXPERIMENTS.filter(e => ['failed', 'retired'].includes(e.state));
    const rows = tab === 'active' ? active : past;

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('div', { className: 'page-scroll scroll' },
          React.createElement('div', { className: 'pad' },
            React.createElement('div', { className: 'row', style: { marginBottom: 16, gap: 12 } },
              React.createElement('div', { className: 'seg' },
                React.createElement('button', { className: tab === 'active' ? 'on' : '', onClick: () => setTab('active') }, 'Active · ' + active.length),
                React.createElement('button', { className: tab === 'past' ? 'on' : '', onClick: () => setTab('past') }, 'Past · ' + past.length)),
              React.createElement('div', { className: 'grow' }),
              React.createElement('button', { className: 'btn btn-primary' }, React.createElement(Icon, { d: 'plus', size: 16 }), 'New experiment')),

            React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 } },
              rows.map(e => {
                const st = STATE[e.state];
                return React.createElement('div', { key: e.id, className: 'card', style: { cursor: 'pointer', overflow: 'hidden' }, onClick: () => setSel(e) },
                  React.createElement('div', { className: 'card-pad', style: { paddingBottom: 12 } },
                    React.createElement('div', { className: 'row', style: { gap: 8, marginBottom: 9 } },
                      React.createElement('span', { className: 'mono', style: { fontSize: 11.5, color: 'var(--text-3)' } }, e.id),
                      React.createElement('span', { className: 'tag ' + st.c + ' dot' }, st.l),
                      e.promote === 'auto' ? React.createElement('span', { className: 'tag ok' }, React.createElement(Icon, { d: 'bolt', size: 12 }), 'Auto-promote ready') : null),
                    React.createElement('div', { className: 'b', style: { fontSize: 15, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em' } }, e.name),
                    React.createElement('div', { className: 'row', style: { gap: 8, marginTop: 6 } },
                      React.createElement('span', { className: 'tag' }, e.champ, React.createElement(Icon, { d: 'arrowR', size: 11 }), e.chal),
                      React.createElement('span', { className: 'muted', style: { fontSize: 12 } }, e.pop))),
                  React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', borderTop: '1px solid var(--border)' } },
                    React.createElement(Cell, { l: 'Enroll Δ', v: (e.delta.enroll > 0 ? '+' : '') + Math.round(e.delta.enroll * 100) + ' pts', good: e.delta.enroll > 0 }),
                    React.createElement(Cell, { l: 'Ladder Δ', v: (e.delta.ladder > 0 ? '+' : '') + e.delta.ladder.toFixed(2), good: e.delta.ladder > 0 }),
                    React.createElement(Cell, { l: 'Significance', v: Math.round(e.sig * 100) + '%', last: true })),
                  e.state === 'blocked' ? React.createElement('div', { style: { padding: '9px 16px', background: 'var(--danger-soft)', borderTop: '1px solid var(--danger-border)', fontSize: 11.5, color: 'var(--danger)', display: 'flex', gap: 7, alignItems: 'center' } }, React.createElement(Icon, { d: 'alert', size: 13 }), e.reason) : null,
                  React.createElement('div', { style: { padding: '10px 16px', borderTop: '1px solid var(--border)' } },
                    React.createElement('div', { className: 'row', style: { justifyContent: 'space-between', marginBottom: 5 } },
                      React.createElement('span', { className: 'faint', style: { fontSize: 11 } }, 'Sample'),
                      React.createElement('span', { className: 'mono', style: { fontSize: 11.5 } }, e.n + ' / ' + e.target)),
                    React.createElement('div', { className: 'bar' }, React.createElement('i', { style: { width: Math.min(100, (e.n / e.target) * 100) + '%', background: e.n >= e.target ? 'var(--ok)' : 'var(--accent)' } }))));
              })))),

        sel ? React.createElement(ExpDrawer, { e: sel, onClose: () => setSel(null) }) : null)
    );
  }

  function Cell({ l, v, good, last }) {
    return React.createElement('div', { style: { padding: '11px 14px', borderRight: last ? 0 : '1px solid var(--border)' } },
      React.createElement('div', { className: 'faint', style: { fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em' } }, l),
      React.createElement('div', { className: 'mono', style: { fontSize: 16, fontWeight: 650, marginTop: 3, color: good === true ? 'var(--ok)' : good === false ? 'var(--danger)' : 'var(--text)' } }, v));
  }

  function ExpDrawer({ e, onClose }) {
    const st = STATE[e.state];
    return React.createElement(React.Fragment, null,
      React.createElement('div', { className: 'scrim', onClick: onClose }),
      React.createElement('div', { className: 'drawer' },
        React.createElement('div', { className: 'card-head' },
          React.createElement('div', { className: 'grow' },
            React.createElement('div', { className: 'row', style: { gap: 8, marginBottom: 4 } }, React.createElement('span', { className: 'mono', style: { fontSize: 11.5, color: 'var(--text-3)' } }, e.id), React.createElement('span', { className: 'tag ' + st.c + ' dot' }, st.l)),
            React.createElement('div', { className: 'b', style: { fontSize: 15.5, fontFamily: 'var(--font-display)' } }, e.name)),
          React.createElement('button', { className: 'gctl', onClick: onClose, style: { width: 36, padding: 0, justifyContent: 'center' } }, React.createElement(Icon, { d: 'x', size: 16 }))),
        React.createElement('div', { className: 'scroll', style: { padding: 18, display: 'flex', flexDirection: 'column', gap: 14, overflow: 'auto' } },
          /* before / after */
          React.createElement('div', { className: 'row', style: { gap: 12, alignItems: 'stretch' } },
            React.createElement('div', { className: 'card solid card-pad grow' },
              React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' } }, 'Champion'),
              React.createElement('div', { className: 'b', style: { fontSize: 17, fontFamily: 'var(--font-display)', margin: '4px 0' } }, e.champ),
              React.createElement('div', { className: 'muted', style: { fontSize: 12 } }, 'Current production')),
            React.createElement('div', { style: { display: 'flex', alignItems: 'center' } }, React.createElement(Icon, { d: 'arrowR', size: 20, style: { color: 'var(--accent)' } })),
            React.createElement('div', { className: 'card card-pad grow', style: { borderColor: 'var(--accent-border)' } },
              React.createElement('div', { style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', color: 'var(--accent-strong)' } }, 'Challenger'),
              React.createElement('div', { className: 'b', style: { fontSize: 17, fontFamily: 'var(--font-display)', margin: '4px 0', color: 'var(--accent-strong)' } }, e.chal),
              React.createElement('div', { className: 'muted', style: { fontSize: 12 } }, e.pop))),

          React.createElement('div', { className: 'card solid card-pad' },
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 7 } }, 'What changed'),
            React.createElement('div', { className: 'mono', style: { fontSize: 13, lineHeight: 1.6 } }, e.diff)),

          React.createElement('div', { className: 'card solid card-pad' },
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 10 } }, 'Lift (challenger − champion)'),
            React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } },
              React.createElement(Stat, { l: 'Same-call enroll', v: (e.delta.enroll > 0 ? '+' : '') + Math.round(e.delta.enroll * 100) + ' pts', good: e.delta.enroll > 0 }),
              React.createElement(Stat, { l: 'Ladder score', v: (e.delta.ladder > 0 ? '+' : '') + e.delta.ladder.toFixed(2), good: e.delta.ladder > 0 }),
              React.createElement(Stat, { l: '95% CI', v: e.ci, mono: true }),
              React.createElement(Stat, { l: 'Significance', v: Math.round(e.sig * 100) + '%', good: e.sig >= 0.95 ? true : null })),
            React.createElement('div', { className: 'row', style: { marginTop: 12, gap: 8, alignItems: 'center', padding: '9px 11px', borderRadius: 10, background: e.guardrail === 'trip' ? 'var(--danger-soft)' : 'var(--ok-soft)', border: '1px solid ' + (e.guardrail === 'trip' ? 'var(--danger-border)' : 'var(--ok-border)') } },
              React.createElement(Icon, { d: 'shield', size: 15, style: { color: e.guardrail === 'trip' ? 'var(--danger)' : 'var(--ok)' } }),
              React.createElement('span', { style: { fontSize: 12.5, fontWeight: 600, color: e.guardrail === 'trip' ? 'var(--danger)' : 'var(--ok)' } }, e.guardrail === 'trip' ? 'Guardrail tripped — needs approval' : e.guardrail === 'warn' ? 'Guardrail warning' : 'All guardrails pass'))),

          e.state === 'result-ready'
            ? React.createElement('button', { className: 'btn btn-primary' }, React.createElement(Icon, { d: 'promote', size: 16 }), 'Promote ' + e.chal + ' to champion')
            : e.state === 'blocked'
              ? React.createElement('button', { className: 'btn btn-primary', onClick: () => window.cadenceGo('approvals') }, React.createElement(Icon, { d: 'badge', size: 16 }), 'Send to approval queue')
              : e.state === 'running'
                ? React.createElement('div', { className: 'row', style: { gap: 10 } }, React.createElement('button', { className: 'btn btn-ghost grow' }, 'Pause'), React.createElement('button', { className: 'btn btn-danger grow' }, 'Stop experiment'))
                : React.createElement('button', { className: 'btn btn-ghost' }, React.createElement(Icon, { d: 'rollback', size: 16 }), 'Clone & re-run')))
    );
  }
  function Stat({ l, v, good, mono }) {
    return React.createElement('div', null,
      React.createElement('div', { className: 'faint', style: { fontSize: 11, fontWeight: 600 } }, l),
      React.createElement('div', { className: mono ? 'mono' : '', style: { fontSize: mono ? 13 : 18, fontWeight: 650, marginTop: 3, fontFamily: mono ? 'var(--font-mono)' : 'var(--font-display)', color: good === true ? 'var(--ok)' : good === false ? 'var(--danger)' : 'var(--text)' } }, v));
  }

  (window.CadencePages = window.CadencePages || {}).lab = Lab;
})();
