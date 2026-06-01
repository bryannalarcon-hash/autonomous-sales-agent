/* CADENCE — app shell: nav, global top bar, hash router.
   Pages register themselves on window.CadencePages[id] = Component.
   Navigate with window.cadenceGo(route, params). */
(function () {
  const { useState, useEffect } = React;
  const Icon = window.Icon;

  const ROUTES = {
    live:        { label: 'Live',           title: 'Live Call Monitor',   group: 'operate', icon: 'broadcast', live: true },
    calls:       { label: 'Calls',          title: 'Calls List',          group: 'operate', icon: 'list' },
    kpi:         { label: 'KPI Views',      title: 'KPI Views',           group: 'operate', icon: 'chart' },
    escalations: { label: 'Escalations',    title: 'Escalation Queue',    group: 'operate', icon: 'alert', badge: 3 },
    review:      { label: 'Call Review',    title: 'Call Review',         group: 'operate', icon: 'eye', hidden: true },
    lab:         { label: 'Experiment Lab', title: 'Experiment Lab',      group: 'improve', icon: 'flask' },
    approvals:   { label: 'Approvals',      title: 'Approval Queue',      group: 'improve', icon: 'badge', badge: 2, amber: true },
    kb:          { label: 'KB / Playbook',  title: 'KB / Playbook Editor',group: 'improve', icon: 'book' },
    versions:    { label: 'Versions',       title: 'Version History',     group: 'improve', icon: 'branch' },
  };
  const OPERATE = ['live', 'calls', 'kpi', 'escalations'];
  const IMPROVE = ['lab', 'approvals', 'kb', 'versions'];

  function parseHash() {
    const h = (location.hash || '#live').replace(/^#/, '');
    const [route, qs] = h.split('?');
    const params = {};
    if (qs) qs.split('&').forEach(kv => { const [k, v] = kv.split('='); params[k] = decodeURIComponent(v || ''); });
    return { route: ROUTES[route] ? route : 'live', params };
  }
  window.cadenceGo = (route, params) => {
    let h = '#' + route;
    if (params && Object.keys(params).length) h += '?' + Object.entries(params).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
    location.hash = h;
  };

  function useRoute() {
    const [r, setR] = useState(parseHash());
    useEffect(() => {
      const on = () => { setR(parseHash()); document.querySelector('.page-scroll')?.scrollTo(0, 0); };
      window.addEventListener('hashchange', on);
      return () => window.removeEventListener('hashchange', on);
    }, []);
    return r;
  }

  /* ----- global top-bar controls (always visible) ----- */
  function GlobalControls() {
    const [live, setLive] = useState(false);
    return (
      React.createElement(React.Fragment, null,
        React.createElement('button', { className: 'gctl', onClick: () => window.cadenceGo('versions'), title: 'Champion version — open Version History' },
          React.createElement(Icon, { d: 'shield', size: 15 }),
          React.createElement('span', null, 'Champion ', React.createElement('b', null, 'v12')),
          React.createElement('span', { className: 'muted' }, 'kb-37'),
          React.createElement(Icon, { d: 'chevDown', className: 'gctl-chev' })),
        React.createElement('div', { className: 'gctl', title: 'Active persona / voice' },
          React.createElement(Icon, { d: 'mic', size: 15 }),
          React.createElement('span', null, React.createElement('b', null, 'Ava'), ' · Warm-Direct')),
        React.createElement('button', {
          className: 'env' + (live ? ' live' : ''), onClick: () => setLive(v => !v),
          title: 'Toggle environment',
        }, React.createElement('i', null), live ? 'LIVE' : 'SANDBOX'))
    );
  }

  function NavItem({ id, active }) {
    const r = ROUTES[id];
    return React.createElement('button', { className: 'nav-item' + (active ? ' on' : ''), onClick: () => window.cadenceGo(id) },
      React.createElement(Icon, { d: r.icon, size: 18 }),
      React.createElement('span', null, r.label),
      r.live ? React.createElement('span', { className: 'live-dot' })
        : r.badge ? React.createElement('span', { className: 'nav-badge' + (r.amber ? ' amber' : '') }, r.badge) : null);
  }

  function Shell() {
    const { route, params } = useRoute();
    const meta = ROUTES[route];
    const mode = meta.group;
    const Page = (window.CadencePages || {})[route];

    return (
      React.createElement('div', { className: 'app' },
        /* NAV */
        React.createElement('aside', { className: 'nav' },
          React.createElement('div', { className: 'brand' },
            React.createElement('div', { className: 'brand-mark' },
              React.createElement(Icon, { d: 'pulse', size: 19, sw: 2.4 })),
            React.createElement('div', { className: 'brand-word' }, 'Cadence')),
          React.createElement('div', { className: 'mode' },
            React.createElement('button', { className: mode === 'operate' ? 'on' : '', onClick: () => window.cadenceGo('live') },
              React.createElement(Icon, { d: 'broadcast', size: 15 }), 'Operate'),
            React.createElement('button', { className: mode === 'improve' ? 'on' : '', onClick: () => window.cadenceGo('lab') },
              React.createElement(Icon, { d: 'flask', size: 15 }), 'Improve')),
          React.createElement('div', { className: 'nav-group' }, 'Operate'),
          React.createElement('div', { className: 'nav-list' }, OPERATE.map(id => React.createElement(NavItem, { key: id, id, active: route === id || (route === 'review' && id === 'calls') }))),
          React.createElement('div', { className: 'nav-group' }, 'Improve'),
          React.createElement('div', { className: 'nav-list' }, IMPROVE.map(id => React.createElement(NavItem, { key: id, id, active: route === id }))),
          React.createElement('div', { className: 'nav-foot' },
            React.createElement('div', { className: 'avatar accent' }, 'OP'),
            React.createElement('div', { style: { lineHeight: 1.3 } },
              React.createElement('div', { style: { fontWeight: 650, fontSize: '13px' } }, 'Operator'),
              React.createElement('div', { style: { fontSize: '11px', color: 'var(--text-3)' } }, 'Solo workspace')))),

        /* MAIN */
        React.createElement('div', { className: 'main' },
          React.createElement('header', { className: 'topbar' },
            route === 'review'
              ? React.createElement('div', { className: 'crumb', onClick: () => window.cadenceGo('calls') },
                  React.createElement(Icon, { d: 'arrowR', size: 15, style: { transform: 'rotate(180deg)' } }), 'Calls')
              : null,
            React.createElement('div', { className: 'top-title' },
              meta.title,
              meta.live ? React.createElement('span', { className: 'live-pill' }, React.createElement('i', null), 'LIVE') : null,
              route === 'review' ? React.createElement('span', { className: 'top-sub' }, '#' + (params.call || 'CALL-4820')) : null),
            React.createElement('div', { className: 'top-spacer' }),
            React.createElement(GlobalControls, null),
            React.createElement('div', { className: 'vrule' }),
            React.createElement('div', { className: 'avatar sm' }, 'OP')),
          Page
            ? React.createElement(Page, { params })
            : React.createElement('div', { className: 'empty', style: { margin: 'auto' } }, 'Page “' + route + '” not found'))
      )
    );
  }

  window.CadenceShell = Shell;
})();
