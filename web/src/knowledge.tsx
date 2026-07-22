import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import cytoscape from "cytoscape";
import type { Core, ElementDefinition } from "cytoscape";

type ApiOptions = {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
  json?: unknown;
};
type Api = <T>(path: string, options?: ApiOptions) => Promise<T>;

type GraphNode = { id: string; kind: string; label: string };
type GraphEdge = {
  source: string;
  target: string;
  rel: string;
  origin: string;
  score?: number | null;
};
type GraphPayload = { nodes: GraphNode[]; edges: GraphEdge[]; truncated: boolean };

type MemoryFile = { filename: string; kb_id: string; size_bytes: number };
type OverviewPayload = {
  postgres: {
    documents: number;
    chunks: number;
    episodes: number;
    provenance_records: number;
  };
  qdrant: {
    ok?: boolean;
    configured?: boolean;
    detail?: string;
    embedding_model?: string;
    episode_points?: number | null;
    chunk_points?: number | null;
    chunk_backfill_pending?: number | null;
  };
  neo4j: { ok?: boolean; detail?: string };
  memory: {
    agents: {
      agent_id: string;
      display_name?: string;
      kb_id: string;
      files: MemoryFile[];
    }[];
  };
};

type DocumentRow = {
  document_id: string;
  kb_id: string;
  title: string;
  source: string;
  document_type: string;
  ingested_at: string;
  superseded?: boolean;
  stale?: boolean;
};
type DocumentsPayload = { total: number; documents: DocumentRow[] };
type EpisodeRow = {
  episode_id: string;
  kb_id?: string;
  agent_id?: string;
  summary?: string;
  created_at?: string;
};
type EpisodesPayload = { total: number; episodes: EpisodeRow[] };

type SearchRow = { score: number | null; payload: Record<string, unknown> };
type SearchPayload = { mode: string; episodes: SearchRow[]; chunks: SearchRow[] };
type NeighborsPayload = { edges: GraphEdge[]; nodes: GraphNode[]; reason?: string | null };
type NodePayload = Record<string, unknown> & { kind: string; kb_id?: string };
type LinkRow = { kbId: string; rel: string; label: string };

const KIND_COLORS: Record<string, string> = {
  document: "var(--c-accent)",
  chunk: "#5b8ba8",
  episode: "#9a7bb8",
  agent: "#c98f4e",
  memory: "#c9b45e",
  task: "#6da878",
  goal: "#4ea89a",
  team: "#a86d6d",
  decision: "#8593a8",
  provenance: "#5a6472",
};

function kindColor(kind: string): string {
  return KIND_COLORS[kind] || "#5a6472";
}

const KIND_FROM_PREFIX: Record<string, string> = {
  doc: "document",
  chunk: "chunk",
  episode: "episode",
  prov: "provenance",
  agent: "agent",
  memory: "memory",
  task: "task",
  goal: "goal",
  team: "team",
  decision: "decision",
};

function kindOf(kbId: string): string {
  return KIND_FROM_PREFIX[kbId.split(":")[0]] || "provenance";
}

function shortLabel(kbId: string): string {
  const rest = kbId.slice(kbId.indexOf(":") + 1);
  return /^[0-9a-f]{8}-[0-9a-f]{4}-/i.test(rest) ? rest.split("-")[0] : rest;
}

function nodeDef(node: GraphNode): ElementDefinition {
  return { data: { id: node.id, label: node.label, kind: node.kind } };
}

export default function KnowledgeView({
  api,
  setStatus,
  canWrite = false,
}: {
  api: Api;
  setStatus: (message: string) => void;
  canWrite?: boolean;
}) {
  const [showAdd, setShowAdd] = useState(false);
  const [overview, setOverview] = useState<OverviewPayload | null>(null);
  const [graph, setGraph] = useState<GraphPayload | null>(null);
  const [documents, setDocuments] = useState<DocumentRow[]>([]);
  const [episodes, setEpisodes] = useState<EpisodeRow[]>([]);
  const [browse, setBrowse] = useState<"memory" | "documents" | "episodes">("memory");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [similarEdges, setSimilarEdges] = useState<GraphEdge[]>([]);
  const [similarNodes, setSimilarNodes] = useState<GraphNode[]>([]);
  const [egoDocument, setEgoDocument] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoadError(null);
    try {
      const graphPath = egoDocument
        ? `/api/knowledge/graph?document_id=${encodeURIComponent(egoDocument)}`
        : "/api/knowledge/graph";
      const [overviewPayload, graphPayload, documentsPayload, episodesPayload] =
        await Promise.all([
          api<OverviewPayload>("/api/knowledge/overview"),
          api<GraphPayload>(graphPath),
          api<DocumentsPayload>("/api/knowledge/documents?limit=100"),
          api<EpisodesPayload>("/api/knowledge/episodes?limit=100"),
        ]);
      setOverview(overviewPayload);
      setGraph(graphPayload);
      setDocuments(documentsPayload.documents);
      setEpisodes(episodesPayload.episodes);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setLoadError(message);
      setStatus(`Knowledge base load failed: ${message}`);
    }
  }, [api, egoDocument, setStatus]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const select = useCallback((kbId: string | null) => {
    setSelectedId(kbId);
    setSimilarEdges([]);
    setSimilarNodes([]);
  }, []);

  const showSimilar = useCallback(
    async (kbId: string) => {
      try {
        const payload = await api<NeighborsPayload>(
          `/api/knowledge/neighbors?kb_id=${encodeURIComponent(kbId)}`,
        );
        setSimilarEdges(payload.edges);
        setSimilarNodes(payload.nodes);
        if (!payload.edges.length) {
          setStatus(payload.reason || "No similar items found");
        }
      } catch (error) {
        setStatus(
          `Similarity lookup failed: ${error instanceof Error ? error.message : error}`,
        );
      }
    },
    [api, setStatus],
  );

  const memoryAgents = overview?.memory.agents || [];
  const agentNames = useMemo(() => {
    const map = new Map<string, string>();
    for (const agent of memoryAgents) {
      map.set(agent.agent_id, agent.display_name || agent.agent_id);
    }
    return map;
  }, [memoryAgents]);

  // Links for the selected node — provenance/memory edges from the loaded graph
  // plus any view-time similarity edges — used by the inspector's traversal list.
  const selectedLinks = useMemo<LinkRow[]>(() => {
    if (!selectedId || !graph) {
      return [];
    }
    const labelOf = new Map<string, string>();
    for (const node of graph.nodes) {
      labelOf.set(node.id, node.label);
    }
    for (const node of similarNodes) {
      if (!labelOf.has(node.id)) {
        labelOf.set(node.id, node.label);
      }
    }
    const seen = new Set<string>();
    const rows: LinkRow[] = [];
    for (const edge of [...graph.edges, ...similarEdges]) {
      let other: string | null = null;
      if (edge.source === selectedId) {
        other = edge.target;
      } else if (edge.target === selectedId) {
        other = edge.source;
      }
      if (!other || seen.has(other)) {
        continue;
      }
      seen.add(other);
      rows.push({ kbId: other, rel: edge.rel, label: labelOf.get(other) || other });
    }
    return rows;
  }, [selectedId, graph, similarEdges, similarNodes]);

  const qdrantOk = Boolean(overview?.qdrant.ok ?? overview?.qdrant.configured);
  const neo4jOk = Boolean(overview?.neo4j.ok);
  const backfillPending = overview?.qdrant.chunk_backfill_pending;

  return (
    <section className="ob-kb">
      <div className="ob-kb-rail ob-panel">
        <div className="ob-panel-head">
          <span className="ob-panel-title">Knowledge Base</span>
          <div className="ob-kb-head-actions">
            {canWrite && (
              <button
                type="button"
                className="ob-kb-refresh"
                onClick={() => setShowAdd(true)}
              >
                + Add
              </button>
            )}
            <button type="button" className="ob-kb-refresh" onClick={() => void refresh()}>
              Refresh
            </button>
          </div>
        </div>
        {showAdd && (
          <AddDocumentDialog
            api={api}
            setStatus={setStatus}
            onClose={() => setShowAdd(false)}
            onAdded={() => {
              setShowAdd(false);
              void refresh();
            }}
          />
        )}
        {overview && (
          <div className="ob-kb-stats">
            <span className="ob-badge">{overview.postgres.documents} docs</span>
            <span className="ob-badge">{overview.postgres.chunks} chunks</span>
            <span className="ob-badge">{overview.postgres.episodes} episodes</span>
            <span className={`ob-badge ${qdrantOk ? "ok" : "warn"}`}>
              qdrant {qdrantOk ? "up" : "down"}
            </span>
            <span className={`ob-badge ${neo4jOk ? "ok" : "warn"}`}>
              neo4j {neo4jOk ? "up" : "down"}
            </span>
            {typeof backfillPending === "number" && backfillPending > 0 && (
              <span className="ob-badge warn">{backfillPending} chunks unindexed</span>
            )}
          </div>
        )}
        <KnowledgeSearch api={api} onSelect={select} setStatus={setStatus} />
        <div className="segmented ob-kb-browse-switch">
          {(["memory", "documents", "episodes"] as const).map((section) => (
            <button
              key={section}
              type="button"
              className={browse === section ? "active" : ""}
              onClick={() => setBrowse(section)}
            >
              {section}
            </button>
          ))}
        </div>
        <div className="ob-kb-browse">
          {browse === "documents" &&
            documents.map((document) => (
              <button
                key={document.document_id}
                type="button"
                className={`ob-kb-row ${selectedId === document.kb_id ? "selected" : ""}`}
                onClick={() => select(document.kb_id)}
              >
                <span className="ob-kb-row-title">{document.title}</span>
                <span className="ob-kb-row-meta">
                  {document.document_type} · {document.source}
                  {document.superseded ? " · superseded" : ""}
                  {document.stale ? " · stale" : ""}
                </span>
              </button>
            ))}
          {browse === "documents" && !documents.length && (
            <div className="ob-kb-empty">No documents ingested yet.</div>
          )}
          {browse === "episodes" &&
            episodes.map((episode) => (
              <button
                key={episode.episode_id}
                type="button"
                className={`ob-kb-row ${
                  selectedId === `episode:${episode.episode_id}` ? "selected" : ""
                }`}
                onClick={() => select(`episode:${episode.episode_id}`)}
              >
                <span className="ob-kb-row-title">{episode.summary || episode.episode_id}</span>
                <span className="ob-kb-row-meta">
                  {episode.agent_id
                    ? agentNames.get(episode.agent_id) || episode.agent_id
                    : "unknown agent"}
                </span>
              </button>
            ))}
          {browse === "episodes" && !episodes.length && (
            <div className="ob-kb-empty">No episodes recorded yet.</div>
          )}
          {browse === "memory" &&
            memoryAgents.map((agent) => (
              <div key={agent.agent_id} className="ob-kb-memory-agent">
                <button
                  type="button"
                  className={`ob-kb-row ${selectedId === agent.kb_id ? "selected" : ""}`}
                  onClick={() => select(agent.kb_id)}
                >
                  <span className="ob-kb-row-title">
                    {agent.display_name || agent.agent_id}
                  </span>
                  <span className="ob-kb-row-meta">{agent.files.length} memory files</span>
                </button>
                {agent.files.map((file) => (
                  <button
                    key={file.kb_id}
                    type="button"
                    className={`ob-kb-row ob-kb-row-sub ${
                      selectedId === file.kb_id ? "selected" : ""
                    }`}
                    onClick={() => select(file.kb_id)}
                  >
                    <span className="ob-kb-row-title">{file.filename}</span>
                    <span className="ob-kb-row-meta">{file.size_bytes} bytes</span>
                  </button>
                ))}
              </div>
            ))}
          {browse === "memory" && !memoryAgents.length && (
            <div className="ob-kb-empty">No agents registered.</div>
          )}
        </div>
      </div>

      <div className="ob-kb-graph-panel ob-panel">
        <div className="ob-panel-head">
          <span className="ob-panel-title">Link Graph</span>
          <div className="ob-kb-graph-controls">
            {egoDocument && (
              <button type="button" onClick={() => setEgoDocument(null)}>
                Full graph
              </button>
            )}
            {graph?.truncated && (
              <span className="ob-badge warn">truncated — filter to narrow</span>
            )}
          </div>
        </div>
        {loadError ? (
          <div className="ob-kb-empty">Failed to load: {loadError}</div>
        ) : (
          <KnowledgeGraph
            graph={graph}
            similarEdges={similarEdges}
            similarNodes={similarNodes}
            selectedId={selectedId}
            onSelect={select}
          />
        )}
      </div>

      <div className="ob-kb-inspector ob-panel">
        <div className="ob-panel-head">
          <span className="ob-panel-title">Inspector</span>
        </div>
        <KnowledgeInspector
          kbId={selectedId}
          api={api}
          links={selectedLinks}
          onSelect={select}
          onShowSimilar={showSimilar}
          onShowDocumentGraph={(documentId) => setEgoDocument(documentId)}
        />
      </div>
    </section>
  );
}

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  for (let index = 0; index < bytes.byteLength; index += 1) {
    binary += String.fromCharCode(bytes[index]);
  }
  return btoa(binary);
}

function AddDocumentDialog({
  api,
  setStatus,
  onClose,
  onAdded,
}: {
  api: Api;
  setStatus: (message: string) => void;
  onClose: () => void;
  onAdded: () => void;
}) {
  const [title, setTitle] = useState("");
  const [source, setSource] = useState("manual");
  const [type, setType] = useState("note");
  const [mode, setMode] = useState<"text" | "file">("text");
  const [content, setContent] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!title.trim()) {
      setStatus("Title is required");
      return;
    }
    setBusy(true);
    try {
      const body: Record<string, unknown> = {
        title: title.trim(),
        source: source.trim() || "manual",
        type: type.trim() || "note",
      };
      if (mode === "file") {
        if (!file) {
          setStatus("Choose a file to upload");
          setBusy(false);
          return;
        }
        body.filename = file.name;
        body.file_b64 = arrayBufferToBase64(await file.arrayBuffer());
      } else {
        if (!content.trim()) {
          setStatus("Paste some content to add");
          setBusy(false);
          return;
        }
        body.content = content;
      }
      const result = await api<{ document_id: string; kb_id: string }>(
        "/api/knowledge/documents",
        { method: "POST", json: body },
      );
      setStatus(`Added document ${result.document_id}`);
      onAdded();
    } catch (error) {
      setStatus(`Add failed: ${error instanceof Error ? error.message : error}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="ob-kb-dialog-backdrop" onClick={onClose}>
      <div className="ob-kb-dialog" onClick={(event) => event.stopPropagation()}>
        <div className="ob-kb-subhead">Add to Knowledge Base</div>
        <input
          aria-label="Title"
          placeholder="Title"
          value={title}
          onChange={(event) => setTitle(event.target.value)}
        />
        <div className="ob-kb-dialog-row">
          <input
            aria-label="Source"
            placeholder="Source"
            value={source}
            onChange={(event) => setSource(event.target.value)}
          />
          <input
            aria-label="Type"
            placeholder="Type"
            value={type}
            onChange={(event) => setType(event.target.value)}
          />
        </div>
        <div className="segmented ob-kb-browse-switch">
          <button
            type="button"
            className={mode === "text" ? "active" : ""}
            onClick={() => setMode("text")}
          >
            paste text
          </button>
          <button
            type="button"
            className={mode === "file" ? "active" : ""}
            onClick={() => setMode("file")}
          >
            upload file
          </button>
        </div>
        {mode === "text" ? (
          <textarea
            aria-label="Content"
            placeholder="Paste content…"
            value={content}
            rows={8}
            onChange={(event) => setContent(event.target.value)}
          />
        ) : (
          <input
            type="file"
            accept=".md,.txt,.pdf,.html,.htm"
            onChange={(event) => setFile(event.target.files?.[0] || null)}
          />
        )}
        <div className="ob-kb-dialog-actions">
          <button type="button" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button type="button" onClick={() => void submit()} disabled={busy}>
            {busy ? "Adding…" : "Add"}
          </button>
        </div>
      </div>
    </div>
  );
}

// The graph is explored incrementally: the cytoscape instance is created once
// and then mutated in place (cy.add / positioning) rather than destroyed and
// re-laid-out on every change. Initial view seeds with agent nodes only;
// selecting a node reveals its neighbours from the full in-memory graph and
// centres on it without disturbing the existing layout or zoom.
function KnowledgeGraph({
  graph,
  similarEdges,
  similarNodes,
  selectedId,
  onSelect,
}: {
  graph: GraphPayload | null;
  similarEdges: GraphEdge[];
  similarNodes: GraphNode[];
  selectedId: string | null;
  onSelect: (kbId: string) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cyRef = useRef<Core | null>(null);
  const onSelectRef = useRef(onSelect);
  onSelectRef.current = onSelect;

  const nodeByIdRef = useRef(new Map<string, GraphNode>());
  const nodeById = useMemo(() => {
    const map = new Map<string, GraphNode>();
    if (graph) {
      for (const node of graph.nodes) {
        map.set(node.id, node);
      }
    }
    for (const node of similarNodes) {
      if (!map.has(node.id)) {
        map.set(node.id, node);
      }
    }
    return map;
  }, [graph, similarNodes]);
  nodeByIdRef.current = nodeById;

  // Create the cytoscape instance once.
  useEffect(() => {
    if (!containerRef.current) {
      return undefined;
    }
    const cy = cytoscape({
      container: containerRef.current,
      elements: [],
      style: [
        {
          selector: "node",
          style: {
            width: 14,
            height: 14,
            label: "data(label)",
            "background-color": (element: cytoscape.NodeSingular) =>
              kindColor(String(element.data("kind"))),
            color: "#a9b6c9",
            "font-size": 7,
            "text-valign": "bottom",
            "text-margin-y": 3,
            "text-wrap": "ellipsis",
            "text-max-width": "110px",
            "border-width": 1,
            "border-color": "rgba(0,0,0,0.55)",
          },
        },
        {
          selector: "node:selected",
          style: {
            "border-width": 2,
            "border-color": "#e8ecf3",
            width: 18,
            height: 18,
          },
        },
        {
          selector: "edge",
          style: {
            width: 1,
            "line-color": "#3a4454",
            "curve-style": "bezier",
            "target-arrow-shape": "triangle",
            "target-arrow-color": "#3a4454",
            "arrow-scale": 0.6,
            label: "data(rel)",
            "font-size": 5,
            color: "#5f6b7d",
            "text-rotation": "autorotate",
          },
        },
        {
          selector: "edge.similarity",
          style: {
            "line-style": "dashed",
            "line-color": "#8b7bb8",
            "target-arrow-shape": "none",
            color: "#8b7bb8",
          },
        },
      ],
      wheelSensitivity: 0.45,
    });
    cy.on("tap", "node", (event) => {
      onSelectRef.current(String(event.target.id()));
    });
    cyRef.current = cy;
    return () => {
      cyRef.current = null;
      cy.destroy();
    };
  }, []);

  // Seed / reset the canvas whenever the underlying graph payload changes.
  // Shows agent nodes only (or the first slice when no agents exist, e.g. an
  // ego graph) so the initial layout is cheap and the operator expands from
  // there.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !graph) {
      return;
    }
    const agents = graph.nodes.filter((node) => node.kind === "agent");
    const seed = agents.length ? agents : graph.nodes.slice(0, 24);
    const present = new Set(seed.map((node) => node.id));
    cy.startBatch();
    cy.elements().remove();
    cy.add(seed.map(nodeDef));
    graph.edges.forEach((edge, index) => {
      if (present.has(edge.source) && present.has(edge.target)) {
        cy.add({
          data: {
            id: `e-${index}`,
            source: edge.source,
            target: edge.target,
            rel: edge.rel,
            origin: edge.origin,
          },
          classes: edge.origin === "similarity" ? "similarity" : undefined,
        });
      }
    });
    cy.endBatch();
    if (cy.nodes().nonempty()) {
      cy.layout({ name: "cose", animate: false, padding: 30 }).run();
      cy.fit(undefined, 40);
    }
  }, [graph]);

  // Select + expand: reveal the selected node's neighbours from the full graph,
  // placing new nodes around the focus so nothing already on screen jumps.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy) {
      return;
    }
    cy.nodes().unselect();
    if (!selectedId || !graph) {
      return;
    }
    let focus = cy.getElementById(selectedId);
    if (focus.empty()) {
      const known = nodeByIdRef.current.get(selectedId);
      const added = cy.add(
        known
          ? nodeDef(known)
          : {
              data: {
                id: selectedId,
                label: shortLabel(selectedId),
                kind: kindOf(selectedId),
              },
            },
      );
      const extent = cy.extent();
      added.position({
        x: (extent.x1 + extent.x2) / 2,
        y: (extent.y1 + extent.y2) / 2,
      });
      focus = added;
    }
    const focusPos = { ...focus.position() };

    const newNodeDefs: ElementDefinition[] = [];
    const newEdgeDefs: ElementDefinition[] = [];
    graph.edges.forEach((edge, index) => {
      let other: string | null = null;
      if (edge.source === selectedId) {
        other = edge.target;
      } else if (edge.target === selectedId) {
        other = edge.source;
      }
      if (!other) {
        return;
      }
      const known = nodeByIdRef.current.get(other);
      if (cy.getElementById(other).empty() && known) {
        newNodeDefs.push(nodeDef(known));
      }
      const edgeId = `x-${index}`;
      if (cy.getElementById(edgeId).empty()) {
        newEdgeDefs.push({
          data: {
            id: edgeId,
            source: edge.source,
            target: edge.target,
            rel: edge.rel,
            origin: edge.origin,
          },
          classes: edge.origin === "similarity" ? "similarity" : undefined,
        });
      }
    });

    if (newNodeDefs.length) {
      cy.startBatch();
      const addedNodes = cy.add(newNodeDefs);
      const count = addedNodes.length;
      addedNodes.forEach((element, index) => {
        const angle = (2 * Math.PI * index) / Math.max(1, count);
        element.position({
          x: focusPos.x + 140 * Math.cos(angle),
          y: focusPos.y + 140 * Math.sin(angle),
        });
      });
      cy.endBatch();
    }
    for (const def of newEdgeDefs) {
      const data = def.data as { source?: string; target?: string };
      if (
        cy.getElementById(String(data.source)).nonempty() &&
        cy.getElementById(String(data.target)).nonempty()
      ) {
        cy.add(def);
      }
    }

    focus.select();
    cy.animate({ center: { eles: focus } }, { duration: 220 });
  }, [selectedId, graph]);

  // Similarity edges arrive on demand ("Show similar"); add them dashed around
  // the current focus without re-laying-out the graph.
  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !selectedId) {
      return;
    }
    const focus = cy.getElementById(selectedId);
    if (focus.empty()) {
      return;
    }
    const focusPos = { ...focus.position() };
    const missing = similarNodes.filter((node) =>
      cy.getElementById(node.id).empty(),
    );
    if (missing.length) {
      cy.startBatch();
      const added = cy.add(missing.map(nodeDef));
      added.forEach((element, index) => {
        const angle = (2 * Math.PI * index) / missing.length + 0.6;
        element.position({
          x: focusPos.x + 200 * Math.cos(angle),
          y: focusPos.y + 200 * Math.sin(angle),
        });
      });
      cy.endBatch();
    }
    similarEdges.forEach((edge, index) => {
      const id = `sim-${index}`;
      if (
        cy.getElementById(id).empty() &&
        cy.getElementById(edge.source).nonempty() &&
        cy.getElementById(edge.target).nonempty()
      ) {
        cy.add({
          data: {
            id,
            source: edge.source,
            target: edge.target,
            rel: edge.rel,
            origin: edge.origin,
          },
          classes: "similarity",
        });
      }
    });
  }, [similarEdges, similarNodes, selectedId]);

  return (
    <div className="ob-kb-graph-wrap">
      <div ref={containerRef} className="ob-kb-graph" />
      {!graph && <div className="ob-kb-empty ob-kb-graph-overlay">Loading graph…</div>}
      {graph && !graph.nodes.length && (
        <div className="ob-kb-empty ob-kb-graph-overlay">
          Nothing to show yet — add a document from the rail.
        </div>
      )}
    </div>
  );
}

function KnowledgeSearch({
  api,
  onSelect,
  setStatus,
}: {
  api: Api;
  onSelect: (kbId: string) => void;
  setStatus: (message: string) => void;
}) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchPayload | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
    }
    const trimmed = query.trim();
    if (trimmed.length < 3) {
      setResults(null);
      return undefined;
    }
    timerRef.current = window.setTimeout(async () => {
      try {
        setResults(
          await api<SearchPayload>(
            `/api/knowledge/search?q=${encodeURIComponent(trimmed)}`,
          ),
        );
      } catch (error) {
        setStatus(
          `Knowledge search failed: ${error instanceof Error ? error.message : error}`,
        );
      }
    }, 300);
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, [api, query, setStatus]);

  const rows = useMemo(() => {
    if (!results) {
      return [];
    }
    const chunkRows = results.chunks.map((row) => ({
      kbId: String(row.payload.kb_id || ""),
      label: String(row.payload.text || row.payload.chunk_id || ""),
      kind: "chunk",
      score: row.score,
    }));
    const episodeRows = results.episodes.map((row) => ({
      kbId: String(row.payload.kb_id || ""),
      label: String(row.payload.summary || row.payload.episode_id || ""),
      kind: "episode",
      score: row.score,
    }));
    return chunkRows.concat(episodeRows).filter((row) => row.kbId);
  }, [results]);

  return (
    <div className="ob-kb-search">
      <input
        aria-label="Search knowledge"
        placeholder="Search knowledge…"
        value={query}
        onChange={(event) => setQuery(event.target.value)}
      />
      {results && (
        <div className="ob-kb-search-results">
          {results.mode === "keyword" && (
            <span className="ob-badge warn">keyword mode</span>
          )}
          {rows.map((row) => (
            <button
              key={row.kbId}
              type="button"
              className="ob-kb-row"
              onClick={() => {
                onSelect(row.kbId);
                setQuery("");
              }}
            >
              <span className="ob-kb-row-title">{row.label.slice(0, 90)}</span>
              <span className="ob-kb-row-meta">
                {row.kind}
                {typeof row.score === "number" ? ` · ${row.score.toFixed(3)}` : ""}
              </span>
            </button>
          ))}
          {!rows.length && <div className="ob-kb-empty">No matches.</div>}
        </div>
      )}
    </div>
  );
}

function KnowledgeInspector({
  kbId,
  api,
  links,
  onSelect,
  onShowSimilar,
  onShowDocumentGraph,
}: {
  kbId: string | null;
  api: Api;
  links: LinkRow[];
  onSelect: (kbId: string) => void;
  onShowSimilar: (kbId: string) => void;
  onShowDocumentGraph: (documentId: string) => void;
}) {
  const [payload, setPayload] = useState<NodePayload | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPayload(null);
    setError(null);
    if (!kbId) {
      return;
    }
    let cancelled = false;
    api<NodePayload>(`/api/knowledge/node/${encodeURIComponent(kbId)}`)
      .then((result) => {
        if (!cancelled) {
          setPayload(result);
        }
      })
      .catch((problem) => {
        if (!cancelled) {
          setError(problem instanceof Error ? problem.message : String(problem));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [api, kbId]);

  if (!kbId) {
    return (
      <div className="ob-kb-empty">
        Select a node in the graph or an item from the rail.
      </div>
    );
  }
  if (error) {
    return <div className="ob-kb-empty">Failed to load {kbId}: {error}</div>;
  }
  if (!payload) {
    return <div className="ob-kb-empty">Loading…</div>;
  }

  const canSimilar = kbId.startsWith("chunk:") || kbId.startsWith("episode:");
  return (
    <div className="ob-kb-inspector-body">
      <div className="ob-kb-inspector-id">
        <span className="ob-badge" style={{ color: kindColor(payload.kind) }}>
          {payload.kind}
        </span>
        <code>{kbId}</code>
      </div>
      {canSimilar && (
        <button type="button" onClick={() => onShowSimilar(kbId)}>
          Show similar
        </button>
      )}
      {payload.kind === "document" && (
        <DocumentInspector
          payload={payload}
          onSelect={onSelect}
          onShowDocumentGraph={onShowDocumentGraph}
        />
      )}
      {payload.kind === "chunk" && <ChunkInspector payload={payload} onSelect={onSelect} />}
      {payload.kind === "episode" && (
        <pre className="ob-kb-pre">{pretty(payload.episode)}</pre>
      )}
      {payload.kind === "memory" && <MemoryInspector payload={payload} />}
      {payload.kind === "agent" && <AgentKbInspector payload={payload} onSelect={onSelect} />}
      {payload.kind === "provenance" && (
        <pre className="ob-kb-pre">{pretty(payload.record)}</pre>
      )}
      {["task", "goal", "team", "decision"].includes(payload.kind) && (
        <pre className="ob-kb-pre">{pretty(payload.records)}</pre>
      )}
      {links.length > 0 && (
        <div className="ob-kb-linked">
          <div className="ob-kb-subhead">linked ({links.length})</div>
          {links.map((link) => (
            <button
              key={link.kbId}
              type="button"
              className="ob-kb-row"
              onClick={() => onSelect(link.kbId)}
            >
              <span className="ob-kb-row-title">{link.label}</span>
              <span className="ob-kb-row-meta">{link.rel}</span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function DocumentInspector({
  payload,
  onSelect,
  onShowDocumentGraph,
}: {
  payload: NodePayload;
  onSelect: (kbId: string) => void;
  onShowDocumentGraph: (documentId: string) => void;
}) {
  const document = (payload.document || {}) as Record<string, unknown>;
  const chunks = (payload.chunks || []) as Record<string, unknown>[];
  const episode = payload.episode as Record<string, unknown> | null;
  const metadata = (document.metadata || {}) as Record<string, unknown>;
  return (
    <>
      <h3 className="ob-kb-inspector-title">{String(document.title || "")}</h3>
      {Boolean(payload.superseded) && (
        <span className="ob-badge warn">
          superseded by {String(metadata.superseded_by || "a newer fetch")}
        </span>
      )}
      {Boolean(payload.stale) && (
        <span className="ob-badge warn">stale — past the web knowledge TTL</span>
      )}
      <dl className="ob-kb-fields">
        <dt>type</dt>
        <dd>{String(document.document_type || "")}</dd>
        <dt>source</dt>
        <dd>{String(document.source || "")}</dd>
        <dt>ingested</dt>
        <dd>{String(document.ingested_at || "")}</dd>
        {Boolean(metadata.source_url) && (
          <>
            <dt>url</dt>
            <dd>{String(metadata.source_url)}</dd>
          </>
        )}
        {Boolean(metadata.fetched_at) && (
          <>
            <dt>fetched</dt>
            <dd>{String(metadata.fetched_at)}</dd>
          </>
        )}
      </dl>
      <button
        type="button"
        onClick={() => onShowDocumentGraph(String(document.document_id || ""))}
      >
        Focus graph on this document
      </button>
      <div className="ob-kb-subhead">{chunks.length} chunks</div>
      <div className="ob-kb-chunk-list">
        {chunks.map((chunk) => (
          <button
            key={String(chunk.chunk_id)}
            type="button"
            className="ob-kb-row"
            onClick={() => onSelect(`chunk:${String(chunk.chunk_id)}`)}
          >
            <span className="ob-kb-row-title">
              #{String(chunk.chunk_index)} {String(chunk.text || "").slice(0, 70)}
            </span>
          </button>
        ))}
      </div>
      {episode && (
        <>
          <div className="ob-kb-subhead">derived episode</div>
          <button
            type="button"
            className="ob-kb-row"
            onClick={() => onSelect(`episode:${String(episode.episode_id)}`)}
          >
            <span className="ob-kb-row-title">{String(episode.summary || "")}</span>
          </button>
        </>
      )}
    </>
  );
}

function ChunkInspector({
  payload,
  onSelect,
}: {
  payload: NodePayload;
  onSelect: (kbId: string) => void;
}) {
  const chunk = (payload.chunk || {}) as Record<string, unknown>;
  const document = (payload.document || {}) as Record<string, unknown>;
  return (
    <>
      <dl className="ob-kb-fields">
        <dt>document</dt>
        <dd>
          {document.kb_id ? (
            <button
              type="button"
              className="ob-kb-link"
              onClick={() => onSelect(String(document.kb_id))}
            >
              {String(document.title || document.document_id || "")}
            </button>
          ) : (
            "—"
          )}
        </dd>
        <dt>index</dt>
        <dd>{String(chunk.chunk_index)}</dd>
        <dt>source</dt>
        <dd>{String(chunk.source || "")}</dd>
      </dl>
      <pre className="ob-kb-pre">{String(chunk.text || "")}</pre>
    </>
  );
}

function MemoryInspector({ payload }: { payload: NodePayload }) {
  return (
    <>
      <dl className="ob-kb-fields">
        <dt>agent</dt>
        <dd>{String(payload.agent_id || "")}</dd>
        <dt>file</dt>
        <dd>{String(payload.filename || "")}</dd>
        <dt>size</dt>
        <dd>{String(payload.size_bytes || 0)} bytes</dd>
      </dl>
      {Boolean(payload.truncated) && (
        <span className="ob-badge warn">content truncated</span>
      )}
      <pre className="ob-kb-pre">{String(payload.content || "(empty)")}</pre>
    </>
  );
}

function AgentKbInspector({
  payload,
  onSelect,
}: {
  payload: NodePayload;
  onSelect: (kbId: string) => void;
}) {
  const agent = (payload.agent || {}) as Record<string, unknown>;
  const files = (payload.memory_files || []) as { filename: string; kb_id: string }[];
  return (
    <>
      <h3 className="ob-kb-inspector-title">{String(agent.display_name || agent.agent_id || "")}</h3>
      <div className="ob-kb-subhead">memory files</div>
      {files.map((file) => (
        <button
          key={file.kb_id}
          type="button"
          className="ob-kb-row"
          onClick={() => onSelect(file.kb_id)}
        >
          <span className="ob-kb-row-title">{file.filename}</span>
        </button>
      ))}
      {!files.length && <div className="ob-kb-empty">No memory files yet.</div>}
    </>
  );
}

function pretty(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}
