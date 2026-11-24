"use client";

// Findings page — plan §13.5 Day 14. Run analysis, show findings grouped by
// type, expose markdown export of the obligation register, and pop the
// LogPanel so the operator can watch progress.

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import { FindingCard } from "@/components/FindingCard";
import { LogPanel } from "@/components/LogPanel";
import { api, type AnalyzeResponse, type Finding, type Project } from "@/lib/api";

export default function ProjectPage({ params }: { params: Promise<{ id: string }> }) {
  // Next 15 route handlers pass params as a Promise; unwrap for the hooks.
  const [projectId, setProjectId] = useState<string | null>(null);
  useEffect(() => {
    params.then((p) => setProjectId(p.id));
  }, [params]);

  const [project, setProject] = useState<Project | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [lastRun, setLastRun] = useState<AnalyzeResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<"all" | "applicable" | "obligation" | "gap">("all");

  const refresh = useCallback(async () => {
    if (!projectId) return;
    setLoading(true);
    try {
      const [p, fs] = await Promise.all([
        api.getProject(projectId),
        api.getFindings(projectId, { limit: 400 }),
      ]);
      setProject(p);
      setFindings(fs.findings);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    // Defer to a microtask so the synchronous `setLoading(true)` inside refresh() is not called in the
    // effect body (react-hooks/set-state-in-effect). Behaviour-equivalent: the load kicks off right
    // after mount/dep-change. The cleanup cancels a still-pending kick so a fast unmount is a no-op.
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) void refresh();
    });
    return () => {
      cancelled = true;
    };
  }, [refresh]);

  const onRun = async () => {
    if (!projectId) return;
    setRunning(true);
    setError(null);
    try {
      const rep = await api.analyzeProject(projectId, {
        top_k: 200,
        limit: 200,
        max_concurrency: 8,
        force: false,
        layers: ["L2", "L3", "L4"],
      });
      setLastRun(rep);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setRunning(false);
    }
  };

  const counts = useMemo(
    () => ({
      total: findings.length,
      applicable: findings.filter((f) => f.finding_type === "applicable").length,
      obligation: findings.filter((f) => f.finding_type === "obligation").length,
      gap: findings.filter((f) => f.finding_type === "gap").length,
    }),
    [findings],
  );

  const filtered = useMemo(() => {
    if (activeTab === "all") return findings;
    return findings.filter((f) => f.finding_type === activeTab);
  }, [findings, activeTab]);

  const onExportObligations = () => {
    const obligations = findings.filter((f) => f.finding_type === "obligation");
    const md = renderObligationMarkdown(project, obligations);
    const blob = new Blob([md], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${project?.name ?? "project"}-obligations.md`;
    a.click();
    URL.revokeObjectURL(url);
  };

  if (!projectId) return <main className="p-8">Loading…</main>;

  return (
    <main className="mx-auto max-w-5xl px-4 py-8">
      <nav className="mb-6 text-sm text-slate-500">
        <Link href="/projects/new" className="hover:underline">
          ← New project
        </Link>
      </nav>

      <header className="mb-6">
        <h1 className="text-2xl font-semibold tracking-tight">
          {project?.name ?? "Loading project…"}
        </h1>
        {project && (
          <p className="mt-1 text-sm text-slate-500">
            {project.sector} · {project.jurisdiction_scope.join(" + ")} ·{" "}
            {project.entity_type ?? "no entity type"}
          </p>
        )}
        {project && (
          <div className="mt-3 flex flex-wrap gap-1 text-xs">
            {project.sector_tags.map((t) => (
              <span key={t} className="rounded-full bg-sky-50 px-2 py-0.5 text-sky-700">
                {t}
              </span>
            ))}
            {project.activity_tags.map((t) => (
              <span key={t} className="rounded-full bg-emerald-50 px-2 py-0.5 text-emerald-700">
                {t}
              </span>
            ))}
          </div>
        )}
      </header>

      <section className="mb-6 flex flex-wrap items-center gap-3">
        <button
          onClick={onRun}
          disabled={running || loading}
          className="rounded bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {running ? "Analysing… (~2 min)" : "▶ Run analysis (L2 → L3 → L4)"}
        </button>
        <button
          onClick={refresh}
          disabled={loading}
          className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
        >
          ↻ Refresh
        </button>
        <button
          onClick={onExportObligations}
          disabled={counts.obligation === 0}
          className="rounded border px-3 py-2 text-sm hover:bg-slate-50 disabled:opacity-50"
        >
          ⬇ Export obligation register (.md)
        </button>
        {lastRun && (
          <div className="ml-auto text-xs text-slate-500">
            Last run: {Math.round(lastRun.total_latency_ms / 1000)}s · L2{" "}
            {lastRun.l2?.n_findings_persisted ?? 0} · L3{" "}
            {lastRun.l3?.n_obligations_persisted ?? 0} · L4 {lastRun.l4?.n_gaps_persisted ?? 0}
          </div>
        )}
      </section>

      {error && (
        <div className="mb-4 rounded border border-rose-300 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {error}
        </div>
      )}

      <nav className="mb-3 flex gap-2 border-b text-sm">
        {(["all", "applicable", "obligation", "gap"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setActiveTab(t)}
            className={`-mb-px border-b-2 px-3 py-2 ${
              activeTab === t
                ? "border-slate-900 font-medium text-slate-900"
                : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {t === "all" ? `All (${counts.total})` : `${t} (${counts[t]})`}
          </button>
        ))}
      </nav>

      <section className="space-y-3">
        {loading && <p className="text-sm text-slate-400">Loading findings…</p>}
        {!loading && filtered.length === 0 && (
          <p className="text-sm text-slate-500">
            No findings yet. Click <strong>Run analysis</strong> to start.
          </p>
        )}
        {filtered.map((f) => (
          <FindingCard key={f.id} finding={f} />
        ))}
      </section>

      <LogPanel projectId={projectId} />
    </main>
  );
}

function renderObligationMarkdown(project: Project | null, obligations: Finding[]): string {
  const lines: string[] = [];
  lines.push(`# Obligation register — ${project?.name ?? "Project"}`);
  lines.push("");
  if (project) {
    lines.push(`- **Sector**: ${project.sector}`);
    lines.push(`- **Jurisdiction**: ${project.jurisdiction_scope.join(", ")}`);
    if (project.entity_type) lines.push(`- **Entity type**: ${project.entity_type}`);
    lines.push("");
  }
  lines.push(`Total obligations: **${obligations.length}**`);
  lines.push("");
  obligations.forEach((f, i) => {
    const p = f.payload as Record<string, unknown>;
    lines.push(`## ${i + 1}. ${p["modal"] ?? "shall"} — ${p["actor"] ?? "unknown actor"}`);
    lines.push("");
    lines.push(`${p["action"] ?? f.statement}`);
    lines.push("");
    if (p["deadline"]) lines.push(`- **Deadline**: ${p["deadline"]}`);
    const conditions = (p["conditions"] as string[] | undefined) ?? [];
    if (conditions.length) lines.push(`- **Conditions**: ${conditions.join("; ")}`);
    const exceptions = (p["exceptions"] as string[] | undefined) ?? [];
    if (exceptions.length) lines.push(`- **Exceptions**: ${exceptions.join("; ")}`);
    lines.push(
      `- **Citation**: ${f.cited_provisions.map((id) => `[\`${id.slice(0, 8)}\`](/sources/${id})`).join(", ")}`,
    );
    lines.push(`- **Confidence**: ${(f.confidence_overall * 100).toFixed(0)}%`);
    lines.push("");
  });
  return lines.join("\n");
}
