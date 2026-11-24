"use client";

// Graph panel (control room): an interactive force-directed view of the whole Nexus act-graph plus a
// right-hand act inspector (metadata · inter-act links · lazy hierarchy tree). The reusable graph lives
// in components/graph/NexusGraph (promotable to a customer route); this page only wires data + controls.

import { useMemo, useState } from "react";
import dynamic from "next/dynamic";
import { BarGauge, MetricTile, Panel, StatusPill } from "@/components/control/primitives";
import type { GraphColorFacet, NexusGraphEdge, NexusGraphNode } from "@/components/graph/NexusGraph";
import {
  ACT_TYPE_STYLE,
  facetHsl,
  useActLinks,
  useActTree,
  useGraphActs,
  type ActLinkGroup,
  type ActTreeNode,
  type EdgeSource,
  type GraphActNode,
} from "@/lib/graph";

// react-force-graph-3d (three/WebGL) needs the DOM → client-only, dynamically imported with a calm placeholder.
const NexusGraph = dynamic(() => import("@/components/graph/NexusGraph"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-[12px]" style={{ color: "var(--ctl-muted)" }}>
      loading graph…
    </div>
  ),
});

const CHIP_ON = { color: "var(--ctl-text)", background: "#1f1f23", borderColor: "var(--ctl-border)" };
const CHIP_OFF = { color: "var(--ctl-muted)", background: "transparent", borderColor: "var(--ctl-border)" };

function Chip({ on, onClick, children }: { on: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-full border px-2.5 py-0.5 text-[11px] transition-colors"
      style={on ? CHIP_ON : CHIP_OFF}
    >
      {children}
    </button>
  );
}

export default function GraphPanel() {
  // ── server-driven state (re-queries) ──────────────────────────────────────────────────────────────
  const [edgeSource, setEdgeSource] = useState<EdgeSource>("cross_reference");
  const [relTypes, setRelTypes] = useState<string[]>([]); // empty = all
  const acts = useGraphActs({ edge_source: edgeSource, rel_types: edgeSource === "pythia_edge" ? relTypes : [] });

  // ── client-side filters (instant) ──────────────────────────────────────────────────────────────────
  const [hideCaseLaw, setHideCaseLaw] = useState(false);
  // default ON: unlinked acts (no inter-act link at the current edge layer) are visual noise and the bulk
  // of the node count — hiding them keeps the default view legible AND cheaper to render. Toggle off to
  // see the full corpus.
  const [hideIsolated, setHideIsolated] = useState(true);
  const [minWeight, setMinWeight] = useState(1);
  const [colorBy, setColorBy] = useState<GraphColorFacet>("jurisdiction");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const d = acts.data;
  const off = d && !d.enabled;
  const unreachable = d?.enabled && d?.reachable === false;

  const rawNodes = useMemo<GraphActNode[]>(() => d?.nodes ?? [], [d]);
  const rawEdges = useMemo(() => d?.edges ?? [], [d]);

  // apply client-side filters → derive the keep-set, then the graph node/edge shapes
  const { gNodes, gEdges, visibleIds } = useMemo(() => {
    const keep = new Map<string, GraphActNode>();
    for (const n of rawNodes) {
      if (hideCaseLaw && n.legal_hierarchy === "CASE_LAW") continue;
      keep.set(n.id, n);
    }
    const ids = new Set(keep.keys());
    // surviving edges first (respect min-weight + both endpoints kept)
    const edges = rawEdges.filter((e) => e.weight >= minWeight && ids.has(e.from) && ids.has(e.to));
    // optionally drop acts with no surviving inter-act link (the bulk of the corpus + layout noise)
    let nodeIds = ids;
    if (hideIsolated) {
      const linked = new Set<string>();
      for (const e of edges) {
        linked.add(e.from);
        linked.add(e.to);
      }
      nodeIds = linked;
    }
    const gNodes: NexusGraphNode[] = [...keep.values()]
      .filter((n) => nodeIds.has(n.id))
      .map((n) => ({
        id: n.id,
        label: n.ext || n.title.slice(0, 28),
        weight: n.provision_count,
        jurisdiction: n.jurisdiction,
        legal_hierarchy: n.legal_hierarchy,
      }));
    const gEdges: NexusGraphEdge[] = edges.map((e) => ({ source: e.from, target: e.to, weight: e.weight }));
    return { gNodes, gEdges, visibleIds: nodeIds };
  }, [rawNodes, rawEdges, hideCaseLaw, hideIsolated, minWeight]);

  // act-type legend (shape + colour per legal_hierarchy present), most-common first
  const typeLegend = useMemo(() => {
    const counts = new Map<string, number>();
    for (const n of gNodes) counts.set(n.legal_hierarchy, (counts.get(n.legal_hierarchy) ?? 0) + 1);
    return [...counts.entries()].sort((a, b) => b[1] - a[1]);
  }, [gNodes]);

  // search → resolve to a single node id, center it
  function runSearch(term: string) {
    setSearch(term);
    const q = term.trim().toLowerCase();
    if (!q) return;
    const hit = rawNodes.find(
      (n) => visibleIds.has(n.id) && (n.ext.toLowerCase().includes(q) || n.title.toLowerCase().includes(q)),
    );
    if (hit) setSelectedId(hit.id);
  }

  const selected = selectedId ? rawNodes.find((n) => n.id === selectedId) ?? null : null;
  const relTypeOptions = d?.available_rel_types ?? [];

  // ── disabled / unreachable empty-state (mirror the Nexus panel) ─────────────────────────────────────
  if (off || unreachable) {
    return (
      <div className="space-y-5">
        <header>
          <h1 className="text-lg font-semibold">Graph</h1>
        </header>
        <Panel>
          <p className="py-6 text-center text-[13px]" style={{ color: off ? "var(--ctl-muted)" : "var(--ctl-critical)" }}>
            {off
              ? d?.reason || "The act-graph is served via the Pythia sidecar, which is disabled (PYTHIA_ENABLED off)."
              : `Sidecar unreachable (${d?.error}).`}
          </p>
        </Panel>
      </div>
    );
  }

  const stats = d?.stats;
  const pillTone = acts.isError ? "critical" : d ? "ok" : "muted";

  return (
    <div className="space-y-5">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-lg font-semibold">Graph</h1>
          <p className="text-[12px]" style={{ color: "var(--ctl-muted)" }}>
            Nexus act-graph · {stats?.n_acts ?? "—"} acts · {stats?.n_edges ?? "—"} links · {d?.corpus ?? ""}
          </p>
        </div>
        <StatusPill tone={pillTone}>
          {acts.isError ? "error" : d ? `${gNodes.length} shown` : "…"}
        </StatusPill>
      </header>

      {/* control strip */}
      <Panel title="View">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-[12px]">
          <div className="flex items-center gap-2">
            <span style={{ color: "var(--ctl-muted)" }}>edges</span>
            <Chip
              on={edgeSource === "cross_reference"}
              onClick={() => {
                setEdgeSource("cross_reference");
                setMinWeight(1);
              }}
            >
              Citations
            </Chip>
            <Chip
              on={edgeSource === "pythia_edge"}
              // the full typed graph is ~16k links; start at a higher min-weight so the default render
              // stays light (and legible). The slider still goes back down to 1 on demand.
              onClick={() => {
                setEdgeSource("pythia_edge");
                setMinWeight((w) => (w < 5 ? 5 : w));
              }}
            >
              Full graph
            </Chip>
          </div>

          {edgeSource === "pythia_edge" && relTypeOptions.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span style={{ color: "var(--ctl-muted)" }}>rel</span>
              {relTypeOptions.map((rt) => {
                const on = relTypes.length === 0 || relTypes.includes(rt);
                return (
                  <Chip
                    key={rt}
                    on={on}
                    onClick={() =>
                      setRelTypes((prev) =>
                        prev.includes(rt) ? prev.filter((x) => x !== rt) : [...prev, rt],
                      )
                    }
                  >
                    {rt}
                  </Chip>
                );
              })}
            </div>
          )}

          <div className="flex items-center gap-2">
            <span style={{ color: "var(--ctl-muted)" }}>color</span>
            <Chip on={colorBy === "jurisdiction"} onClick={() => setColorBy("jurisdiction")}>
              Jurisdiction
            </Chip>
            <Chip on={colorBy === "legal_hierarchy"} onClick={() => setColorBy("legal_hierarchy")}>
              Type
            </Chip>
          </div>

          <div className="flex items-center gap-2">
            <Chip on={hideIsolated} onClick={() => setHideIsolated((v) => !v)}>
              {hideIsolated ? "linked only" : "show all acts"}
            </Chip>
            <Chip on={hideCaseLaw} onClick={() => setHideCaseLaw((v) => !v)}>
              {hideCaseLaw ? "case-law hidden" : "hide case-law"}
            </Chip>
          </div>

          <div className="flex items-center gap-2">
            <span style={{ color: "var(--ctl-muted)" }}>min weight</span>
            <input
              type="range"
              min={1}
              max={50}
              value={minWeight}
              onChange={(e) => setMinWeight(Number(e.target.value))}
              className="h-1 w-28 cursor-pointer"
              style={{ accentColor: "var(--ctl-ok)" }}
            />
            <span className="tnum" style={{ color: "var(--ctl-text)" }}>
              {minWeight}
            </span>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <input
              type="text"
              value={search}
              onChange={(e) => runSearch(e.target.value)}
              placeholder="search act (ext / title)…"
              className="w-56 rounded-md border px-2 py-1 text-[12px] outline-none"
              style={{ background: "var(--ctl-bg)", borderColor: "var(--ctl-border)", color: "var(--ctl-text)" }}
            />
          </div>
        </div>
      </Panel>

      {/* graph + inspector */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
        <Panel title="Act-graph" className="lg:col-span-2">
          <div
            className="relative h-[560px] w-full overflow-hidden rounded-md border"
            style={{ borderColor: "var(--ctl-border)", background: "var(--ctl-bg)" }}
          >
            {acts.isLoading ? (
              <div className="flex h-full items-center justify-center text-[12px]" style={{ color: "var(--ctl-muted)" }}>
                loading acts…
              </div>
            ) : gNodes.length === 0 ? (
              <div className="flex h-full items-center justify-center text-[12px]" style={{ color: "var(--ctl-muted)" }}>
                no acts match the current filters
              </div>
            ) : (
              <NexusGraph
                nodes={gNodes}
                edges={gEdges}
                colorBy={colorBy}
                selectedId={selectedId}
                onSelectAct={setSelectedId}
                onBackgroundTap={() => setSelectedId(null)}
                className="h-full w-full"
              />
            )}
          </div>
          <p className="mt-2 text-[10px]" style={{ color: "var(--ctl-muted)" }}>
            node shape = act type · size ∝ provision count · color by{" "}
            {colorBy === "jurisdiction" ? "jurisdiction" : "legal type"} · hover to light up neighbours · tap to inspect
          </p>
          {typeLegend.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[10px]">
              {typeLegend.map(([t, c]) => {
                const s = ACT_TYPE_STYLE[t];
                return (
                  <span
                    key={t}
                    className="inline-flex items-center gap-1"
                    style={{ color: "var(--ctl-muted)" }}
                    title={`${c.toLocaleString()} acts`}
                  >
                    <span style={{ color: facetHsl(t), fontSize: 12, lineHeight: 1 }}>{s?.glyph ?? "●"}</span>
                    {s?.label ?? t}
                  </span>
                );
              })}
            </div>
          )}
        </Panel>

        <ActInspector actId={selectedId} act={selected} edgeSource={edgeSource} onFocus={setSelectedId} />
      </div>
    </div>
  );
}

// ── right-hand act inspector ────────────────────────────────────────────────────────────────────────
function ActInspector({
  actId,
  act,
  edgeSource,
  onFocus,
}: {
  actId: string | null;
  act: GraphActNode | null;
  edgeSource: EdgeSource;
  onFocus: (id: string) => void;
}) {
  const links = useActLinks(actId, { edge_source: edgeSource });
  const tree = useActTree(actId);

  if (!actId) {
    return (
      <Panel title="Inspector">
        <p className="py-10 text-center text-[12px]" style={{ color: "var(--ctl-muted)" }}>
          select an act in the graph to inspect its metadata, links and structure
        </p>
      </Panel>
    );
  }

  const ld = links.data;
  const maxLink = Math.max(
    1,
    ...(ld?.outbound ?? []).map((g) => g.weight),
    ...(ld?.inbound ?? []).map((g) => g.weight),
  );

  return (
    <Panel
      title={act?.ext ?? "Inspector"}
      right={
        <button
          type="button"
          onClick={() => onFocus("")}
          className="text-[11px]"
          style={{ color: "var(--ctl-muted)" }}
        >
          clear
        </button>
      }
    >
      <div className="max-h-[600px] space-y-4 overflow-y-auto pr-1">
        {/* (i) metadata */}
        {act && (
          <div>
            <p className="mb-2 text-[12px]" title={act.title}>
              {act.title}
            </p>
            <div className="grid grid-cols-2 gap-2">
              <MetricTile label="provisions" value={act.provision_count.toLocaleString()} tone="neutral" />
              <MetricTile label="jurisdiction" value={act.jurisdiction} tone="muted" />
              <MetricTile label="type" value={act.legal_hierarchy} tone="muted" />
              <MetricTile
                label="valid"
                value={act.valid_from ?? "—"}
                sub={act.valid_to ? `→ ${act.valid_to}` : act.superseded_by ? "superseded" : "in force"}
                tone={act.valid_to || act.superseded_by ? "warn" : "ok"}
              />
            </div>
          </div>
        )}

        {/* (ii) inter-act links */}
        <div>
          <h3 className="mb-2 text-[12px] font-medium tracking-wide" style={{ color: "var(--ctl-muted)" }}>
            Inter-act links
          </h3>
          {links.isLoading && (
            <p className="text-[11px]" style={{ color: "var(--ctl-muted)" }}>
              loading links…
            </p>
          )}
          {ld && (
            <div className="space-y-3">
              <LinkGroupList title="outbound (cites)" groups={ld.outbound ?? []} max={maxLink} onFocus={onFocus} />
              <LinkGroupList title="inbound (cited by)" groups={ld.inbound ?? []} max={maxLink} onFocus={onFocus} />
              {(ld.dangling_out?.length ?? 0) > 0 && (
                <div>
                  <p className="mb-1 text-[10px] uppercase tracking-wide" style={{ color: "var(--ctl-muted)" }}>
                    points outside corpus
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {(ld.dangling_out ?? []).map((dg) => (
                      <span
                        key={dg.to_external_ref}
                        className="tnum rounded border px-1.5 py-0.5 text-[10px]"
                        style={{ color: "var(--ctl-muted)", borderColor: "var(--ctl-border)" }}
                        title={`${dg.count} references`}
                      >
                        {dg.to_external_ref} · {dg.count}
                      </span>
                    ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* (iii) lazy hierarchy tree */}
        <div>
          <h3 className="mb-2 text-[12px] font-medium tracking-wide" style={{ color: "var(--ctl-muted)" }}>
            Structure
          </h3>
          {tree.isLoading && (
            <p className="text-[11px]" style={{ color: "var(--ctl-muted)" }}>
              loading structure…
            </p>
          )}
          {tree.data?.tree && (
            <div className="space-y-0.5">
              {tree.data.tree
                .filter((t): t is ActTreeNode => typeof t !== "string")
                .map((node) => (
                  <TreeRow key={node.id} node={node} depth={0} />
                ))}
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

function LinkGroupList({
  title,
  groups,
  max,
  onFocus,
}: {
  title: string;
  groups: ActLinkGroup[];
  max: number;
  onFocus: (id: string) => void;
}) {
  if (groups.length === 0) {
    return (
      <div>
        <p className="mb-1 text-[10px] uppercase tracking-wide" style={{ color: "var(--ctl-muted)" }}>
          {title}
        </p>
        <p className="text-[11px]" style={{ color: "var(--ctl-muted)" }}>
          none
        </p>
      </div>
    );
  }
  return (
    <div>
      <p className="mb-1 text-[10px] uppercase tracking-wide" style={{ color: "var(--ctl-muted)" }}>
        {title}
      </p>
      <div className="space-y-2">
        {groups.map((g) => (
          <div key={g.to} className="rounded-md border p-2" style={{ borderColor: "var(--ctl-border)" }}>
            <button
              type="button"
              onClick={() => onFocus(g.to)}
              className="block w-full text-left"
              title={`focus ${g.to_ext} in graph`}
            >
              <div className="flex items-center justify-between gap-2 text-[11px]">
                <span className="truncate" style={{ color: "var(--ctl-text)" }}>
                  {g.to_ext}
                </span>
                <span className="tnum" style={{ color: "var(--ctl-muted)" }}>
                  {g.weight}
                </span>
              </div>
            </button>
            <div className="mt-1">
              <BarGauge label="" pct={(100 * g.weight) / max} tone="neutral" />
            </div>
            <div className="mt-1 flex flex-wrap gap-1">
              {g.types.map((tp) => (
                <span
                  key={tp}
                  className="rounded px-1 py-0.5 text-[9px]"
                  style={{ color: "var(--ctl-muted)", background: "#1f1f23" }}
                >
                  {tp}
                </span>
              ))}
            </div>
            {/* example provision deep-links */}
            <div className="mt-1 space-y-0.5">
              {g.examples.slice(0, 2).map((ex, exIdx) => (
                <a
                  key={`${g.to}-${ex.from_provision}-${ex.to_provision}-${exIdx}`}
                  href={`/sources/${ex.to_provision}?corpus=pythia`}
                  target="_blank"
                  rel="noreferrer"
                  className="block truncate text-[10px] hover:underline"
                  style={{ color: "var(--ctl-muted)" }}
                  title={`${ex.from_label} → ${ex.to_label}`}
                >
                  {ex.from_label} → {ex.to_label} ↗
                </a>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// One hierarchy row. Lazy expand: structural children render only when the row is opened, and we never
// dump the whole tree — children arrays may contain a "...(more children)" sentinel string we surface as muted.
function TreeRow({ node, depth }: { node: ActTreeNode; depth: number }) {
  const [open, setOpen] = useState(false);
  const childNodes = (node.children ?? []).filter((c): c is ActTreeNode => typeof c !== "string");
  const truncated = (node.children ?? []).some((c) => typeof c === "string");
  const hasChildren = node.kind === "structure" && (childNodes.length > 0 || (node.children_count ?? 0) > 0 || truncated);

  return (
    <div>
      <div className="flex items-center gap-1 text-[11px]" style={{ paddingLeft: depth * 12 }}>
        {hasChildren ? (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="tnum w-4 shrink-0 text-left"
            style={{ color: "var(--ctl-muted)" }}
          >
            {open ? "−" : "+"}
          </button>
        ) : (
          <span className="w-4 shrink-0" />
        )}
        {node.kind === "provision" ? (
          <a
            href={`/sources/${node.id}?corpus=pythia`}
            target="_blank"
            rel="noreferrer"
            className="truncate hover:underline"
            style={{ color: "var(--ctl-text)" }}
            title={node.preview}
          >
            {node.label} ↗
          </a>
        ) : (
          <span className="truncate" style={{ color: "var(--ctl-text)" }} title={node.preview}>
            {node.label}
          </span>
        )}
      </div>
      {open && (
        <div className="space-y-0.5">
          {childNodes.map((c) => (
            <TreeRow key={c.id} node={c} depth={depth + 1} />
          ))}
          {truncated && (
            <div className="text-[10px]" style={{ paddingLeft: (depth + 1) * 12, color: "var(--ctl-muted)" }}>
              …more (open in source viewer)
            </div>
          )}
        </div>
      )}
    </div>
  );
}
