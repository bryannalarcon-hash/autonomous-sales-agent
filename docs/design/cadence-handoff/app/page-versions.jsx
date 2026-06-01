/* P9 — Version History & Rollback : lineage list + detail + rollback modal */
(function () {
  const { useState } = React;
  const { Icon, DB } = window;

  function Versions() {
    const [sel, setSel] = useState(DB.VERSIONS[0]);
    const [rollback, setRollback] = useState(null);
    const V = DB.VERSIONS;
    const maxLadder = Math.max(...V.map(v => v.ladder));

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('div', { style: { flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: '1fr 400px' } },
          /* lineage */
          React.createElement('div', { className: 'scroll', style: { overflow: 'auto', padding: 22 } },
            React.createElement('div', { className: 'sec-head' }, React.createElement('h2', null, 'Version lineage'), React.createElement('span', { className: 'sub' }, V.length + ' versions · newest first')),
            React.createElement('div', { style: { position: 'relative' } },
              React.createElement('div', { style: { position: 'absolute', left: 19, top: 12, bottom: 12, width: 2, background: 'var(--border)' } }),
              V.map((v, i) =>
                React.createElement('div', { key: v.id, onClick: () => setSel(v), style: { position: 'relative', display: 'flex', gap: 16, padding: '8px 0', cursor: 'pointer' } },
                  React.createElement('div', { style: { flex: '0 0 auto', width: 40, display: 'flex', justifyContent: 'center', zIndex: 1 } },
                    React.createElement('div', { style: { width: 16, height: 16, borderRadius: 50, marginTop: 18, background: v.champion ? 'var(--accent)' : 'var(--surface-3)', border: '3px solid var(--panel)', boxShadow: v.champion ? '0 0 0 3px var(--accent-soft)' : v.id === sel.id ? '0 0 0 3px var(--surface-3)' : 'none' } })),
                  React.createElement('div', { className: 'card' + (v.id === sel.id ? '' : ' solid'), style: { flex: 1, padding: '13px 16px', borderColor: v.id === sel.id ? 'var(--accent-border)' : undefined, boxShadow: v.id === sel.id ? '0 0 0 3px var(--accent-soft)' : undefined } },
                    React.createElement('div', { className: 'row', style: { gap: 9 } },
                      React.createElement('span', { className: 'b mono', style: { fontSize: 14 } }, v.label),
                      v.champion ? React.createElement('span', { className: 'tag accent dot' }, 'Champion') : null,
                      React.createElement('span', { className: 'tag' }, v.kb),
                      React.createElement('span', { className: 'tag ' + (v.guardrail === 'pass' ? 'ok' : 'warn') }, React.createElement(Icon, { d: 'shield', size: 11 }), v.guardrail),
                      React.createElement('span', { className: 'faint', style: { fontSize: 11.5, marginLeft: 'auto' } }, v.created)),
                    React.createElement('div', { className: 'muted', style: { fontSize: 12.5, marginTop: 5 } }, v.note),
                    React.createElement('div', { className: 'row', style: { gap: 16, marginTop: 9 } },
                      React.createElement(Mini, { l: 'Ladder', v: v.ladder.toFixed(2), w: v.ladder / maxLadder }),
                      React.createElement(Mini, { l: 'Enroll', v: Math.round(v.enroll * 100) + '%', w: v.enroll }),
                      React.createElement(Mini, { l: 'Qual', v: Math.round(v.qual * 100) + '%', w: v.qual }),
                      React.createElement('span', { className: 'faint', style: { fontSize: 11.5, marginLeft: 'auto', alignSelf: 'flex-end' } }, v.calls.toLocaleString() + ' calls'))))))),

          /* detail */
          React.createElement('div', { className: 'scroll', style: { borderLeft: '1px solid var(--border)', overflow: 'auto', padding: 20, display: 'flex', flexDirection: 'column', gap: 14 } },
            React.createElement('div', { className: 'row', style: { gap: 10 } },
              React.createElement('span', { className: 'b mono', style: { fontSize: 22, fontFamily: 'var(--font-display)' } }, sel.label),
              sel.champion ? React.createElement('span', { className: 'tag accent dot' }, 'Champion') : React.createElement('span', { className: 'tag' }, 'Archived')),
            React.createElement('div', { className: 'muted', style: { fontSize: 13, marginTop: -6 } }, sel.note),
            React.createElement('div', { className: 'card solid card-pad', style: { display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 14 } },
              [['Knowledge base', sel.kb], ['Persona', sel.persona], ['Parent', sel.parent || '—'], ['Created', sel.created], ['Calls run', sel.calls.toLocaleString()], ['Guardrail', sel.guardrail]].map(([l, vv], i) =>
                React.createElement('div', { key: i }, React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' } }, l), React.createElement('div', { className: 'b', style: { fontSize: 13.5, marginTop: 3 } }, vv)))),
            React.createElement('div', { className: 'card solid card-pad' },
              React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 } }, 'Performance'),
              [['Weighted ladder', sel.ladder.toFixed(2) + ' / 5', sel.ladder / 5, 'var(--accent)'], ['Same-call enroll', Math.round(sel.enroll * 100) + '%', sel.enroll, 'var(--ok)'], ['Qualification', Math.round(sel.qual * 100) + '%', sel.qual, 'var(--info)']].map(([l, vv, w, c], i) =>
                React.createElement('div', { key: i, className: 'row', style: { gap: 10, marginBottom: 9 } },
                  React.createElement('span', { style: { width: 116, fontSize: 12, color: 'var(--text-2)', fontWeight: 600 } }, l),
                  React.createElement('span', { className: 'bar grow' }, React.createElement('i', { style: { width: (w * 100) + '%', background: c } })),
                  React.createElement('span', { className: 'mono', style: { fontSize: 12, width: 50, textAlign: 'right' } }, vv)))),
            React.createElement('div', { className: 'card solid card-pad' },
              React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 } }, 'Change from parent'),
              React.createElement('div', { className: 'mono', style: { fontSize: 12.5, lineHeight: 1.5 } }, sel.diff)),
            sel.champion
              ? React.createElement('div', { className: 'card card-pad', style: { borderColor: 'var(--accent-border)', display: 'flex', gap: 10, alignItems: 'center' } },
                  React.createElement(Icon, { d: 'check', size: 18, style: { color: 'var(--accent)' } }),
                  React.createElement('span', { style: { fontSize: 12.5, color: 'var(--text-2)' } }, 'This version is live in production.'))
              : React.createElement('div', { className: 'row', style: { gap: 10 } },
                  React.createElement('button', { className: 'btn btn-ghost grow' }, React.createElement(Icon, { d: 'diff', size: 16 }), 'Diff vs champion'),
                  React.createElement('button', { className: 'btn btn-primary grow', onClick: () => setRollback(sel) }, React.createElement(Icon, { d: 'rollback', size: 16 }), 'Roll back to ' + sel.label)))),

        rollback ? React.createElement(RollbackModal, { v: rollback, onClose: () => setRollback(null) }) : null)
    );
  }

  function Mini({ l, v, w }) {
    return React.createElement('div', { style: { minWidth: 78 } },
      React.createElement('div', { className: 'row', style: { justifyContent: 'space-between', marginBottom: 3 } },
        React.createElement('span', { className: 'faint', style: { fontSize: 10.5, fontWeight: 600 } }, l),
        React.createElement('span', { className: 'mono', style: { fontSize: 11.5, fontWeight: 650 } }, v)),
      React.createElement('div', { className: 'bar', style: { height: 4 } }, React.createElement('i', { style: { width: (w * 100) + '%' } })));
  }

  function RollbackModal({ v, onClose }) {
    return React.createElement(React.Fragment, null,
      React.createElement('div', { className: 'scrim', onClick: onClose }),
      React.createElement('div', { className: 'modal' },
        React.createElement('div', { className: 'card-pad', style: { display: 'flex', flexDirection: 'column', gap: 13 } },
          React.createElement('div', { style: { width: 42, height: 42, borderRadius: 12, background: 'var(--warn-soft)', color: 'var(--warn-strong)', display: 'flex', alignItems: 'center', justifyContent: 'center' } }, React.createElement(Icon, { d: 'rollback', size: 20 })),
          React.createElement('h3', { style: { fontSize: 17 } }, 'Roll back to ' + v.label + '?'),
          React.createElement('p', { className: 'muted', style: { fontSize: 13, lineHeight: 1.55 } }, 'This makes ', React.createElement('b', { className: 'mono', style: { color: 'var(--text)' } }, v.label + ' · ' + v.kb), ' the live champion. The current champion ', React.createElement('b', { className: 'mono', style: { color: 'var(--text)' } }, 'v12'), ' will be archived — no calls in flight are interrupted.'),
          React.createElement('div', { className: 'card solid card-pad', style: { display: 'flex', justifyContent: 'space-between' } },
            React.createElement('span', { className: 'muted', style: { fontSize: 12.5 } }, 'Expected ladder change'),
            React.createElement('span', { className: 'mono b', style: { fontSize: 13, color: 'var(--danger)' } }, (v.ladder - 3.42).toFixed(2))),
          React.createElement('div', { className: 'row', style: { gap: 10, marginTop: 4 } },
            React.createElement('button', { className: 'btn btn-ghost grow', onClick: onClose }, 'Cancel'),
            React.createElement('button', { className: 'btn btn-primary grow', onClick: onClose }, React.createElement(Icon, { d: 'rollback', size: 15 }), 'Confirm rollback'))))
    );
  }

  (window.CadencePages = window.CadencePages || {}).versions = Versions;
})();
