// Nexus act-graph data layer (control room). Slim mirrors of the live /control/api/graph/* contract +
// React-Query hooks. Pure data: no DOM, no Cytoscape — so the same types feed the reusable NexusGraph
// component and (later) a customer-facing route. The hooks default to controlGet (control proxy), but the
// component itself never imports this file — the panel injects the resolved nodes/edges as props, keeping
// the graph promotable to any data source.

import { useQuery } from "@tanstack/react-query";
import { controlGet } from "@/lib/control";

// ── /control/api/graph/acts ─────────────────────────────────────────────────────────────────────────
export type EdgeSource = "cross_reference" | "pythia_edge";
export type ColorFacet = "jurisdiction" | "legal_hierarchy";

// ── shared visual vocabulary (no DOM / no three) — used by the 3D graph AND the page legend ───────────
// Each act type renders as a distinct solid so the type is readable regardless of the colour-by facet.
export const ACT_TYPE_STYLE: Record<string, { shape: string; glyph: string; label: string }> = {
  STATUTE: { shape: "sphere", glyph: "●", label: "Zákon (statute)" },
  GOV_REGULATION: { shape: "box", glyph: "■", label: "Nařízení (gov. regulation)" },
  MINISTERIAL_DECREE: { shape: "octahedron", glyph: "◆", label: "Vyhláška (decree)" },
  EU_SECONDARY: { shape: "icosahedron", glyph: "⬢", label: "EU regulation / directive" },
  EU_PRIMARY: { shape: "dodecahedron", glyph: "⬟", label: "EU primary law" },
  CASE_LAW: { shape: "tetrahedron", glyph: "▲", label: "Case law" },
  SOFT_LAW: { shape: "torus", glyph: "◎", label: "Soft law" },
  CONSTITUTIONAL_ORDER: { shape: "dodecahedron", glyph: "⬟", label: "Constitutional order" },
  REGIONAL_MUNICIPAL_PREDPIS: { shape: "cylinder", glyph: "▮", label: "Regional / municipal" },
  MEASURE_GENERAL_NATURE: { shape: "cone", glyph: "▴", label: "General measure" },
};
export function shapeForType(type: string): string {
  return ACT_TYPE_STYLE[type]?.shape ?? "sphere";
}

// Calm categorical hue per facet value (deterministic hash fallback for unknown values).
const FACET_HUES: Record<string, number> = {
  CZ: 210,
  EU: 265,
  STATUTE: 210,
  EU_SECONDARY: 265,
  EU_PRIMARY: 285,
  CASE_LAW: 5,
  MINISTERIAL_DECREE: 45,
  GOV_REGULATION: 150,
  REGIONAL_MUNICIPAL_PREDPIS: 90,
  SOFT_LAW: 190,
  CONSTITUTIONAL_ORDER: 320,
  MEASURE_GENERAL_NATURE: 60,
};
export function hueFor(value: string): number {
  if (value in FACET_HUES) return FACET_HUES[value];
  let h = 0;
  for (let i = 0; i < value.length; i++) h = (h * 31 + value.charCodeAt(i)) % 360;
  return h;
}
export function facetHsl(value: string, sat = 55, light = 60): string {
  return `hsl(${hueFor(value)}, ${sat}%, ${light}%)`;
}

export interface GraphActNode {
  id: string;
  ext: string;
  title: string;
  jurisdiction: string;
  legal_hierarchy: string;
  document_type: string;
  provision_count: number;
  valid_from: string | null;
  valid_to: string | null;
  superseded_by: string | null;
}

export interface GraphEdge {
  from: string;
  to: string;
  weight: number;
  types: string[];
  avg_confidence: number;
}

export interface GraphStats {
  n_acts: number;
  n_edges: number;
  max_provisions_per_act: number;
  max_depth: number;
}

export interface GraphActsResponse {
  enabled: boolean;
  reachable?: boolean;
  reason?: string | null;
  error?: string;
  corpus?: string;
  edge_source?: EdgeSource;
  stats?: GraphStats;
  available_rel_types?: string[];
  nodes?: GraphActNode[];
  edges?: GraphEdge[];
}

// ── /control/api/graph/act/{id} (lazy hierarchy tree) ─────────────────────────────────────────────────
export interface ActTreeNode {
  id: string;
  label: string;
  eid: string;
  path: string;
  depth: number;
  ordinal: number;
  preview: string;
  kind: "provision" | "structure";
  children?: (ActTreeNode | string)[];
  children_count?: number;
}

export interface ActTreeResponse {
  enabled: boolean;
  found: boolean;
  document?: {
    id: string;
    ext: string;
    title: string;
    jurisdiction: string;
    legal_hierarchy: string;
    document_type: string;
    provisions_total: number;
  };
  tree?: (ActTreeNode | string)[];
}

// ── /control/api/graph/act/{id}/links (inter-act links) ───────────────────────────────────────────────
export interface LinkExample {
  from_provision: string;
  from_label: string;
  to_provision: string;
  to_label: string;
  rel: string;
}

export interface ActLinkGroup {
  to: string;
  to_ext: string;
  to_title: string;
  weight: number;
  types: string[];
  examples: LinkExample[];
}

export interface DanglingLink {
  to_external_ref: string;
  count: number;
}

export interface ActLinksResponse {
  enabled: boolean;
  found: boolean;
  edge_source?: EdgeSource;
  act?: { id: string; ext: string; title: string };
  outbound?: ActLinkGroup[];
  inbound?: ActLinkGroup[];
  dangling_out?: DanglingLink[];
  _note?: string;
}

// ── query params ──────────────────────────────────────────────────────────────────────────────────────
export interface GraphActsParams {
  edge_source?: EdgeSource;
  rel_types?: string[];
}

export interface ActLinksParams {
  edge_source?: EdgeSource;
}

function qs(params: Record<string, string | undefined>): string {
  const e = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  if (e.length === 0) return "";
  return "?" + e.map(([k, v]) => `${k}=${encodeURIComponent(v as string)}`).join("&");
}

// ── hooks ─────────────────────────────────────────────────────────────────────────────────────────────
export function useGraphActs(params: GraphActsParams = {}) {
  const edge = params.edge_source ?? "cross_reference";
  const rel = params.rel_types && params.rel_types.length ? params.rel_types.slice().sort().join(",") : undefined;
  return useQuery({
    queryKey: ["graph-acts", edge, rel ?? null],
    queryFn: () => controlGet<GraphActsResponse>(`graph/acts${qs({ edge_source: edge, rel_types: rel })}`),
    staleTime: 60_000,
  });
}

export function useActTree(actId: string | null) {
  return useQuery({
    queryKey: ["graph-act-tree", actId],
    queryFn: () => controlGet<ActTreeResponse>(`graph/act/${actId}`),
    enabled: !!actId,
    staleTime: 5 * 60_000,
  });
}

export function useActLinks(actId: string | null, params: ActLinksParams = {}) {
  const edge = params.edge_source ?? "cross_reference";
  return useQuery({
    queryKey: ["graph-act-links", actId, edge],
    queryFn: () => controlGet<ActLinksResponse>(`graph/act/${actId}/links${qs({ edge_source: edge })}`),
    enabled: !!actId,
    staleTime: 60_000,
  });
}
