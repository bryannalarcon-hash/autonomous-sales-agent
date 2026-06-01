/* P3 — Calls List : filter bar + table + quick-peek drawer */
(function () {
  const { useState } = React;
  const { Icon, DB } = window;

  const OUTCOME_STYLE = {
    'Enrolled': 'ok', 'Booked': 'accent', 'In progress': 'info', 'Escalated': 'warn',
    'Disqualified': 'danger', 'No-interest': '', 'Interested': 'info',
  };

  function Calls() {
    const [q, setQ] = useState('');
    const [outcome, setOutcome] = useState('All');
    const [esc, setEsc] = useState(false);
    const [sel, setSel] = useState(null);

    const outcomes = ['All', 'Enrolled', 'Booked', 'Escalated', 'Disqualified', 'No-interest'];
    let rows = DB.CALLS.filter(c =>
      (outcome === 'All' || c.outcome === outcome) &&
      (!esc || c.escalated) &&
      (!q || (c.who + c.co + c.id).toLowerCase().includes(q.toLowerCase())));

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('div', { className: 'page-scroll scroll' },
          React.createElement('div', { className: 'pad' },
            /* filter bar */
            React.createElement('div', { className: 'row wrap', style: { gap: 10, marginBottom: 16 } },
              React.createElement('div', { className: 'row', style: { position: 'relative' } },
                React.createElement(Icon, { d: 'search', size: 15, style: { position: 'absolute', left: 11, color: 'var(--text-3)' } }),
                React.createElement('input', { className: 'input', placeholder: 'Search caller, company, ID…', value: q, onChange: e => setQ(e.target.value), style: { paddingLeft: 32, width: 260 } })),
              React.createElement('div', { className: 'seg' },
                outcomes.map(o => React.createElement('button', { key: o, className: outcome === o ? 'on' : '', onClick: () => setOutcome(o) }, o))),
              React.createElement('button', { className: 'gctl', onClick: () => setEsc(v => !v), style: esc ? { borderColor: 'var(--warn-border)', background: 'var(--warn-soft)', color: 'var(--warn-strong)' } : null },
                React.createElement(Icon, { d: 'alert', size: 15 }), 'Escalated only'),
              React.createElement('div', { className: 'grow' }),
              React.createElement('span', { className: 'muted', style: { fontSize: 12.5 } }, rows.length + ' calls'),
              React.createElement('button', { className: 'gctl' }, React.createElement(Icon, { d: 'download', size: 15 }), 'Export')),

            /* table */
            React.createElement('div', { className: 'card', style: { padding: '14px 8px 6px' } },
              React.createElement('table', { className: 'tbl' },
                React.createElement('thead', null, React.createElement('tr', null,
                  React.createElement('th', null, 'Call'),
                  React.createElement('th', null, 'Prospect'),
                  React.createElement('th', null, 'Archetype'),
                  React.createElement('th', null, 'Outcome'),
                  React.createElement('th', null, 'Ladder tier'),
                  React.createElement('th', { className: 'num' }, 'Duration'),
                  React.createElement('th', null, 'Version'),
                  React.createElement('th', { className: 'num' }, 'When'))),
                React.createElement('tbody', null,
                  rows.map(c =>
                    React.createElement('tr', { key: c.id, onClick: () => setSel(c) },
                      React.createElement('td', null, React.createElement('span', { className: 'mono', style: { fontSize: 12, color: 'var(--text-2)' } }, '#' + c.id),
                        c.live ? React.createElement('span', { className: 'live-pill', style: { marginLeft: 8 } }, React.createElement('i', null), 'LIVE') : null),
                      React.createElement('td', null,
                        React.createElement('div', { className: 'b' }, c.who),
                        React.createElement('div', { className: 'muted', style: { fontSize: 11.5 } }, c.co)),
                      React.createElement('td', null, React.createElement('span', { className: 'tag' }, c.persona)),
                      React.createElement('td', null, React.createElement('span', { className: 'tag dot ' + (OUTCOME_STYLE[c.outcome] || '') }, c.outcome)),
                      React.createElement('td', null, React.createElement('span', { className: 'muted', style: { fontSize: 12.5 } }, c.tier)),
                      React.createElement('td', { className: 'num mono', style: { fontSize: 12.5 } }, c.dur),
                      React.createElement('td', null, React.createElement('span', { className: 'tag accent' }, c.ver)),
                      React.createElement('td', { className: 'num muted', style: { fontSize: 12 } }, c.when)))))))),

        sel ? React.createElement(Drawer, { c: sel, onClose: () => setSel(null) }) : null)
    );
  }

  function Drawer({ c, onClose }) {
    const style = OUTCOME_STYLE[c.outcome] || '';
    return React.createElement(React.Fragment, null,
      React.createElement('div', { className: 'scrim', onClick: onClose }),
      React.createElement('div', { className: 'drawer' },
        React.createElement('div', { className: 'card-head', style: { background: 'transparent' } },
          React.createElement('div', { className: 'avatar' }, c.who.split(' ').map(x => x[0]).join('')),
          React.createElement('div', { className: 'grow' },
            React.createElement('div', { className: 'b', style: { fontSize: 15, fontFamily: 'var(--font-display)' } }, c.who),
            React.createElement('div', { className: 'muted', style: { fontSize: 12 } }, c.co + ' · ' + c.persona)),
          React.createElement('button', { className: 'gctl', onClick: onClose, style: { width: 36, padding: 0, justifyContent: 'center' } }, React.createElement(Icon, { d: 'x', size: 16 }))),
        React.createElement('div', { className: 'scroll', style: { padding: 18, display: 'flex', flexDirection: 'column', gap: 14, overflow: 'auto' } },
          React.createElement('div', { className: 'row wrap', style: { gap: 8 } },
            React.createElement('span', { className: 'tag dot ' + style, style: { fontSize: 12, padding: '5px 11px' } }, c.outcome),
            React.createElement('span', { className: 'tag' }, c.tier),
            React.createElement('span', { className: 'tag accent' }, c.ver + ' · ' + c.kb),
            React.createElement('span', { className: 'tag' }, c.channel),
            c.escalated ? React.createElement('span', { className: 'tag warn' }, React.createElement(Icon, { d: 'alert', size: 12 }), 'Escalated') : null),
          React.createElement('div', { className: 'card solid card-pad', style: { display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 14 } },
            [['Duration', c.dur], ['Channel', c.channel], ['When', c.when], ['Qualified', c.qualified == null ? '—' : c.qualified ? 'Yes' : 'No'], ['Ladder tier', c.tier], ['Call ID', c.id]].map(([l, v], i) =>
              React.createElement('div', { key: i },
                React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em' } }, l),
                React.createElement('div', { className: 'b', style: { fontSize: 13.5, marginTop: 3 } }, v)))),
          React.createElement('div', { className: 'card solid card-pad' },
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 8 } }, 'Belief at close'),
            [['Trust', .74, 'var(--ok)'], ['Bail risk', .18, 'var(--warn)'], ['Qualification conf.', .93, 'var(--accent)']].map(([l, v, col], i) =>
              React.createElement('div', { key: i, className: 'row', style: { gap: 10, marginBottom: 8 } },
                React.createElement('span', { style: { width: 110, fontSize: 12, color: 'var(--text-2)', fontWeight: 600 } }, l),
                React.createElement('span', { className: 'bar grow' }, React.createElement('i', { style: { width: (v * 100) + '%', background: col } })),
                React.createElement('span', { className: 'mono', style: { fontSize: 12, width: 34, textAlign: 'right' } }, v.toFixed(2))))),
          React.createElement('button', { className: 'btn btn-primary', onClick: () => window.cadenceGo('review', { call: c.id }) }, React.createElement(Icon, { d: 'eye', size: 16 }), 'Open full call review', React.createElement(Icon, { d: 'arrowR', size: 15 })))))
  }

  (window.CadencePages = window.CadencePages || {}).calls = Calls;
})();
