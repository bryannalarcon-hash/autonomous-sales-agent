/* Cadence — Live Call Monitor (P1)
   A single, fully-themed screen. All color comes from CSS custom properties
   set per-artboard, so the same markup renders in every theme. */

(function () {
  const { useState } = React;

  /* ----------------------------- styles ----------------------------- */
  if (!document.getElementById('cm-styles')) {
    const s = document.createElement('style');
    s.id = 'cm-styles';
    s.textContent = `
    .cm-root{
      --r:18px; --r-sm:11px; --r-xs:8px;
      position:absolute; inset:0; display:grid; grid-template-columns:250px 1fr; grid-template-rows:100%;
      font-family:var(--font,"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif);
      background:var(--bg); color:var(--text);
      font-size:14px; line-height:1.45; -webkit-font-smoothing:antialiased;
      letter-spacing:-0.006em;
    }
    .cm-root *{box-sizing:border-box; scrollbar-width:none;}
    .cm-root *::-webkit-scrollbar{display:none;}
    .cm-root svg{width:18px; height:18px; flex:0 0 auto;}
    .cm-word,.cm-title,.cm-pname,.cm-bhead h3,.cm-thead h3,.cm-decard .big{font-family:var(--font-display,inherit);}

    /* ---- nav ---- */
    .cm-nav{background:var(--panel); border-right:1px solid var(--border); display:flex; flex-direction:column; padding:16px 14px; min-height:0;}
    .cm-brand{display:flex; align-items:center; gap:10px; padding:4px 6px 16px;}
    .cm-mark{width:30px; height:30px; border-radius:9px; background:var(--accent); display:flex; align-items:center; justify-content:center; flex:0 0 auto; box-shadow:0 2px 8px var(--accent-glow);}
    .cm-mark svg{width:17px; height:17px;}
    .cm-word{font-weight:680; font-size:16.5px; letter-spacing:-0.02em; background:var(--word-fill,var(--text)); -webkit-background-clip:text; background-clip:text; -webkit-text-fill-color:transparent;}
    .cm-mode{display:flex; background:var(--surface-2); border:1px solid var(--border); border-radius:11px; padding:3px; gap:3px; margin-bottom:18px;}
    .cm-mode button{flex:1; border:0; background:transparent; color:var(--text-3); font:inherit; font-weight:600; font-size:13px; padding:7px 0; border-radius:8px; cursor:pointer; letter-spacing:-0.01em;}
    .cm-mode button.on{background:var(--surface); color:var(--text); box-shadow:var(--shadow-sm);}
    .cm-grouplbl{font-size:10.5px; font-weight:700; letter-spacing:0.09em; text-transform:uppercase; color:var(--text-3); padding:0 8px; margin:14px 0 7px;}
    .cm-navlist{display:flex; flex-direction:column; gap:2px;}
    .cm-navitem{display:flex; align-items:center; gap:10px; padding:8px 10px; border-radius:10px; color:var(--text-2); cursor:pointer; font-weight:550; font-size:13.5px; position:relative;}
    .cm-navitem svg{width:18px; height:18px; flex:0 0 auto; opacity:.85;}
    .cm-navitem:hover{background:var(--surface-2);}
    .cm-navitem.on{background:var(--accent-soft); color:var(--accent-strong); font-weight:650;}
    .cm-navitem.on svg{opacity:1;}
    .cm-navitem.dim{opacity:.5;}
    .cm-navbadge{margin-left:auto; font-size:11px; font-weight:700; min-width:19px; height:19px; padding:0 6px; border-radius:7px; display:flex; align-items:center; justify-content:center; background:var(--danger-soft); color:var(--danger);}
    .cm-livedot{margin-left:auto; width:7px; height:7px; border-radius:50%; background:var(--danger); box-shadow:0 0 0 0 var(--danger); animation:cm-pulse 1.8s infinite;}
    @keyframes cm-pulse{0%{box-shadow:0 0 0 0 var(--danger-glow);}70%{box-shadow:0 0 0 7px transparent;}100%{box-shadow:0 0 0 0 transparent;}}
    .cm-profile{margin-top:auto; display:flex; align-items:center; gap:10px; padding:9px 8px; border-top:1px solid var(--border); margin-top:auto;}
    .cm-avatar{width:32px;height:32px;border-radius:9px;background:var(--surface-3);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:12.5px;color:var(--text-2);flex:0 0 auto;}

    /* ---- main ---- */
    .cm-main{display:flex; flex-direction:column; min-width:0; min-height:0;}
    .cm-top{height:62px; flex:0 0 auto; border-bottom:1px solid var(--border); background:var(--panel); display:flex; align-items:center; gap:14px; padding:0 20px;}
    .cm-title{font-size:18px; font-weight:680; letter-spacing:-0.02em;}
    .cm-livepill{display:inline-flex; align-items:center; gap:6px; background:var(--danger-soft); color:var(--danger); font-weight:700; font-size:11.5px; letter-spacing:0.04em; padding:4px 9px 4px 8px; border-radius:8px;}
    .cm-livepill i{width:7px;height:7px;border-radius:50%;background:var(--danger);animation:cm-blink 1.4s infinite;}
    @keyframes cm-blink{0%,100%{opacity:1;}50%{opacity:.35;}}
    .cm-callmeta{color:var(--text-3); font-size:13px; font-variant-numeric:tabular-nums;}
    .cm-spacer{flex:1;}
    .cm-chip{display:inline-flex; align-items:center; gap:7px; height:34px; padding:0 11px; border:1px solid var(--border); border-radius:10px; background:var(--surface); color:var(--text-2); font-size:12.5px; font-weight:550; cursor:pointer; white-space:nowrap;}
    .cm-chip svg{width:15px;height:15px;opacity:.8;}
    .cm-chip b{color:var(--text); font-weight:650;}
    .cm-chip .cm-sub{color:var(--text-3); font-size:11px;}
    .cm-env{display:inline-flex;align-items:center;gap:6px;height:34px;padding:0 11px;border-radius:10px;font-size:11.5px;font-weight:700;letter-spacing:0.03em;background:var(--warn-soft);color:var(--warn);border:1px solid var(--warn-border);}
    .cm-env i{width:6px;height:6px;border-radius:50%;background:var(--warn);}
    .cm-divider{width:1px;height:26px;background:var(--border);}

    /* ---- content ---- */
    .cm-body{flex:1; min-height:0; display:grid; grid-template-columns:1fr 376px; grid-template-rows:minmax(0,1fr); gap:16px; padding:16px 18px;}
    .cm-col{display:flex; flex-direction:column; gap:14px; min-height:0;}
    .cm-card{background:var(--surface); border:1px solid var(--border); border-radius:var(--r); box-shadow:var(--shadow);}

    /* prospect header */
    .cm-prospect{padding:15px 17px; display:flex; align-items:center; gap:14px;}
    .cm-pavatar{width:46px;height:46px;border-radius:13px;background:var(--accent-soft);color:var(--accent-strong);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px;flex:0 0 auto;}
    .cm-pname{font-weight:680; font-size:16px; letter-spacing:-0.015em;}
    .cm-pcompany{color:var(--text-3); font-size:12.5px; margin-top:1px;}
    .cm-tags{display:flex; gap:6px; margin-top:8px; flex-wrap:wrap;}
    .cm-tag{font-size:11px; font-weight:600; padding:3px 8px; border-radius:7px; background:var(--surface-2); color:var(--text-2); border:1px solid var(--border);}
    .cm-tag.accent{background:var(--accent-soft); color:var(--accent-strong); border-color:var(--accent-border);}
    .cm-pright{margin-left:auto; text-align:right; align-self:flex-start;}
    .cm-dur{font-size:22px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-0.02em;}
    .cm-vtag{font-size:11px; color:var(--text-3); margin-top:2px; font-variant-numeric:tabular-nums;}

    /* transcript */
    .cm-tcard{flex:1; min-height:0; display:flex; flex-direction:column; overflow:hidden; position:relative;}
    .cm-thead{display:flex; align-items:center; gap:10px; padding:13px 17px; border-bottom:1px solid var(--border); flex:0 0 auto;}
    .cm-thead h3{font-size:13px; font-weight:680; letter-spacing:-0.01em;}
    .cm-autoscroll{margin-left:auto; display:inline-flex; align-items:center; gap:6px; font-size:11.5px; color:var(--text-3); font-weight:550;}
    .cm-toggle{width:30px;height:17px;border-radius:9px;background:var(--accent);position:relative;}
    .cm-toggle i{position:absolute;top:2px;right:2px;width:13px;height:13px;border-radius:50%;background:#fff;}
    .cm-stream{flex:1; min-height:0; overflow:hidden; padding:16px 17px 22px; display:flex; flex-direction:column; gap:15px; justify-content:flex-end;}
    .cm-turn{display:flex; flex-direction:column; gap:5px; max-width:90%;}
    .cm-turn.p{align-self:flex-start;}
    .cm-turn.a{align-self:flex-end; align-items:flex-end; max-width:93%;}
    .cm-turnhead{display:flex; align-items:center; gap:7px; font-size:11px; color:var(--text-3); font-weight:600;}
    .cm-bubble{padding:10px 13px; border-radius:14px; font-size:13.5px; line-height:1.5;}
    .cm-turn.p .cm-bubble{background:var(--surface-2); border:1px solid var(--border); border-top-left-radius:5px; color:var(--text);}
    .cm-turn.a .cm-bubble{background:var(--accent); color:var(--accent-ink); border-top-right-radius:5px;}
    .cm-decision{display:flex; align-items:center; gap:8px; margin-top:2px; flex-wrap:wrap; justify-content:flex-end;}
    .cm-dchip{display:inline-flex;align-items:center;gap:6px;font-size:10.5px;font-weight:700;padding:3px 8px;border-radius:7px;background:var(--accent-soft);color:var(--accent-strong);border:1px solid var(--accent-border);letter-spacing:0.01em;}
    .cm-dchip svg{width:12px;height:12px;}
    .cm-rationale{font-size:11.5px; color:var(--text-3); font-style:italic; text-align:right;}
    .cm-lat{font-size:10.5px; color:var(--text-3); font-variant-numeric:tabular-nums; font-weight:600;}
    .cm-speaking{display:inline-flex; gap:4px; padding:11px 14px;}
    .cm-speaking i{width:7px;height:7px;border-radius:50%;background:var(--accent-ink);opacity:.7;animation:cm-dots 1.2s infinite;}
    .cm-speaking i:nth-child(2){animation-delay:.2s;} .cm-speaking i:nth-child(3){animation-delay:.4s;}
    @keyframes cm-dots{0%,60%,100%{transform:translateY(0);opacity:.4;}30%{transform:translateY(-4px);opacity:1;}}
    .cm-fade{position:absolute; left:1px; right:1px; top:48px; height:42px; background:linear-gradient(to bottom,var(--surface),transparent); pointer-events:none;}

    /* belief panel */
    .cm-belief{flex:1; min-height:0; display:flex; flex-direction:column; overflow:hidden;}
    .cm-bhead{display:flex; align-items:center; gap:9px; padding:9px 16px; border-bottom:1px solid var(--border);}
    .cm-bhead h3{font-size:13px;font-weight:680;}
    .cm-stagechip{margin-left:auto;font-size:11px;font-weight:650;padding:3px 9px;border-radius:7px;background:var(--surface-2);border:1px solid var(--border);color:var(--text-2);}
    .cm-bbody{padding:12px 16px; display:flex; flex-direction:column; gap:9px; overflow:hidden;}
    .cm-gauges{display:grid; grid-template-columns:1fr 1fr; gap:10px;}
    .cm-gauge{background:var(--surface-2); border:1px solid var(--border); border-radius:13px; padding:9px 11px;}
    .cm-gtop{display:flex; align-items:center; justify-content:space-between; font-size:11.5px; color:var(--text-3); font-weight:600;}
    .cm-gval{font-size:22px; font-weight:730; letter-spacing:-0.03em; font-variant-numeric:tabular-nums; margin-top:2px; display:flex; align-items:baseline; gap:6px;}
    .cm-trend{font-size:12px; font-weight:700;}
    .cm-trend.up{color:var(--ok);} .cm-trend.down{color:var(--ok);} .cm-trend.flat{color:var(--text-3);}
    .cm-track{height:6px; border-radius:4px; background:var(--surface-3); margin-top:7px; overflow:hidden;}
    .cm-trackfill{height:100%; border-radius:4px;}
    .cm-stagebox{background:var(--surface-2); border:1px solid var(--border); border-radius:13px; padding:9px 13px; display:flex; gap:14px;}
    .cm-stagebox .lbl{font-size:10.5px; color:var(--text-3); font-weight:650; text-transform:uppercase; letter-spacing:0.06em;}
    .cm-stagebox .val{font-size:13px; font-weight:650; margin-top:3px;}
    .cm-escal{display:flex; align-items:center; gap:11px; padding:9px 13px; border-radius:13px; background:var(--warn-soft); border:1px solid var(--warn-border);}
    .cm-escal .ico{width:30px;height:30px;border-radius:9px;background:var(--warn);display:flex;align-items:center;justify-content:center;flex:0 0 auto;}
    .cm-escal .ico svg{width:17px;height:17px;color:var(--warn-ink);}
    .cm-escal .t{font-size:12.5px; font-weight:700; color:var(--warn-strong);}
    .cm-escal .s{font-size:11px; color:var(--warn-strong); opacity:.8; margin-top:1px;}
    .cm-decard{border-radius:13px; padding:11px 13px; background:var(--accent-strong-bg); border:1px solid var(--accent-border);}
    .cm-decard .lbl{font-size:10.5px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--accent-strong);opacity:.9;}
    .cm-decard .big{font-size:17px; font-weight:720; letter-spacing:-0.02em; margin-top:4px; color:var(--accent-strong); display:flex; align-items:center; gap:8px;}
    .cm-decard .rat{font-size:12px; color:var(--text-2); margin-top:6px; line-height:1.45;}
    .cm-conf{display:inline-flex;align-items:center;gap:6px;margin-top:9px;font-size:11px;color:var(--text-3);font-weight:600;}
    .cm-confbar{width:54px;height:5px;border-radius:3px;background:var(--surface-3);overflow:hidden;}
    .cm-confbar i{display:block;height:100%;background:var(--accent);}

    .cm-expand{border-top:1px solid var(--border); padding:10px 16px 12px; display:flex; flex-direction:column; gap:7px; flex:1; min-height:0; overflow:hidden;}
    .cm-exhead{display:flex; align-items:center; gap:8px; font-size:11.5px; font-weight:700; color:var(--text-2); text-transform:uppercase; letter-spacing:0.05em;}
    .cm-exhead svg{width:14px;height:14px;margin-left:auto;}
    .cm-slot{display:grid; grid-template-columns:84px 1fr 34px; align-items:center; gap:10px; font-size:12px;}
    .cm-slot .sn{color:var(--text-2); font-weight:600;}
    .cm-slot .sv{text-align:right; font-variant-numeric:tabular-nums; font-weight:650; color:var(--text); font-size:11.5px;}
    .cm-sbar{height:5px;border-radius:3px;background:var(--surface-3);overflow:hidden;}
    .cm-sbar i{display:block;height:100%;border-radius:3px;background:var(--accent);}
    .cm-drivers{display:flex; flex-wrap:wrap; gap:6px;}
    .cm-driver{display:inline-flex;align-items:center;gap:5px;font-size:11px;font-weight:600;padding:4px 8px;border-radius:7px;background:var(--surface-2);border:1px solid var(--border);color:var(--text-2);}
    .cm-driver .ar{font-weight:800;}
    .cm-driver .ar.up{color:var(--danger);} .cm-driver .ar.dn{color:var(--ok);} .cm-driver .ar.fl{color:var(--text-3);}

    /* bottom action bar */
    .cm-actionbar{flex:0 0 auto; height:66px; border-top:1px solid var(--border); background:var(--panel); display:flex; align-items:center; gap:18px; padding:0 20px;}
    .cm-rec{display:inline-flex; align-items:center; gap:8px; font-size:12.5px; color:var(--text-2); font-weight:550;}
    .cm-rec .rd{width:9px;height:9px;border-radius:50%;background:var(--danger);animation:cm-blink 1.4s infinite;}
    .cm-tl{display:flex; align-items:center; gap:10px; min-width:300px;}
    .cm-tllabel{font-size:11px;font-weight:650;color:var(--text-3);white-space:nowrap;}
    .cm-tlbar{flex:1; height:9px; border-radius:5px; overflow:hidden; display:flex; background:var(--surface-3);}
    .cm-tlbar .ag{background:var(--accent);} .cm-tlbar .pr{background:var(--ok);}
    .cm-takeover{margin-left:auto; display:inline-flex; align-items:center; gap:9px; background:var(--btn); color:var(--btn-ink); border:1px solid var(--btn-border,transparent); font:inherit; font-weight:650; font-size:14px; padding:11px 20px; border-radius:12px; cursor:pointer; box-shadow:var(--shadow); letter-spacing:-0.01em;}
    .cm-takeover svg{width:17px;height:17px;}
    `;
    document.head.appendChild(s);
  }

  /* ----------------------------- icons ----------------------------- */
  const P = {
    broadcast: 'M4.9 4.9a10 10 0 000 14.2M19.1 4.9a10 10 0 010 14.2M7.8 7.8a6 6 0 000 8.4M16.2 7.8a6 6 0 010 8.4M12 11a1 1 0 100 2 1 1 0 000-2z',
    list: 'M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01',
    chart: 'M3 3v18h18M8 14v4M13 9v9M18 5v13',
    alert: 'M12 3l9 16H3l9-16zM12 10v4M12 17h.01',
    flask: 'M9 3h6M10 3v6L5 19a1.5 1.5 0 001.4 2h11.2A1.5 1.5 0 0019 19l-5-10V3M8 14h8',
    badge: 'M9 12l2 2 4-4M12 3l2.5 1.7L18 4l.3 3.5L21 9l-1.5 3L21 15l-2.7 1.5L18 20l-3.5-.7L12 21l-2.5-1.7L6 20l-.3-3.5L3 15l1.5-3L3 9l2.7-1.5L6 4l3.5.7z',
    book: 'M4 5a2 2 0 012-2h13v16H6a2 2 0 00-2 2V5zM4 19a2 2 0 012-2h13',
    branch: 'M6 4v12M6 4a2 2 0 100 4 2 2 0 000-4zM6 20a2 2 0 100-4 2 2 0 000 4zM18 8a2 2 0 100-4 2 2 0 000 4zM18 8a8 8 0 01-8 8',
    mic: 'M12 3a3 3 0 00-3 3v6a3 3 0 006 0V6a3 3 0 00-3-3zM5 11a7 7 0 0014 0M12 18v3',
    spark: 'M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M18 6l-2.5 2.5M8.5 15.5L6 18',
    pivot: 'M3 12a9 9 0 0115.5-6.3M21 12a9 9 0 01-15.5 6.3M18 4v4h-4M6 20v-4h4',
    hand: 'M8 11V5.5a1.5 1.5 0 013 0V11m0-1V4.5a1.5 1.5 0 013 0V11m0-.5V6a1.5 1.5 0 013 0v7a6 6 0 01-6 6h-1.2a4 4 0 01-2.9-1.2l-3.1-3.3a1.6 1.6 0 012.3-2.2L8 13.5V8a1.5 1.5 0 013 0',
    chevron: 'M6 9l6 6 6-6',
    shield: 'M12 3l8 3v6c0 4.4-3.2 7.6-8 9-4.8-1.4-8-4.6-8-9V6l8-3z',
  };
  function Icon({ d }) {
    return React.createElement('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.7, strokeLinecap: 'round', strokeLinejoin: 'round' },
      React.createElement('path', { d: P[d] }));
  }

  /* ----------------------------- data ----------------------------- */
  const TURNS = [
    { who: 'p', t: '03:31', text: "We're a small team — fifteen people. The per-seat pricing adds up fast." },
    { who: 'a', t: '03:38', text: "Fair — at fifteen seats it's a real line item. Most teams that size make it back the first month on recovered no-shows alone.", dec: 'Acknowledge · reframe-cost', rat: 'Validate the objection before reframing to ROI.', lat: '0.7s' },
    { who: 'p', t: '03:52', text: "Maybe. We've been burned by tools that overpromise and underdeliver." },
    { who: 'a', t: '04:01', text: "Completely understandable. That's why everyone starts on a 30-day pilot — no annual lock-in until you've seen the numbers yourself.", dec: 'Build trust · de-risk', rat: 'Skepticism spike → lower commitment with pilot.', lat: '0.9s' },
    { who: 'p', t: '04:10', text: "Okay, that's reassuring. How fast could we actually be up and running?" },
  ];
  const SLOTS = [
    { n: 'Budget', v: 0.58 }, { n: 'Authority', v: 0.82 }, { n: 'Need', v: 0.77 },
    { n: 'Timeline', v: 0.40 }, { n: 'Team size', v: 0.95 },
  ];
  const DRIVERS = [
    { n: 'Price sensitivity', ar: 'up' }, { n: 'Skepticism', ar: 'dn', note: 'easing' },
    { n: 'Urgency', ar: 'fl' }, { n: 'Rapport', ar: 'up' },
  ];

  /* ----------------------------- component ----------------------------- */
  function CallMonitor() {
    return (
      React.createElement('div', { className: 'cm-root' },
        /* NAV */
        React.createElement('aside', { className: 'cm-nav' },
          React.createElement('div', { className: 'cm-brand' },
            React.createElement('div', { className: 'cm-mark' },
              React.createElement('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: '#fff', strokeWidth: 2, strokeLinecap: 'round', strokeLinejoin: 'round' },
                React.createElement('path', { d: 'M3 12h3l2.5-7 5 14L18 8l1 4h2' }))),
            React.createElement('div', { className: 'cm-word' }, 'Cadence')),
          React.createElement('div', { className: 'cm-mode' },
            React.createElement('button', { className: 'on' }, 'Operate'),
            React.createElement('button', null, 'Improve')),
          React.createElement('div', { className: 'cm-grouplbl' }, 'Operate'),
          React.createElement('div', { className: 'cm-navlist' },
            React.createElement('div', { className: 'cm-navitem on' }, React.createElement(Icon, { d: 'broadcast' }), 'Live', React.createElement('span', { className: 'cm-livedot' })),
            React.createElement('div', { className: 'cm-navitem' }, React.createElement(Icon, { d: 'list' }), 'Calls'),
            React.createElement('div', { className: 'cm-navitem' }, React.createElement(Icon, { d: 'chart' }), 'KPI Views'),
            React.createElement('div', { className: 'cm-navitem' }, React.createElement(Icon, { d: 'alert' }), 'Escalations', React.createElement('span', { className: 'cm-navbadge' }, '3'))),
          React.createElement('div', { className: 'cm-grouplbl' }, 'Improve'),
          React.createElement('div', { className: 'cm-navlist' },
            React.createElement('div', { className: 'cm-navitem dim' }, React.createElement(Icon, { d: 'flask' }), 'Experiment Lab'),
            React.createElement('div', { className: 'cm-navitem dim' }, React.createElement(Icon, { d: 'badge' }), 'Approvals', React.createElement('span', { className: 'cm-navbadge' }, '2')),
            React.createElement('div', { className: 'cm-navitem dim' }, React.createElement(Icon, { d: 'book' }), 'KB / Playbook'),
            React.createElement('div', { className: 'cm-navitem dim' }, React.createElement(Icon, { d: 'branch' }), 'Versions')),
          React.createElement('div', { className: 'cm-profile' },
            React.createElement('div', { className: 'cm-avatar' }, 'OP'),
            React.createElement('div', null,
              React.createElement('div', { style: { fontWeight: 650, fontSize: '13px' } }, 'Operator'),
              React.createElement('div', { style: { fontSize: '11px', color: 'var(--text-3)' } }, 'Solo workspace')))),

        /* MAIN */
        React.createElement('div', { className: 'cm-main' },
          /* top bar */
          React.createElement('header', { className: 'cm-top' },
            React.createElement('div', { className: 'cm-title' }, 'Live Call Monitor'),
            React.createElement('span', { className: 'cm-livepill' }, React.createElement('i', null), 'LIVE'),
            React.createElement('span', { className: 'cm-callmeta' }, '#CALL-4821 · 04:12'),
            React.createElement('div', { className: 'cm-spacer' }),
            React.createElement('span', { className: 'cm-env' }, React.createElement('i', null), 'SANDBOX'),
            React.createElement('div', { className: 'cm-chip' }, React.createElement(Icon, { d: 'shield' }), React.createElement('span', null, 'Champion ', React.createElement('b', null, 'v12')), React.createElement('span', { className: 'cm-sub' }, 'kb-37'), React.createElement(Icon, { d: 'chevron' })),
            React.createElement('div', { className: 'cm-chip' }, React.createElement(Icon, { d: 'mic' }), React.createElement('span', null, React.createElement('b', null, 'Ava'), ' · Warm-Direct')),
            React.createElement('div', { className: 'cm-divider' }),
            React.createElement('div', { className: 'cm-avatar' }, 'OP')),

          /* body */
          React.createElement('div', { className: 'cm-body' },
            /* left column */
            React.createElement('div', { className: 'cm-col' },
              React.createElement('div', { className: 'cm-card cm-prospect' },
                React.createElement('div', { className: 'cm-pavatar' }, 'JA'),
                React.createElement('div', null,
                  React.createElement('div', { className: 'cm-pname' }, 'Jordan Avery'),
                  React.createElement('div', { className: 'cm-pcompany' }, 'Northwind Logistics'),
                  React.createElement('div', { className: 'cm-tags' },
                    React.createElement('span', { className: 'cm-tag accent' }, 'Skeptical Analyzer'),
                    React.createElement('span', { className: 'cm-tag' }, 'Inbound · SMB'),
                    React.createElement('span', { className: 'cm-tag' }, 'Web-voice'))),
                React.createElement('div', { className: 'cm-pright' },
                  React.createElement('div', { className: 'cm-dur' }, '04:12'),
                  React.createElement('div', { className: 'cm-vtag' }, 'v12 · kb-37'))),

              React.createElement('div', { className: 'cm-card cm-tcard' },
                React.createElement('div', { className: 'cm-thead' },
                  React.createElement('h3', null, 'Transcript'),
                  React.createElement('span', { className: 'cm-autoscroll' }, 'Auto-scroll', React.createElement('span', { className: 'cm-toggle' }, React.createElement('i', null)))),
                React.createElement('div', { className: 'cm-stream' },
                  TURNS.map((t, i) =>
                    React.createElement('div', { className: 'cm-turn ' + t.who, key: i },
                      React.createElement('div', { className: 'cm-turnhead' }, t.who === 'a' ? 'Ava (agent)' : 'Jordan', '·', t.t),
                      React.createElement('div', { className: 'cm-bubble' }, t.text),
                      t.dec && React.createElement('div', { className: 'cm-decision' },
                        React.createElement('span', { className: 'cm-dchip' }, React.createElement(Icon, { d: 'spark' }), t.dec),
                        React.createElement('span', { className: 'cm-lat' }, t.lat)),
                      t.rat && React.createElement('div', { className: 'cm-rationale' }, '“' + t.rat + '”'))),
                  React.createElement('div', { className: 'cm-turn a', key: 'speaking' },
                    React.createElement('div', { className: 'cm-turnhead' }, 'Ava (agent)', '·', 'now'),
                    React.createElement('div', { className: 'cm-bubble cm-speaking' }, React.createElement('i', null), React.createElement('i', null), React.createElement('i', null)),
                    React.createElement('div', { className: 'cm-decision' },
                      React.createElement('span', { className: 'cm-dchip' }, React.createElement(Icon, { d: 'pivot' }), 'Pivot · ROI-proof')))),
                React.createElement('div', { className: 'cm-fade' }))),

            /* right column — belief state */
            React.createElement('div', { className: 'cm-col' },
              React.createElement('div', { className: 'cm-card cm-belief' },
                React.createElement('div', { className: 'cm-bhead' },
                  React.createElement(Icon, { d: 'spark' }),
                  React.createElement('h3', null, 'Belief State'),
                  React.createElement('span', { className: 'cm-stagechip' }, 'Objection Handling')),
                React.createElement('div', { className: 'cm-bbody' },
                  React.createElement('div', { className: 'cm-gauges' },
                    React.createElement('div', { className: 'cm-gauge' },
                      React.createElement('div', { className: 'cm-gtop' }, 'Trust', React.createElement('span', { className: 'cm-trend up' }, '↑ +.08')),
                      React.createElement('div', { className: 'cm-gval' }, '0.66'),
                      React.createElement('div', { className: 'cm-track' }, React.createElement('div', { className: 'cm-trackfill', style: { width: '66%', background: 'var(--ok)' } }))),
                    React.createElement('div', { className: 'cm-gauge' },
                      React.createElement('div', { className: 'cm-gtop' }, 'Bail risk', React.createElement('span', { className: 'cm-trend down' }, '↓ −.11')),
                      React.createElement('div', { className: 'cm-gval' }, '0.34'),
                      React.createElement('div', { className: 'cm-track' }, React.createElement('div', { className: 'cm-trackfill', style: { width: '34%', background: 'var(--warn)' } })))),
                  React.createElement('div', { className: 'cm-stagebox' },
                    React.createElement('div', null,
                      React.createElement('div', { className: 'lbl' }, 'Stage'),
                      React.createElement('div', { className: 'val' }, 'Objection Handling')),
                    React.createElement('div', { style: { borderLeft: '1px solid var(--border)', paddingLeft: '14px' } },
                      React.createElement('div', { className: 'lbl' }, 'Last act'),
                      React.createElement('div', { className: 'val' }, 'Rebuttal · price→ROI'))),
                  React.createElement('div', { className: 'cm-escal' },
                    React.createElement('div', { className: 'ico' }, React.createElement(Icon, { d: 'alert' })),
                    React.createElement('div', null,
                      React.createElement('div', { className: 't' }, 'Escalation · Armed'),
                      React.createElement('div', { className: 's' }, 'Watching concession pressure'))),
                  React.createElement('div', { className: 'cm-decard' },
                    React.createElement('div', { className: 'lbl' }, 'Current decision'),
                    React.createElement('div', { className: 'big' }, React.createElement(Icon, { d: 'pivot' }), 'Pivot → ROI proof'),
                    React.createElement('div', { className: 'rat' }, 'Price raised twice; trust recovering — lead with value before re-close.'),
                    React.createElement('div', { className: 'cm-conf' }, 'Confidence', React.createElement('span', { className: 'cm-confbar' }, React.createElement('i', { style: { width: '81%' } })), '0.81'))),
                React.createElement('div', { className: 'cm-expand' },
                  React.createElement('div', { className: 'cm-exhead' }, 'Full belief state', React.createElement(Icon, { d: 'chevron' })),
                  SLOTS.map((s, i) =>
                    React.createElement('div', { className: 'cm-slot', key: i },
                      React.createElement('span', { className: 'sn' }, s.n),
                      React.createElement('span', { className: 'cm-sbar' }, React.createElement('i', { style: { width: (s.v * 100) + '%', background: s.v < 0.5 ? 'var(--warn)' : 'var(--accent)' } })),
                      React.createElement('span', { className: 'sv' }, s.v.toFixed(2)))),
                  React.createElement('div', { className: 'cm-drivers' },
                    DRIVERS.map((d, i) =>
                      React.createElement('span', { className: 'cm-driver', key: i }, d.n,
                        React.createElement('span', { className: 'ar ' + d.ar }, d.ar === 'up' ? '↑' : d.ar === 'dn' ? '↓' : '→'),
                        d.note && React.createElement('span', { style: { color: 'var(--text-3)', fontWeight: 500 } }, d.note)))))))),

          /* bottom action bar */
          React.createElement('div', { className: 'cm-actionbar' },
            React.createElement('span', { className: 'cm-rec' }, React.createElement('span', { className: 'rd' }), 'Recording · Turn 14 · synced 0.8s ago'),
            React.createElement('div', { className: 'cm-tl' },
              React.createElement('span', { className: 'cm-tllabel' }, 'Talk / Listen'),
              React.createElement('div', { className: 'cm-tlbar' },
                React.createElement('div', { className: 'ag', style: { width: '44%' } }),
                React.createElement('div', { className: 'pr', style: { width: '56%' } })),
              React.createElement('span', { className: 'cm-tllabel' }, '44 / 56')),
            React.createElement('button', { className: 'cm-takeover' }, React.createElement(Icon, { d: 'hand' }), 'Take over'))))
    );
  }

  window.CadenceCallMonitor = CallMonitor;
})();
