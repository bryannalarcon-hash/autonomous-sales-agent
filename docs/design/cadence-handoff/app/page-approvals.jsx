/* P7 — Approval Queue : guardrail-tripped changes awaiting sign-off */
(function () {
  const { useState } = React;
  const { Icon, DB } = window;

  function Approvals() {
    const [sel, setSel] = useState(null);
    const [decided, setDecided] = useState({});

    const rows = DB.APPROVALS.filter(a => !decided[a.id]);

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('div', { className: 'page-scroll scroll' },
          React.createElement('div', { className: 'pad' },
            React.createElement('div', { className: 'sec-head' },
              React.createElement(Icon, { d: 'badge', size: 18, style: { color: 'var(--warn)' } }),
              React.createElement('h2', null, 'Awaiting your sign-off'),
              React.createElement('span', { className: 'sub' }, 'Changes that lift outcomes but breach a guardrail — promotion is paused until you decide.'),
              React.createElement('span', { className: 'sp' })),

            rows.length === 0
              ? React.createElement('div', { className: 'card' }, React.createElement('div', { className: 'empty' }, React.createElement('div', { className: 'ico' }, React.createElement(Icon, { d: 'check', size: 28 })), React.createElement('h3', null, 'Queue clear'), React.createElement('p', null, 'No changes are waiting on approval. Guardrail-blocked experiments will surface here.')))
              : React.createElement('div', { className: 'col', style: { gap: 14 } },
                  rows.map(a =>
                    React.createElement('div', { key: a.id, className: 'card' },
                      React.createElement('div', { className: 'card-pad' },
                        React.createElement('div', { className: 'row', style: { gap: 9, marginBottom: 9 } },
                          React.createElement('span', { className: 'mono', style: { fontSize: 11.5, color: 'var(--text-3)' } }, a.id),
                          React.createElement('span', { className: 'tag warn dot' }, a.reason),
                          React.createElement('span', { className: 'tag' }, a.exp + ' · ' + a.chal),
                          React.createElement('span', { className: 'faint', style: { fontSize: 11.5, marginLeft: 'auto' } }, a.when)),
                        React.createElement('div', { className: 'b', style: { fontSize: 16, fontFamily: 'var(--font-display)', letterSpacing: '-0.02em' } }, a.name),
                        React.createElement('div', { className: 'muted', style: { fontSize: 13, marginTop: 5, lineHeight: 1.5, maxWidth: 760 } }, a.detail)),
                      React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1.4fr', borderTop: '1px solid var(--border)' } },
                        React.createElement(Box, { l: 'Enroll lift', v: '+' + Math.round(a.delta.enroll * 100) + ' pts', good: true }),
                        React.createElement(Box, { l: 'Ladder lift', v: '+' + a.delta.ladder.toFixed(2), good: true }),
                        React.createElement(Box, { l: 'Guardrail breached', v: a.guardrail, warn: true, last: true })),
                      React.createElement('div', { style: { padding: '13px 16px', borderTop: '1px solid var(--border)', display: 'flex', gap: 10 } },
                        React.createElement('button', { className: 'btn btn-ghost btn-sm', onClick: () => setSel(a) }, React.createElement(Icon, { d: 'eye', size: 14 }), 'Review detail'),
                        React.createElement('div', { className: 'grow' }),
                        React.createElement('button', { className: 'btn btn-danger', onClick: () => setDecided(d => ({ ...d, [a.id]: 'rejected' })) }, React.createElement(Icon, { d: 'x', size: 15 }), 'Reject'),
                        React.createElement('button', { className: 'btn btn-ok', onClick: () => setDecided(d => ({ ...d, [a.id]: 'approved' })) }, React.createElement(Icon, { d: 'check', size: 15 }), 'Approve with override'))))),

            Object.keys(decided).length > 0
              ? React.createElement('div', { className: 'row', style: { gap: 8, marginTop: 16, color: 'var(--text-3)', fontSize: 12.5 } },
                  React.createElement(Icon, { d: 'check', size: 14 }),
                  Object.entries(decided).map(([k, v]) => k + ' ' + v).join(' · '),
                  ' — logged to version history')
              : null)),

        sel ? React.createElement(AprDrawer, { a: sel, onClose: () => setSel(null), onDecide: (v) => { setDecided(d => ({ ...d, [sel.id]: v })); setSel(null); } }) : null)
    );
  }

  function Box({ l, v, good, warn, last }) {
    return React.createElement('div', { style: { padding: '12px 16px', borderRight: last ? 0 : '1px solid var(--border)' } },
      React.createElement('div', { className: 'faint', style: { fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.05em' } }, l),
      React.createElement('div', { style: { fontSize: warn ? 13 : 17, fontWeight: 650, marginTop: 3, fontFamily: warn ? 'var(--font-mono)' : 'var(--font-display)', color: good ? 'var(--ok)' : warn ? 'var(--warn-strong)' : 'var(--text)' } }, v));
  }

  function AprDrawer({ a, onClose, onDecide }) {
    return React.createElement(React.Fragment, null,
      React.createElement('div', { className: 'scrim', onClick: onClose }),
      React.createElement('div', { className: 'drawer' },
        React.createElement('div', { className: 'card-head' },
          React.createElement('div', { className: 'grow' },
            React.createElement('div', { className: 'row', style: { gap: 8, marginBottom: 4 } }, React.createElement('span', { className: 'mono', style: { fontSize: 11.5, color: 'var(--text-3)' } }, a.id), React.createElement('span', { className: 'tag warn dot' }, a.reason)),
            React.createElement('div', { className: 'b', style: { fontSize: 15.5, fontFamily: 'var(--font-display)' } }, a.name)),
          React.createElement('button', { className: 'gctl', onClick: onClose, style: { width: 36, padding: 0, justifyContent: 'center' } }, React.createElement(Icon, { d: 'x', size: 16 }))),
        React.createElement('div', { className: 'scroll', style: { padding: 18, display: 'flex', flexDirection: 'column', gap: 14, overflow: 'auto' } },
          React.createElement('div', { className: 'card solid card-pad' },
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 } }, 'Why it needs sign-off'),
            React.createElement('div', { style: { fontSize: 13.5, lineHeight: 1.55 } }, a.detail)),
          React.createElement('div', { className: 'card card-pad', style: { borderColor: 'var(--warn-border)', background: 'var(--warn-soft)' } },
            React.createElement('div', { className: 'row', style: { gap: 9 } },
              React.createElement(Icon, { d: 'shield', size: 17, style: { color: 'var(--warn-strong)' } }),
              React.createElement('div', null,
                React.createElement('div', { className: 'b', style: { fontSize: 13, color: 'var(--warn-strong)' } }, 'Guardrail: ' + a.guardrail),
                React.createElement('div', { style: { fontSize: 12, color: 'var(--warn-strong)', opacity: .85, marginTop: 2 } }, 'Approving records an override on your account and ships ' + a.chal + ' as champion.')))),
          React.createElement('div', { style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 } },
            React.createElement('div', { className: 'card solid card-pad' }, React.createElement('div', { className: 'faint', style: { fontSize: 11, fontWeight: 600 } }, 'Enroll lift'), React.createElement('div', { style: { fontSize: 19, fontWeight: 650, color: 'var(--ok)', fontFamily: 'var(--font-display)', marginTop: 3 } }, '+' + Math.round(a.delta.enroll * 100) + ' pts')),
            React.createElement('div', { className: 'card solid card-pad' }, React.createElement('div', { className: 'faint', style: { fontSize: 11, fontWeight: 600 } }, 'Ladder lift'), React.createElement('div', { style: { fontSize: 19, fontWeight: 650, color: 'var(--ok)', fontFamily: 'var(--font-display)', marginTop: 3 } }, '+' + a.delta.ladder.toFixed(2)))),
          React.createElement('div', { className: 'row', style: { gap: 10, marginTop: 4 } },
            React.createElement('button', { className: 'btn btn-danger grow', onClick: () => onDecide('rejected') }, React.createElement(Icon, { d: 'x', size: 15 }), 'Reject'),
            React.createElement('button', { className: 'btn btn-ok grow', onClick: () => onDecide('approved') }, React.createElement(Icon, { d: 'check', size: 15 }), 'Approve with override'))))
    );
  }

  (window.CadencePages = window.CadencePages || {}).approvals = Approvals;
})();
