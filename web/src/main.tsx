import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const OPS_ROOM_FALLBACK_ROOMS: OpsRoomRoom[] = [
  { id: "orchestrator", label: "Orchestrator", domains: [], kind: "orchestrator", fixed_agent_id: "orchestrator" },
  { id: "studio", label: "Studio", domains: ["content", "writing", "marketing"], kind: "work" },
  { id: "craft", label: "Craft Room", domains: ["build", "design", "implementation", "prototype"], kind: "work" },
  { id: "cubicles", label: "Cubicles", domains: ["research", "ops", "coordination", "support"], kind: "work" },
  { id: "server", label: "Server Room", domains: ["infra", "security", "code", "test"], kind: "work" },
  { id: "finance", label: "Finance", domains: ["finance", "budget", "usage", "reporting"], kind: "work" },
  { id: "breakroom", label: "Break Room", domains: [], statuses: ["idle", "queued"], kind: "rest" },
  {
    id: "barracks",
    label: "Barracks",
    domains: [],
    statuses: ["blocked", "awaiting_human", "reflecting", "ruminating", "dreaming"],
    kind: "rest",
  },
];

type User = {
  username: string;
  role: "owner" | "operator" | "observer";
  created_at?: string;
};

type AuthMe = {
  ok: boolean;
  method: string;
  user: User | null;
  permissions: string[];
  token: {
    issued_at?: number | null;
    expires_at?: number | null;
  };
};

type Mission = {
  statement: string;
  success_criteria: string[];
  explicitly_not: string[];
  latest_reasoning?: string | null;
};

type Goal = {
  statement: string;
  success_criteria: string[];
  explicitly_not: string[];
  set_by: string;
  human_confirmed: boolean;
  set_at: string;
};

type Assignment = {
  assignment_id: string;
  assignment: string;
  assigned_to: string;
  status: string;
  priority: string;
  work_mode: string;
  progress_summary?: string | null;
  blockers: string[];
  awaiting_human: boolean;
  last_run_provider?: string | null;
  last_run_model?: string | null;
  goal_statement?: string | null;
  room_id?: string | null;
  dependency_ids?: string[];
  parent_assignment_id?: string | null;
  last_error?: string | null;
  consecutive_failures?: number;
  reissued_from_assignment_id?: string | null;
  created_at?: string;
  updated_at?: string;
  archived?: boolean;
  final_status?: string | null;
  executive_summary?: string | null;
  archived_at?: string | null;
  failure_info?: string | null;
};

type AgentState = {
  agent: string;
  status: string;
  current_assignment_id?: string | null;
  current_assignment_summary?: string | null;
  assignment_progress?: string | null;
  blockers: string[];
  last_completed?: string | null;
  next_available: string;
};

type AgentRoom = {
  id: string;
  label: string;
  source: string;
  reason: string;
  domain?: string | null;
};

type VisualAgent = {
  agent_id: string;
  display_name: string;
  role: string;
  model_provider: string;
  model_name: string;
  team_id?: string | null;
  team_role: string;
  status: string;
  activity: string;
  room?: AgentRoom | null;
  current_assignment?: Assignment | null;
  state?: AgentState | null;
  goals: Goal[];
  usage: Usage;
};

type OpsRoomRoom = {
  id: string;
  label: string;
  domains: string[];
  statuses?: string[];
  fixed_agent_id?: string | null;
  kind?: string;
};

type Team = {
  team_id: string;
  display_name: string;
  description?: string | null;
  parent_team_id?: string | null;
  crew_chief_id?: string | null;
  members: string[];
  delegation_policy?: string;
  escalation_team_id?: string | null;
};

type Message = {
  message_id: string;
  channel: string;
  sender: string;
  recipient: string;
  content: string;
  metadata?: Record<string, unknown>;
  created_at: string;
};

type OrchestrationEvent = {
  id: string;
  schema_version: number;
  recorded_at: string;
  type: string;
  decision?: string | null;
  status?: string | null;
  summary: string;
  source: string;
  mission_statement?: string | null;
  goal_statement?: string | null;
  trigger?: string | null;
  assignment_id?: string | null;
  assignment_ids: string[];
  agent_id?: string | null;
  parent_assignment_id?: string | null;
  child_assignment_ids: string[];
  idempotency_key?: string | null;
  payload?: Record<string, unknown>;
  record_id?: string | null;
  cycle_id?: string | null;
};

type OrchestrationPayload = {
  version: number;
  generated_at: string;
  latest_event?: OrchestrationEvent | null;
  events: OrchestrationEvent[];
  decisions: OrchestrationEvent[];
  proposals: OrchestrationEvent[];
  counts: Record<string, number>;
};

type OpsRoomSnapshot = {
  version: number;
  generated_at: string;
  mission: Mission | null;
  latest_reasoning?: { decision_summary?: string; cycle_id?: string } | null;
  orchestration?: OrchestrationPayload | null;
  rooms?: OpsRoomRoom[];
  agents: VisualAgent[];
  teams: Team[];
  assignments: Assignment[];
  goals: Record<string, Goal[]>;
  alerts: string[];
  financial_report?: Record<string, unknown> | null;
  local_inference?: Record<string, unknown>;
  cloud_jobs?: Record<string, unknown>[];
  messages: Message[];
};

type Usage = {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  last_recorded_at?: string | null;
};

type CockpitPayload = {
  version: number;
  generated_at: string;
  started_at: string;
  uptime_seconds: number;
  auth: {
    require_auth: boolean;
    web_host: string;
    unsafe_bind_without_auth: boolean;
  };
  mission: Mission | null;
  latest_reasoning?: { decision_summary?: string; cycle_id?: string } | null;
  orchestration?: OrchestrationPayload | null;
  agents: VisualAgent[];
  teams: Team[];
  tasks: {
    active: Assignment[];
    queued: Assignment[];
    blocked: Assignment[];
    all: Assignment[];
    history: Record<string, unknown>[];
  };
  counts: {
    agents: number;
    active_tasks: number;
    queued_tasks: number;
    blocked_tasks: number;
    alerts: number;
    status_by_agent: Record<string, number>;
  };
  alerts: string[];
  datastores: { name: string; ok: boolean; detail: string }[];
  models: {
    default_provider: string;
    default_model: string;
    ollama_base_url: string;
    openai_configured: boolean;
    gemini_configured: boolean;
  };
  usage: Usage & { by_agent: Record<string, Usage> };
  financial_report?: Record<string, unknown> | null;
  local_inference?: Record<string, unknown>;
  cloud_jobs?: Record<string, unknown>[];
  orchestrator: { agent_id: string; display_name: string; channel: string };
};

type SettingsPayload = {
  api_version: string;
  config_path: string;
  config_hash: string;
  require_auth: boolean;
  web_host: string;
  web_port: number;
  default_provider: string;
  default_model: string;
  proactive_mode?: string;
  proactive_creation_enabled?: boolean;
  max_proactive_proposals_per_cycle?: number;
  max_proactive_creations_per_cycle?: number;
  runtime_overrides?: Record<string, unknown>;
  runtime_override_keys?: string[];
  postgres_configured: boolean;
  redis_configured: boolean;
  qdrant_configured: boolean;
  neo4j_configured: boolean;
  editable_keys: string[];
  [key: string]: unknown;
};

type ChatPayload = {
  selected_channel: string | null;
  channels: { channel: string; message_count: number }[];
  messages: Message[];
  agents: unknown[];
};

type OrchestratorMarkdownResult = {
  status: string;
  response_message_id: string | null;
  response_html: string;
};

type ModelOption = {
  provider: string;
  model: string;
  label: string;
  route_type: string;
  available: boolean;
  configured: boolean;
  base_url?: string | null;
  detail?: string | null;
  is_default: boolean;
};

type ModelInventory = {
  default: ModelOption;
  recommended: ModelOption;
  options: ModelOption[];
};

type ModelRoute = {
  provider: string;
  model: string;
  base_url?: string | null;
};

type ApiOptions = RequestInit & { json?: unknown };
type TaskDialogDraft = {
  agentId?: string;
  assignment?: string;
};

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function initialView(): "cockpit" | "brigade" | "telemetry" | "manual" {
  const requested = new URLSearchParams(window.location.search).get("view");
  if (requested === "brigade" || requested === "ops") {
    return "brigade";
  }
  if (requested === "telemetry") {
    return "telemetry";
  }
  if (requested === "manual") {
    return "manual";
  }
  if (requested === "cockpit") {
    return "cockpit";
  }
  const saved = localStorage.getItem("brigade_view");
  if (saved === "brigade" || saved === "ops") {
    return "brigade";
  }
  if (saved === "telemetry") {
    return "telemetry";
  }
  if (saved === "manual") {
    return "manual";
  }
  return "cockpit";
}

function App() {
  const [token, setToken] = useState(localStorage.getItem("brigade_token") || "");
  const [auth, setAuth] = useState<AuthMe | null>(null);
  const [authMessage, setAuthMessage] = useState("");
  const [cockpit, setCockpit] = useState<CockpitPayload | null>(null);
  const [settings, setSettings] = useState<SettingsPayload | null>(null);
  const [models, setModels] = useState<ModelInventory | null>(null);
  const [snapshot, setSnapshot] = useState<OpsRoomSnapshot | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [agentModelSelections, setAgentModelSelections] = useState<Record<string, ModelRoute>>({});
  const [orchestratorModel, setOrchestratorModel] = useState<ModelRoute | null>(null);
  const [status, setStatus] = useState("Loading");
  const [streamStatus, setStreamStatus] = useState("connecting");
  const [authClock, setAuthClock] = useState(Date.now());
  const [activePanel, setActivePanel] = useState<"tasks" | "chat" | "goals">("tasks");
  const [view, setView] = useState<"cockpit" | "brigade" | "telemetry" | "manual">(() => initialView());
  const [taskDialogOpen, setTaskDialogOpen] = useState(false);
  const [taskDialogDraft, setTaskDialogDraft] = useState<TaskDialogDraft | null>(null);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [heartbeatPaused, setHeartbeatPaused] = useState(false);

  const headers = useMemo(() => {
    const next: Record<string, string> = { "Content-Type": "application/json" };
    if (token) {
      next.Authorization = `Bearer ${token}`;
    }
    return next;
  }, [token]);

  const api = useCallback(
    async <T,>(path: string, options: ApiOptions = {}): Promise<T> => {
      const response = await fetch(path, {
        ...options,
        headers: { ...headers, ...(options.headers || {}) },
        body: options.json === undefined ? options.body : JSON.stringify(options.json),
      });
      if (!response.ok) {
        const message = await responseText(response);
        throw new ApiError(response.status, message);
      }
      return response.json() as Promise<T>;
    },
    [headers],
  );

  const permissions = useMemo(() => new Set(auth?.permissions || []), [auth]);
  const tokenMetadata = useMemo(() => readJwtMetadata(token), [token]);
  const tokenExpired = isTokenExpired(auth, tokenMetadata, authClock);
  const tokenMalformed = Boolean(token && !tokenMetadata);
  const can = useCallback(
    (permission: string) => permissions.has("admin") || permissions.has(permission),
    [permissions],
  );

  const loadAuth = useCallback(async (): Promise<AuthMe | null> => {
    try {
      const next = await api<AuthMe>("/api/auth/me");
      setAuth(next);
      setAuthMessage("");
      return next;
    } catch (error) {
      setAuth(null);
      setAuthMessage(errorMessage(error));
      if (error instanceof ApiError && error.status === 401) {
        setStatus("Authentication required");
        return null;
      }
      setStatus(errorMessage(error));
      return null;
    }
  }, [api]);

  const loadCockpit = useCallback(async () => {
    const next = await api<CockpitPayload>("/api/cockpit");
    setCockpit(next);
    setStatus("Cockpit loaded");
    if (!selectedAgentId && next.agents[0]) {
      setSelectedAgentId(next.agents[0].agent_id);
    }
  }, [api, selectedAgentId]);

  const loadSettings = useCallback(async () => {
    const next = await api<SettingsPayload>("/api/settings/effective");
    setSettings(next);
  }, [api]);

  const loadModels = useCallback(async () => {
    const next = await api<ModelInventory>("/api/models");
    setModels(next);
    setOrchestratorModel((current) => current || modelRouteFromOption(next.recommended));
  }, [api]);

  const loadSnapshot = useCallback(async () => {
    const next = await api<OpsRoomSnapshot>("/api/ops-room");
    setSnapshot(next);
    if (!selectedAgentId && next.agents[0]) {
      setSelectedAgentId(next.agents[0].agent_id);
    }
  }, [api, selectedAgentId]);

  const refreshAll = useCallback(async () => {
    if (tokenExpired) {
      setAuth(null);
      setAuthMessage("Token expired; paste a fresh JWT or clear the token.");
      setStatus("Authentication required");
      return;
    }
    const authResult = await loadAuth();
    if (!authResult) {
      return;
    }
    await Promise.all([loadCockpit(), loadSettings(), loadModels(), loadSnapshot()]);
  }, [loadAuth, loadCockpit, loadModels, loadSettings, loadSnapshot, tokenExpired]);

  useEffect(() => {
    localStorage.setItem("brigade_token", token);
  }, [token]);

  useEffect(() => {
    const interval = window.setInterval(() => setAuthClock(Date.now()), 30000);
    return () => window.clearInterval(interval);
  }, []);

  useEffect(() => {
    localStorage.setItem("brigade_view", view);
  }, [view]);

  useEffect(() => {
    let cancelled = false;
    refreshAll().catch((error) => {
      if (!cancelled) {
        if (error instanceof ApiError && error.status === 401) {
          setAuthMessage(error.message);
          setStatus("Authentication required");
          return;
        }
        setStatus(errorMessage(error));
      }
    });
    return () => {
      cancelled = true;
    };
  }, [refreshAll]);

  useEffect(() => {
    const controller = new AbortController();
    let reconnectTimer: number | undefined;

    async function connect() {
      if (tokenExpired) {
        setStreamStatus("paused");
        return;
      }
      try {
        setStreamStatus("connecting");
        const streamHeaders: Record<string, string> = {};
        if (token) {
          streamHeaders.Authorization = `Bearer ${token}`;
        }
        const response = await fetch("/api/ops-room/events", {
          headers: streamHeaders,
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          const message = await responseText(response);
          throw new ApiError(response.status, message);
        }
        setStreamStatus("live");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (!controller.signal.aborted) {
          const { value, done } = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          const chunks = buffer.split("\n\n");
          buffer = chunks.pop() || "";
          for (const chunk of chunks) {
            const data = chunk
              .split("\n")
              .filter((line) => line.startsWith("data: "))
              .map((line) => line.slice(6))
              .join("\n");
            if (!data) {
              continue;
            }
            const next = JSON.parse(data) as OpsRoomSnapshot;
            setSnapshot(next);
            if (!selectedAgentId && next.agents[0]) {
              setSelectedAgentId(next.agents[0].agent_id);
            }
          }
        }
        if (!controller.signal.aborted) {
          reconnectTimer = window.setTimeout(connect, 4000);
        }
      } catch (error) {
        if (controller.signal.aborted) {
          return;
        }
        setStreamStatus("degraded");
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          setAuthMessage(error.message);
          return;
        }
        reconnectTimer = window.setTimeout(connect, 5000);
      }
    }

    connect();
    return () => {
      controller.abort();
      if (reconnectTimer !== undefined) {
        window.clearTimeout(reconnectTimer);
      }
    };
  }, [selectedAgentId, token, tokenExpired]);

  const selectedAgent = useMemo(
    () => allAgents(cockpit, snapshot).find((agent) => agent.agent_id === selectedAgentId) || null,
    [cockpit, selectedAgentId, snapshot],
  );

  const agents = useMemo(() => allAgents(cockpit, snapshot), [cockpit, snapshot]);
  const recommendedModel = modelRouteFromOption(models?.recommended || null);
  const selectedAgentModel = selectedAgentId
    ? agentModelSelections[selectedAgentId] || recommendedModel
    : recommendedModel;

  const selectAgent = useCallback((agentId: string, panel: "tasks" | "chat" | "goals" = "tasks") => {
    if (!agentId) {
      return;
    }
    setSelectedAgentId(agentId);
    setActivePanel(panel);
    setView("brigade");
  }, []);

  const openTaskDialog = useCallback((draft?: TaskDialogDraft) => {
    setTaskDialogDraft(draft || null);
    if (draft?.agentId) {
      setSelectedAgentId(draft.agentId);
    }
    setTaskDialogOpen(true);
  }, []);

  const closeTaskDialog = useCallback(() => {
    setTaskDialogOpen(false);
    setTaskDialogDraft(null);
  }, []);

  const setSelectedAgentModel = useCallback((route: ModelRoute) => {
    if (!selectedAgentId) {
      return;
    }
    setAgentModelSelections((current) => ({ ...current, [selectedAgentId]: route }));
  }, [selectedAgentId]);

  const statusTone = tokenExpired || authMessage ? "bad" : streamStatus === "live" ? "good" : "warn";

  const authWarnings: string[] = [];
  if (cockpit?.auth.unsafe_bind_without_auth) {
    authWarnings.push(`Auth disabled on ${cockpit.auth.web_host}`);
  }
  if (tokenExpired) {
    authWarnings.push("Token expired");
  }
  if (tokenMalformed) {
    authWarnings.push("Token format unreadable");
  }
  if (authMessage) {
    authWarnings.push(authMessage);
  }

  return (
    <div className="ob-desktop">
      <main className="ob-window">
        <TitleBar
          online={streamStatus === "live"}
          statusTone={statusTone}
          authLabel={auth?.user?.role || auth?.method || "auth"}
          paused={heartbeatPaused}
          role={auth?.user?.role || ""}
          view={view}
          onTogglePause={() => setHeartbeatPaused((value) => !value)}
          onReconnect={() => refreshAll().catch((error) => setStatus(errorMessage(error)))}
          onAbout={() => setAboutOpen(true)}
          onOpenManual={() => setView("manual")}
          onBackToCockpit={() => setView("cockpit")}
        />

        {view === "manual" ? (
          <div className="ob-tabstrip">
            <span className="ob-tab active">Manual Orchestration</span>
            <span className="ob-tab-add" aria-hidden="true">+</span>
            <div className="ob-tab-right">
              <div className="ob-tab-token token-control">
                <input
                  aria-label="JWT token"
                  placeholder="JWT token"
                  value={token}
                  onChange={(event) => setToken(event.target.value)}
                />
                <button onClick={() => refreshAll().catch((error) => setStatus(errorMessage(error)))}>
                  Refresh
                </button>
              </div>
            </div>
          </div>
        ) : (
          <TabStrip
            view={view}
            onSelect={setView}
            token={token}
            onTokenChange={setToken}
            onRefresh={() => refreshAll().catch((error) => setStatus(errorMessage(error)))}
            warnings={authWarnings}
          />
        )}

        <section className="status-strip">
          <span className={`health-dot ${statusTone}`}>{auth?.user?.role || auth?.method || "auth"}</span>
          <span className={`health-dot ${streamStatus === "live" ? "good" : "warn"}`}>{streamStatus}</span>
          <span>{status}</span>
        </section>

        {view === "cockpit" ? (
          <CockpitView
            cockpit={cockpit}
            settings={settings}
            auth={auth}
            authMessage={authMessage}
            tokenExpired={tokenExpired}
            models={models}
            selectedAgentId={selectedAgentId}
            selectedAgentModel={selectedAgentModel}
            orchestratorModel={orchestratorModel || recommendedModel}
            heartbeatPaused={heartbeatPaused}
            can={can}
            api={api}
            onSelectAgent={selectAgent}
            onSelectedAgentModelChange={setSelectedAgentModel}
            onOrchestratorModelChange={setOrchestratorModel}
            onModelsChange={setModels}
            onSettingsChange={setSettings}
            onRefresh={refreshAll}
            setStatus={setStatus}
            onOpenTaskDialog={openTaskDialog}
          />
        ) : view === "telemetry" ? (
          <TelemetryView
            cockpit={cockpit}
            settings={settings}
            models={models}
            canEdit={can("task:write")}
            api={api}
            onSettingsChange={setSettings}
            setStatus={setStatus}
          />
        ) : view === "manual" ? (
          <ManualOrchestrationView
            assignments={snapshot?.assignments || cockpit?.tasks?.all || []}
            agents={snapshot?.agents || cockpit?.agents || []}
            events={(snapshot?.orchestration || cockpit?.orchestration)?.events || []}
            role={auth?.user?.role || ""}
            api={api}
            onRefresh={refreshAll}
            setStatus={setStatus}
          />
        ) : (
          <OpsRoomView
            snapshot={snapshot}
            selectedAgent={selectedAgent}
            selectedAgentId={selectedAgentId}
            selectedAgentModel={selectedAgentModel}
            teams={snapshot?.teams || cockpit?.teams || []}
            activePanel={activePanel}
            setActivePanel={setActivePanel}
            can={can}
            api={api}
            onSelectAgent={setSelectedAgentId}
            onRefresh={refreshAll}
            setStatus={setStatus}
            onOpenTaskDialog={openTaskDialog}
          />
        )}

        {aboutOpen && <AboutDialog cockpit={cockpit} onClose={() => setAboutOpen(false)} />}

        {taskDialogOpen && (
          <TaskDialog
            agents={agents}
            selectedAgentId={selectedAgentId}
            draft={taskDialogDraft}
            canCreate={can("task:write")}
            api={api}
            onClose={closeTaskDialog}
            onDone={refreshAll}
            setStatus={setStatus}
          />
        )}
      </main>
    </div>
  );
}

function TitleBar({
  online,
  statusTone,
  authLabel,
  paused,
  role,
  view,
  onTogglePause,
  onReconnect,
  onAbout,
  onOpenManual,
  onBackToCockpit,
}: {
  online: boolean;
  statusTone: "good" | "warn" | "bad";
  authLabel: string;
  paused: boolean;
  role: string;
  view: string;
  onTogglePause: () => void;
  onReconnect: () => void;
  onAbout: () => void;
  onOpenManual: () => void;
  onBackToCockpit: () => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [clock, setClock] = useState(() => formatClock(new Date()));

  useEffect(() => {
    const timer = window.setInterval(() => setClock(formatClock(new Date())), 1000);
    return () => window.clearInterval(timer);
  }, []);

  const close = () => setMenuOpen(false);

  return (
    <div className="ob-titlebar">
      <div className="ob-sysmenu">
        <button
          type="button"
          className="ob-sysmenu-btn"
          title="System menu"
          aria-haspopup="true"
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((value) => !value)}
        >
          <span />
        </button>
        {menuOpen && (
          <>
            <div className="ob-menu-scrim" onClick={close} />
            <div className="ob-menu" role="menu">
              <button type="button" className="ob-menu-item" role="menuitem" onClick={() => { onAbout(); close(); }}>
                <span className="ob-menu-glyph ob-menu-logo" />
                <span>About OpenBrigade</span>
              </button>
              <div className="ob-menu-divider" />
              <div className="ob-menu-status">
                <span className={`ob-online-dot ${online ? "online" : "offline"}`} />
                <span>Daemon&nbsp;·&nbsp;
                  <strong className={online ? "ob-ok" : "ob-bad"}>{online ? "Online" : "Offline"}</strong>
                </span>
              </div>
              <button type="button" className="ob-menu-item" role="menuitem" onClick={() => { onTogglePause(); close(); }}>
                <span className="ob-menu-glyph">{paused ? "▶" : "⏸"}</span>
                <span>{paused ? "Resume Heartbeat" : "Pause Heartbeat"}</span>
              </button>
              <button type="button" className="ob-menu-item" role="menuitem" onClick={() => { onReconnect(); close(); }}>
                <span className="ob-menu-glyph">↻</span>
                <span>Reconnect / Refresh</span>
                <span className="ob-menu-key">⌘R</span>
              </button>
              {(role === "owner" || role === "operator") &&
                (view === "manual" ? (
                  <button type="button" className="ob-menu-item" role="menuitem" onClick={() => { onBackToCockpit(); close(); }}>
                    <span className="ob-menu-glyph">←</span>
                    <span>Back to Cockpit</span>
                  </button>
                ) : (
                  <button type="button" className="ob-menu-item" role="menuitem" onClick={() => { onOpenManual(); close(); }}>
                    <span className="ob-menu-glyph">⚒</span>
                    <span>Manual Orchestration</span>
                  </button>
                ))}
              <div className="ob-menu-divider" />
              <a
                className="ob-menu-item"
                role="menuitem"
                href="https://github.com/"
                target="_blank"
                rel="noreferrer"
                onClick={close}
              >
                <span className="ob-menu-glyph">?</span>
                <span>Documentation</span>
              </a>
            </div>
          </>
        )}
      </div>
      <span className="ob-title">OpenBrigade</span>
      <span className="ob-subtitle">— Orchestrator daemon</span>
      <span className="ob-spacer" />
      <span className="ob-online-pill">
        <span className={`ob-online-dot ${statusTone === "good" ? "online" : statusTone === "bad" ? "offline" : "warn"}`} />
        {online ? "ONLINE" : "OFFLINE"}
        <span className="ob-online-sep">|</span>
        <span className="ob-clock">{clock}</span>
      </span>
      <span className="ob-winbtn" aria-hidden="true">
        <span />
      </span>
      <span className="ob-auth-chip" title="Auth context">{authLabel}</span>
    </div>
  );
}

function TabStrip({
  view,
  onSelect,
  token,
  onTokenChange,
  onRefresh,
  warnings,
}: {
  view: "cockpit" | "brigade" | "telemetry" | "manual";
  onSelect: (view: "cockpit" | "brigade" | "telemetry" | "manual") => void;
  token: string;
  onTokenChange: (token: string) => void;
  onRefresh: () => void;
  warnings: string[];
}) {
  return (
    <div className="ob-tabstrip">
      <button
        type="button"
        className={`ob-tab ${view === "cockpit" ? "active" : ""}`}
        onClick={() => onSelect("cockpit")}
      >
        Cockpit
      </button>
      <button
        type="button"
        className={`ob-tab ${view === "brigade" ? "active" : ""}`}
        onClick={() => onSelect("brigade")}
      >
        Brigade
      </button>
      <button
        type="button"
        className={`ob-tab ${view === "telemetry" ? "active" : ""}`}
        onClick={() => onSelect("telemetry")}
      >
        Telemetry
      </button>
      <span className="ob-tab disabled" aria-disabled="true">Knowledge Base</span>
      <span className="ob-tab-add" aria-hidden="true">+</span>
      <div className="ob-tab-right">
        {warnings.length > 0 && (
          <div className="ob-tab-warnings">
            {warnings.map((warning) => (
              <strong key={warning} className="inline-warning">{warning}</strong>
            ))}
          </div>
        )}
        <div className="ob-tab-token token-control">
          <input
            aria-label="JWT token"
            placeholder="JWT token"
            value={token}
            onChange={(event) => onTokenChange(event.target.value)}
          />
          <button onClick={onRefresh}>Refresh</button>
        </div>
      </div>
    </div>
  );
}

function AboutDialog({ cockpit, onClose }: { cockpit: CockpitPayload | null; onClose: () => void }) {
  return (
    <div className="modal-backdrop ob-about-backdrop" onClick={onClose}>
      <div className="ob-about" onClick={(event) => event.stopPropagation()}>
        <div className="ob-about-titlebar">About</div>
        <div className="ob-about-body">
          <div className="ob-about-logo">
            <span />
          </div>
          <div className="ob-about-name">OpenBrigade</div>
          <div className="ob-about-version">
            Orchestrator daemon{cockpit ? ` · up ${formatDuration(cockpit.uptime_seconds)}` : ""}
          </div>
          <p className="ob-about-blurb">
            An always-on control panel for a brigade of AI agents, coordinated by a
            heartbeat-driven Orchestrator.
          </p>
          <button type="button" className="active" onClick={onClose}>OK</button>
          <div className="ob-about-copy">© 2025-2026 The Brigade Project</div>
        </div>
      </div>
    </div>
  );
}

function formatClock(date: Date) {
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

const HOST_METRIC_CARDS: { label: string; unit?: string }[] = [
  { label: "CPU Usage", unit: "%" },
  { label: "CPU Temp", unit: "°C" },
  { label: "GPU Usage", unit: "%" },
  { label: "GPU Temp", unit: "°C" },
  { label: "Memory", unit: "%" },
  { label: "Disk", unit: "%" },
  { label: "Load Avg" },
  { label: "Host Uptime" },
];

function MetricCard({
  label,
  value,
  sublabel,
  unit,
  tone,
  pending,
}: {
  label: string;
  value?: React.ReactNode;
  sublabel?: string;
  unit?: string;
  tone?: "ok" | "warn" | "bad";
  pending?: boolean;
}) {
  return (
    <div className={`ob-panel ob-metric-card${pending ? " pending" : ""}`}>
      <div className="ob-metric-label">{label}</div>
      <div className="ob-metric-value">
        {pending ? "—" : value}
        {unit && !pending && <span className="ob-metric-unit">{unit}</span>}
      </div>
      <div className="ob-metric-sub">
        {tone && !pending && <span className={`status-light ${tone === "ok" ? "ok" : tone === "bad" ? "bad" : ""}`} />}
        <span>{pending ? "pending host collector" : sublabel || ""}</span>
      </div>
    </div>
  );
}

function TelemetryRow({
  tone,
  name,
  badge,
  detail,
  tag,
}: {
  tone: "ok" | "warn" | "bad";
  name: React.ReactNode;
  badge?: React.ReactNode;
  detail?: React.ReactNode;
  tag?: React.ReactNode;
}) {
  return (
    <div className="ob-tele-row">
      <span className={`status-light ${tone === "ok" ? "ok" : tone === "bad" ? "bad" : ""}`} />
      <span className="ob-tele-row-main">
        <span className="ob-tele-row-name">
          {name}
          {badge}
        </span>
        {detail && <span className="ob-tele-row-detail">{detail}</span>}
      </span>
      {tag && <span className="ob-tele-row-tag">{tag}</span>}
    </div>
  );
}

function TelemetryView({
  cockpit,
  settings,
  models,
  canEdit,
  api,
  onSettingsChange,
  setStatus,
}: {
  cockpit: CockpitPayload | null;
  settings: SettingsPayload | null;
  models: ModelInventory | null;
  canEdit: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onSettingsChange: (settings: SettingsPayload) => void;
  setStatus: (message: string) => void;
}) {
  const usage = cockpit?.usage || null;
  const datastores = cockpit?.datastores || [];
  const options = models?.options || [];
  const availableModels = options.filter((opt) => opt.available).length;
  const okStores = datastores.filter((store) => store.ok).length;
  const allStoresOk = datastores.length > 0 && okStores === datastores.length;
  const unsafeBind = cockpit?.auth?.unsafe_bind_without_auth ?? false;
  const localInference = (cockpit?.local_inference || null) as {
    status?: string;
    holder?: string | null;
    next_available?: string | null;
  } | null;
  const cloudJobs = (cockpit?.cloud_jobs || []) as {
    job_id?: string;
    agent_id?: string;
    provider?: string;
    model?: string;
    status?: string;
  }[];
  const infBusy = localInference?.status === "busy";

  const proactiveMode = String(settings?.proactive_mode ?? "propose");
  const proactiveCreationEnabled = Boolean(settings?.proactive_creation_enabled);
  const maxProactivePerCycle = Number(settings?.max_proactive_creations_per_cycle ?? 1);
  const overriddenKeys = settings?.runtime_overrides
    ? Object.keys(settings.runtime_overrides)
    : [];

  const [modeDraft, setModeDraft] = useState(proactiveMode);
  const [creationDraft, setCreationDraft] = useState(proactiveCreationEnabled);
  const [maxDraft, setMaxDraft] = useState(String(maxProactivePerCycle));
  const [savingProactive, setSavingProactive] = useState(false);

  useEffect(() => {
    setModeDraft(proactiveMode);
    setCreationDraft(proactiveCreationEnabled);
    setMaxDraft(String(maxProactivePerCycle));
  }, [proactiveMode, proactiveCreationEnabled, maxProactivePerCycle]);

  const proactiveDirty =
    modeDraft !== proactiveMode ||
    creationDraft !== proactiveCreationEnabled ||
    maxDraft !== String(maxProactivePerCycle);

  const saveProactive = async () => {
    const parsedMax = Number.parseInt(maxDraft, 10);
    if (!Number.isFinite(parsedMax) || parsedMax < 0) {
      setStatus("Max creations per cycle must be a non-negative integer");
      return;
    }
    setSavingProactive(true);
    try {
      const next = await api<SettingsPayload>("/api/settings/runtime", {
        method: "PUT",
        json: {
          proactive_mode: modeDraft,
          proactive_creation_enabled: creationDraft,
          max_proactive_creations_per_cycle: parsedMax,
        },
      });
      onSettingsChange(next);
      setStatus("Proactive controls saved — applies on the next orchestrator cycle.");
    } catch (error) {
      setStatus(errorMessage(error));
    } finally {
      setSavingProactive(false);
    }
  };

  const settingValue = (key: string): string => {
    const raw = settings ? settings[key] : null;
    return raw === null || raw === undefined || raw === "" ? "—" : String(raw);
  };

  const settingsRows: { key: string; value: React.ReactNode }[] = [
    {
      key: "Authentication",
      value: settings ? (
        <span className={`ob-badge ${settings.require_auth ? "ok" : "warn"}`}>
          {settings.require_auth ? "REQUIRED" : "OPEN"}
        </span>
      ) : (
        "—"
      ),
    },
    {
      key: "Bind address",
      value: settings ? (
        <span className={unsafeBind ? "ob-bad" : undefined}>
          {settings.web_host}:{settings.web_port}
          {unsafeBind ? " ⚠ unsafe" : ""}
        </span>
      ) : (
        "—"
      ),
    },
    { key: "Default provider", value: settingValue("default_provider") },
    { key: "Default model", value: settingValue("default_model") },
    { key: "Log level", value: settingValue("log_level") },
    {
      key: "Orchestrator cadence",
      value: settings?.orchestrator_cadence_seconds != null
        ? `${settingValue("orchestrator_cadence_seconds")}s`
        : "—",
    },
    { key: "Store backend", value: settingValue("store_backend") },
    { key: "Config hash", value: settingValue("config_hash") },
    { key: "API version", value: settingValue("api_version") },
  ];

  const storageRows: { key: string; value: React.ReactNode }[] = [
    { key: "Data directory", value: settingValue("data_dir") },
    { key: "Secret store", value: settingValue("secret_store_path") },
    { key: "Ollama base URL", value: settingValue("ollama_base_url") },
    { key: "Config path", value: settingValue("config_path") },
  ];

  return (
    <section className="telemetry ob-telemetry-view">
      {unsafeBind && (
        <div className="ob-tele-banner">
          ⚠ Gateway is bound to a non-loopback host without authentication
          (unsafe_bind_without_auth). Treat this deployment as development-only.
        </div>
      )}

      <div className="ob-tele-head">
        <span className="ob-panel-title">Server &amp; Host</span>
        <span className="ob-badge warn">PENDING HOST COLLECTOR</span>
      </div>
      <div className="ob-telemetry-cards">
        {HOST_METRIC_CARDS.map((card) => (
          <MetricCard key={card.label} label={card.label} unit={card.unit} pending />
        ))}
      </div>

      <div className="ob-tele-head">
        <span className="ob-panel-title">Gateway</span>
        <span className="ob-badge subtle">live snapshot</span>
      </div>
      <div className="ob-telemetry-cards">
        <MetricCard
          label="Gateway Uptime"
          value={cockpit ? formatDuration(cockpit.uptime_seconds) : "—"}
          sublabel="web process"
        />
        <MetricCard
          label="Models Available"
          value={models ? `${availableModels}/${options.length}` : "—"}
          sublabel="providers reachable"
          tone={availableModels ? "ok" : "warn"}
        />
        <MetricCard
          label="Total Tokens"
          value={usage ? usage.total_tokens.toLocaleString() : "—"}
          sublabel={usage?.last_recorded_at ? `last ${formatLogTime(usage.last_recorded_at)}` : "no usage yet"}
        />
        <MetricCard
          label="Est. Cost"
          value={usage ? `$${usage.estimated_cost_usd.toFixed(2)}` : "—"}
          sublabel="cumulative"
        />
      </div>

      <div className="ob-telemetry-panels">
        <div className="ob-panel ob-tele-panel">
          <div className="ob-panel-head">
            <span className="ob-panel-title">Model Availability</span>
            <span className={`ob-badge ${availableModels ? "ok" : "warn"}`}>
              {models ? `${availableModels}/${options.length}` : "—"}
            </span>
          </div>
          <div className="ob-tele-list">
            {options.length === 0 && <p className="muted">No models reported.</p>}
            {options.map((opt) => (
              <TelemetryRow
                key={`${opt.provider}:${opt.model}`}
                tone={opt.available ? "ok" : opt.configured ? "warn" : "bad"}
                name={opt.label}
                badge={opt.is_default ? <span className="ob-badge subtle">DEFAULT</span> : null}
                detail={`${opt.provider} · ${opt.route_type}${opt.detail ? ` · ${opt.detail}` : ""}`}
                tag={opt.available ? "AVAILABLE" : opt.configured ? "CONFIGURED" : "OFFLINE"}
              />
            ))}
          </div>
        </div>

        <div className="ob-panel ob-tele-panel">
          <div className="ob-panel-head">
            <span className="ob-panel-title">Infrastructure</span>
            <span className={`ob-badge ${allStoresOk ? "ok" : "warn"}`}>
              {datastores.length ? `${okStores}/${datastores.length} OK` : "—"}
            </span>
          </div>
          <div className="ob-tele-list">
            {datastores.length === 0 && <p className="muted">No datastores configured.</p>}
            {datastores.map((store) => (
              <TelemetryRow
                key={store.name}
                tone={store.ok ? "ok" : "bad"}
                name={store.name}
                detail={store.detail}
                tag={store.ok ? "UP" : "DOWN"}
              />
            ))}
          </div>
        </div>

        <div className="ob-panel ob-tele-panel">
          <div className="ob-panel-head">
            <span className="ob-panel-title">Local Inference</span>
            <span className={`ob-badge ${infBusy ? "warn" : "ok"}`}>
              {localInference?.status ? localInference.status.toUpperCase() : "—"}
            </span>
          </div>
          <div className="ob-sys-rows">
            <div className="ob-sys-row">
              <span>Status</span>
              <span className="ob-sys-val">{localInference?.status || "—"}</span>
            </div>
            <div className="ob-sys-row">
              <span>Holder</span>
              <span className="ob-sys-val">{localInference?.holder || "idle"}</span>
            </div>
            <div className="ob-sys-row">
              <span>Cloud jobs</span>
              <span className="ob-sys-val">{cloudJobs.length}</span>
            </div>
            {cloudJobs.slice(0, 4).map((job, idx) => (
              <div key={job.job_id || idx} className="ob-sys-row">
                <span>{job.agent_id || job.job_id || `job ${idx + 1}`}</span>
                <span className="ob-sys-val">
                  {[job.provider, job.model, job.status].filter(Boolean).join(" · ") || "—"}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div className="ob-panel ob-tele-panel">
          <div className="ob-panel-head">
            <span className="ob-panel-title">Storage &amp; Config</span>
          </div>
          <div className="ob-sys-rows">
            {storageRows.map((row) => (
              <div key={row.key} className="ob-sys-row">
                <span>{row.key}</span>
                <span className="ob-sys-val">{row.value}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="ob-panel ob-tele-panel">
          <div className="ob-panel-head">
            <span className="ob-panel-title">OpenBrigade Settings</span>
            {unsafeBind && <span className="ob-badge warn">UNSAFE BIND</span>}
          </div>
          <div className="ob-sys-rows">
            {settingsRows.map((row) => (
              <div key={row.key} className="ob-sys-row">
                <span>{row.key}</span>
                <span className="ob-sys-val">{row.value}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="ob-panel ob-tele-panel ob-tele-controls">
          <div className="ob-panel-head">
            <span className="ob-panel-title">Proactive Continuation</span>
            {overriddenKeys.length > 0 ? (
              <span className="ob-badge subtle">RUNTIME OVERRIDE</span>
            ) : (
              <span className="ob-badge subtle">CONFIG DEFAULT</span>
            )}
          </div>
          <p className="ob-tele-control-note">
            Live orchestrator controls. Changes are stored as runtime overrides and take
            effect on the next cycle — no redeploy.
          </p>
          <div className="ob-control-row">
            <label htmlFor="ob-proactive-mode">Mode</label>
            <select
              id="ob-proactive-mode"
              className="ob-mo-input"
              value={modeDraft}
              disabled={!canEdit || savingProactive}
              onChange={(event) => setModeDraft(event.target.value)}
            >
              <option value="off">off — no proactive work</option>
              <option value="propose">propose — suggest only</option>
              <option value="create">create — auto-create &amp; assign</option>
            </select>
          </div>
          <div className="ob-control-row">
            <label htmlFor="ob-proactive-creation">Creation enabled</label>
            <label className="ob-control-toggle">
              <input
                id="ob-proactive-creation"
                type="checkbox"
                checked={creationDraft}
                disabled={!canEdit || savingProactive}
                onChange={(event) => setCreationDraft(event.target.checked)}
              />
              <span>{creationDraft ? "ON" : "OFF"}</span>
            </label>
          </div>
          <div className="ob-control-row">
            <label htmlFor="ob-proactive-max">Max creations / cycle</label>
            <input
              id="ob-proactive-max"
              className="ob-mo-input ob-control-number"
              type="number"
              min={0}
              step={1}
              value={maxDraft}
              disabled={!canEdit || savingProactive}
              onChange={(event) => setMaxDraft(event.target.value)}
            />
          </div>
          <div className="ob-control-actions">
            {!canEdit && (
              <span className="muted">Operator or owner role required to edit.</span>
            )}
            <button
              type="button"
              className="ob-mo-btn primary"
              disabled={!canEdit || savingProactive || !proactiveDirty}
              onClick={saveProactive}
            >
              {savingProactive ? "Saving…" : "Save"}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}

const MO_PRIORITIES = ["low", "normal", "high", "critical"];

function moStatusGroup(status: string): "running" | "blocked" | "queued" | "done" {
  if (status === "working" || status === "assigned") return "running";
  if (status === "blocked") return "blocked";
  if (status === "queued") return "queued";
  return "done";
}

function moStatusBadgeTone(status: string): string {
  const group = moStatusGroup(status);
  if (group === "running") return "ok";
  if (group === "blocked") return "warn";
  return "subtle";
}

function moAuditAction(event: OrchestrationEvent): string {
  const type = (event.type || "").toLowerCase();
  if (type.startsWith("operator_")) return type.slice("operator_".length).toUpperCase();
  if (type.includes("escalat")) return "ESCALATE";
  if (type.includes("reassign")) return "REASSIGN";
  if (type.includes("retry")) return "RETRY";
  if (type.includes("analysis")) return "ANALYZE";
  if (type.includes("deleg")) return "DELEGATE";
  if (type.includes("block")) return "BLOCK";
  if (type.includes("complete")) return "COMPLETE";
  if (type.includes("decision")) return "DECISION";
  if (type.includes("outcome")) return "CYCLE";
  return (event.type || "EVENT").split("_")[0].toUpperCase().slice(0, 10);
}

function moAuditActor(event: OrchestrationEvent): string {
  const source = (event.source || "").toLowerCase();
  if (source.includes("operator")) return "OPERATOR";
  if (source.includes("runner") || source.includes("system")) return "SYSTEM";
  return "ORCH";
}

function moActionTone(action: string): string {
  if (["CANCEL", "ARCHIVE", "RETIRE", "FAIL", "ABANDON"].includes(action)) return "bad";
  if (["BLOCK", "ESCALATE"].includes(action)) return "warn";
  if (["COMPLETE", "RETRY", "UNBLOCK", "REISSUE"].includes(action)) return "ok";
  return "info";
}

function manualDependencyHealth(assignments: Assignment[]): {
  analyzed: number;
  cycles: number;
  broken: number;
  orphans: number;
} {
  const ids = new Set(assignments.map((a) => a.assignment_id));
  const byId = new Map(assignments.map((a) => [a.assignment_id, a]));
  let analyzed = 0;
  let broken = 0;
  let orphans = 0;
  for (const task of assignments) {
    const deps = task.dependency_ids || [];
    analyzed += deps.length;
    for (const dep of deps) {
      if (!ids.has(dep)) broken += 1;
    }
    if (task.parent_assignment_id && !ids.has(task.parent_assignment_id)) orphans += 1;
  }
  const visiting = new Set<string>();
  const settled = new Set<string>();
  let cycles = 0;
  const walk = (id: string): boolean => {
    if (visiting.has(id)) return true;
    if (settled.has(id)) return false;
    visiting.add(id);
    for (const dep of byId.get(id)?.dependency_ids || []) {
      if (byId.has(dep) && walk(dep)) {
        visiting.delete(id);
        return true;
      }
    }
    visiting.delete(id);
    settled.add(id);
    return false;
  };
  for (const task of assignments) {
    if (!settled.has(task.assignment_id) && walk(task.assignment_id)) cycles += 1;
  }
  return { analyzed, cycles, broken, orphans };
}

function ManualTaskRow({
  task,
  selected,
  agents,
  onSelect,
  spinning,
}: {
  task: Assignment;
  selected: boolean;
  agents: VisualAgent[];
  onSelect: (id: string) => void;
  spinning?: boolean;
}) {
  const agent = agents.find((a) => a.agent_id === task.assigned_to);
  const hasDeps = (task.dependency_ids || []).length > 0;
  return (
    <button
      type="button"
      className={`ob-mo-row ${selected ? "selected" : ""}`}
      style={sigStyle(task.assigned_to)}
      onClick={() => onSelect(task.assignment_id)}
    >
      <span className="ob-mo-row-id">
        {task.assignment_id.slice(0, 8)}
        {spinning && <span className="ob-mo-spin" aria-hidden="true" />}
      </span>
      <span className="ob-mo-row-title">{task.assignment}</span>
      <span className="ob-mo-row-foot">
        <span className="ob-mo-row-agent">{agent?.display_name || task.assigned_to}</span>
        {hasDeps && <span className="ob-mo-row-dep">↳ dep</span>}
      </span>
    </button>
  );
}

function ManualTaskGroup({
  label,
  tone,
  tasks,
  selectedId,
  agents,
  onSelect,
  spinning,
}: {
  label: string;
  tone: string;
  tasks: Assignment[];
  selectedId: string | null;
  agents: VisualAgent[];
  onSelect: (id: string) => void;
  spinning?: boolean;
}) {
  return (
    <>
      <div className={`ob-mo-group ${tone}`}>
        <span className="ob-mo-group-dot" />
        <span className="ob-mo-group-label">{label}</span>
        <span className="ob-mo-group-count">{tasks.length}</span>
      </div>
      {tasks.map((task) => (
        <ManualTaskRow
          key={task.assignment_id}
          task={task}
          selected={task.assignment_id === selectedId}
          agents={agents}
          onSelect={onSelect}
          spinning={spinning}
        />
      ))}
    </>
  );
}

function ManualDepRow({ label, value, tone }: { label: string; value: number; tone: string }) {
  return (
    <div className="ob-mo-dep-row">
      <span>{label}</span>
      <span className={`ob-mo-dep-val ${tone}`}>{value}</span>
    </div>
  );
}

function ManualOrchestrationView({
  assignments,
  agents,
  events,
  role,
  api,
  onRefresh,
  setStatus,
}: {
  assignments: Assignment[];
  agents: VisualAgent[];
  events: OrchestrationEvent[];
  role: string;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onRefresh: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState({ assignment: "", priority: "normal", assigned_to: "" });
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResult, setSearchResult] = useState<Assignment | null>(null);
  const [searching, setSearching] = useState(false);
  const [reassigning, setReassigning] = useState(false);
  const [reassignTarget, setReassignTarget] = useState("");

  if (role !== "owner" && role !== "operator") {
    return (
      <section className="manual ob-mo ob-mo-restricted">
        <div>
          <div className="ob-mo-restricted-title">Restricted</div>
          <p className="muted">Manual Orchestration is available to operator and owner roles only.</p>
        </div>
      </section>
    );
  }

  const running = assignments.filter((a) => moStatusGroup(a.status) === "running");
  const blocked = assignments.filter((a) => moStatusGroup(a.status) === "blocked");
  const queued = assignments.filter((a) => moStatusGroup(a.status) === "queued");
  const done = assignments.filter((a) => moStatusGroup(a.status) === "done");
  const selected =
    assignments.find((a) => a.assignment_id === selectedId) ||
    (searchResult && searchResult.assignment_id === selectedId ? searchResult : null);
  const agentName = (id: string) => agents.find((a) => a.agent_id === id)?.display_name || id;
  const deps = manualDependencyHealth(assignments);
  const dependents = selected
    ? assignments.filter((a) => (a.dependency_ids || []).includes(selected.assignment_id))
    : [];

  const select = (id: string) => {
    setSelectedId(id);
    setEditing(false);
    setReassigning(false);
    setNote("");
    if (!assignments.some((a) => a.assignment_id === id)) return;
    setSearchResult(null);
  };

  const runSearch = async () => {
    const query = searchQuery.trim();
    if (!query) return;
    const local = assignments.find(
      (a) => a.assignment_id === query || a.assignment_id.startsWith(query),
    );
    if (local) {
      select(local.assignment_id);
      setSearchResult(null);
      setStatus(`Selected ${local.assignment_id.slice(0, 8)}`);
      return;
    }
    setSearching(true);
    try {
      const found = await api<Assignment>(
        `/api/tasks/${encodeURIComponent(query)}?include_history=true`,
      );
      setSearchResult(found);
      setSelectedId(found.assignment_id);
      setEditing(false);
      setNote("");
      setStatus(
        found.archived
          ? `Found archived task ${found.assignment_id.slice(0, 8)} (${found.status})`
          : `Found ${found.assignment_id.slice(0, 8)}`,
      );
    } catch (error) {
      setSearchResult(null);
      setStatus(errorMessage(error));
    } finally {
      setSearching(false);
    }
  };

  const run = (label: string, work: Promise<unknown>) => {
    setBusy(true);
    work
      .then(() => onRefresh())
      .then(() => {
        setStatus(label);
        setSelectedId(null);
        setEditing(false);
        setNote("");
      })
      .catch((error) => setStatus(errorMessage(error)))
      .finally(() => setBusy(false));
  };

  const path = (id: string) => `/api/tasks/${encodeURIComponent(id)}`;
  const cancelTask = (id: string) =>
    run(`Cancelled ${id}`, api(`${path(id)}?force=true`, { method: "DELETE" }));
  const retryTask = (id: string) =>
    run(`Retried ${id}`, api(`${path(id)}/reissue`, { method: "POST" }));
  const reissueAsNew = (id: string) =>
    run(`Reissued ${id} as a new task`, api(`${path(id)}/reissue-as-new`, { method: "POST", json: { note } }));
  const reassignTask = (id: string, agentId: string) => {
    setReassigning(false);
    run(`Reassigned ${id.slice(0, 8)} → ${agents.find((a) => a.agent_id === agentId)?.display_name ?? agentId}`, api(path(id), { method: "PATCH", json: { assigned_to: agentId } }));
  };
  const saveEdit = (id: string) =>
    run(`Updated ${id}`, api(path(id), {
      method: "PATCH",
      json: { assignment: draft.assignment, priority: draft.priority, assigned_to: draft.assigned_to },
    }));

  const beginEdit = () => {
    if (!selected) return;
    setDraft({ assignment: selected.assignment, priority: selected.priority, assigned_to: selected.assigned_to });
    setEditing(true);
  };

  return (
    <section className="manual ob-mo">
      {/* ===== LEFT: task queue + inspector ===== */}
      <div className="ob-mo-left">
        <div className="ob-mo-col-head">
          <span className="ob-panel-title">Task Queue</span>
          <span className="ob-mo-summary">{running.length}R · {blocked.length}B · {queued.length}Q</span>
        </div>
        <form
          className="ob-mo-search"
          onSubmit={(event) => {
            event.preventDefault();
            void runSearch();
          }}
        >
          <input
            className="ob-mo-input"
            type="text"
            placeholder="Find by task ID (incl. failed/archived)…"
            value={searchQuery}
            onChange={(event) => setSearchQuery(event.target.value)}
            spellCheck={false}
          />
          <button type="submit" className="ob-mo-btn" disabled={searching || !searchQuery.trim()}>
            {searching ? "…" : "Find"}
          </button>
        </form>
        {searchResult && searchResult.assignment_id === selectedId && (
          <div className="ob-mo-search-hit">
            <span className={`ob-badge ${searchResult.archived ? "warn" : "ok"}`}>
              {searchResult.archived ? "ARCHIVED" : "LIVE"}
            </span>
            <span className="ob-mono-id">{searchResult.assignment_id.slice(0, 8)}</span>
            <span className="muted">· {searchResult.status}</span>
          </div>
        )}
        <div className="ob-mo-list">
          <ManualTaskGroup label="RUNNING" tone="run" tasks={running} selectedId={selectedId} agents={agents} onSelect={select} spinning />
          <ManualTaskGroup label="BLOCKED" tone="warn" tasks={blocked} selectedId={selectedId} agents={agents} onSelect={select} />
          <ManualTaskGroup label="QUEUED" tone="dim" tasks={queued} selectedId={selectedId} agents={agents} onSelect={select} />
          <ManualTaskGroup label="DONE" tone="faint" tasks={done} selectedId={selectedId} agents={agents} onSelect={select} />
        </div>
        <div className="ob-mo-inspector">
          <div className="ob-mo-col-head">
            <span className="ob-panel-title">Task Inspector</span>
            {selected && <span className={`ob-badge ${moStatusBadgeTone(selected.status)}`}>{selected.status.toUpperCase()}</span>}
          </div>
          {!selected ? (
            <p className="muted ob-mo-pad">No task selected</p>
          ) : (
            <div className="ob-mo-pad">
              <div className="ob-mo-insp-title">
                <span className="ob-mono-id">{selected.assignment_id.slice(0, 8)}</span> {selected.assignment}
              </div>
              <div className="ob-mo-insp-meta">
                <span className="ob-mo-agent" style={sigStyle(selected.assigned_to)}>{agentName(selected.assigned_to)}</span>
                <span>· {selected.priority}</span>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* ===== CENTER: detail ===== */}
      <div className="ob-mo-center">
        {!selected ? (
          <div className="ob-mo-empty">↑ Select a task from the list to view or act on it.</div>
        ) : (
          <>
            <div className={`ob-mo-detail-head ${moStatusGroup(selected.status)}`}>
              <div className="ob-mo-detail-tags">
                <span className="ob-mono-id">{selected.assignment_id.slice(0, 8)}</span>
                <span className={`ob-badge ${moStatusBadgeTone(selected.status)}`}>{selected.status.toUpperCase()}</span>
                <span className="ob-mo-prio">▲ {selected.priority}</span>
                {selected.reissued_from_assignment_id && (
                  <span className="ob-mo-lineage">reissued from {selected.reissued_from_assignment_id.slice(0, 8)}</span>
                )}
              </div>
              <div className="ob-mo-detail-title">{selected.assignment}</div>
            </div>

            <div className="ob-mo-detail-body">
              <div className="ob-mo-fields">
                <span>Assigned to</span>
                <span className="ob-mo-agent" style={sigStyle(selected.assigned_to)}>{agentName(selected.assigned_to)}</span>
                <span>Description</span>
                <span className="ob-mo-desc">{selected.progress_summary || selected.assignment}</span>
                <span>Depends on</span>
                <span className="ob-mono-id">
                  {(selected.dependency_ids || []).length ? (selected.dependency_ids || []).map((d) => d.slice(0, 8)).join(", ") : "None"}
                </span>
                {moStatusGroup(selected.status) === "blocked" && (
                  <>
                    <span>Block reason</span>
                    <span className="ob-mo-block">{selected.last_error || selected.blockers?.[0] || "unknown"}</span>
                  </>
                )}
              </div>

              {/* ARCHIVED: read-only history record */}
              {selected.archived && (
                <div className="ob-mo-archived">
                  <div className="ob-mo-archived-head">
                    ⓘ Archived task — read-only history record. IDs are never reused.
                  </div>
                  <div className="ob-mo-fields">
                    <span>Final status</span>
                    <span>{selected.final_status || selected.status}</span>
                    <span>Archived at</span>
                    <span>{selected.archived_at ? formatLogTime(selected.archived_at) : "—"}</span>
                    {selected.executive_summary && (
                      <>
                        <span>Summary</span>
                        <span className="ob-mo-desc">{selected.executive_summary}</span>
                      </>
                    )}
                    {selected.failure_info && (
                      <>
                        <span>Failure</span>
                        <span className="ob-mo-block">{selected.failure_info}</span>
                      </>
                    )}
                  </div>
                </div>
              )}

              {/* TASK RELATIONSHIPS: full upstream → selected → downstream tree */}
              {!selected.archived && ((selected.dependency_ids || []).length > 0 || dependents.length > 0) && (() => {
                const upstreamIds = selected.dependency_ids || [];
                const selectedColor = moStatusGroup(selected.status) === "blocked" ? "var(--c-warn)" : moStatusGroup(selected.status) === "running" ? "var(--sig)" : "var(--c-text-faint)";
                const depColor = (s: string) => moStatusGroup(s) === "done" ? "var(--c-ok)" : moStatusGroup(s) === "blocked" ? "var(--c-warn)" : moStatusGroup(s) === "running" ? "var(--sig)" : "var(--c-text-faint)";
                return (
                  <div className="ob-mo-rel">
                    <div className="ob-mo-rel-head">TASK RELATIONSHIPS</div>
                    <div className="ob-mo-rel-graph ob-mo-rel-vertical">
                      {upstreamIds.length > 0 && (
                        <>
                          <div className="ob-mo-rel-row">
                            {upstreamIds.map((depId) => {
                              const dep = assignments.find((a) => a.assignment_id === depId);
                              return dep ? (
                                <button key={depId} type="button" className="ob-mo-rel-node clickable" style={sigStyle(dep.assigned_to)} onClick={() => select(dep.assignment_id)}>
                                  <div className="ob-mo-rel-bar" style={{ background: depColor(dep.status) }} />
                                  <div className="ob-mo-rel-id">{dep.assignment_id.slice(0, 8)} · inspect →</div>
                                  <div className="ob-mo-rel-title">{dep.assignment}</div>
                                </button>
                              ) : (
                                <div key={depId} className="ob-mo-rel-node">
                                  <div className="ob-mo-rel-bar" style={{ background: "var(--c-text-faint)" }} />
                                  <div className="ob-mo-rel-id">{depId.slice(0, 8)}</div>
                                  <div className="ob-mo-rel-title muted">not in active queue</div>
                                </div>
                              );
                            })}
                          </div>
                          <div className="ob-mo-rel-vlink"><span>blocks</span><span className="ob-mo-rel-arrow">▼</span></div>
                        </>
                      )}
                      <div className="ob-mo-rel-node" style={{ outline: "1px solid var(--c-border)" }}>
                        <div className="ob-mo-rel-bar" style={{ background: selectedColor }} />
                        <div className="ob-mo-rel-id">{selected.assignment_id.slice(0, 8)} ← this task</div>
                        <div className="ob-mo-rel-title">{selected.assignment}</div>
                      </div>
                      {dependents.length > 0 && (
                        <>
                          <div className="ob-mo-rel-vlink"><span>blocks</span><span className="ob-mo-rel-arrow">▼</span></div>
                          <div className="ob-mo-rel-row">
                            {dependents.map((dep) => (
                              <button key={dep.assignment_id} type="button" className="ob-mo-rel-node clickable" style={sigStyle(dep.assigned_to)} onClick={() => select(dep.assignment_id)}>
                                <div className="ob-mo-rel-bar" style={{ background: depColor(dep.status) }} />
                                <div className="ob-mo-rel-id">{dep.assignment_id.slice(0, 8)} · inspect →</div>
                                <div className="ob-mo-rel-title">{dep.assignment}</div>
                              </button>
                            ))}
                          </div>
                        </>
                      )}
                    </div>
                  </div>
                );
              })()}

              {/* QUEUED edit form */}
              {moStatusGroup(selected.status) === "queued" && editing ? (
                <div className="ob-mo-form">
                  <label className="ob-mo-label">TITLE / INSTRUCTION</label>
                  <textarea className="ob-mo-input" rows={3} value={draft.assignment} onChange={(e) => setDraft({ ...draft, assignment: e.target.value })} />
                  <div className="ob-mo-form-grid">
                    <div>
                      <label className="ob-mo-label">ASSIGNED AGENT</label>
                      <select className="ob-mo-input" value={draft.assigned_to} onChange={(e) => setDraft({ ...draft, assigned_to: e.target.value })}>
                        {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{a.display_name}</option>)}
                      </select>
                    </div>
                    <div>
                      <label className="ob-mo-label">PRIORITY</label>
                      <select className="ob-mo-input" value={draft.priority} onChange={(e) => setDraft({ ...draft, priority: e.target.value })}>
                        {MO_PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
                      </select>
                    </div>
                  </div>
                  <div className="ob-mo-actions">
                    <button className="ob-mo-btn primary" disabled={busy} onClick={() => saveEdit(selected.assignment_id)}>Save Changes</button>
                    <button className="ob-mo-btn" disabled={busy} onClick={() => setEditing(false)}>Discard</button>
                  </div>
                  <div className="ob-mo-rules">Task ID does not change on edit. Use “Reissue as New” to create a new versioned attempt.</div>
                </div>
              ) : (
                <>
                  {/* Action buttons by status */}
                  {moStatusGroup(selected.status) === "queued" && (
                    <>
                      <div className="ob-mo-actions">
                        <button className="ob-mo-btn" disabled={busy} onClick={beginEdit}>Edit Task</button>
                        <button className="ob-mo-btn" disabled={busy} onClick={() => { setReassignTarget(selected.assigned_to); setReassigning((r) => !r); }}>↔ Reassign</button>
                        <button className="ob-mo-btn" disabled={busy} onClick={() => reissueAsNew(selected.assignment_id)}>Reissue as New ↗</button>
                        <button className="ob-mo-btn danger" disabled={busy} onClick={() => cancelTask(selected.assignment_id)}>Cancel Task</button>
                      </div>
                      {reassigning && (
                        <div className="ob-mo-reassign">
                          <select className="ob-mo-input" value={reassignTarget} onChange={(e) => setReassignTarget(e.target.value)}>
                            {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{a.display_name}</option>)}
                          </select>
                          <button className="ob-mo-btn primary" disabled={busy || reassignTarget === selected.assigned_to} onClick={() => reassignTask(selected.assignment_id, reassignTarget)}>Confirm Reassign</button>
                          <button className="ob-mo-btn" onClick={() => setReassigning(false)}>Cancel</button>
                        </div>
                      )}
                    </>
                  )}
                  {moStatusGroup(selected.status) === "blocked" && (
                    <>
                      <div className="ob-mo-actions">
                        <button className="ob-mo-btn" disabled={busy} onClick={() => retryTask(selected.assignment_id)}>↻ Retry Now</button>
                        <button className="ob-mo-btn" disabled={busy} onClick={() => { setReassignTarget(selected.assigned_to); setReassigning((r) => !r); }}>↔ Reassign</button>
                        <button className="ob-mo-btn" disabled={busy} onClick={() => reissueAsNew(selected.assignment_id)}>Reissue as New ↗</button>
                        <button className="ob-mo-btn danger" disabled={busy} onClick={() => cancelTask(selected.assignment_id)}>✕ Retire Task</button>
                        <button className="ob-mo-btn" disabled title="Phase 2">⬡ Repair Relationship</button>
                      </div>
                      {reassigning && (
                        <div className="ob-mo-reassign">
                          <select className="ob-mo-input" value={reassignTarget} onChange={(e) => setReassignTarget(e.target.value)}>
                            {agents.map((a) => <option key={a.agent_id} value={a.agent_id}>{a.display_name}</option>)}
                          </select>
                          <button className="ob-mo-btn primary" disabled={busy || reassignTarget === selected.assigned_to} onClick={() => reassignTask(selected.assignment_id, reassignTarget)}>Confirm Reassign</button>
                          <button className="ob-mo-btn" onClick={() => setReassigning(false)}>Cancel</button>
                        </div>
                      )}
                    </>
                  )}
                  {moStatusGroup(selected.status) === "running" && (
                    <>
                      <div className="ob-mo-locked">⚑ Running — task properties are locked. Archive stops it; other interventions arrive in Phase 2.</div>
                      <div className="ob-mo-actions">
                        <button className="ob-mo-btn danger" disabled={busy} onClick={() => cancelTask(selected.assignment_id)}>✕ Archive</button>
                        <button className="ob-mo-btn" disabled title="Phase 2">⏸ Pause</button>
                        <button className="ob-mo-btn" disabled title="Phase 2">↔ Reassign</button>
                        <button className="ob-mo-btn" disabled title="Phase 2">▲ Escalate</button>
                        <button className="ob-mo-btn" disabled title="Phase 2">✎ Annotate</button>
                      </div>
                    </>
                  )}
                  {/* reissue note (queued/blocked) */}
                  {(moStatusGroup(selected.status) === "queued" || moStatusGroup(selected.status) === "blocked") && (
                    <input className="ob-mo-input" placeholder="Optional note for reissue / audit…" value={note} onChange={(e) => setNote(e.target.value)} />
                  )}
                  {!selected.archived && (
                    <div className="ob-mo-rules">
                      Cancel archives the task — started tasks are never hard-deleted. Reissue as New mints a new task ID and retires this one (IDs are never reused). Every action is written to the audit log.
                    </div>
                  )}
                </>
              )}
            </div>
          </>
        )}
      </div>

      {/* ===== RIGHT: audit + deps ===== */}
      <div className="ob-mo-right">
        <div className="ob-mo-col-head"><span className="ob-panel-title">Audit Log</span><span className="ob-badge subtle">live</span></div>
        <div className="ob-mo-audit">
          {events.length === 0 && <p className="muted ob-mo-pad">No audit activity recorded.</p>}
          {events.slice(0, 40).map((event) => {
            const action = moAuditAction(event);
            return (
              <div key={event.id} className="ob-mo-audit-row">
                <div className="ob-mo-audit-head">
                  <span className="ob-mo-audit-time">{formatLogTime(event.recorded_at)}</span>
                  <span className={`ob-mo-audit-action ${moActionTone(action)}`}>{action}</span>
                  <span className="ob-mo-audit-actor">{moAuditActor(event)}</span>
                </div>
                <div className="ob-mo-audit-detail">{event.summary}</div>
              </div>
            );
          })}
        </div>
        <div className="ob-mo-col-head"><span className="ob-panel-title">Dependencies</span></div>
        <div className="ob-mo-deps">
          <ManualDepRow label="Dependencies analyzed" value={deps.analyzed} tone="ok" />
          <ManualDepRow label="Cycles detected" value={deps.cycles} tone={deps.cycles ? "bad" : "ok"} />
          <ManualDepRow label="Broken links" value={deps.broken} tone={deps.broken ? "warn" : "ok"} />
          <ManualDepRow label="Orphan tasks" value={deps.orphans} tone={deps.orphans ? "warn" : "ok"} />
        </div>
      </div>
    </section>
  );
}

function CockpitView({
  cockpit,
  settings,
  auth,
  authMessage,
  tokenExpired,
  models,
  selectedAgentId,
  selectedAgentModel,
  orchestratorModel,
  heartbeatPaused,
  can,
  api,
  onSelectAgent,
  onSelectedAgentModelChange,
  onOrchestratorModelChange,
  onModelsChange,
  onSettingsChange,
  onRefresh,
  setStatus,
  onOpenTaskDialog,
}: {
  cockpit: CockpitPayload | null;
  settings: SettingsPayload | null;
  auth: AuthMe | null;
  authMessage: string;
  tokenExpired: boolean;
  models: ModelInventory | null;
  selectedAgentId: string;
  selectedAgentModel: ModelRoute | null;
  orchestratorModel: ModelRoute | null;
  heartbeatPaused: boolean;
  can: (permission: string) => boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onSelectAgent: (agentId: string, panel?: "tasks" | "chat" | "goals") => void;
  onSelectedAgentModelChange: (route: ModelRoute) => void;
  onOrchestratorModelChange: (route: ModelRoute) => void;
  onModelsChange: (models: ModelInventory) => void;
  onSettingsChange: (settings: SettingsPayload) => void;
  onRefresh: () => Promise<void>;
  setStatus: (status: string) => void;
  onOpenTaskDialog: (draft?: TaskDialogDraft) => void;
}) {
  const selectedAgent = cockpit?.agents.find((agent) => agent.agent_id === selectedAgentId) || null;
  const agents = cockpit?.agents || [];
  const datastores = cockpit?.datastores || [];
  const alerts = cockpit?.alerts || [];
  const telemetry = cockpit?.orchestration || null;
  const workingAgents = agents.filter(
    (agent) => agent.status === "working" || agent.status === "assigned",
  );

  return (
    <section className="cockpit ob-cockpit">
      {alerts.length > 0 && (
        <div className="ob-alert-banner">
          <AlertList
            alerts={alerts}
            canClear={can("orchestrator:write")}
            api={api}
            onDone={onRefresh}
            setStatus={setStatus}
          />
        </div>
      )}

      <div className="ob-cockpit-grid">
        {/* ============ LEFT COLUMN ============ */}
        <div className="ob-col ob-col-left">
          <AgentRosterPanel agents={agents} onSelect={(id) => onSelectAgent(id, "tasks")} />
          <ConcurrencyPanel working={workingAgents} />
          <DatastorePanel datastores={datastores} />
        </div>

        {/* ============ CENTER — ORCHESTRATOR ============ */}
        <div className="ob-orchestrator">
          <OrchestratorHeader
            telemetry={telemetry}
            paused={heartbeatPaused}
            cadenceSeconds={Number(settings?.orchestrator_cadence_seconds) || 900}
          />
          <MissionStrip
            mission={cockpit?.mission || null}
            latestReasoning={cockpit?.latest_reasoning || null}
            canEdit={can("mission:write")}
            api={api}
            onDone={onRefresh}
            setStatus={setStatus}
          />
          <TaskQueuePanel
            tasks={cockpit?.tasks || null}
            counts={cockpit?.counts || null}
            agents={agents}
            canCreate={can("task:write")}
            onOpenTaskDialog={onOpenTaskDialog}
            onSelectAgent={(id) => onSelectAgent(id, "tasks")}
          />
          <div className="ob-panel ob-chat-section">
            <div className="ob-panel-head">
              <span className="ob-panel-title">Talk to Orchestrator</span>
            </div>
            <div className="ob-chat-host">
              <OrchestratorChat
                canChat={can("chat:write")}
                api={api}
                inventory={models}
                route={orchestratorModel}
                onRouteChange={onOrchestratorModelChange}
                setStatus={setStatus}
              />
            </div>
          </div>
        </div>

        {/* ============ RIGHT COLUMN ============ */}
        <div className="ob-col ob-col-right">
          <SystemPanel cockpit={cockpit} telemetry={telemetry} />
          <ActivityLogPanel telemetry={telemetry} />
        </div>
      </div>

      <details className="ob-manage">
        <summary>Management &amp; configuration</summary>
        <div className="ob-manage-grid">
          <div className="ob-panel ob-manage-panel">
            <div className="ob-panel-head"><span className="ob-panel-title">Selected Agent</span></div>
            <div className="ob-panel-pad">
              <AgentInspector agent={selectedAgent} teams={cockpit?.teams || []} />
              {selectedAgent && (
                <>
                  <ModelSelect
                    label="Agent model"
                    inventory={models}
                    route={agentModelRoute(selectedAgent, models)}
                    onChange={(route) => {
                      api(`/api/agents/${encodeURIComponent(selectedAgent.agent_id)}`, {
                        method: "PATCH",
                        json: { model_provider: route.provider, model_name: route.model },
                      })
                        .then(() => onRefresh())
                        .then(() => setStatus(`${selectedAgent.agent_id} model set to ${route.model}`))
                        .catch((error) => setStatus(errorMessage(error)));
                    }}
                  />
                  <div className="button-row">
                    <button onClick={() => onSelectAgent(selectedAgent.agent_id, "chat")}>Chat</button>
                    <button onClick={() => onSelectAgent(selectedAgent.agent_id, "tasks")}>Tasks</button>
                  </div>
                </>
              )}
            </div>
          </div>
          <div className="ob-panel ob-manage-panel">
            <div className="ob-panel-head"><span className="ob-panel-title">Models</span></div>
            <div className="ob-panel-pad">
              <ModelSummary
                cockpit={cockpit}
                models={models}
                canEdit={can("admin")}
                api={api}
                onModelsChange={onModelsChange}
                onSettingsChange={onSettingsChange}
                onDone={onRefresh}
                setStatus={setStatus}
              />
            </div>
          </div>
          <div className="ob-panel ob-manage-panel">
            <div className="ob-panel-head"><span className="ob-panel-title">Teams</span></div>
            <div className="ob-panel-pad">
              <TeamBoard
                teams={cockpit?.teams || []}
                agents={agents}
                canEdit={can("team:write")}
                canManageAgents={can("agent:write")}
                models={models}
                api={api}
                onDone={onRefresh}
                setStatus={setStatus}
              />
            </div>
          </div>
          <div className="ob-panel ob-manage-panel">
            <div className="ob-panel-head"><span className="ob-panel-title">Usage</span></div>
            <div className="ob-panel-pad">
              <UsageSummary usage={cockpit?.usage || null} />
            </div>
          </div>
          <div className="ob-panel ob-manage-panel">
            <div className="ob-panel-head"><span className="ob-panel-title">Settings &amp; Auth</span></div>
            <div className="ob-panel-pad">
              <SettingsStatus
                settings={settings}
                cockpit={cockpit}
                auth={auth}
                authMessage={authMessage}
                tokenExpired={tokenExpired}
              />
            </div>
          </div>
        </div>
      </details>
    </section>
  );
}

const AGENT_SIG_COLORS = [
  "var(--c-sage)",
  "var(--c-garde)",
  "var(--c-abacus)",
  "var(--c-accent)",
  "#b07cd6",
  "#d6708f",
  "#4ec9c4",
];

function agentSignature(id: string) {
  let hash = 0;
  for (let index = 0; index < id.length; index += 1) {
    hash = (hash * 31 + id.charCodeAt(index)) >>> 0;
  }
  return AGENT_SIG_COLORS[hash % AGENT_SIG_COLORS.length];
}

function sigStyle(id: string): React.CSSProperties {
  return { "--sig": agentSignature(id) } as React.CSSProperties;
}

function agentInitials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (parts.length >= 2) {
    return (parts[0][0] + parts[1][0]).toUpperCase();
  }
  return (name.trim() || "?").slice(0, 2).toUpperCase();
}

function agentActivityText(agent: VisualAgent) {
  return (
    agent.current_assignment?.assignment ||
    agent.activity ||
    agent.state?.current_assignment_summary ||
    (agent.status === "idle" ? "Idle" : agent.status)
  );
}

function agentStatusBadge(status: string): { label: string; tone: string } {
  switch (status) {
    case "working":
    case "assigned":
      return { label: "WORKING", tone: "ok" };
    case "reflecting":
    case "ruminating":
    case "dreaming":
      return { label: status.toUpperCase(), tone: "warn" };
    case "queued":
      return { label: "QUEUED", tone: "warn" };
    case "blocked":
      return { label: "BLOCKED", tone: "bad" };
    case "awaiting_human":
      return { label: "NEEDS YOU", tone: "bad" };
    case "idle":
      return { label: "IDLE", tone: "idle" };
    default:
      return { label: status.replace(/_/g, " ").toUpperCase(), tone: "idle" };
  }
}

function formatLogTime(value?: string | null) {
  if (!value) {
    return "--:--:--";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--:--:--";
  }
  return date.toLocaleTimeString([], { hour12: false });
}

function AgentRosterPanel({
  agents,
  onSelect,
}: {
  agents: VisualAgent[];
  onSelect: (agentId: string) => void;
}) {
  return (
    <div className="ob-panel ob-roster">
      <div className="ob-panel-head">
        <span className="ob-panel-title">Agents</span>
        <span className="ob-badge">{agents.length}</span>
      </div>
      <div className="ob-roster-list">
        {agents.length === 0 && <p className="muted">No agents.</p>}
        {agents.map((agent) => {
          const badge = agentStatusBadge(agent.status);
          return (
            <button
              key={agent.agent_id}
              type="button"
              className="ob-agent-row"
              style={sigStyle(agent.agent_id)}
              onClick={() => onSelect(agent.agent_id)}
            >
              <span className="ob-agent-avatar">{agentInitials(agent.display_name)}</span>
              <span className="ob-agent-meta">
                <span className="ob-agent-name">{agent.display_name}</span>
                <span className="ob-agent-task">{agentActivityText(agent)}</span>
              </span>
              <span className={`ob-agent-badge ${badge.tone}`}>{badge.label}</span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

function ConcurrencyPanel({ working }: { working: VisualAgent[] }) {
  const slots = working.slice(0, 4);
  return (
    <div className="ob-panel ob-concurrency">
      <div className="ob-panel-head">
        <span className="ob-panel-title">Concurrency</span>
        <span className="ob-badge">{working.length} active</span>
      </div>
      <div className="ob-slots">
        {slots.map((agent, index) => (
          <div key={agent.agent_id} className="ob-slot" style={sigStyle(agent.agent_id)}>
            <span className="ob-slot-avatar">{agentInitials(agent.display_name)}</span>
            <span className="ob-slot-name">{agent.display_name}</span>
            <span className="ob-slot-idx">slot {index + 1}</span>
          </div>
        ))}
        <div className="ob-slot ob-slot-free">
          <span className="ob-slot-plus">+</span>
          <span className="ob-slot-name">FREE</span>
          <span className="ob-slot-idx">slot {slots.length + 1}</span>
        </div>
      </div>
    </div>
  );
}

function DatastorePanel({
  datastores,
}: {
  datastores: { name: string; ok: boolean; detail: string }[];
}) {
  const okCount = datastores.filter((item) => item.ok).length;
  const allOk = datastores.length > 0 && okCount === datastores.length;
  return (
    <div className="ob-panel ob-datastores">
      <div className="ob-panel-head">
        <span className="ob-panel-title">Datastores</span>
        <span className={`ob-badge ${allOk ? "ok" : "warn"}`}>
          {datastores.length ? `${okCount} OK` : "—"}
        </span>
      </div>
      <div className="ob-ds-grid">
        {datastores.length === 0 && <p className="muted">No datastores reported.</p>}
        {datastores.map((store) => (
          <div key={store.name} className="ob-ds">
            <span className={`status-light ${store.ok ? "ok" : "bad"}`} />
            <span className="ob-ds-meta">
              <span className="ob-ds-name">{store.name}</span>
              <span className="ob-ds-detail">{store.detail}</span>
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function NextCycleCountdown({
  events,
  cadenceSeconds,
}: {
  events: OrchestrationEvent[];
  cadenceSeconds: number;
}) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  const lastCycle = events.find((event) => (event.type || "").startsWith("cycle"));
  if (!lastCycle?.recorded_at || !cadenceSeconds) {
    return null;
  }
  const lastMs = new Date(lastCycle.recorded_at).getTime();
  if (Number.isNaN(lastMs)) {
    return null;
  }
  const remaining = Math.round((lastMs + cadenceSeconds * 1000 - now) / 1000);
  const label =
    remaining <= 0
      ? "due now"
      : `${Math.floor(remaining / 60)}:${String(remaining % 60).padStart(2, "0")}`;
  return <span className="ob-beat-next"> · next cycle {label}</span>;
}

function OrchestratorHeader({
  telemetry,
  paused,
  cadenceSeconds,
}: {
  telemetry: OrchestrationPayload | null;
  paused: boolean;
  cadenceSeconds: number;
}) {
  const latest = telemetry?.latest_event || telemetry?.events?.[0] || null;
  const beatState = latest ? orchestrationEventKind(latest).toUpperCase() : "IDLE";
  const beatCount = telemetry?.events?.length ?? 0;
  return (
    <div className="ob-orc-header">
      <div className={`ob-hb ${paused ? "paused" : ""}`} aria-hidden="true">
        <span className="ob-hb-ring" />
        <span className="ob-hb-core" />
      </div>
      <div className="ob-orc-title">
        <span className="ob-orc-name">ORCHESTRATOR</span>
        <span className="ob-orc-sub">always-on daemon · heartbeat-driven</span>
      </div>
      <span className="ob-spacer" />
      <div className="ob-beat">
        <div className="ob-beat-state">{paused ? "PAUSED" : beatState}</div>
        <div className="ob-beat-sub">
          {latest ? `last beat ${formatLogTime(latest.recorded_at)}` : "no beats yet"}
          <NextCycleCountdown events={telemetry?.events || []} cadenceSeconds={cadenceSeconds} />
          {" · "}
          {beatCount} events
        </div>
      </div>
    </div>
  );
}

function MissionStrip({
  mission,
  latestReasoning,
  canEdit,
  api,
  onDone,
  setStatus,
}: {
  mission: Mission | null;
  latestReasoning: { decision_summary?: string; cycle_id?: string } | null;
  canEdit: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="ob-mission">
      <div className="ob-mission-label">
        <span>Current Mission</span>
        {canEdit && (
          <button type="button" className="ob-mini-btn" onClick={() => setOpen((value) => !value)}>
            {open ? "Close" : "Edit"}
          </button>
        )}
      </div>
      {open ? (
        <MissionWidget
          mission={mission}
          latestReasoning={latestReasoning}
          canEdit={canEdit}
          api={api}
          onDone={onDone}
          setStatus={setStatus}
        />
      ) : (
        <div className="ob-mission-text">{mission?.statement || "Mission not set."}</div>
      )}
    </div>
  );
}

function TaskQueuePanel({
  tasks,
  counts,
  agents,
  canCreate,
  onOpenTaskDialog,
  onSelectAgent,
}: {
  tasks: CockpitPayload["tasks"] | null;
  counts: CockpitPayload["counts"] | null;
  agents: VisualAgent[];
  canCreate: boolean;
  onOpenTaskDialog: (draft?: TaskDialogDraft) => void;
  onSelectAgent: (agentId: string) => void;
}) {
  const [filter, setFilter] = useState<"active" | "queued" | "blocked" | "all">("active");
  const source = tasks ? tasks[filter] : [];
  const nameOf = new Map(agents.map((agent) => [agent.agent_id, agent.display_name]));
  const active = counts?.active_tasks ?? tasks?.active.length ?? 0;
  const queued = counts?.queued_tasks ?? tasks?.queued.length ?? 0;
  return (
    <div className="ob-panel ob-queue">
      <div className="ob-panel-head">
        <span className="ob-panel-title">Task Queue</span>
        <span className="ob-queue-counts">{active} active · {queued} pending</span>
      </div>
      <div className="ob-queue-toolbar">
        <div className="segmented">
          {(["active", "queued", "blocked", "all"] as const).map((item) => (
            <button
              key={item}
              className={filter === item ? "active" : ""}
              onClick={() => setFilter(item)}
            >
              {item}
            </button>
          ))}
        </div>
        <button disabled={!canCreate} onClick={() => onOpenTaskDialog()}>
          Add Task
        </button>
      </div>
      <div className="ob-queue-list">
        {source.length === 0 && <p className="muted">No {filter} tasks.</p>}
        {source.map((task) => {
          const working = task.status === "working" || task.status === "assigned";
          return (
            <div
              key={task.assignment_id}
              className="ob-task"
              onClick={() => onSelectAgent(task.assigned_to)}
            >
              <span className={`ob-task-mark ${working ? "spinning" : statusClass(task.status)}`} />
              <span className="ob-task-meta">
                <span className="ob-task-text">{task.assignment}</span>
                <span className="ob-task-sub">
                  {task.status}
                  {task.progress_summary ? ` · ${shortText(task.progress_summary, 44)}` : ""}
                  {task.blockers.length > 0 ? ` · ${shortText(task.blockers[0], 44)}` : ""}
                </span>
              </span>
              <span className="ob-task-agent" style={sigStyle(task.assigned_to)}>
                {nameOf.get(task.assigned_to) || task.assigned_to}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SystemPanel({
  cockpit,
  telemetry,
}: {
  cockpit: CockpitPayload | null;
  telemetry: OrchestrationPayload | null;
}) {
  const counts = cockpit?.counts;
  const usage = cockpit?.usage;
  const latest = telemetry?.latest_event || telemetry?.events?.[0] || null;
  const nominal = counts ? counts.blocked_tasks === 0 && counts.alerts === 0 : true;
  const rows: { key: string; value: React.ReactNode }[] = [
    { key: "Uptime", value: cockpit ? formatDuration(cockpit.uptime_seconds) : "—" },
    { key: "Agents", value: counts ? String(counts.agents) : "—" },
    { key: "Active tasks", value: counts ? String(counts.active_tasks) : "—" },
    { key: "Queue depth", value: counts ? String(counts.queued_tasks) : "—" },
    {
      key: "Blocked",
      value: counts ? (
        <span className={counts.blocked_tasks ? "ob-bad" : undefined}>{counts.blocked_tasks}</span>
      ) : (
        "—"
      ),
    },
    { key: "Last beat", value: latest ? formatLogTime(latest.recorded_at) : "—" },
    { key: "Tokens", value: usage ? usage.total_tokens.toLocaleString() : "—" },
    { key: "Cost", value: usage ? `$${usage.estimated_cost_usd.toFixed(2)}` : "—" },
    { key: "Default model", value: cockpit?.models.default_model || "—" },
  ];
  return (
    <div className="ob-panel ob-system">
      <div className="ob-panel-head">
        <span className="ob-panel-title">System</span>
        <span className={`ob-badge ${nominal ? "ok" : "warn"}`}>{nominal ? "NOMINAL" : "ATTENTION"}</span>
      </div>
      <div className="ob-sys-rows">
        {rows.map((row) => (
          <div key={row.key} className="ob-sys-row">
            <span>{row.key}</span>
            <span className="ob-sys-val">{row.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ActivityLogPanel({ telemetry }: { telemetry: OrchestrationPayload | null }) {
  const events = telemetry?.events?.slice(0, 40) || [];
  return (
    <div className="ob-panel ob-activity">
      <div className="ob-panel-head">
        <span className="ob-panel-title">Activity</span>
        <span className="ob-badge subtle">live</span>
      </div>
      <div className="ob-activity-log">
        {events.length === 0 && <p className="muted">No orchestration activity recorded.</p>}
        {events.map((event) => (
          <div key={event.id} className={`ob-log-line ${orchestrationEventClass(event)}`}>
            <span className="ob-log-time">{formatLogTime(event.recorded_at)}</span>
            <span className="ob-log-text">
              <span className="ob-log-kind">{orchestrationEventKind(event)}</span>
              {" · "}
              {event.summary}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ModelSummary({
  cockpit,
  models,
  canEdit,
  api,
  onModelsChange,
  onSettingsChange,
  onDone,
  setStatus,
}: {
  cockpit: CockpitPayload | null;
  models: ModelInventory | null;
  canEdit: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onModelsChange: (models: ModelInventory) => void;
  onSettingsChange: (settings: SettingsPayload) => void;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  if (!cockpit) {
    return <p className="muted">Loading model status.</p>;
  }
  const options = visibleModelOptions(models);
  const available = options.filter((option) => option.available);
  const defaultOption = options.find((option) => option.is_default) || options[0] || null;
  const defaultKey = defaultOption ? modelOptionKey(defaultOption) : "";

  async function setDefault(value: string) {
    const option = options.find((item) => modelOptionKey(item) === value);
    if (!option) {
      return;
    }
    setStatus("Saving default model");
    const result = await api<{ settings: SettingsPayload; models: ModelInventory }>("/api/models/default", {
      method: "PUT",
      json: {
        provider: option.provider,
        model: option.model,
      },
    });
    onSettingsChange(result.settings);
    onModelsChange(result.models);
    setStatus("Default model saved");
    await onDone();
  }

  return (
    <div className="model-summary">
      <dl className="compact-dl">
        <dt>Default</dt>
        <dd>
          {cockpit.models.default_provider} / {cockpit.models.default_model}
        </dd>
        <dt>Recommended</dt>
        <dd>{models ? `${models.recommended.provider} / ${models.recommended.model}` : "loading"}</dd>
        <dt>Ollama</dt>
        <dd>{cockpit.models.ollama_base_url}</dd>
        <dt>Available</dt>
        <dd>{available.length}</dd>
      </dl>
      <label className="model-select">
        <span>Global default</span>
        <select
          value={defaultKey}
          disabled={!canEdit || !models}
          onChange={(event) => setDefault(event.target.value).catch((error) => setStatus(errorMessage(error)))}
        >
          {!models && <option value="">Loading models</option>}
          {options.map((option) => (
            <option
              key={modelOptionKey(option)}
              value={modelOptionKey(option)}
              disabled={!option.available}
            >
              {option.label}
              {!option.available ? " (unavailable)" : ""}
            </option>
          ))}
        </select>
      </label>
      <PermissionNotice
        allowed={canEdit}
        permission="admin"
        action="global model changes are disabled"
      />
      <div className="mini-list">
        {options.slice(0, 5).map((option) => (
          <span key={modelOptionKey(option)} className={option.available ? "" : "muted-bad"}>
            {option.label} / {option.available ? "ready" : option.detail || "unavailable"}
          </span>
        ))}
      </div>
    </div>
  );
}

function ModelSelect({
  label,
  inventory,
  route,
  onChange,
}: {
  label: string;
  inventory: ModelInventory | null;
  route: ModelRoute | null;
  onChange: (route: ModelRoute) => void;
}) {
  const visibleOptions = visibleModelOptions(inventory);
  const fallback = visibleOptions.find((option) => option.is_default) || visibleOptions[0] || null;
  const value = route
    ? modelRouteKey(route)
    : fallback
      ? modelOptionKey(fallback)
      : "";
  return (
    <label className="model-select">
      <span>{label}</span>
      <select
        value={value}
        disabled={!inventory || visibleOptions.length === 0}
        onChange={(event) => {
          const option = visibleOptions.find((item) => modelOptionKey(item) === event.target.value);
          const nextRoute = modelRouteFromOption(option || null);
          if (nextRoute) {
            onChange(nextRoute);
          }
        }}
      >
        {!inventory && <option value="">Loading models</option>}
        {visibleOptions.map((option) => (
          <option
            key={modelOptionKey(option)}
            value={modelOptionKey(option)}
            disabled={!option.available}
          >
            {option.label}
            {option.is_default ? " (default)" : ""}
            {!option.available ? " (unavailable)" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}

function UsageSummary({ usage }: { usage: (Usage & { by_agent: Record<string, Usage> }) | null }) {
  if (!usage) {
    return <p className="muted">No usage data loaded.</p>;
  }
  return (
    <div className="usage-summary">
      <div className="stat-large">{usage.total_tokens.toLocaleString()}</div>
      <p className="muted">tokens</p>
      <strong>${usage.estimated_cost_usd.toFixed(4)}</strong>
      <div className="mini-list">
        {Object.entries(usage.by_agent).slice(0, 4).map(([agentId, item]) => (
          <span key={agentId}>
            {agentId}: {item.total_tokens.toLocaleString()}
          </span>
        ))}
      </div>
    </div>
  );
}

function MissionWidget({
  mission,
  latestReasoning,
  canEdit,
  api,
  onDone,
  setStatus,
}: {
  mission: Mission | null;
  latestReasoning: { decision_summary?: string; cycle_id?: string } | null;
  canEdit: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [statement, setStatement] = useState(mission?.statement || "");
  const [success, setSuccess] = useState((mission?.success_criteria || []).join("\n"));
  const [notScope, setNotScope] = useState((mission?.explicitly_not || []).join("\n"));

  useEffect(() => {
    setStatement(mission?.statement || "");
    setSuccess((mission?.success_criteria || []).join("\n"));
    setNotScope((mission?.explicitly_not || []).join("\n"));
  }, [mission]);

  async function saveMission() {
    if (!statement.trim()) {
      return;
    }
    setStatus("Saving mission");
    await api<Mission>("/api/mission", {
      method: "PUT",
      json: {
        statement,
        success_criteria: lines(success),
        explicitly_not: lines(notScope),
      },
    });
    setEditing(false);
    setStatus("Mission saved");
    await onDone();
  }

  if (editing) {
    return (
      <div className="form-stack">
        <label>
          <span>Mission</span>
          <textarea value={statement} onChange={(event) => setStatement(event.target.value)} />
        </label>
        <label>
          <span>Success criteria</span>
          <textarea value={success} onChange={(event) => setSuccess(event.target.value)} />
        </label>
        <label>
          <span>Explicitly not</span>
          <textarea value={notScope} onChange={(event) => setNotScope(event.target.value)} />
        </label>
        <div className="button-row">
          <button onClick={() => saveMission().catch((error) => setStatus(errorMessage(error)))}>
            Save
          </button>
          <button onClick={() => setEditing(false)}>Cancel</button>
        </div>
      </div>
    );
  }

  return (
    <div className="mission-widget">
      <p className="lead-text">{mission?.statement || "Mission not set."}</p>
      <ListBlock title="Success" items={mission?.success_criteria || []} />
      <ListBlock title="Explicitly Not" items={mission?.explicitly_not || []} />
      {latestReasoning?.decision_summary && (
        <div className="reasoning-box">
          <span>Latest reasoning</span>
          <p>{latestReasoning.decision_summary}</p>
        </div>
      )}
      {canEdit ? <button onClick={() => setEditing(true)}>Edit Mission</button> : null}
      <PermissionNotice
        allowed={canEdit}
        permission="mission:write"
        action="mission edits are disabled"
      />
    </div>
  );
}

function ListBlock({ title, items }: { title: string; items: string[] }) {
  if (!items.length) {
    return null;
  }
  return (
    <div className="list-block">
      <strong>{title}</strong>
      <ul>
        {items.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function orchestrationEventKind(event: OrchestrationEvent) {
  if (event.type === "cycle_outcome") {
    return (event.decision || "cycle outcome").replace(/_/g, " ");
  }
  if (event.type.startsWith("ladder_")) {
    return event.type.replace(/_/g, " ");
  }
  if (event.type === "rest_scheduled" || event.type === "rest_completed") {
    return event.type.replace(/_/g, " ");
  }
  if (event.type === "recurrence_materialized") {
    return "recurrence";
  }
  if (
    event.status === "proposed" ||
    event.type === "proactive_proposal" ||
    event.type === "intake_proposal" ||
    event.type === "proposal_created"
  ) {
    return "proposal";
  }
  if (
    event.status === "created" ||
    event.decision === "created" ||
    event.type === "intake_created"
  ) {
    return "created";
  }
  if (event.type === "delegated_task") {
    return "delegated";
  }
  if (event.type === "parent_synthesis") {
    return "synthesis";
  }
  return event.decision || event.type.replace(/_/g, " ");
}

function orchestrationEventClass(event: OrchestrationEvent) {
  const kind = orchestrationEventKind(event);
  if (kind === "proposal") {
    return "proposed";
  }
  if (kind === "ladder escalated human" || kind === "no work") {
    return "blocked";
  }
  if (kind.startsWith("ladder") || kind.startsWith("rest")) {
    return "active";
  }
  if (
    ["created", "assigned", "delegated", "synthesis", "recurrence", "worked"].includes(
      kind,
    )
  ) {
    return "active";
  }
  if (["blocked", "skipped"].includes(kind)) {
    return "blocked";
  }
  return "neutral";
}

function orchestrationEventText(event: OrchestrationEvent) {
  const kind = orchestrationEventKind(event);
  return `${kind}: ${event.summary}`;
}

function AlertList({
  alerts,
  canClear,
  api,
  onDone,
  setStatus,
}: {
  alerts: string[];
  canClear: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  async function clear() {
    setStatus("Clearing alerts");
    const result = await api<{ count: number }>("/api/alerts", { method: "DELETE" });
    setStatus(`Cleared ${result.count} alerts`);
    await onDone();
  }

  if (!alerts.length) {
    return <p className="muted">No current alerts.</p>;
  }
  return (
    <div className="alert-list">
      <div className="toolbar-row">
        <span className="muted">{alerts.length} active</span>
        <button
          disabled={!canClear}
          onClick={() => clear().catch((error) => setStatus(errorMessage(error)))}
        >
          Clear
        </button>
      </div>
      <div className="stack-list compact">
        {alerts.slice(-8).map((alert, index) => (
          <article key={`${index}:${alert}`} className="alert-row">
            <p>{alert}</p>
          </article>
        ))}
      </div>
      <PermissionNotice
        allowed={canClear}
        permission="orchestrator:write"
        action="alert clearing is disabled"
      />
    </div>
  );
}

function slugifyId(value: string) {
  return value
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function TeamBoard({
  teams,
  agents,
  canEdit,
  canManageAgents,
  models,
  api,
  onDone,
  setStatus,
}: {
  teams: Team[];
  agents: VisualAgent[];
  canEdit: boolean;
  canManageAgents: boolean;
  models: ModelInventory | null;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [selectedTeamId, setSelectedTeamId] = useState(teams[0]?.team_id || "");
  const selected = teams.find((team) => team.team_id === selectedTeamId) || teams[0] || null;
  const [displayName, setDisplayName] = useState(selected?.display_name || "");
  const [delegationPolicy, setDelegationPolicy] = useState(selected?.delegation_policy || "chief_only");
  const [parentTeamId, setParentTeamId] = useState(selected?.parent_team_id || "");
  const [escalationTeamId, setEscalationTeamId] = useState(selected?.escalation_team_id || "");
  const [chiefId, setChiefId] = useState(selected?.crew_chief_id || "");
  const [addMemberId, setAddMemberId] = useState("");
  const agentNames = new Map(agents.map((agent) => [agent.agent_id, agent.display_name]));
  const modelOptions = visibleModelOptions(models);
  const defaultModelKey = models ? `${models.default.provider}::${models.default.model}` : "";

  // Add-agent form.
  const [agName, setAgName] = useState("");
  const [agId, setAgId] = useState("");
  const [agIdEdited, setAgIdEdited] = useState(false);
  const [agRole, setAgRole] = useState("line_worker");
  const [agModelKey, setAgModelKey] = useState(defaultModelKey);
  const [agTeamId, setAgTeamId] = useState("");
  const [agNewTeamId, setAgNewTeamId] = useState("");
  const [agMakeChief, setAgMakeChief] = useState(false);

  // New-team form.
  const [ntId, setNtId] = useState("");
  const [ntName, setNtName] = useState("");
  const [ntParent, setNtParent] = useState("");
  const [ntPolicy, setNtPolicy] = useState("chief_only");

  // Delegate form.
  const [dTarget, setDTarget] = useState("");
  const [dAssignment, setDAssignment] = useState("");
  const [dGoal, setDGoal] = useState("");
  const [dPriority, setDPriority] = useState("normal");

  // Delete-agent form.
  const [delAgentId, setDelAgentId] = useState("");

  useEffect(() => {
    if (!selectedTeamId && teams[0]) {
      setSelectedTeamId(teams[0].team_id);
    }
  }, [selectedTeamId, teams]);

  useEffect(() => {
    setDisplayName(selected?.display_name || "");
    setDelegationPolicy(selected?.delegation_policy || "chief_only");
    setParentTeamId(selected?.parent_team_id || "");
    setEscalationTeamId(selected?.escalation_team_id || "");
    setChiefId(selected?.crew_chief_id || "");
    setAddMemberId("");
    setDTarget("");
  }, [selected]);

  useEffect(() => {
    if (!agModelKey && defaultModelKey) {
      setAgModelKey(defaultModelKey);
    }
  }, [agModelKey, defaultModelKey]);

  function run(label: string, action: () => Promise<unknown>, reset?: () => void) {
    setStatus(label);
    action()
      .then(async () => {
        reset?.();
        await onDone();
      })
      .catch((error) => setStatus(errorMessage(error)));
  }

  function patchTeam(teamId: string, json: Record<string, unknown>, label: string) {
    run(label, () =>
      api<Team>(`/api/teams/${encodeURIComponent(teamId)}`, { method: "PATCH", json }),
    );
  }

  function saveTeam() {
    if (!selected) {
      return;
    }
    patchTeam(
      selected.team_id,
      {
        display_name: displayName,
        delegation_policy: delegationPolicy,
        parent_team_id: parentTeamId || null,
        escalation_team_id: escalationTeamId || null,
        crew_chief_id: chiefId || null,
      },
      "Saving team",
    );
  }

  function addMember() {
    if (!selected || !addMemberId) {
      return;
    }
    const members = Array.from(new Set([...selected.members, addMemberId]));
    patchTeam(selected.team_id, { members }, "Adding member");
  }

  function removeMember(agentId: string) {
    if (!selected) {
      return;
    }
    const members = selected.members.filter((id) => id !== agentId);
    const json: Record<string, unknown> = { members };
    if (selected.crew_chief_id === agentId) {
      json.crew_chief_id = null;
    }
    patchTeam(selected.team_id, json, "Removing member");
  }

  function addAgent() {
    if (!agName.trim()) {
      return;
    }
    const agentId = (agIdEdited ? agId : slugifyId(agName)).trim();
    if (!agentId) {
      setStatus("Agent id is required");
      return;
    }
    const useNewTeam = agTeamId === "__new__";
    const teamId = (useNewTeam ? agNewTeamId : agTeamId).trim();
    if (agMakeChief && !teamId) {
      setStatus("Crew chief needs a team");
      return;
    }
    const [provider, model] = agModelKey.split("::");
    run(
      `Onboarding ${agentId}`,
      async () => {
        const result = await api<{ valid: boolean; diagnostics: { message: string }[] }>(
          "/api/agents",
          {
            method: "POST",
            json: {
              agent_id: agentId,
              display_name: agName.trim(),
              role: agRole.trim() || "line_worker",
              model_provider: provider || undefined,
              model_name: model || undefined,
              team_id: teamId || undefined,
              create_team: useNewTeam || undefined,
              crew_chief: agMakeChief || undefined,
            },
          },
        );
        setStatus(
          result.valid
            ? `Onboarded ${agentId}`
            : `Onboarded ${agentId} with warnings: ${result.diagnostics.map((d) => d.message).join("; ")}`,
        );
      },
      () => {
        setAgName("");
        setAgId("");
        setAgIdEdited(false);
        setAgMakeChief(false);
        setAgNewTeamId("");
      },
    );
  }

  function createTeam() {
    const teamId = (ntId.trim() ? ntId : slugifyId(ntName)).trim();
    if (!teamId) {
      setStatus("Team id is required");
      return;
    }
    run(
      `Creating ${teamId}`,
      () =>
        api<Team>("/api/teams", {
          method: "POST",
          json: {
            team_id: teamId,
            display_name: ntName.trim() || teamId,
            parent_team_id: ntParent || undefined,
            delegation_policy: ntPolicy,
          },
        }),
      () => {
        setNtId("");
        setNtName("");
        setNtParent("");
      },
    );
  }

  function delegate() {
    if (!selected || !selected.crew_chief_id || !dTarget || !dAssignment.trim()) {
      return;
    }
    run(
      "Delegating work",
      () =>
        api(`/api/teams/${encodeURIComponent(selected.team_id)}/delegate`, {
          method: "POST",
          json: {
            chief_agent_id: selected.crew_chief_id,
            target_agent_id: dTarget,
            assignment: dAssignment.trim(),
            goal_statement: dGoal.trim() || undefined,
            priority: dPriority,
          },
        }),
      () => {
        setDAssignment("");
        setDGoal("");
      },
    );
  }

  function deleteAgent() {
    if (!delAgentId) {
      return;
    }
    const name = agentNames.get(delAgentId) || delAgentId;
    if (!confirm(`Delete agent ${name}? This removes it from any team.`)) {
      return;
    }
    run(`Deleting ${delAgentId}`, () =>
      api(`/api/agents/${encodeURIComponent(delAgentId)}`, { method: "DELETE" }),
    );
    setDelAgentId("");
  }

  const otherTeams = teams.filter((team) => team.team_id !== selected?.team_id);
  const nonMembers = agents.filter((agent) => !selected?.members.includes(agent.agent_id));

  return (
    <div className="team-board">
      <details className="compact-form">
        <summary>Add agent</summary>
        <div className="form-stack compact-form">
          <label>
            <span>Name</span>
            <input
              value={agName}
              disabled={!canManageAgents}
              onChange={(event) => {
                setAgName(event.target.value);
                if (!agIdEdited) {
                  setAgId(slugifyId(event.target.value));
                }
              }}
            />
          </label>
          <label>
            <span>Agent id</span>
            <input
              value={agIdEdited ? agId : slugifyId(agName)}
              disabled={!canManageAgents}
              onChange={(event) => {
                setAgIdEdited(true);
                setAgId(event.target.value);
              }}
            />
          </label>
          <label>
            <span>Role</span>
            <input
              list="agent-roles"
              value={agRole}
              disabled={!canManageAgents}
              onChange={(event) => setAgRole(event.target.value)}
            />
            <datalist id="agent-roles">
              <option value="crew_chief" />
              <option value="researcher" />
              <option value="builder" />
              <option value="line_worker" />
            </datalist>
          </label>
          <label>
            <span>Model</span>
            <select
              value={agModelKey}
              disabled={!canManageAgents}
              onChange={(event) => setAgModelKey(event.target.value)}
            >
              {modelOptions.map((option) => (
                <option key={`${option.provider}::${option.model}`} value={`${option.provider}::${option.model}`}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Team</span>
            <select
              value={agTeamId}
              disabled={!canManageAgents}
              onChange={(event) => {
                setAgTeamId(event.target.value);
                if (!event.target.value) {
                  setAgMakeChief(false);
                }
              }}
            >
              <option value="">— none —</option>
              {teams.map((team) => (
                <option key={team.team_id} value={team.team_id}>
                  {team.display_name}
                </option>
              ))}
              <option value="__new__">+ new team…</option>
            </select>
          </label>
          {agTeamId === "__new__" && (
            <label>
              <span>New team id</span>
              <input
                value={agNewTeamId}
                disabled={!canManageAgents}
                onChange={(event) => setAgNewTeamId(event.target.value)}
              />
            </label>
          )}
          <label className="inline-checkbox">
            <input
              type="checkbox"
              checked={agMakeChief}
              disabled={!canManageAgents || (!agTeamId)}
              onChange={(event) => setAgMakeChief(event.target.checked)}
            />
            <span>Make crew chief of this team</span>
          </label>
          <button disabled={!canManageAgents} onClick={addAgent}>
            Add Agent
          </button>
          <PermissionNotice
            allowed={canManageAgents}
            permission="agent:write"
            action="agent creation is disabled"
          />
        </div>
      </details>

      <details className="compact-form">
        <summary>New team</summary>
        <div className="form-stack compact-form">
          <label>
            <span>Display name</span>
            <input value={ntName} disabled={!canEdit} onChange={(event) => setNtName(event.target.value)} />
          </label>
          <label>
            <span>Team id</span>
            <input
              value={ntId || slugifyId(ntName)}
              disabled={!canEdit}
              onChange={(event) => setNtId(event.target.value)}
            />
          </label>
          <label>
            <span>Parent team</span>
            <select value={ntParent} disabled={!canEdit} onChange={(event) => setNtParent(event.target.value)}>
              <option value="">— none —</option>
              {teams.map((team) => (
                <option key={team.team_id} value={team.team_id}>
                  {team.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Delegation policy</span>
            <select value={ntPolicy} disabled={!canEdit} onChange={(event) => setNtPolicy(event.target.value)}>
              <option value="chief_only">chief_only</option>
              <option value="open">open</option>
              <option value="orchestrator_only">orchestrator_only</option>
            </select>
          </label>
          <button disabled={!canEdit} onClick={createTeam}>
            Create Team
          </button>
          <PermissionNotice allowed={canEdit} permission="team:write" action="team creation is disabled" />
        </div>
      </details>

      {!teams.length ? (
        <p className="muted">No teams configured. Add an agent or create a team to start.</p>
      ) : (
        <>
          <select value={selected?.team_id || ""} onChange={(event) => setSelectedTeamId(event.target.value)}>
            {teams.map((team) => (
              <option key={team.team_id} value={team.team_id}>
                {team.display_name}
              </option>
            ))}
          </select>
          {selected && (
            <>
              <dl className="compact-dl">
                <dt>Crew Chief</dt>
                <dd>{selected.crew_chief_id ? agentNames.get(selected.crew_chief_id) || selected.crew_chief_id : "none"}</dd>
                <dt>Members</dt>
                <dd>
                  {selected.members.length
                    ? selected.members.map((id) => (
                        <span key={id} className="member-chip">
                          {agentNames.get(id) || id}
                          {canEdit && (
                            <button className="chip-remove" title="Remove from team" onClick={() => removeMember(id)}>
                              ×
                            </button>
                          )}
                        </span>
                      ))
                    : "none"}
                </dd>
                <dt>Parent</dt>
                <dd>{selected.parent_team_id || "none"}</dd>
                <dt>Escalation</dt>
                <dd>{selected.escalation_team_id || "none"}</dd>
              </dl>
              <div className="form-stack compact-form">
                <label>
                  <span>Display name</span>
                  <input value={displayName} disabled={!canEdit} onChange={(event) => setDisplayName(event.target.value)} />
                </label>
                <label>
                  <span>Crew chief</span>
                  <select value={chiefId} disabled={!canEdit} onChange={(event) => setChiefId(event.target.value)}>
                    <option value="">— none —</option>
                    {selected.members.map((id) => (
                      <option key={id} value={id}>
                        {agentNames.get(id) || id}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Parent team</span>
                  <select value={parentTeamId} disabled={!canEdit} onChange={(event) => setParentTeamId(event.target.value)}>
                    <option value="">— none —</option>
                    {otherTeams.map((team) => (
                      <option key={team.team_id} value={team.team_id}>
                        {team.display_name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Escalation team</span>
                  <select
                    value={escalationTeamId}
                    disabled={!canEdit}
                    onChange={(event) => setEscalationTeamId(event.target.value)}
                  >
                    <option value="">— none —</option>
                    {otherTeams.map((team) => (
                      <option key={team.team_id} value={team.team_id}>
                        {team.display_name}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  <span>Delegation policy</span>
                  <select value={delegationPolicy} disabled={!canEdit} onChange={(event) => setDelegationPolicy(event.target.value)}>
                    <option value="chief_only">chief_only</option>
                    <option value="open">open</option>
                    <option value="orchestrator_only">orchestrator_only</option>
                  </select>
                </label>
                <div className="inline-fields">
                  <select value={addMemberId} disabled={!canEdit} onChange={(event) => setAddMemberId(event.target.value)}>
                    <option value="">— add member —</option>
                    {nonMembers.map((agent) => (
                      <option key={agent.agent_id} value={agent.agent_id}>
                        {agent.display_name}
                      </option>
                    ))}
                  </select>
                  <button disabled={!canEdit || !addMemberId} onClick={addMember}>
                    Add
                  </button>
                </div>
                <button disabled={!canEdit} onClick={saveTeam}>
                  Save Team
                </button>
                <PermissionNotice allowed={canEdit} permission="team:write" action="team edits are disabled" />
              </div>

              <details className="compact-form">
                <summary>Delegate work</summary>
                <div className="form-stack compact-form">
                  {selected.crew_chief_id ? (
                    <>
                      <p className="muted">
                        From chief {agentNames.get(selected.crew_chief_id) || selected.crew_chief_id}
                      </p>
                      <label>
                        <span>To member</span>
                        <select value={dTarget} disabled={!canEdit} onChange={(event) => setDTarget(event.target.value)}>
                          <option value="">— select —</option>
                          {selected.members
                            .filter((id) => id !== selected.crew_chief_id)
                            .map((id) => (
                              <option key={id} value={id}>
                                {agentNames.get(id) || id}
                              </option>
                            ))}
                        </select>
                      </label>
                      <label>
                        <span>Assignment</span>
                        <textarea value={dAssignment} disabled={!canEdit} onChange={(event) => setDAssignment(event.target.value)} />
                      </label>
                      <label>
                        <span>Goal (optional)</span>
                        <input value={dGoal} disabled={!canEdit} onChange={(event) => setDGoal(event.target.value)} />
                      </label>
                      <label>
                        <span>Priority</span>
                        <select value={dPriority} disabled={!canEdit} onChange={(event) => setDPriority(event.target.value)}>
                          <option value="low">low</option>
                          <option value="normal">normal</option>
                          <option value="high">high</option>
                          <option value="urgent">urgent</option>
                        </select>
                      </label>
                      <button disabled={!canEdit || !dTarget || !dAssignment.trim()} onClick={delegate}>
                        Delegate
                      </button>
                    </>
                  ) : (
                    <p className="muted">Designate a crew chief to delegate work.</p>
                  )}
                  <PermissionNotice allowed={canEdit} permission="team:write" action="delegation is disabled" />
                </div>
              </details>
            </>
          )}
        </>
      )}

      {canManageAgents && agents.length > 0 && (
        <details className="compact-form">
          <summary>Delete agent</summary>
          <div className="inline-fields compact-form">
            <select value={delAgentId} onChange={(event) => setDelAgentId(event.target.value)}>
              <option value="">— select agent —</option>
              {agents.map((agent) => (
                <option key={agent.agent_id} value={agent.agent_id}>
                  {agent.display_name}
                </option>
              ))}
            </select>
            <button className="danger" disabled={!delAgentId} onClick={deleteAgent}>
              Delete
            </button>
          </div>
        </details>
      )}
    </div>
  );
}

// Shared chat composer behavior: Ctrl/Cmd+Enter submits; plain Enter inserts a
// newline. Keep both chat composers wired through this so the chord stays
// consistent and documented in one place.
function handleChatSubmitKey(
  event: React.KeyboardEvent<HTMLTextAreaElement>,
  submit: () => void,
) {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    submit();
  }
}

function OrchestratorChat({
  canChat,
  api,
  inventory,
  route,
  onRouteChange,
  setStatus,
}: {
  canChat: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  inventory: ModelInventory | null;
  route: ModelRoute | null;
  onRouteChange: (route: ModelRoute) => void;
  setStatus: (status: string) => void;
}) {
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [responseHtmlById, setResponseHtmlById] = useState<Record<string, string>>({});
  const [pending, setPending] = useState(false);
  const feedRef = useAutoScroll<HTMLDivElement>([messages.length, pending]);

  const loadMessages = useCallback(async () => {
    const payload = await api<ChatPayload>("/api/chat/messages?channel=orchestrator");
    setMessages(payload.messages);
  }, [api]);

  useEffect(() => {
    loadMessages().catch((error) => setStatus(errorMessage(error)));
  }, [loadMessages, setStatus]);

  async function send() {
    if (!message.trim()) {
      return;
    }
    setPending(true);
    setStatus("Sending orchestrator prompt");
    try {
      const result = await api<OrchestratorMarkdownResult>("/api/chat/ask-orchestrator-markdown", {
        method: "POST",
        json: {
          channel: "orchestrator",
          content: message,
          idempotency_key: randomId("web-orchestrator"),
          ...modelRoutePayload(route),
        },
      });
      if (result.response_message_id && result.response_html) {
        setResponseHtmlById((current) => ({
          ...current,
          [result.response_message_id]: result.response_html,
        }));
      }
      setMessage("");
      setStatus(`Orchestrator chat ${result.status || "complete"}`);
      await loadMessages();
    } finally {
      setPending(false);
    }
  }

  return (
    <div className="chat-panel compact-chat">
      <ModelSelect
        label="Orchestrator model"
        inventory={inventory}
        route={route}
        onChange={onRouteChange}
      />
      <div className="chat-feed" ref={feedRef}>
        {messages.slice(-8).map((item) => (
          <ChatMessageRow
            key={item.message_id}
            message={item}
            html={item.sender === "orchestrator"
              ? responseHtmlById[item.message_id] || renderMarkdownHtml(item.content)
              : undefined}
            perspective="orchestrator"
          />
        ))}
        {messages.length === 0 && <p className="muted">No orchestrator messages.</p>}
      </div>
      <div className="chat-compose">
        <textarea
          value={message}
          disabled={!canChat || pending}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) =>
            handleChatSubmitKey(event, () =>
              send().catch((error) => setStatus(errorMessage(error))),
            )
          }
          placeholder="Message the Orchestrator — Ctrl+Enter to send, Enter for newline"
        />
        <button
          disabled={!canChat || pending || !message.trim()}
          onClick={() => send().catch((error) => setStatus(errorMessage(error)))}
          title="Send message (Ctrl+Enter)"
        >
          {pending ? "Sending" : "Send"}
        </button>
      </div>
      <PermissionNotice
        allowed={canChat}
        permission="chat:write"
        action="orchestrator chat is disabled"
      />
    </div>
  );
}

function ChatMessageRow({
  message,
  html,
  perspective,
}: {
  message: Message;
  html?: string;
  perspective: string;
}) {
  const authoredHere = message.sender === perspective;
  const fromOrchestrator = message.sender === "orchestrator";
  const displayName = fromOrchestrator ? "Orchestrator" : message.sender;
  return (
    <article className={`message-row ${authoredHere ? "from-selected" : "to-selected"}`}>
      <div className="message-avatar" aria-hidden="true">
        {displayName.slice(0, 1).toUpperCase()}
      </div>
      <div className="message-bubble">
        <div className="message-meta">
          <strong>{displayName}</strong>
          <span>{message.sender} -&gt; {message.recipient}</span>
          <time dateTime={message.created_at}>{formatTime(message.created_at)}</time>
        </div>
        {html ? (
          <div
            className="message-markdown"
            dangerouslySetInnerHTML={{
              __html: html,
            }}
          />
        ) : (
          <p>{message.content}</p>
        )}
      </div>
    </article>
  );
}

function SettingsStatus({
  settings,
  cockpit,
  auth,
  authMessage,
  tokenExpired,
}: {
  settings: SettingsPayload | null;
  cockpit: CockpitPayload | null;
  auth: AuthMe | null;
  authMessage: string;
  tokenExpired: boolean;
}) {
  if (!settings) {
    return <p className="muted">Settings not loaded.</p>;
  }
  return (
    <div className="settings-grid">
      <dl className="compact-dl">
        <dt>API</dt>
        <dd>{settings.api_version}</dd>
        <dt>Config</dt>
        <dd>{settings.config_hash}</dd>
        <dt>Auth</dt>
        <dd>{settings.require_auth ? "required" : "disabled"}</dd>
        <dt>Bind</dt>
        <dd>
          {settings.web_host}:{settings.web_port}
        </dd>
      </dl>
      <dl className="compact-dl">
        <dt>Postgres</dt>
        <dd>{settings.postgres_configured ? "configured" : "missing"}</dd>
        <dt>Redis</dt>
        <dd>{settings.redis_configured ? "configured" : "missing"}</dd>
        <dt>Qdrant</dt>
        <dd>{settings.qdrant_configured ? "configured" : "missing"}</dd>
        <dt>Neo4j</dt>
        <dd>{settings.neo4j_configured ? "configured" : "missing"}</dd>
      </dl>
      {cockpit?.auth.unsafe_bind_without_auth && (
        <p className="warning-banner">Authentication is disabled while the web host is reachable.</p>
      )}
      <AuthStateDetails auth={auth} authMessage={authMessage} tokenExpired={tokenExpired} />
    </div>
  );
}

function AuthStateDetails({
  auth,
  authMessage,
  tokenExpired,
}: {
  auth: AuthMe | null;
  authMessage: string;
  tokenExpired: boolean;
}) {
  if (tokenExpired) {
    return (
      <p className="warning-banner">
        Token expired; paste a fresh JWT or clear the saved token before refreshing.
      </p>
    );
  }
  if (authMessage) {
    return <p className="warning-banner">{authMessage}</p>;
  }
  if (!auth) {
    return <p className="muted full-row">Authentication state is loading.</p>;
  }
  if (auth.method === "bootstrap") {
    return (
      <p className="warning-banner">
        Bootstrap mode is active. No browser user is authenticated, so write controls stay disabled.
      </p>
    );
  }
  if (auth.method.startsWith("implicit")) {
    return (
      <p className="warning-banner">
        Authentication is disabled; browser access is using {auth.method.replace(/-/g, " ")}.
      </p>
    );
  }
  if (!auth.permissions.some((permission) => permission.endsWith(":write") || permission === "admin")) {
    return (
      <p className="permission-note full-row">
        Read-only role: write controls are disabled and denied API actions are reported in the status bar.
      </p>
    );
  }
  return (
    <p className="permission-note full-row">
      Signed in as {auth.user?.username || "unknown"} with {auth.user?.role || "unknown"} permissions.
    </p>
  );
}

function PermissionNotice({
  allowed,
  permission,
  action,
}: {
  allowed: boolean;
  permission: string;
  action: string;
}) {
  if (allowed) {
    return null;
  }
  return (
    <p className="permission-note">
      Missing {permission}; {action}.
    </p>
  );
}

function OpsRoomView({
  snapshot,
  selectedAgent,
  selectedAgentId,
  selectedAgentModel,
  teams,
  activePanel,
  setActivePanel,
  can,
  api,
  onSelectAgent,
  onRefresh,
  setStatus,
  onOpenTaskDialog,
}: {
  snapshot: OpsRoomSnapshot | null;
  selectedAgent: VisualAgent | null;
  selectedAgentId: string;
  selectedAgentModel: ModelRoute | null;
  teams: Team[];
  activePanel: "tasks" | "chat" | "goals";
  setActivePanel: (panel: "tasks" | "chat" | "goals") => void;
  can: (permission: string) => boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onSelectAgent: (agentId: string) => void;
  onRefresh: () => Promise<void>;
  setStatus: (status: string) => void;
  onOpenTaskDialog: (draft?: TaskDialogDraft) => void;
}) {
  return (
    <section className="ops-layout">
      <div className="room-column">
        <div className="room-toolbar">
          <div>
            <strong>Ops Room</strong>
            <span>{snapshot?.generated_at || "Waiting for snapshot"}</span>
          </div>
          <div className="room-tools">
            <button onClick={() => onRefresh().catch((error) => setStatus(errorMessage(error)))}>
              Refresh
            </button>
          </div>
        </div>
        <OpsRoomFloor
          snapshot={snapshot}
          selectedAgentId={selectedAgentId}
          onSelectAgent={onSelectAgent}
        />
      </div>

      <aside className="side-panel">
        <AgentInspector agent={selectedAgent} teams={teams} />
        <nav className="panel-tabs" aria-label="Agent panels">
          {(["tasks", "chat", "goals"] as const).map((panel) => (
            <button
              key={panel}
              className={activePanel === panel ? "active" : ""}
              onClick={() => setActivePanel(panel)}
            >
              {panel}
            </button>
          ))}
        </nav>
        {activePanel === "tasks" && (
          <TasksPanel
            snapshot={snapshot}
            selectedAgentId={selectedAgentId}
            canCreate={can("task:write")}
            onOpenTaskDialog={onOpenTaskDialog}
          />
        )}
        {activePanel === "chat" && (
          <AgentChatPanel
            snapshot={snapshot}
            selectedAgentId={selectedAgentId}
            modelRoute={selectedAgentModel}
            canChat={can("chat:write")}
            canCreateTask={can("task:write")}
            api={api}
            onOpenTaskDialog={onOpenTaskDialog}
            onDone={onRefresh}
            setStatus={setStatus}
          />
        )}
        {activePanel === "goals" && (
          <GoalsPanel
            snapshot={snapshot}
            selectedAgentId={selectedAgentId}
            canEdit={can("goal:write")}
            api={api}
            onDone={onRefresh}
            setStatus={setStatus}
          />
        )}
      </aside>
    </section>
  );
}

function AgentInspector({ agent, teams }: { agent: VisualAgent | null; teams: Team[] }) {
  if (!agent) {
    return (
      <section className="inspector empty">
        <h2>No Agent Selected</h2>
        <p>Select an agent from the header or Ops Room.</p>
      </section>
    );
  }
  const team = teams.find((item) => item.team_id === agent.team_id);
  const current = agent.current_assignment;
  return (
    <section className="inspector">
      <div className="inspector-heading">
        <div>
          <h2>{agent.display_name}</h2>
          <p>{agent.agent_id}</p>
        </div>
        <span className={`status-pill ${agent.status}`}>{agent.status}</span>
      </div>
      <dl>
        <dt>Role</dt>
        <dd>{agent.team_role || agent.role}</dd>
        <dt>Team</dt>
        <dd>{team?.display_name || agent.team_id || "none"}</dd>
        <dt>Usage</dt>
        <dd>
          {agent.usage.total_tokens.toLocaleString()} tokens, $
          {agent.usage.estimated_cost_usd.toFixed(4)}
        </dd>
      </dl>
      {current ? (
        <div className="current-work">
          <span>
            {current.priority} / {current.work_mode}
          </span>
          <p>{current.assignment}</p>
          {current.progress_summary && <small>{current.progress_summary}</small>}
          {current.blockers.length > 0 && <small>{current.blockers.join("; ")}</small>}
        </div>
      ) : (
        <div className="current-work muted">No active assignment.</div>
      )}
    </section>
  );
}

function TasksPanel({
  snapshot,
  selectedAgentId,
  canCreate,
  onOpenTaskDialog,
}: {
  snapshot: OpsRoomSnapshot | null;
  selectedAgentId: string;
  canCreate: boolean;
  onOpenTaskDialog: (draft?: TaskDialogDraft) => void;
}) {
  const tasks = (snapshot?.assignments || [])
    .filter((task) => task.assigned_to === selectedAgentId)
    .sort((a, b) => taskSortKey(a).localeCompare(taskSortKey(b)));

  return (
    <section className="panel-body">
      <div className="toolbar-row">
        <h3>Agent Tasks</h3>
        <button disabled={!canCreate || !selectedAgentId} onClick={onOpenTaskDialog}>
          Add Task
        </button>
      </div>
      <PermissionNotice
        allowed={canCreate}
        permission="task:write"
        action="task creation is disabled"
      />
      <div className="stack-list">
        {tasks.map((task) => (
          <article key={task.assignment_id} className={`task-row ${task.status}`}>
            <span>
              {task.status} / {task.priority} / {task.work_mode}
            </span>
            <p>{task.assignment}</p>
            {task.progress_summary && <small>{task.progress_summary}</small>}
            {task.blockers.length > 0 && <small>{task.blockers.join("; ")}</small>}
          </article>
        ))}
        {tasks.length === 0 && <p className="muted">No tasks for this agent.</p>}
      </div>
    </section>
  );
}

function AgentChatPanel({
  snapshot,
  selectedAgentId,
  modelRoute,
  canChat,
  canCreateTask,
  api,
  onOpenTaskDialog,
  onDone,
  setStatus,
}: {
  snapshot: OpsRoomSnapshot | null;
  selectedAgentId: string;
  modelRoute: ModelRoute | null;
  canChat: boolean;
  canCreateTask: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onOpenTaskDialog: (draft?: TaskDialogDraft) => void;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const selectedAgent = snapshot?.agents.find((agent) => agent.agent_id === selectedAgentId) || null;
  const messages = (snapshot?.messages || []).filter(
    (item) => item.sender === selectedAgentId || item.recipient === selectedAgentId,
  );
  const feedRef = useAutoScroll<HTMLDivElement>([selectedAgentId, messages.length, pending]);
  const lastRunProvider = selectedAgent?.current_assignment?.last_run_provider;
  const lastRunModel = selectedAgent?.current_assignment?.last_run_model;
  const configuredProvider = selectedAgent?.model_provider;
  const configuredModel = selectedAgent?.model_name;
  const routeProvider = lastRunProvider || configuredProvider || modelRoute?.provider;
  const routeModel = lastRunModel || configuredModel || modelRoute?.model;
  const routeLabel = routeProvider && routeModel
    ? `${routeProvider} / ${routeModel}${lastRunProvider ? " (last run)" : " (configured)"}`
    : "model not loaded";

  async function send() {
    if (!message.trim() || !selectedAgentId) {
      return;
    }
    setPending(true);
    setStatus("Sending chat");
    try {
      await api<Record<string, unknown>>("/api/chat/ask-agent", {
        method: "POST",
        json: {
          agent_id: selectedAgentId,
          content: message,
          idempotency_key: randomId("web-chat"),
          ...modelRoutePayload(modelRoute),
        },
      });
      setMessage("");
      setStatus("Chat reply saved");
      await onDone();
    } finally {
      setPending(false);
    }
  }

  function createTaskFromDraft() {
    const draft = message.trim();
    if (!draft || !selectedAgentId || !canCreateTask) {
      return;
    }
    setStatus("Preparing task draft");
    onOpenTaskDialog({ agentId: selectedAgentId, assignment: draft });
  }

  return (
    <section className="panel-body chat-panel ob-agent-chat">
      <p className="model-route-note">Agent model: {routeLabel}</p>
      <div className="chat-feed" ref={feedRef}>
        {messages.length === 0 && <p className="muted">No messages for this agent.</p>}
        {messages.slice(-12).map((item) => (
          <ChatMessageRow key={item.message_id} message={item} perspective={selectedAgentId} />
        ))}
      </div>
      <div className="chat-compose">
        <textarea
          value={message}
          disabled={!canChat || pending}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) =>
            handleChatSubmitKey(event, () =>
              send().catch((error) => setStatus(errorMessage(error))),
            )
          }
          placeholder="Message the selected agent — Ctrl+Enter to send, Enter for newline"
        />
        <div className="chat-actions">
          <button
            disabled={!canChat || pending || !message.trim() || !selectedAgentId}
            onClick={() => send().catch((error) => setStatus(errorMessage(error)))}
            title="Send message (Ctrl+Enter)"
          >
            {pending ? "Sending" : "Send"}
          </button>
          <button
            disabled={!canCreateTask || pending || !message.trim() || !selectedAgentId}
            onClick={createTaskFromDraft}
            title="Create an assignment for the selected agent from this text"
          >
            Create Task
          </button>
        </div>
      </div>
      <PermissionNotice
        allowed={canChat}
        permission="chat:write"
        action="agent chat is disabled"
      />
    </section>
  );
}

function GoalsPanel({
  snapshot,
  selectedAgentId,
  canEdit,
  api,
  onDone,
  setStatus,
}: {
  snapshot: OpsRoomSnapshot | null;
  selectedAgentId: string;
  canEdit: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [statement, setStatement] = useState("");
  const [success, setSuccess] = useState("");
  const [notScope, setNotScope] = useState("");
  const goals = selectedAgentId ? snapshot?.goals[selectedAgentId] || [] : [];

  async function addGoal() {
    if (!statement.trim() || !selectedAgentId) {
      return;
    }
    setStatus("Adding goal");
    await api<Record<string, unknown>>("/api/goals", {
      method: "POST",
      json: {
        agent_id: selectedAgentId,
        statement,
        success_criteria: lines(success),
        explicitly_not: lines(notScope),
        human_confirmed: true,
      },
    });
    setStatement("");
    setSuccess("");
    setNotScope("");
    setStatus("Goal added");
    await onDone();
  }

  return (
    <section className="panel-body">
      <div className="form-stack">
        <label>
          <span>Goal statement</span>
          <input
            value={statement}
            disabled={!canEdit}
            onChange={(event) => setStatement(event.target.value)}
          />
        </label>
        <label>
          <span>Success criteria</span>
          <textarea
            value={success}
            disabled={!canEdit}
            onChange={(event) => setSuccess(event.target.value)}
          />
        </label>
        <label>
          <span>Explicitly not</span>
          <textarea
            value={notScope}
            disabled={!canEdit}
            onChange={(event) => setNotScope(event.target.value)}
          />
        </label>
        <button
          disabled={!canEdit || !statement.trim() || !selectedAgentId}
          onClick={() => addGoal().catch((error) => setStatus(errorMessage(error)))}
        >
          Add Goal
        </button>
        <PermissionNotice
          allowed={canEdit}
          permission="goal:write"
          action="goal edits are disabled"
        />
      </div>
      <div className="stack-list">
        {goals.map((goal) => (
          <article key={`${goal.set_at}:${goal.statement}`}>
            <span>{goal.human_confirmed ? "confirmed" : "draft"}</span>
            <p>{goal.statement}</p>
          </article>
        ))}
        {goals.length === 0 && <p className="muted">No goals for this agent.</p>}
      </div>
    </section>
  );
}

function TaskDialog({
  agents,
  selectedAgentId,
  draft,
  canCreate,
  api,
  onClose,
  onDone,
  setStatus,
}: {
  agents: VisualAgent[];
  selectedAgentId: string;
  draft: TaskDialogDraft | null;
  canCreate: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onClose: () => void;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [agentId, setAgentId] = useState(draft?.agentId || selectedAgentId || agents[0]?.agent_id || "");
  const [assignment, setAssignment] = useState(draft?.assignment || "");
  const [priority, setPriority] = useState("normal");
  const [workMode, setWorkMode] = useState("heartbeat");
  const [roomId, setRoomId] = useState("");
  const [goalStatement, setGoalStatement] = useState("");
  const taskRooms = OPS_ROOM_FALLBACK_ROOMS.filter((room) => room.kind === "work");

  async function createTask() {
    if (!assignment.trim() || !agentId || !canCreate) {
      return;
    }
    setStatus("Creating assignment");
    await api<Assignment>("/api/tasks", {
      method: "POST",
      json: {
        agent_id: agentId,
        assignment,
        priority,
        work_mode: workMode,
        room_id: roomId || undefined,
        goal_statement: goalStatement || undefined,
        idempotency_key: randomId("web-task"),
      },
    });
    setStatus("Assignment created");
    onClose();
    await onDone();
  }

  return (
    <div className="modal-backdrop" role="presentation">
      <section className="modal" role="dialog" aria-modal="true" aria-labelledby="task-dialog-title">
        <div className="modal-heading">
          <h2 id="task-dialog-title">Add User Task</h2>
          <button onClick={onClose}>Close</button>
        </div>
        <div className="form-stack">
          <label>
            <span>Agent</span>
            <select value={agentId} disabled={!canCreate} onChange={(event) => setAgentId(event.target.value)}>
              {agents.map((agent) => (
                <option key={agent.agent_id} value={agent.agent_id}>
                  {agent.display_name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Task</span>
            <textarea
              value={assignment}
              disabled={!canCreate}
              onChange={(event) => setAssignment(event.target.value)}
            />
          </label>
          <div className="inline-fields">
            <label>
              <span>Priority</span>
              <select value={priority} disabled={!canCreate} onChange={(event) => setPriority(event.target.value)}>
                <option value="low">low</option>
                <option value="normal">normal</option>
                <option value="high">high</option>
                <option value="urgent">urgent</option>
              </select>
            </label>
            <label>
              <span>Room</span>
              <select value={roomId} disabled={!canCreate} onChange={(event) => setRoomId(event.target.value)}>
                <option value="">auto</option>
                {taskRooms.map((room) => (
                  <option key={room.id} value={room.id}>
                    {room.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Work mode</span>
              <select value={workMode} disabled={!canCreate} onChange={(event) => setWorkMode(event.target.value)}>
                <option value="heartbeat">heartbeat</option>
                <option value="standard">standard</option>
                <option value="extended">extended</option>
              </select>
            </label>
          </div>
          <label>
            <span>Goal link</span>
            <input
              value={goalStatement}
              disabled={!canCreate}
              onChange={(event) => setGoalStatement(event.target.value)}
            />
          </label>
          <div className="button-row">
            <button
              disabled={!canCreate || !assignment.trim() || !agentId}
              onClick={() => createTask().catch((error) => setStatus(errorMessage(error)))}
            >
              Create Task
            </button>
            <button onClick={onClose}>Cancel</button>
          </div>
          <PermissionNotice
            allowed={canCreate}
            permission="task:write"
            action="task creation is disabled"
          />
        </div>
      </section>
    </div>
  );
}

function OpsRoomFloor({
  snapshot,
  selectedAgentId,
  onSelectAgent,
}: {
  snapshot: OpsRoomSnapshot | null;
  selectedAgentId: string;
  onSelectAgent: (agentId: string) => void;
}) {
  const rooms = snapshot?.rooms?.length ? snapshot.rooms : OPS_ROOM_FALLBACK_ROOMS;
  const agents = snapshot?.agents || [];
  const agentsByRoom = useMemo(() => {
    const next = new Map<string, VisualAgent[]>();
    rooms.forEach((room) => next.set(room.id, []));
    agents.forEach((agent) => {
      const roomId = agentRoomId(agent);
      const bucket = next.get(roomId) || [];
      bucket.push(agent);
      next.set(roomId, bucket);
    });
    return next;
  }, [agents, rooms]);

  return (
    <div className="floor-shell">
      <div className="floor-legend" aria-label="Agent status legend">
        {["working", "queued", "blocked", "idle"].map((status) => (
          <span key={status}>
            <i className={`agent-token-dot ${statusClass(status)}`} />
            {status}
          </span>
        ))}
      </div>
      <div className="floor-grid">
        {rooms.map((room) => {
          const occupants = agentsByRoom.get(room.id) || [];
          const isRest = room.kind === "rest";
          const isOrchestrator = room.id === "orchestrator";
          const latestOrchestrationEvent = snapshot?.orchestration?.latest_event || null;
          const isActiveOrchestrator =
            isOrchestrator && Boolean(latestOrchestrationEvent || snapshot?.latest_reasoning);
          const tokenUse = occupants.reduce(
            (sum, agent) => sum + (agent.usage?.total_tokens || 0),
            0,
          );
          return (
            <article
              key={room.id}
              className={[
                "floor-room",
                `room-${room.id}`,
                room.kind || "work",
                occupants.length ? "occupied" : "",
                isActiveOrchestrator ? "active" : "",
              ].join(" ")}
            >
              <header className="floor-room-head">
                <h3>{room.label}</h3>
                <span>{roomSubtitle(room)}</span>
              </header>
              <div className="floor-occupants">
                {occupants.length ? (
                  occupants.map((agent) => (
                    <button
                      key={agent.agent_id}
                      className={`agent-token ${statusClass(agent.status)} ${
                        selectedAgentId === agent.agent_id ? "selected" : ""
                      }`}
                      title={agent.room?.reason || agent.status}
                      onClick={() => onSelectAgent(agent.agent_id)}
                    >
                      <span className="agent-token-name">
                        <i className={`agent-token-dot ${statusClass(agent.status)}`} />
                        {agent.display_name}
                      </span>
                      <span className="agent-token-task">
                        {shortText(agent.current_assignment?.assignment || agent.status, 58)}
                      </span>
                      {!isRest && (
                        <span className="agent-token-bar" aria-hidden="true">
                          <i style={{ width: `${tokenPercent(agent)}%` }} />
                        </span>
                      )}
                    </button>
                  ))
                ) : (
                  <p className="floor-empty">
                    {isOrchestrator && latestOrchestrationEvent
                      ? shortText(orchestrationEventText(latestOrchestrationEvent), 72)
                      : isOrchestrator && snapshot?.latest_reasoning?.decision_summary
                        ? shortText(snapshot.latest_reasoning.decision_summary, 72)
                      : isRest
                        ? "-"
                        : "empty"}
                  </p>
                )}
              </div>
              <footer className="floor-room-foot">
                {isRest ? (
                  <>
                    <span>occupants</span>
                    <strong>{occupants.length || "-"}</strong>
                  </>
                ) : (
                  <>
                    <span>recorded tokens</span>
                    <strong>{tokenUse ? tokenUse.toLocaleString() : "-"}</strong>
                  </>
                )}
              </footer>
            </article>
          );
        })}
      </div>
    </div>
  );
}

function agentRoomId(agent: VisualAgent) {
  if (agent.room?.id) {
    return agent.room.id;
  }
  if (agent.current_assignment?.room_id) {
    return agent.current_assignment.room_id;
  }
  if (["blocked", "awaiting_human", "reflecting", "ruminating", "dreaming"].includes(agent.status)) {
    return "barracks";
  }
  return agent.current_assignment ? "cubicles" : "breakroom";
}

function roomSubtitle(room: OpsRoomRoom) {
  if (room.statuses?.length) {
    return room.statuses.join(" / ");
  }
  return room.domains.length ? room.domains.join(" / ") : "-";
}

function tokenPercent(agent: VisualAgent) {
  return Math.min(100, Math.max(8, Math.round(((agent.usage?.total_tokens || 0) / 60000) * 100)));
}

function statusClass(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}

function useAutoScroll<T extends HTMLElement>(dependencies: React.DependencyList) {
  const ref = useRef<T | null>(null);
  useEffect(() => {
    const element = ref.current;
    if (element) {
      element.scrollTop = element.scrollHeight;
    }
  }, dependencies);
  return ref;
}

function renderMarkdownHtml(text: string) {
  const lines = text.split(/\r?\n/);
  const parts: string[] = [];
  let inCode = false;
  let codeLines: string[] = [];
  let inList = false;
  let tableRows: string[][] = [];

  const flushTable = () => {
    if (!tableRows.length) {
      return;
    }
    const [header, ...body] = tableRows;
    parts.push("<table><thead><tr>");
    header.forEach((cell) => parts.push(`<th>${renderInlineMarkdown(cell)}</th>`));
    parts.push("</tr></thead>");
    if (body.length) {
      parts.push("<tbody>");
      body.forEach((row) => {
        parts.push("<tr>");
        header.forEach((_, index) => {
          parts.push(`<td>${renderInlineMarkdown(row[index] || "")}</td>`);
        });
        parts.push("</tr>");
      });
      parts.push("</tbody>");
    }
    parts.push("</table>");
    tableRows = [];
  };

  const flushList = () => {
    if (inList) {
      parts.push("</ul>");
      inList = false;
    }
  };

  lines.forEach((raw) => {
    const line = raw.trim();
    if (line.startsWith("```")) {
      flushTable();
      if (!inCode) {
        inCode = true;
        codeLines = [];
      } else {
        parts.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
        inCode = false;
      }
      return;
    }
    if (inCode) {
      codeLines.push(raw);
      return;
    }
    if (!line) {
      flushTable();
      flushList();
      return;
    }
    if (isMarkdownTableRow(line)) {
      const cells = splitMarkdownTableRow(line);
      if (!isMarkdownTableSeparator(cells)) {
        tableRows.push(cells);
      }
      return;
    }
    flushTable();
    if (line.startsWith("- ") || line.startsWith("* ")) {
      if (!inList) {
        parts.push("<ul>");
        inList = true;
      }
      parts.push(`<li>${renderInlineMarkdown(line.slice(2).trim())}</li>`);
      return;
    }
    flushList();
    if (line.startsWith("### ")) {
      parts.push(`<h3>${renderInlineMarkdown(line.slice(4))}</h3>`);
    } else if (line.startsWith("## ")) {
      parts.push(`<h2>${renderInlineMarkdown(line.slice(3))}</h2>`);
    } else if (line.startsWith("# ")) {
      parts.push(`<h1>${renderInlineMarkdown(line.slice(2))}</h1>`);
    } else if (line.startsWith("> ")) {
      parts.push(`<blockquote>${renderInlineMarkdown(line.slice(2))}</blockquote>`);
    } else {
      parts.push(`<p>${renderInlineMarkdown(line)}</p>`);
    }
  });

  flushTable();
  flushList();
  if (inCode) {
    parts.push(`<pre><code>${escapeHtml(codeLines.join("\n"))}</code></pre>`);
  }
  return parts.join("\n");
}

function renderInlineMarkdown(text: string) {
  return escapeHtml(text)
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\*([^*]+)\*/g, "<em>$1</em>");
}

function escapeHtml(text: string) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function isMarkdownTableRow(text: string) {
  return text.startsWith("|") && text.endsWith("|") && text.split("|").length >= 3;
}

function splitMarkdownTableRow(text: string) {
  return text.slice(1, -1).split("|").map((cell) => cell.trim());
}

function isMarkdownTableSeparator(cells: string[]) {
  return cells.length > 0 && cells.every((cell) => cell.length > 0 && /^:?-+:?$/.test(cell));
}

function modelOptionKey(option: ModelOption) {
  return `${option.provider}::${option.model}::${option.base_url || ""}`;
}

function modelRouteKey(route: ModelRoute) {
  return `${route.provider}::${route.model}::${route.base_url || ""}`;
}

function modelRouteFromOption(option: ModelOption | null | undefined): ModelRoute | null {
  if (!option) {
    return null;
  }
  return {
    provider: option.provider,
    model: option.model,
    base_url: option.base_url,
  };
}

// The agent's *persisted* model (Agent.model_provider/model_name), as a route the
// ModelSelect can show selected. Matches an inventory option when available so the
// option key lines up; otherwise constructs a bare route from the stored fields.
function agentModelRoute(
  agent: VisualAgent | null,
  models: ModelInventory | null,
): ModelRoute | null {
  if (!agent) {
    return null;
  }
  const match = models?.options.find(
    (option) => option.provider === agent.model_provider && option.model === agent.model_name,
  );
  if (match) {
    return modelRouteFromOption(match);
  }
  return { provider: agent.model_provider, model: agent.model_name };
}

function modelRoutePayload(route: ModelRoute | null): Record<string, string> {
  if (!route) {
    return {};
  }
  return {
    provider: route.provider,
    model: route.model,
    ...(route.base_url ? { base_url: route.base_url } : {}),
  };
}

function visibleModelOptions(inventory: ModelInventory | null) {
  return inventory?.options || [];
}

function allAgents(cockpit: CockpitPayload | null, snapshot: OpsRoomSnapshot | null) {
  return cockpit?.agents || snapshot?.agents || [];
}

function taskSortKey(task: Assignment) {
  const rank = task.status === "working" || task.status === "assigned" ? "0" : "1";
  return `${rank}:${task.created_at || task.updated_at || ""}:${task.assignment_id}`;
}

function lines(value: string) {
  return value
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

function randomId(prefix: string) {
  if ("randomUUID" in crypto) {
    return `${prefix}:${crypto.randomUUID()}`;
  }
  return `${prefix}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
}

function shortText(value: string, limit: number) {
  const normalized = value.split(/\s+/).join(" ");
  return normalized.length <= limit ? normalized : `${normalized.slice(0, limit - 3)}...`;
}

function formatDuration(seconds: number) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days > 0) {
    return `${days}d ${hours}h`;
  }
  if (hours > 0) {
    return `${hours}h ${minutes}m`;
  }
  return `${minutes}m`;
}

function formatTime(value?: string | null) {
  if (!value) {
    return "unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

type JwtMetadata = {
  issuedAt?: number | null;
  expiresAt?: number | null;
};

function readJwtMetadata(token: string): JwtMetadata | null {
  if (!token) {
    return null;
  }
  const parts = token.split(".");
  if (parts.length !== 3) {
    return null;
  }
  try {
    const paddedPayload = parts[1]
      .replace(/-/g, "+")
      .replace(/_/g, "/")
      .padEnd(Math.ceil(parts[1].length / 4) * 4, "=");
    const payload = JSON.parse(atob(paddedPayload)) as {
      iat?: number | null;
      exp?: number | null;
    };
    return { issuedAt: payload.iat ?? null, expiresAt: payload.exp ?? null };
  } catch {
    return null;
  }
}

function isTokenExpired(auth: AuthMe | null, tokenMetadata: JwtMetadata | null, nowMs: number) {
  const expiresAt = auth?.token?.expires_at ?? tokenMetadata?.expiresAt;
  return typeof expiresAt === "number" && expiresAt * 1000 <= nowMs;
}

async function responseText(response: Response) {
  const text = await response.text();
  if (!text) {
    return response.statusText || `${response.status}`;
  }
  try {
    const parsed = JSON.parse(text) as { detail?: unknown };
    return typeof parsed.detail === "string" ? parsed.detail : text;
  } catch {
    return text;
  }
}

function errorMessage(error: unknown) {
  if (error instanceof Error) {
    return error.message;
  }
  return String(error);
}

createRoot(document.getElementById("root")!).render(<App />);
