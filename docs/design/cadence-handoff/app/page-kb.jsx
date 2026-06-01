/* P8 — KB / Playbook Editor : section tree + editor + champion-vs-draft diff */
(function () {
  const { useState } = React;
  const { Icon } = window;

  const SECTIONS = [
    { id: 'persona', t: 'Persona & tone', n: 4 },
    { id: 'discovery', t: 'Discovery flow', n: 7 },
    { id: 'objections', t: 'Objection rebuttals', n: 9, active: true },
    { id: 'pricing', t: 'Pricing & concessions', n: 5, lock: true },
    { id: 'closing', t: 'Closing triggers', n: 6 },
    { id: 'escalation', t: 'Escalation rules', n: 4 },
    { id: 'compliance', t: 'Compliance guardrails', n: 8, lock: true },
  ];

  const DIFF = [
    { type: 'ctx', text: 'WHEN objection = "price" AND bail_risk < 0.5:' },
    { type: 'del', text: '  → respond with discount_availability framing' },
    { type: 'add', text: '  → respond with ROI_proof (recovered no-shows, first-month payback)' },
    { type: 'add', text: '  → THEN offer 30-day pilot if skepticism > 0.4' },
    { type: 'ctx', text: 'WHEN objection = "price" AND bail_risk ≥ 0.5:' },
    { type: 'ctx', text: '  → acknowledge + de-risk before any number' },
    { type: 'del', text: '  → escalate immediately' },
    { type: 'add', text: '  → attempt one ROI reframe, escalate only if pressure persists' },
  ];

  function Kb() {
    const [active, setActive] = useState('objections');
    const [mode, setMode] = useState('edit'); // edit | diff
    const [dirty, setDirty] = useState(true);

    return (
      React.createElement('div', { className: 'page' },
        React.createElement('div', { style: { flex: 1, minHeight: 0, display: 'grid', gridTemplateColumns: '262px minmax(0,1fr)' } },
          /* section tree */
          React.createElement('div', { className: 'scroll', style: { borderRight: '1px solid var(--border)', padding: 16, overflow: 'auto' } },
            React.createElement('div', { className: 'row', style: { justifyContent: 'space-between', marginBottom: 12 } },
              React.createElement('span', { className: 'nav-group', style: { margin: 0, padding: 0 } }, 'Playbook · kb-37'),
              React.createElement('button', { className: 'gctl', style: { height: 28, padding: '0 8px' } }, React.createElement(Icon, { d: 'plus', size: 14 }))),
            React.createElement('div', { className: 'col', style: { gap: 3 } },
              SECTIONS.map(s =>
                React.createElement('button', { key: s.id, className: 'nav-item' + (active === s.id ? ' on' : ''), onClick: () => setActive(s.id), style: { fontSize: 13 } },
                  React.createElement(Icon, { d: s.lock ? 'shield' : 'note', size: 16 }),
                  React.createElement('span', { className: 'grow', style: { textAlign: 'left' } }, s.t),
                  s.id === 'objections' && dirty ? React.createElement('span', { style: { width: 7, height: 7, borderRadius: 50, background: 'var(--warn)' } }) : React.createElement('span', { className: 'faint', style: { fontSize: 11 } }, s.n)))),
            React.createElement('div', { className: 'card solid card-pad', style: { marginTop: 16 } },
              React.createElement('div', { className: 'row', style: { gap: 8, marginBottom: 6 } }, React.createElement(Icon, { d: 'branch', size: 15, style: { color: 'var(--accent)' } }), React.createElement('span', { className: 'b', style: { fontSize: 12.5 } }, 'Draft challenger')),
              React.createElement('div', { className: 'muted', style: { fontSize: 11.5, lineHeight: 1.5 } }, 'Editing forks ', React.createElement('b', { className: 'mono' }, 'kb-37'), ' into a draft. It won’t affect live calls until promoted via an experiment.'))),

          /* editor / diff */
          React.createElement('div', { className: 'col', style: { minHeight: 0 } },
            React.createElement('div', { className: 'row', style: { padding: '12px 18px', borderBottom: '1px solid var(--border)', gap: 12 } },
              React.createElement('div', null,
                React.createElement('div', { className: 'b', style: { fontSize: 15, fontFamily: 'var(--font-display)' } }, 'Objection rebuttals'),
                React.createElement('div', { className: 'muted', style: { fontSize: 12 } }, '9 rules · last edited just now')),
              React.createElement('div', { className: 'grow' }),
              React.createElement('div', { className: 'seg' },
                React.createElement('button', { className: mode === 'edit' ? 'on' : '', onClick: () => setMode('edit') }, React.createElement(Icon, { d: 'edit', size: 14 }), ' Edit'),
                React.createElement('button', { className: mode === 'diff' ? 'on' : '', onClick: () => setMode('diff') }, React.createElement(Icon, { d: 'diff', size: 14 }), ' Diff vs champion')),
              React.createElement('button', { className: 'btn btn-ghost btn-sm' }, React.createElement(Icon, { d: 'rollback', size: 14 }), 'Revert'),
              React.createElement('button', { className: 'btn btn-primary btn-sm', onClick: () => window.cadenceGo('lab') }, React.createElement(Icon, { d: 'flask', size: 14 }), 'Test as experiment')),

            React.createElement('div', { className: 'scroll', style: { flex: 1, minHeight: 0, overflow: 'auto', padding: 18 } },
              mode === 'edit'
                ? React.createElement(Editor, null)
                : React.createElement(Diff, null))))));
  }

  function Editor() {
    return React.createElement('div', { className: 'col', style: { gap: 14, maxWidth: 880 } },
      React.createElement('div', { className: 'card' },
        React.createElement('div', { className: 'card-head' }, React.createElement('span', { className: 'tag warn' }, 'Price'), React.createElement('h3', { style: { fontSize: 13 } }, 'Rule 3 · Price objection'), React.createElement('span', { className: 'tag', style: { marginLeft: 'auto' } }, 'recovery 72%')),
        React.createElement('div', { className: 'card-pad', style: { display: 'flex', flexDirection: 'column', gap: 12 } },
          React.createElement('div', null,
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 } }, 'Trigger condition'),
            React.createElement('div', { className: 'mono', style: { fontSize: 13, padding: '10px 12px', borderRadius: 10, background: 'var(--surface-2)', border: '1px solid var(--border)' } }, 'objection == "price" && bail_risk < 0.5')),
          React.createElement('div', null,
            React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 } }, 'Response strategy'),
            React.createElement('textarea', { className: 'input', style: { width: '100%', height: 92, padding: 12, lineHeight: 1.55, resize: 'vertical', fontFamily: 'var(--font)' }, defaultValue: 'Acknowledge the cost as real, then reframe to ROI — lead with recovered no-shows and first-month payback. If skepticism remains high, offer the 30-day pilot with no annual lock-in.' })),
          React.createElement('div', { className: 'row', style: { gap: 10 } },
            React.createElement('div', { className: 'grow' },
              React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 } }, 'Next stage'),
              React.createElement('div', { className: 'field', style: { width: '100%' } }, 'Closing', React.createElement(Icon, { d: 'chevDown' }))),
            React.createElement('div', { className: 'grow' },
              React.createElement('div', { className: 'faint', style: { fontSize: 10.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', marginBottom: 6 } }, 'Escalate if'),
              React.createElement('div', { className: 'field', style: { width: '100%' } }, 'concession_pressure > 0.6', React.createElement(Icon, { d: 'chevDown' })))))),
      React.createElement('button', { className: 'btn btn-ghost', style: { alignSelf: 'flex-start' } }, React.createElement(Icon, { d: 'plus', size: 15 }), 'Add rebuttal rule'));
  }

  function Diff() {
    return React.createElement('div', { style: { maxWidth: 880 } },
      React.createElement('div', { className: 'row', style: { gap: 8, marginBottom: 12 } },
        React.createElement('span', { className: 'tag' }, 'Champion kb-37'), React.createElement(Icon, { d: 'arrowR', size: 14, style: { color: 'var(--text-3)' } }), React.createElement('span', { className: 'tag accent' }, 'Draft kb-37-d1'),
        React.createElement('span', { className: 'faint', style: { fontSize: 12, marginLeft: 'auto' } }, '+3 lines · −2 lines')),
      React.createElement('div', { className: 'card', style: { overflow: 'hidden' } },
        React.createElement('div', { className: 'card-head' }, React.createElement(Icon, { d: 'note', size: 15 }), React.createElement('h3', { style: { fontSize: 13 } }, 'Rule 3 · Price objection')),
        React.createElement('div', { style: { fontFamily: 'var(--font-mono)', fontSize: 12.5 } },
          DIFF.map((d, i) =>
            React.createElement('div', { key: i, style: {
              padding: '6px 16px', lineHeight: 1.5,
              background: d.type === 'add' ? 'var(--ok-soft)' : d.type === 'del' ? 'var(--danger-soft)' : 'transparent',
              color: d.type === 'add' ? 'var(--ok)' : d.type === 'del' ? 'var(--danger)' : 'var(--text-2)',
              borderLeft: '2px solid ' + (d.type === 'add' ? 'var(--ok)' : d.type === 'del' ? 'var(--danger)' : 'transparent'),
            } },
              React.createElement('span', { style: { opacity: .6, marginRight: 10, userSelect: 'none' } }, d.type === 'add' ? '+' : d.type === 'del' ? '−' : ' '),
              d.text)))));
  }

  (window.CadencePages = window.CadencePages || {}).kb = Kb;
})();
