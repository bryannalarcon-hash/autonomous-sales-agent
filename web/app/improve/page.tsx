// Placeholder for the operator "Improve" surface (experiment lab, approval queue, KB/playbook
// editor, version history) built in U16. Scaffolded so the shared web app routes /improve now; the
// real UI is driven by the dashboard design handoff and is out of scope for the U13 demo.
export default function ImprovePage() {
  return (
    <main className="mx-auto flex min-h-screen max-w-2xl flex-col items-center justify-center gap-3 p-8 text-center">
      <h1 className="text-xl font-semibold">Improve</h1>
      <p className="text-sm text-neutral-500">
        Experiment lab, approval queue, and editors — implemented in U16.
      </p>
    </main>
  );
}
