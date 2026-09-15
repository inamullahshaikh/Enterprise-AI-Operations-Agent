export default function HomePage() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-4 px-6 py-24">
      <h1 className="text-3xl font-semibold">Relay</h1>
      <p className="text-neutral-600">
        Enterprise AI Operations Agent — app shell. Chat, connectors, approvals, knowledge,
        memory, usage, and audit pages land starting Phase 2 (see{" "}
        <code>docs/system-design.md</code> section 22).
      </p>
    </main>
  );
}
