// Placeholder for the operator "Operate" surface (live-call monitor, call review, KPIs) built in
// U15. Scaffolded here so the shared web app already routes /operate; the real UI/IA comes from the
// dashboard design handoff and is intentionally NOT part of the U13 demo deliverable.
export default function OperatePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col items-center justify-center gap-3 p-8 text-center">
      <h1 className="text-xl font-semibold">Operate</h1>
      <p className="text-sm text-neutral-500">
        Operator dashboard (live-call monitor, call review, KPIs) — implemented in U15.
      </p>
    </main>
  );
}
