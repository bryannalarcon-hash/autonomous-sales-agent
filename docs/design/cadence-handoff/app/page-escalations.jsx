/* P5 — Escalation Queue : lifecycle (unreviewed → reviewed → resolved) + detail */
(function () {
  const { useState } = React;
  const { Icon, DB } = window;

  const SEV = { high: 'danger', med: 'warn', low: 'info' };

  function Escalations() {
    const [tab, setTab] = useState('unreviewed');
    const [sel, setSel] = useState(null);
    const counts = { unreviewed: 0, reviewed: 0, resolved: 0 };
    DB.ESCALATIONS.forEach(e => counts[e.state]++);
    const rows = DB.ESCALATIONS.filter(e => e.state === tab);

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('div', { className: 'page-scroll scroll' },
          React.createElement('div', { className: 'pad' },
            React.createElement('div', { className: 'row', style: { marginBottom: 16, gap: 12 } },
              React.createElement('div', { className: 'seg' },
                [['unreviewed', 'Unreviewed'], ['reviewed', 'Reviewed'], ['resolved', 'Resolved']].map(([k, l]) =>
                  React.createElement('button', { key: k, className: tab === k ? 'on' : '', onClick: () => setTab(k) }, l,
                    React.createElement('span', { className: 'nav-badge', style: { display: 'inline-flex', marginLeft: 7, background: tab === k ? 'var(--surface)' : 'transparent', color: 'inherit', minWidth: 16, height: 16, fontSize: 10.5 } }, counts[k])))),
              React.createElement('div', { className: 'grow' }),
              React.createElement('button', { className: 'gctl' }, React.createElement(Icon, { d: 'filter', size: 15 }), 'Reason', React.createElement(Icon, { d: 'chevDown', className: 'gctl-chev' }))),

            rows.length === 0
              ? React.createElement('div', { className: 'card' }, React.createElement('div', { className: 'empty' }, React.createElement('div', { className: 'ico' }, React.createElement(Icon, { d: 'check', size: 28 })), React.createElement('h3', null, 'Nothing here'), React.createElement('p', null, 'No ' + tab + ' escalations right now.')))
              : React.createElement('div', { className: 'col', style: { gap: 11 } },
                  rows.map(e =>
                    React.createElement('div', { key: e.id, className: 'card', style: { padding: '15px 17px', cursor: 'pointer', display: 'flex', gap: 14, alignItems: 'center' }, onClick: () => setSel(e) },
                      React.createElement('div', { style: { width: 4, alignSelf: 'stretch', borderRadius: 4, background: 'var(--' + SEV[e.sev] + ')' } }),
                      React.createElement('div', { className: 'grow' },
                        React.createElement('div', { className: 'row', style: { gap: 9, marginBottom: 5 } },
                          React.createElement('span', { className: 'mono', style: { fontSize: 12, color: 'var(--text-3)' } }, e.id),
                          React.createElement('span', { className: 'tag ' + SEV[e.sev], style: { textTransform: 'capitalize' } }, e.sev + ' severity'),
                          React.createElement('span', { className: 'tag' }, e.reason)),
                        React.createElement('div', { className: 'b', style: { fontSize: 14 } }, e.who, React.createElement('span', { className: 'muted', style: { fontWeight: 400 } }, ' · ' + e.co)),
                        React.createElement('div', { className: 'muted', style: { fontSize: 12.5, marginTop: 3, maxWidth: 680 } }, e.moment)),
                      React.createElement('div', { className: 'col', style: { alignItems: 'flex-end', gap: 8 } },
                        React.createElement('span', { className: 'faint', style: { fontSize: 11.5 } }, e.when),
                        React.createElement('span', { className: 'tag accent' }, e.ver)),
                      React.createElement(Icon, { d: 'chevron', size: 18, style: { color: 'var(--text-3)' } })))))),

        sel ? React.createElement(EscDrawer, { e: sel, onClose: () => setSel(null) }) : null)
    );
  }

  function EscDrawer({ e, onClose }) {
    return React.createElement(React.Fragment, null,
      React.createElement('div', { className: 'scrim', onClick: onClose }),
      React.createElement('div', { className: 'drawer' },
        React.createElement('div', { className: 'card-head' },
          React.createElement('div', { style: { width: 36, height: 36, borderRadius: 10, background: 'var(--' + SEV[e.sev] + '-soft)', color: 'var(--' + SEV[e.sev] + ')', display: 'flex', alignItems: 'center', justifyContent: 'center' } }, React.createElement(Icon, { d: 'alert', size: 18 })),
          React.createElement('div', { className: 'grow' },
            React.createElement('div', { className: 'b', style: { fontSize: 15, fontFamily: 'var(--font-display)' } }, e.reason),
            React.createElement('div', { className: 'muted', style: { fontSize: 12 } }, e.id + ' · ' + e.when)),
          React.createElement('button', { className: 'gctl', onClick: onClose, style: { width: 36, padding: 0, justifyContent: 'center' } }, React.createElement(Icon, { d: 'x', size: 16 }))),
        React.createElement('div', { className: 'scroll', style: { padding: 18, display: 'flex', flexDirection: 'column', gap: 14, overflow: 'auto' } },
          React.createElement('div', { className: 'row wrap', style: { gap: 8 } },
            React.createElement('span', { className: 'tag ' + SEV[e.sev], style: { textTransform: 'capitalize' } }, e.sev + ' severity'),
            React.createElement('span', { className: 'tag' }, e.who + ' · ' + e.co),
            React.createElement('span', { className: 'tag accent' }, e.ver)),
          React.createElement('div', { className: 'card solid card-pad' },
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 } }, 'Trigger moment'),
            React.createElement('div', { style: { fontSize: 13.5, lineHeight: 1.5 } }, e.moment)),
          React.createElement('div', { className: 'card solid card-pad' },
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 } }, 'Belief at escalation'),
            [['Bail risk', .71, 'var(--danger)'], ['Concession pressure', .64, 'var(--warn)'], ['Trust', .38, 'var(--warn)']].map(([l, v, c], i) =>
              React.createElement('div', { key: i, className: 'row', style: { gap: 10, marginBottom: 8 } },
                React.createElement('span', { style: { width: 140, fontSize: 12, color: 'var(--text-2)', fontWeight: 600 } }, l),
                React.createElement('span', { className: 'bar grow' }, React.createElement('i', { style: { width: (v * 100) + '%', background: c } })),
                React.createElement('span', { className: 'mono', style: { fontSize: 12, width: 34, textAlign: 'right' } }, v.toFixed(2))))),
          React.createElement('div', { className: 'row', style: { gap: 10 } },
            React.createElement('button', { className: 'btn btn-ghost grow', onClick: () => window.cadenceGo('review', { call: e.call }) }, React.createElement(Icon, { d: 'eye', size: 16 }), 'Open call'),
            React.createElement('button', { className: 'btn btn-ghost grow' }, React.createElement(Icon, { d: 'phone', size: 16 }), 'Assign callback')),
          React.createElement('div', { className: 'row', style: { gap: 10 } },
            e.state === 'unreviewed' ? React.createElement('button', { className: 'btn btn-ghost grow' }, React.createElement(Icon, { d: 'check', size: 16 }), 'Mark reviewed') : null,
            React.createElement('button', { className: 'btn btn-ok grow' }, React.createElement(Icon, { d: 'check', size: 16 }), 'Resolve'),
            React.createElement('button', { className: 'btn btn-primary grow', onClick: () => window.cadenceGo('lab') }, React.createElement(Icon, { d: 'flask', size: 16 }), 'Turn into fix'))))
    );
  }

  (window.CadencePages = window.CadencePages || {}).escalations = Escalations;
})();
