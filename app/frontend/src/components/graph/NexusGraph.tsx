"use client";

// Reusable 3D force-directed act-graph (Three.js / WebGL via react-force-graph-3d). PROMOTABLE-BY-DESIGN:
// never fetches — the caller injects nodes/edges and gets callbacks back; themed ONLY through --ctl-*
// CSS variables resolved from its own container at mount. Calm at rest; the focal node + its
// neighbourhood re-saturate on hover / selection / search-focus while everything else dims.
//
// ACT-TYPE SHAPES: every node is a distinct Three.js solid keyed off legal_hierarchy (zákon = sphere,
// nařízení = cube, vyhláška = octahedron, EU = icosahedron, case-law = tetrahedron, …) so the act type
// is readable REGARDLESS of the colour-by facet. Colour still encodes the chosen facet; size ∝ provisions.
//
// MOTION: the layout never hard-freezes — it holds a tiny constant energy (d3AlphaDecay=0) with heavy
// velocity damping, so nodes drift gently and continuously without fast/jittery motion. View changes
// (select/search) animate via a smooth camera fly-to.
//
// PERFORMANCE: WebGL (GPU); device-pixel-ratio capped at 2; render pauses when off-screen / tab hidden;
// hover-only HTML labels; directional arrows + bright/thick links only on the highlighted neighbourhood;
// graphData memoised (no re-heat on colour/selection); geometries are shared singletons across nodes.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph3D, { type ForceGraphMethods } from "react-force-graph-3d";
import * as THREE from "three";
import { facetHsl, shapeForType } from "@/lib/graph";

export type GraphColorFacet = "jurisdiction" | "legal_hierarchy";

export interface NexusGraphNode {
  id: string;
  label: string;
  /** drives node size (sqrt scale) */
  weight: number;
  /** facet value used for color-by (e.g. "CZ", "EU_SECONDARY") */
  jurisdiction: string;
  legal_hierarchy: string;
}

export interface NexusGraphEdge {
  source: string;
  target: string;
  /** drives edge width */
  weight: number;
}

export interface NexusGraphProps {
  nodes: NexusGraphNode[];
  edges: NexusGraphEdge[];
  colorBy: GraphColorFacet;
  selectedId?: string | null;
  onSelectAct?: (id: string) => void;
  onBackgroundTap?: () => void;
  className?: string;
}

interface GNode extends NexusGraphNode {
  __r: number; // rendered radius (∝ √provisions)
  __neighbors?: Set<GNode>;
  __links?: GLink[];
  x?: number;
  y?: number;
  z?: number;
}
interface GLink {
  source: string | GNode;
  target: string | GNode;
  weight: number;
}

// shared, low-poly geometries (one instance per shape, reused by every node of that type) at unit radius
const _geomCache: Record<string, THREE.BufferGeometry> = {};
function geomFor(type: string): THREE.BufferGeometry {
  const shape = shapeForType(type);
  if (!_geomCache[shape]) {
    _geomCache[shape] = (() => {
      switch (shape) {
        case "box":
          return new THREE.BoxGeometry(1.5, 1.5, 1.5);
        case "octahedron":
          return new THREE.OctahedronGeometry(1.25);
        case "icosahedron":
          return new THREE.IcosahedronGeometry(1.2);
        case "dodecahedron":
          return new THREE.DodecahedronGeometry(1.2);
        case "tetrahedron":
          return new THREE.TetrahedronGeometry(1.45);
        case "torus":
          return new THREE.TorusGeometry(0.85, 0.34, 8, 16);
        case "cylinder":
          return new THREE.CylinderGeometry(1, 1, 1.7, 12);
        case "cone":
          return new THREE.ConeGeometry(1.15, 2.1, 12);
        default:
          return new THREE.SphereGeometry(1, 12, 10);
      }
    })();
  }
  return _geomCache[shape];
}

// Gentle "breathing" motion WITHOUT jitter or cluster drift. Strategy: let the real force layout settle
// (alpha decays), then capture each node's settled position as its fixed HOME and thereafter PIN it
// (fx/fy/fz) to home + a tiny, slow, per-node sine orbit. Because the homes never change, the cluster as
// a whole stays put — only individual nodes move; and because positions are pinned (not pushed by
// forces) there is no force interplay, so the motion is perfectly smooth and barely noticeable.
function makeBobForce() {
  type N = { x?: number; y?: number; z?: number; fx?: number | null; fy?: number | null; fz?: number | null };
  let nodes: N[] = [];
  let homes: { x: number; y: number; z: number }[] = [];
  let captured = false;
  let t = 0;
  const A = 1.4; // amplitude in graph units — barely noticeable on a ~150-unit cluster
  const force = ((alpha: number) => {
    if (!captured) {
      if (alpha > 0.03) return; // still settling under the real forces — don't pin yet
      homes = nodes.map((n) => ({ x: n.x ?? 0, y: n.y ?? 0, z: n.z ?? 0 }));
      captured = true;
    }
    t += 1;
    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const h = homes[i];
      const ph = i * 0.6;
      n.fx = h.x + A * Math.sin(t * 0.010 + ph);
      n.fy = h.y + A * Math.sin(t * 0.013 + ph + 2.1);
      n.fz = h.z + A * Math.sin(t * 0.008 + ph + 4.2);
    }
  }) as ((alpha: number) => void) & { initialize?: (n: N[]) => void };
  force.initialize = (n) => {
    // (re)init on data change: unpin so the new set settles under the real forces, then recapture homes
    nodes = n;
    captured = false;
    homes = [];
    for (const node of n) {
      node.fx = null;
      node.fy = null;
      node.fz = null;
    }
  };
  return force;
}

function resolveTheme(el: HTMLElement) {
  const cs = getComputedStyle(el);
  const v = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback;
  return {
    bg: v("--ctl-bg", "#09090b"),
    text: v("--ctl-text", "#e4e4e7"),
    accent: v("--ctl-graph-accent", v("--ctl-ok", "#5eb0ef")),
  };
}

export default function NexusGraph({
  nodes,
  edges,
  colorBy,
  selectedId,
  onSelectAct,
  onBackgroundTap,
  className = "",
}: NexusGraphProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<ForceGraphMethods<GNode, GLink> | undefined>(undefined);

  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const [theme, setTheme] = useState(() => ({ bg: "#09090b", text: "#e4e4e7", accent: "#5eb0ef" }));
  const [hoverNode, setHoverNode] = useState<GNode | null>(null);

  // build the graph + adjacency ONLY when data changes
  const data = useMemo(() => {
    const maxW = Math.max(1, ...nodes.map((n) => n.weight));
    const byId = new Map<string, GNode>();
    const gnodes: GNode[] = nodes.map((n) => {
      const __r = 2.2 + 7 * Math.sqrt(Math.max(0, n.weight) / maxW);
      const g: GNode = { ...n, __r };
      byId.set(n.id, g);
      return g;
    });
    const glinks: GLink[] = [];
    for (const e of edges) {
      const s = byId.get(e.source);
      const t = byId.get(e.target);
      if (!s || !t) continue;
      const l: GLink = { source: e.source, target: e.target, weight: e.weight };
      glinks.push(l);
      (s.__neighbors ??= new Set()).add(t);
      (t.__neighbors ??= new Set()).add(s);
      (s.__links ??= []).push(l);
      (t.__links ??= []).push(l);
    }
    return { nodes: gnodes, links: glinks, byId };
  }, [nodes, edges]);

  const selectedNode = selectedId ? data.byId.get(selectedId) ?? null : null;
  const focal = hoverNode ?? selectedNode;
  const dim = !!focal;

  const { hlNodes, hlLinks } = useMemo(() => {
    if (!focal) return { hlNodes: new Set<GNode>(), hlLinks: new Set<GLink>() };
    const ns = new Set<GNode>([focal]);
    focal.__neighbors?.forEach((n) => ns.add(n));
    return { hlNodes: ns, hlLinks: new Set<GLink>(focal.__links ?? []) };
  }, [focal]);

  // colour facet read through a ref so node-object creation stays STABLE (no mesh rebuild on hover);
  // synced in an effect (never written during render)
  const colorRef = useRef(colorBy);
  useEffect(() => {
    colorRef.current = colorBy;
  }, [colorBy]);
  // materials live in a ref Map (keyed by node id), NOT on the memoised node objects, so the recolour
  // effect mutates plain three.js materials rather than hook-owned values.
  const materialsRef = useRef(new Map<string, THREE.MeshLambertMaterial>());
  const restingHsl = useCallback((n: GNode) => {
    return facetHsl(colorRef.current === "jurisdiction" ? n.jurisdiction : n.legal_hierarchy);
  }, []);

  // one mesh per node: shared geometry (by type) + its own material (so colour/opacity can animate)
  const nodeThreeObject = useCallback(
    (n: GNode) => {
      const mat = new THREE.MeshLambertMaterial({
        color: new THREE.Color(restingHsl(n)),
        transparent: true,
        opacity: 0.9,
      });
      const mesh = new THREE.Mesh(geomFor(n.legal_hierarchy), mat);
      mesh.scale.setScalar(n.__r);
      materialsRef.current.set(n.id, mat);
      return mesh;
    },
    [restingHsl],
  );

  // recolour / dim materials reactively (cheap: a few hundred materials, no mesh rebuild)
  useEffect(() => {
    for (const n of data.nodes) {
      const mat = materialsRef.current.get(n.id);
      if (!mat) continue;
      const facet = colorBy === "jurisdiction" ? n.jurisdiction : n.legal_hierarchy;
      if (focal && n === focal) {
        mat.color.set(theme.accent);
        mat.opacity = 1;
      } else if (dim && !hlNodes.has(n)) {
        mat.color.set(facetHsl(facet, 18, 42));
        mat.opacity = 0.12;
      } else {
        mat.color.set(facetHsl(facet));
        mat.opacity = dim ? 0.97 : 0.9;
      }
    }
  }, [data, colorBy, focal, hlNodes, dim, theme]);

  // link accessors (re-applied on hover/select render, not per frame)
  const linkColor = (l: GLink) => {
    if (dim) return hlLinks.has(l) ? theme.accent : "rgba(120,120,130,0.02)";
    return "rgba(140,140,150,0.15)";
  };
  const linkArrow = (l: GLink) => (hlLinks.has(l) ? 2.6 : 0);
  // PERF: linkWidth>0 makes each link a cylinder MESH. Tubes look nice for the small citation graph, but
  // for a dense graph that's thousands of meshes — fall back to cheap 1px lines (linkWidth 0) past a
  // threshold. Highlighted links stay legible via colour + directional arrows in both modes.
  const manyLinks = data.links.length > 2500;
  const linkWidth = (l: GLink) => (hlLinks.has(l) ? 1.6 : 0.6);

  // responsive sizing + theme resolve
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    setTheme(resolveTheme(el));
    const measure = () => setSize({ w: el.clientWidth, h: el.clientHeight });
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // cap device pixel ratio once the renderer exists
  useEffect(() => {
    let raf = 0;
    const tune = () => {
      const r = fgRef.current?.renderer?.();
      if (r) {
        r.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
        return;
      }
      raf = requestAnimationFrame(tune);
    };
    tune();
    return () => cancelAnimationFrame(raf);
  }, []);

  // pause the render loop when off-screen or the tab is hidden
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    let onScreen = true;
    const apply = () => {
      const fg = fgRef.current;
      if (!fg) return;
      if (onScreen && !document.hidden) fg.resumeAnimation();
      else fg.pauseAnimation();
    };
    const io = new IntersectionObserver(
      ([e]) => {
        onScreen = e.isIntersecting;
        apply();
      },
      { threshold: 0.01 },
    );
    io.observe(el);
    document.addEventListener("visibilitychange", apply);
    return () => {
      io.disconnect();
      document.removeEventListener("visibilitychange", apply);
    };
  }, []);

  // gentle, bounded forces + frame the graph once it has spread (timed fallbacks — the sim never "stops")
  useEffect(() => {
    let raf = 0;
    const fit = () => fgRef.current?.zoomToFit(700, 44);
    const t1 = setTimeout(fit, 1600);
    const t2 = setTimeout(fit, 4200);
    const arm = () => {
      const fg = fgRef.current;
      if (!fg) {
        raf = requestAnimationFrame(arm);
        return;
      }
      const charge = fg.d3Force("charge") as { strength?: (s: number) => void } | undefined;
      charge?.strength?.(-26);
      const link = fg.d3Force("link") as { distance?: (d: number) => void } | undefined;
      link?.distance?.(36);
      // register the settle-then-pin "breathing" motion (re-inits with the current node set)
      (fg.d3Force as unknown as (name: string, f: unknown) => void)("bob", makeBobForce());
    };
    arm();
    return () => {
      clearTimeout(t1);
      clearTimeout(t2);
      cancelAnimationFrame(raf);
    };
  }, [data]);

  // smooth camera fly-to on select/search
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || !selectedNode || selectedNode.x === undefined) return;
    const { x = 0, y = 0, z = 0 } = selectedNode;
    const ratio = 1 + 150 / Math.max(1, Math.hypot(x, y, z));
    fg.cameraPosition({ x: x * ratio, y: y * ratio, z: z * ratio }, { x, y, z }, 900);
  }, [selectedNode]);

  return (
    <div ref={containerRef} className={className} style={{ width: "100%", height: "100%" }}>
      {size.w > 0 && (
        <ForceGraph3D<GNode, GLink>
          ref={fgRef}
          width={size.w}
          height={size.h}
          backgroundColor={theme.bg}
          graphData={data}
          showNavInfo={false}
          controlType="orbit"
          enableNodeDrag={false}
          nodeThreeObject={nodeThreeObject}
          nodeLabel={(n) => `${n.label} · ${n.weight.toLocaleString()} provisions`}
          linkColor={linkColor}
          linkWidth={manyLinks ? 0 : linkWidth}
          linkResolution={4}
          linkOpacity={1}
          linkDirectionalArrowLength={linkArrow}
          linkDirectionalArrowRelPos={1}
          // motion: brief smooth settle (forces, alpha decaying), then the bob force pins nodes to their
          // homes and gently orbits each one. cooldownTicks/Time=Infinity keeps the frame loop alive so
          // the pinned positions keep syncing (links follow); the bob ignores alpha so it never stops.
          warmupTicks={40}
          cooldownTicks={Infinity}
          cooldownTime={Infinity}
          d3AlphaDecay={0.05}
          d3AlphaMin={0}
          d3VelocityDecay={0.45}
          onNodeHover={(n) => {
            setHoverNode((n as GNode | null) ?? null);
            const el = containerRef.current;
            if (el) el.style.cursor = n ? "pointer" : "default";
          }}
          onNodeClick={(n) => onSelectAct?.((n as GNode).id)}
          onBackgroundClick={() => onBackgroundTap?.()}
        />
      )}
    </div>
  );
}
