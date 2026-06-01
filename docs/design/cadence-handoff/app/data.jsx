/* CADENCE — mock data shared across pages. window.DB */
(function () {
  const VERSIONS = [
    { id: 'v12', label: 'v12', kb: 'kb-37', champion: true, parent: 'v10', created: 'May 24', persona: 'Warm-Direct', note: 'ROI-first objection map', ladder: 3.42, enroll: 0.61, qual: 0.93, guardrail: 'pass', calls: 1284, diff: '+ ROI rebuttal before re-close' },
    { id: 'v11', label: 'v11', kb: 'kb-36', champion: false, parent: 'v10', created: 'May 21', persona: 'Warm-Direct', note: 'Faster pivot threshold', ladder: 3.18, enroll: 0.55, qual: 0.9, guardrail: 'pass', calls: 642, diff: '− pivot threshold 0.55→0.42' },
    { id: 'v10', label: 'v10', kb: 'kb-35', champion: false, parent: 'v7', created: 'May 16', persona: 'Warm-Direct', note: 'Discovery depth +1', ladder: 3.05, enroll: 0.52, qual: 0.89, guardrail: 'pass', calls: 2103, diff: '+ extra SPIN implication Q' },
    { id: 'v9', label: 'v9', kb: 'kb-34', champion: false, parent: 'v7', created: 'May 12', persona: 'Brisk-Expert', note: 'Brisk persona trial', ladder: 2.71, enroll: 0.41, qual: 0.84, guardrail: 'warn', calls: 510, diff: '~ persona Warm→Brisk' },
    { id: 'v7', label: 'v7', kb: 'kb-31', champion: false, parent: 'v4', created: 'May 5', persona: 'Warm-Direct', note: 'Baseline rebuild', ladder: 2.66, enroll: 0.39, qual: 0.83, guardrail: 'pass', calls: 3320, diff: '+ rebuilt close triggers' },
    { id: 'v4', label: 'v4', kb: 'kb-22', champion: false, parent: 'v1', created: 'Apr 27', persona: 'Warm-Direct', note: 'First objection map', ladder: 2.4, enroll: 0.33, qual: 0.79, guardrail: 'pass', calls: 1870, diff: '+ 9-objection rebuttal set' },
    { id: 'v1', label: 'v1', kb: 'kb-08', champion: false, parent: null, created: 'Apr 18', persona: 'Neutral', note: 'Genesis', ladder: 1.9, enroll: 0.24, qual: 0.7, guardrail: 'warn', calls: 980, diff: 'initial config' },
  ];

  const CALLS = [
    { id: 'CALL-4821', who: 'Jordan Avery', co: 'Northwind Logistics', persona: 'Skeptical Analyzer', when: 'Live now', dur: '04:12', outcome: 'In progress', tier: '—', ver: 'v12', kb: 'kb-37', qualified: null, escalated: false, channel: 'Web-voice', live: true },
    { id: 'CALL-4820', who: 'Priya Nair', co: 'Cedar & Co', persona: 'Busy Decider', when: '6 min ago', dur: '05:48', outcome: 'Enrolled', tier: 'T3 · Same-call', ver: 'v12', kb: 'kb-37', qualified: true, escalated: false, channel: 'Web-voice' },
    { id: 'CALL-4819', who: 'Marcus Bell', co: 'Halden Group', persona: 'Skeptical Analyzer', when: '22 min ago', dur: '07:10', outcome: 'Escalated', tier: 'T2 · Booked', ver: 'v12', kb: 'kb-37', qualified: true, escalated: true, channel: 'Web-voice' },
    { id: 'CALL-4818', who: 'Dana Liu', co: 'Brightpath', persona: 'Price Hawk', when: '38 min ago', dur: '03:02', outcome: 'Disqualified', tier: 'T0', ver: 'v12', kb: 'kb-37', qualified: false, escalated: false, channel: 'Text' },
    { id: 'CALL-4817', who: 'Sam Okafor', co: 'Verge Studio', persona: 'Warm Champion', when: '51 min ago', dur: '06:25', outcome: 'Enrolled', tier: 'T3 · Same-call', ver: 'v12', kb: 'kb-37', qualified: true, escalated: false, channel: 'Web-voice' },
    { id: 'CALL-4816', who: 'Elena Voss', co: 'Atlas Freight', persona: 'Busy Decider', when: '1 hr ago', dur: '04:40', outcome: 'Booked', tier: 'T2 · Booked', ver: 'v11', kb: 'kb-36', qualified: true, escalated: false, channel: 'Web-voice' },
    { id: 'CALL-4815', who: 'Theo Park', co: 'Lumen Health', persona: 'Skeptical Analyzer', when: '1 hr ago', dur: '08:55', outcome: 'Escalated', tier: 'T1 · Interested', ver: 'v11', kb: 'kb-36', qualified: false, escalated: true, channel: 'Web-voice' },
    { id: 'CALL-4814', who: 'Rosa Mendez', co: 'Kettle & Stone', persona: 'Price Hawk', when: '2 hr ago', dur: '02:38', outcome: 'No-interest', tier: 'T0', ver: 'v11', kb: 'kb-36', qualified: false, escalated: false, channel: 'Text' },
    { id: 'CALL-4813', who: 'Will Tanaka', co: 'Orbit Media', persona: 'Warm Champion', when: '2 hr ago', dur: '05:12', outcome: 'Enrolled', tier: 'T3 · Same-call', ver: 'v12', kb: 'kb-37', qualified: true, escalated: false, channel: 'Web-voice' },
    { id: 'CALL-4812', who: 'Aisha Rahman', co: 'Pinnacle Realty', persona: 'Busy Decider', when: '3 hr ago', dur: '04:01', outcome: 'Booked', tier: 'T2 · Booked', ver: 'v12', kb: 'kb-37', qualified: true, escalated: false, channel: 'Web-voice' },
    { id: 'CALL-4811', who: 'Greg Holt', co: 'Summit Tools', persona: 'Price Hawk', when: '3 hr ago', dur: '06:48', outcome: 'Escalated', tier: 'T1 · Interested', ver: 'v12', kb: 'kb-37', qualified: false, escalated: true, channel: 'Web-voice' },
    { id: 'CALL-4810', who: 'Nina Coste', co: 'Fjord Apps', persona: 'Warm Champion', when: '4 hr ago', dur: '05:33', outcome: 'Enrolled', tier: 'T3 · Same-call', ver: 'v12', kb: 'kb-37', qualified: true, escalated: false, channel: 'Web-voice' },
  ];

  // transcript for CALL-4821 / review
  const TRANSCRIPT = [
    { who: 'a', t: '00:04', text: "Hi Jordan — thanks for hopping on. I saw Northwind runs a lean ops team. What pulled you in today?", dec: 'Open · discovery', rat: 'Warm open, surface the trigger.', lat: '0.6s', stage: 'Discovery', trust: .42, bail: .28 },
    { who: 'p', t: '00:18', text: "We're drowning in missed follow-ups. Leads go cold before anyone calls them back." },
    { who: 'a', t: '00:27', text: "That's the exact gap this closes — every inbound gets called in under a minute, day or night. How many leads a week roughly slip?", dec: 'Quantify pain', rat: 'SPIN implication — size the problem.', lat: '0.7s', stage: 'Discovery', trust: .5, bail: .24 },
    { who: 'p', t: '00:42', text: "Maybe forty? Hard to say. Look, we're a small team — fifteen people. Per-seat pricing adds up fast." },
    { who: 'a', t: '00:54', text: "Fair — at fifteen seats it's a real line item. Most teams that size make it back the first month on recovered no-shows alone.", dec: 'Acknowledge · reframe-cost', rat: 'Validate before reframing to ROI.', lat: '0.7s', stage: 'Objection Handling', trust: .54, bail: .33 },
    { who: 'p', t: '01:08', text: "Maybe. We've been burned by tools that overpromise and underdeliver." },
    { who: 'a', t: '01:17', text: "Completely understandable. That's why everyone starts on a 30-day pilot — no annual lock-in until you've seen the numbers yourself.", dec: 'Build trust · de-risk', rat: 'Skepticism spike → lower commitment.', lat: '0.9s', stage: 'Objection Handling', trust: .58, bail: .41 },
    { who: 'p', t: '01:30', text: "Okay, that's reassuring. How fast could we actually be up and running?" },
    { who: 'a', t: '01:38', text: "Same day. We import your lead source, you pick a voice, and it's live in about twenty minutes. Want me to set the pilot up now?", dec: 'Pivot → ROI proof · trial-close', rat: 'Buying signal — move to close.', lat: '0.8s', stage: 'Closing', trust: .66, bail: .34 },
  ];

  const ESCALATIONS = [
    { id: 'ESC-204', call: 'CALL-4819', who: 'Marcus Bell', co: 'Halden Group', reason: 'Pricing concession', moment: 'Prospect demanded 30% off or walk; agent deferred rather than discount.', sev: 'high', when: '22 min ago', state: 'unreviewed', ver: 'v12' },
    { id: 'ESC-203', call: 'CALL-4815', who: 'Theo Park', co: 'Lumen Health', reason: 'Human requested', moment: '“Can I talk to a real person?” — agent acknowledged and deferred to queue.', sev: 'med', when: '1 hr ago', state: 'unreviewed', ver: 'v11' },
    { id: 'ESC-202', call: 'CALL-4811', who: 'Greg Holt', co: 'Summit Tools', reason: 'Compliance', moment: 'Prospect asked for a written earnings guarantee; agent declined per policy and flagged.', sev: 'high', when: '3 hr ago', state: 'unreviewed', ver: 'v12' },
    { id: 'ESC-201', call: 'CALL-4802', who: 'Ivan Petrov', co: 'Delta Stone', reason: 'Pricing concession', moment: 'Repeated discount pressure; recurring across 4 calls this week.', sev: 'med', when: 'Yesterday', state: 'reviewed', ver: 'v12' },
    { id: 'ESC-200', call: 'CALL-4790', who: 'Mae Lin', co: 'Harbor Co', reason: 'Human requested', moment: 'Asked for manager callback; resolved — callback booked.', sev: 'low', when: 'Yesterday', state: 'resolved', ver: 'v11' },
  ];

  const EXPERIMENTS = [
    { id: 'EXP-31', name: 'ROI-first rebuttal ordering', champ: 'v12', chal: 'v12-c2', state: 'running', pop: 'Skeptical Analyzer · 20%', n: 184, target: 400, delta: { enroll: +0.04, ladder: +0.12 }, ci: '[+0.01, +0.07]', sig: 0.91, guardrail: 'pass', diff: 'Reorder: lead ROI proof before pilot de-risk' },
    { id: 'EXP-30', name: 'Lower pivot threshold 0.42', champ: 'v12', chal: 'v13-c1', state: 'result-ready', pop: 'All · 25%', n: 412, target: 400, delta: { enroll: +0.06, ladder: +0.21 }, ci: '[+0.03, +0.09]', sig: 0.98, guardrail: 'pass', diff: 'pivot threshold 0.55 → 0.42', promote: 'auto' },
    { id: 'EXP-29', name: 'Aggressive same-call close', champ: 'v12', chal: 'v13-c2', state: 'blocked', pop: 'Warm Champion · 15%', n: 220, target: 300, delta: { enroll: +0.11, ladder: +0.18 }, ci: '[+0.05, +0.17]', sig: 0.95, guardrail: 'trip', diff: 'Add 2nd close attempt + urgency line', reason: 'Pushiness guardrail tripped (0.34 > 0.25)' },
    { id: 'EXP-28', name: 'Brisk-Expert persona', champ: 'v12', chal: 'v9', state: 'failed', pop: 'All · 20%', n: 510, target: 500, delta: { enroll: -0.14, ladder: -0.34 }, ci: '[-0.19, -0.09]', sig: 0.97, guardrail: 'warn', diff: 'persona Warm-Direct → Brisk-Expert' },
    { id: 'EXP-27', name: 'Extra discovery implication Q', champ: 'v10', chal: 'v11', state: 'retired', pop: 'All · 30%', n: 980, target: 900, delta: { enroll: +0.03, ladder: +0.13 }, ci: '[+0.01, +0.06]', sig: 0.93, guardrail: 'pass', diff: '+ SPIN implication question in discovery' },
  ];

  const APPROVALS = [
    { id: 'APR-12', exp: 'EXP-29', name: 'Aggressive same-call close', reason: 'Pushiness tripwire', chal: 'v13-c2', delta: { enroll: +0.11, ladder: +0.18 }, guardrail: 'Pushiness 0.34 (cap 0.25)', when: '20 min ago', detail: 'Adds a second close attempt with an urgency line. Lifts enrollment but pushiness score breaches the 0.25 guardrail on 3 archetypes.' },
    { id: 'APR-11', exp: 'EXP-33', name: 'Pilot price-floor concession', reason: 'Pricing concession', chal: 'v13-c3', delta: { enroll: +0.08, ladder: +0.09 }, guardrail: 'Allows 15% pilot discount', when: '2 hr ago', detail: 'Permits the agent to offer up to 15% off the pilot when bail_risk > 0.6. Touches pricing policy — requires sign-off.' },
  ];

  // KPI aggregates for v12 (single) and deltas vs v11
  const KPI = {
    primary: [
      { k: 'Weighted-ladder score', v: '3.42', u: '/5', delta: '+0.24', dir: 'up', spark: [3.05, 3.1, 3.18, 3.2, 3.3, 3.38, 3.42], ic: 'sigma' },
      { k: 'Same-call enrollment', v: '61', u: '%', delta: '+6 pts', dir: 'up', spark: [52, 53, 55, 56, 58, 60, 61], ic: 'bolt' },
      { k: 'Qualification accuracy', v: '93', u: '%', delta: '+3 pts', dir: 'up', spark: [89, 89, 90, 91, 92, 92, 93], ic: 'target' },
      { k: 'Guardrail status', v: 'Pass', u: '', delta: '0 trips', dir: 'flat', spark: [1, 1, 1, 1, 1, 1, 1], ic: 'shield', good: true },
    ],
    ladder: [
      { t: 'T3 · Same-call enroll', v: 61, c: 'var(--ok)' },
      { t: 'T2 · Booked', v: 18, c: 'var(--accent)' },
      { t: 'T1 · Interested', v: 9, c: 'var(--info)' },
      { t: 'T0 · No-interest', v: 8, c: 'var(--text-3)' },
      { t: 'DQ · Disqualified', v: 4, c: 'var(--danger)' },
    ],
    objection: [
      { t: 'Price', rate: 0.72 }, { t: 'Trust / risk', rate: 0.81 }, { t: 'Timing', rate: 0.58 },
      { t: 'Authority', rate: 0.66 }, { t: 'Competitor', rate: 0.69 }, { t: 'Feature gap', rate: 0.54 },
    ],
    secondary: [
      { k: 'Objection recovery', v: '68%', sub: 'overall · 6 types' },
      { k: 'Escalation rate', v: '4.2%', sub: '54 of 1284' },
      { k: 'Disqualification rate', v: '7.1%', sub: 'mostly budget' },
      { k: 'Talk / listen', v: '44 / 56', sub: 'agent / prospect' },
      { k: 'Avg turns', v: '17.4', sub: 'per call' },
      { k: 'Avg duration', v: '5:21', sub: 'per call' },
      { k: 'Discovery completeness', v: '82%', sub: 'slots filled' },
      { k: 'Time-to-pivot', v: '2.1', sub: 'turns avg' },
      { k: 'Abandon timing', v: 'T-6.4', sub: 'turns before close' },
    ],
    archetype: [
      { t: 'Warm Champion', conv: 0.78 }, { t: 'Busy Decider', conv: 0.64 },
      { t: 'Skeptical Analyzer', conv: 0.49 }, { t: 'Price Hawk', conv: 0.31 },
    ],
    dwell: [
      { t: 'Discovery', v: 5.2 }, { t: 'Objection', v: 6.1 }, { t: 'Closing', v: 3.8 }, { t: 'Wrap', v: 1.4 },
    ],
  };

  window.DB = { VERSIONS, CALLS, TRANSCRIPT, ESCALATIONS, EXPERIMENTS, APPROVALS, KPI };
})();
