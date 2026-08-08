/**
 * The knowledge graph, rendered to `<canvas>` (`docs/UI.md` §8.7).
 *
 * **Canvas, never SVG or DOM.** `GET /knowledge/graph` returns up to 500 nodes, and 500 DOM
 * nodes in a force layout is a guaranteed frame-rate failure — one paint per node per frame,
 * plus a layout pass. One canvas is one paint.
 *
 * **The layout is computed once and does not move.** Nodes are clustered by entity family and
 * then pulled along their edges for a fixed number of passes, all synchronously, before the
 * first frame. There is no simulation loop, no ambient drift and no auto-rotation: a graph
 * that keeps moving is a graph you cannot point at, and §6.4 gives ambient motion no budget at
 * all. Panning and zooming are the only motion, and they are the user's.
 *
 * **Colour is a family, not a kind.** There are fifteen `EntityKind` values and exactly eight
 * validated categorical slots, and §11.2 forbids generating a ninth hue. So the kinds fold
 * into eight families, the legend names the families, and the inspector — not the colour —
 * carries the exact kind. Folding the tail is the sanctioned answer; inventing a colour is not.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useTheme } from '@/hooks/use-theme';
import type { EntityKind, GraphEdge, GraphNode, GraphView } from '@/lib/api/types';
import { seriesColor } from '@/lib/chart/series';
import { cn, clamp } from '@/lib/utils';

/** The eight families the fifteen entity kinds fold into, with their permanent slots. */
export const ENTITY_FAMILIES = [
  { key: 'skill', label: 'Skills & tech', slot: 1, kinds: ['skill', 'technology', 'language'] },
  { key: 'organization', label: 'Organizations', slot: 2, kinds: ['organization'] },
  { key: 'project', label: 'Projects', slot: 3, kinds: ['project'] },
  { key: 'role', label: 'Roles', slot: 4, kinds: ['role'] },
  { key: 'recognition', label: 'Recognition', slot: 5, kinds: ['award', 'publication'] },
  { key: 'leadership', label: 'Leadership', slot: 6, kinds: ['leadership'] },
  {
    key: 'education',
    label: 'Education',
    slot: 7,
    kinds: ['education', 'course', 'certification'],
  },
  { key: 'people', label: 'People & goals', slot: 8, kinds: ['person', 'interest', 'goal'] },
] as const satisfies readonly {
  key: string;
  label: string;
  slot: number;
  kinds: readonly EntityKind[];
}[];

/** Kind → family index, built once. */
const FAMILY_OF: Readonly<Record<string, number>> = Object.fromEntries(
  ENTITY_FAMILIES.flatMap((family, index) => family.kinds.map((kind) => [kind, index])),
);

/** Node radius in graph units. */
const NODE_RADIUS = 6;

/** Passes of edge attraction. Bounded, synchronous, and run before the first paint. */
const RELAX_PASSES = 48;

/** Zoom above which every label is drawn; below it, only the hovered node is labelled. */
const LABEL_ZOOM = 1.35;

/** Zoom bounds. */
const MIN_ZOOM = 0.35;
const MAX_ZOOM = 3;

interface Placed {
  node: GraphNode;
  x: number;
  y: number;
  family: number;
}

/** Deterministic pseudo-random in [0, 1) from a string — the same graph lays out identically. */
function seeded(id: string, salt: number): number {
  let hash = salt * 2654435761;
  for (let index = 0; index < id.length; index += 1) {
    hash = (hash ^ id.charCodeAt(index)) * 16777619;
    hash >>>= 0;
  }
  return (hash % 100000) / 100000;
}

/**
 * Cluster by family, then relax along the edges.
 *
 * Cost is O(nodes) for the seeding plus O(edges × passes) for the relaxation — linear in the
 * graph rather than quadratic, which is what keeps a 500-node view instant instead of janky.
 */
function layout(view: GraphView): { placed: Placed[]; edges: GraphEdge[] } {
  const count = view.nodes.length;
  if (count === 0) return { placed: [], edges: [] };

  const families = ENTITY_FAMILIES.length;
  const spread = 260 + Math.sqrt(count) * 26;

  const placed: Placed[] = view.nodes.map((node) => {
    const family = FAMILY_OF[node.kind] ?? 0;
    const centreAngle = (family / families) * Math.PI * 2;
    const centreX = Math.cos(centreAngle) * spread;
    const centreY = Math.sin(centreAngle) * spread;
    const angle = seeded(node.id, 1) * Math.PI * 2;
    const radius = Math.sqrt(seeded(node.id, 2)) * (spread * 0.62);
    return {
      node,
      family,
      x: centreX + Math.cos(angle) * radius,
      y: centreY + Math.sin(angle) * radius,
    };
  });

  const index = new Map(placed.map((entry, position) => [entry.node.id, position]));

  for (let pass = 0; pass < RELAX_PASSES; pass += 1) {
    const strength = 0.06 * (1 - pass / RELAX_PASSES);
    for (const edge of view.edges) {
      const from = index.get(edge.source);
      const to = index.get(edge.target);
      if (from === undefined || to === undefined) continue;
      const a = placed[from];
      const b = placed[to];
      if (a === undefined || b === undefined) continue;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const distance = Math.hypot(dx, dy) || 1;
      const pull = ((distance - 90) / distance) * strength;
      a.x += dx * pull;
      a.y += dy * pull;
      b.x -= dx * pull;
      b.y -= dy * pull;
    }
  }

  return { placed, edges: view.edges };
}

/** Resolve a CSS custom property to a concrete colour the canvas can use. */
function resolve(token: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(token).trim();
  return value === '' ? '#888888' : value;
}

/** Props for {@link GraphCanvas}. */
export interface GraphCanvasProps {
  view: GraphView;
  /** The entity currently in the inspector, drawn with a ring. */
  selectedId?: string | null;
  onSelect: (node: GraphNode | null) => void;
  className?: string;
}

/** A pannable, zoomable canvas rendering of the entity graph. */
export function GraphCanvas({ view, selectedId, onSelect, className }: GraphCanvasProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const { resolved } = useTheme();
  const [zoom, setZoom] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [hovered, setHovered] = useState<string | null>(null);
  const drag = useRef<{ x: number; y: number; ox: number; oy: number } | null>(null);

  const { placed, edges } = useMemo(() => layout(view), [view]);
  const byId = useMemo(() => new Map(placed.map((entry) => [entry.node.id, entry])), [placed]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const context = canvas.getContext('2d');
    if (context === null) return;

    const ratio = window.devicePixelRatio || 1;
    const width = canvas.clientWidth;
    const height = canvas.clientHeight;
    if (canvas.width !== width * ratio || canvas.height !== height * ratio) {
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
    }

    const edgeColor = resolve('--border-default');
    const labelColor = resolve('--fg-secondary');
    const surfaceColor = resolve('--bg-surface');
    const accentColor = resolve('--accent');
    const familyColors = ENTITY_FAMILIES.map((family) =>
      resolve(seriesColor(family.slot).replace('var(', '').replace(')', '')),
    );

    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    context.save();
    context.translate(width / 2 + offset.x, height / 2 + offset.y);
    context.scale(zoom, zoom);

    context.lineWidth = 1 / zoom;
    context.strokeStyle = edgeColor;
    context.beginPath();
    for (const edge of edges) {
      const from = byId.get(edge.source);
      const to = byId.get(edge.target);
      if (from === undefined || to === undefined) continue;
      context.moveTo(from.x, from.y);
      context.lineTo(to.x, to.y);
    }
    context.stroke();

    for (const entry of placed) {
      const isHovered = hovered === entry.node.id;
      const isSelected = selectedId === entry.node.id;
      context.beginPath();
      context.arc(entry.x, entry.y, NODE_RADIUS, 0, Math.PI * 2);
      context.fillStyle = familyColors[entry.family] ?? accentColor;
      context.fill();

      if (isSelected || isHovered) {
        context.lineWidth = 2 / zoom;
        context.strokeStyle = isSelected ? accentColor : surfaceColor;
        context.beginPath();
        context.arc(entry.x, entry.y, NODE_RADIUS + 3, 0, Math.PI * 2);
        context.stroke();
      }
    }

    if (zoom >= LABEL_ZOOM || hovered !== null) {
      context.font = `${String(11 / zoom)}px var(--font-sans, sans-serif)`;
      context.fillStyle = labelColor;
      context.textAlign = 'center';
      context.textBaseline = 'top';
      for (const entry of placed) {
        if (zoom < LABEL_ZOOM && entry.node.id !== hovered) continue;
        context.fillText(entry.node.label, entry.x, entry.y + NODE_RADIUS + 3 / zoom);
      }
    }

    context.restore();
  }, [byId, edges, hovered, offset.x, offset.y, placed, selectedId, zoom]);

  useEffect(() => {
    draw();
  }, [draw, resolved]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (canvas === null) return;
    const observer = new ResizeObserver(() => {
      draw();
    });
    observer.observe(canvas);
    return () => {
      observer.disconnect();
    };
  }, [draw]);

  /** Screen point → graph point. */
  const toGraph = useCallback(
    (clientX: number, clientY: number): { x: number; y: number } | null => {
      const canvas = canvasRef.current;
      if (canvas === null) return null;
      const rect = canvas.getBoundingClientRect();
      return {
        x: (clientX - rect.left - rect.width / 2 - offset.x) / zoom,
        y: (clientY - rect.top - rect.height / 2 - offset.y) / zoom,
      };
    },
    [offset.x, offset.y, zoom],
  );

  const nodeAt = useCallback(
    (clientX: number, clientY: number): Placed | null => {
      const point = toGraph(clientX, clientY);
      if (point === null) return null;
      const reach = (NODE_RADIUS + 5) / zoom;
      let best: Placed | null = null;
      let bestDistance = reach;
      for (const entry of placed) {
        const distance = Math.hypot(entry.x - point.x, entry.y - point.y);
        if (distance <= bestDistance) {
          best = entry;
          bestDistance = distance;
        }
      }
      return best;
    },
    [placed, toGraph, zoom],
  );

  return (
    <div className={cn('relative min-h-0 overflow-hidden rounded-lg border border-default bg-surface', className)}>
      <canvas
        ref={canvasRef}
        className="size-full cursor-grab active:cursor-grabbing"
        role="img"
        aria-label={`Entity graph: ${String(view.nodes.length)} entities and ${String(view.edges.length)} relationships. Use the facts table for a keyboard-navigable view of the same data.`}
        onPointerDown={(event) => {
          event.currentTarget.setPointerCapture(event.pointerId);
          drag.current = {
            x: event.clientX,
            y: event.clientY,
            ox: offset.x,
            oy: offset.y,
          };
        }}
        onPointerMove={(event) => {
          const active = drag.current;
          if (active !== null) {
            setOffset({
              x: active.ox + (event.clientX - active.x),
              y: active.oy + (event.clientY - active.y),
            });
            return;
          }
          const found = nodeAt(event.clientX, event.clientY);
          setHovered(found?.node.id ?? null);
        }}
        onPointerUp={(event) => {
          const active = drag.current;
          drag.current = null;
          if (
            active !== null &&
            Math.abs(event.clientX - active.x) < 3 &&
            Math.abs(event.clientY - active.y) < 3
          ) {
            onSelect(nodeAt(event.clientX, event.clientY)?.node ?? null);
          }
        }}
        onPointerLeave={() => {
          drag.current = null;
          setHovered(null);
        }}
        onWheel={(event) => {
          setZoom((current) =>
            clamp(current * (event.deltaY < 0 ? 1.12 : 1 / 1.12), MIN_ZOOM, MAX_ZOOM),
          );
        }}
      />

      <div className="pointer-events-none absolute inset-x-3 bottom-3 flex flex-wrap items-center gap-x-3 gap-y-1">
        {ENTITY_FAMILIES.map((family) => (
          <span
            key={family.key}
            className="inline-flex items-center gap-1.5 text-micro tracking-normal text-muted"
          >
            <span
              aria-hidden="true"
              className="size-2 rounded-full"
              style={{ backgroundColor: seriesColor(family.slot) }}
            />
            {family.label}
          </span>
        ))}
      </div>

      <p className="pointer-events-none absolute right-3 top-3 font-mono text-micro tracking-normal text-muted">
        {view.nodes.length} nodes · {view.edges.length} edges · {Math.round(zoom * 100)}%
      </p>
    </div>
  );
}
