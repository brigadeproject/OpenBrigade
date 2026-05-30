import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const TILE_SIZE = 16;
const ASSET_ROOT = "/assets/pixel-agents";
const DEFAULT_SEATS: Seat[] = [
  { agent_id: "", x: 3, y: 14 },
  { agent_id: "", x: 7, y: 14 },
  { agent_id: "", x: 4, y: 17 },
  { agent_id: "", x: 6, y: 17 },
  { agent_id: "", x: 4, y: 19 },
  { agent_id: "", x: 6, y: 19 },
  { agent_id: "", x: 12, y: 13 },
  { agent_id: "", x: 16, y: 13 },
  { agent_id: "", x: 12, y: 16 },
  { agent_id: "", x: 16, y: 16 },
];

const FURNITURE_PATHS: Record<string, string> = {
  BIN: "BIN/BIN.png",
  CLOCK: "CLOCK/CLOCK.png",
  COFFEE: "COFFEE/COFFEE.png",
  COFFEE_TABLE: "COFFEE_TABLE/COFFEE_TABLE.png",
  CUSHIONED_BENCH: "CUSHIONED_BENCH/CUSHIONED_BENCH.png",
  DESK_FRONT: "DESK/DESK_FRONT.png",
  DOUBLE_BOOKSHELF: "DOUBLE_BOOKSHELF/DOUBLE_BOOKSHELF.png",
  HANGING_PLANT: "HANGING_PLANT/HANGING_PLANT.png",
  LARGE_PAINTING: "LARGE_PAINTING/LARGE_PAINTING.png",
  PC_FRONT_OFF: "PC/PC_FRONT_OFF.png",
  PC_SIDE: "PC/PC_SIDE.png",
  PLANT: "PLANT/PLANT.png",
  PLANT_2: "PLANT_2/PLANT_2.png",
  SMALL_PAINTING: "SMALL_PAINTING/SMALL_PAINTING.png",
  SMALL_PAINTING_2: "SMALL_PAINTING_2/SMALL_PAINTING_2.png",
  SMALL_TABLE_FRONT: "SMALL_TABLE/SMALL_TABLE_FRONT.png",
  SMALL_TABLE_SIDE: "SMALL_TABLE/SMALL_TABLE_SIDE.png",
  SOFA_BACK: "SOFA/SOFA_BACK.png",
  SOFA_FRONT: "SOFA/SOFA_FRONT.png",
  SOFA_SIDE: "SOFA/SOFA_SIDE.png",
  TABLE_FRONT: "TABLE_FRONT/TABLE_FRONT.png",
  WOODEN_CHAIR_SIDE: "WOODEN_CHAIR/WOODEN_CHAIR_SIDE.png",
};

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
  goal_statement?: string | null;
  created_at?: string;
  updated_at?: string;
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

type VisualAgent = {
  agent_id: string;
  display_name: string;
  role: string;
  team_id?: string | null;
  team_role: string;
  status: string;
  activity: string;
  current_assignment?: Assignment | null;
  state?: AgentState | null;
  goals: Goal[];
  usage: Usage;
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
  created_at: string;
};

type Seat = {
  agent_id: string;
  x: number;
  y: number;
};

type OpsRoomLayout = {
  version: number;
  layout_key: string;
  seats: Seat[];
};

type OpsRoomSnapshot = {
  version: number;
  generated_at: string;
  mission: Mission | null;
  latest_reasoning?: { decision_summary?: string; cycle_id?: string } | null;
  agents: VisualAgent[];
  teams: Team[];
  assignments: Assignment[];
  goals: Record<string, Goal[]>;
  alerts: string[];
  financial_report?: Record<string, unknown> | null;
  local_inference?: Record<string, unknown>;
  cloud_jobs?: Record<string, unknown>[];
  layout: OpsRoomLayout;
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

type DefaultLayout = {
  cols: number;
  rows: number;
  tiles: number[];
  furniture: { uid: string; type: string; col: number; row: number }[];
};

type Assets = {
  layout: DefaultLayout;
  floors: HTMLImageElement[];
  characters: HTMLImageElement[];
  furniture: Record<string, HTMLImageElement>;
};

type ApiOptions = RequestInit & { json?: unknown };

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

function initialView(): "cockpit" | "ops" {
  const requested = new URLSearchParams(window.location.search).get("view");
  if (requested === "ops" || requested === "cockpit") {
    return requested;
  }
  const saved = localStorage.getItem("brigade_view");
  return saved === "ops" ? "ops" : "cockpit";
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
  const [seatDraft, setSeatDraft] = useState<Seat[]>([]);
  const [layoutDirty, setLayoutDirty] = useState(false);
  const [editingSeats, setEditingSeats] = useState(false);
  const [activePanel, setActivePanel] = useState<"tasks" | "chat" | "goals">("tasks");
  const [view, setView] = useState<"cockpit" | "ops">(() => initialView());
  const [taskDialogOpen, setTaskDialogOpen] = useState(false);

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
    if (!layoutDirty) {
      setSeatDraft(next.layout?.seats || []);
    }
  }, [api, layoutDirty, selectedAgentId]);

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
            if (!layoutDirty) {
              setSeatDraft(next.layout?.seats || []);
            }
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
  }, [layoutDirty, selectedAgentId, token, tokenExpired]);

  const selectedAgent = useMemo(
    () => allAgents(cockpit, snapshot).find((agent) => agent.agent_id === selectedAgentId) || null,
    [cockpit, selectedAgentId, snapshot],
  );

  const agents = useMemo(() => allAgents(cockpit, snapshot), [cockpit, snapshot]);
  const tasks = useMemo(() => allTasks(cockpit, snapshot), [cockpit, snapshot]);
  const recommendedModel = modelRouteFromOption(models?.recommended || null);
  const selectedAgentModel = selectedAgentId
    ? agentModelSelections[selectedAgentId] || recommendedModel
    : recommendedModel;
  const blockedAgents = useMemo(
    () =>
      agents
        .filter((agent) => agent.status === "blocked" || agent.status === "awaiting_human")
        .sort((a, b) => Number(b.status === "awaiting_human") - Number(a.status === "awaiting_human")),
    [agents],
  );
  const idleAgents = useMemo(
    () => agents.filter((agent) => agent.status === "idle"),
    [agents],
  );
  const headerTasks = useMemo(
    () =>
      tasks
        .filter((task) => !isBlockedTask(task))
        .filter((task) => ["assigned", "working", "queued"].includes(task.status))
        .sort((a, b) => taskSortKey(a).localeCompare(taskSortKey(b))),
    [tasks],
  );

  const selectAgent = useCallback((agentId: string, panel: "tasks" | "chat" | "goals" = "tasks") => {
    if (!agentId) {
      return;
    }
    setSelectedAgentId(agentId);
    setActivePanel(panel);
    setView("ops");
  }, []);

  const setSelectedAgentModel = useCallback((route: ModelRoute) => {
    if (!selectedAgentId) {
      return;
    }
    setAgentModelSelections((current) => ({ ...current, [selectedAgentId]: route }));
  }, [selectedAgentId]);

  async function saveSeatLayout() {
    if (!snapshot) {
      return;
    }
    const resolved = resolveSeats(snapshot.agents, seatDraft);
    await api<OpsRoomLayout>("/api/ops-room/layout", {
      method: "PUT",
      json: { version: 1, layout_key: "ops-room", seats: resolved },
    });
    setSeatDraft(resolved);
    setLayoutDirty(false);
    setStatus("Seat layout saved");
    await loadSnapshot();
  }

  const statusTone = tokenExpired || authMessage ? "bad" : streamStatus === "live" ? "good" : "warn";

  return (
    <main className="app-shell">
      <header className="command-bar">
        <div className="brand-block">
          <strong>OpenBrigade</strong>
          <span>{cockpit?.mission?.statement || "Mission not set"}</span>
        </div>

        <HeaderSelect
          label="Agents"
          value=""
          placeholder={`${agents.length} agents`}
          options={agents.map((agent) => ({
            value: agent.agent_id,
            label: `${agent.display_name} / ${agent.status}`,
          }))}
          onSelect={(agentId) => selectAgent(agentId)}
        />
        <HeaderSelect
          label="Idle"
          value=""
          placeholder={`${idleAgents.length} idle`}
          options={idleAgents.map((agent) => ({
            value: agent.agent_id,
            label: agent.display_name,
          }))}
          onSelect={(agentId) => selectAgent(agentId)}
        />
        <HeaderSelect
          label="Blocked"
          value=""
          placeholder={`${blockedAgents.length} blocked`}
          alert={blockedAgents.some((agent) => agent.status === "awaiting_human")}
          options={blockedAgents.map((agent) => ({
            value: agent.agent_id,
            label: `${agent.display_name} / ${blockerSummary(agent)}`,
          }))}
          onSelect={(agentId) => selectAgent(agentId, "tasks")}
        />
        <HeaderSelect
          label="Tasks"
          value=""
          placeholder={`${headerTasks.length} tasks`}
          options={headerTasks.map((task) => ({
            value: task.assignment_id,
            label: `${task.status} / ${task.assigned_to} / ${shortText(task.assignment, 46)}`,
          }))}
          onSelect={(assignmentId) => {
            const task = tasks.find((item) => item.assignment_id === assignmentId);
            if (task) {
              selectAgent(task.assigned_to, "tasks");
            }
          }}
        />

        <div className="mode-toggle" aria-label="View selector">
          <button className={view === "cockpit" ? "active" : ""} onClick={() => setView("cockpit")}>
            Cockpit
          </button>
          <button className={view === "ops" ? "active" : ""} onClick={() => setView("ops")}>
            Ops Room
          </button>
        </div>
      </header>

      <section className="status-strip">
        <span className={`health-dot ${statusTone}`}>{auth?.user?.role || auth?.method || "auth"}</span>
        <span className={`health-dot ${streamStatus === "live" ? "good" : "warn"}`}>{streamStatus}</span>
        <span>{status}</span>
        {cockpit?.auth.unsafe_bind_without_auth && (
          <strong className="inline-warning">Auth disabled on {cockpit.auth.web_host}</strong>
        )}
        {tokenExpired && <strong className="inline-warning">Token expired</strong>}
        {tokenMalformed && <strong className="inline-warning">Token format unreadable</strong>}
        {authMessage && <strong className="inline-warning">{authMessage}</strong>}
        <div className="token-control">
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
          can={can}
          api={api}
          onSelectAgent={selectAgent}
          onSelectedAgentModelChange={setSelectedAgentModel}
          onOrchestratorModelChange={setOrchestratorModel}
          onRefresh={refreshAll}
          setStatus={setStatus}
          onOpenTaskDialog={() => setTaskDialogOpen(true)}
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
          seatDraft={seatDraft}
          editingSeats={editingSeats}
          layoutDirty={layoutDirty}
          can={can}
          api={api}
          onSaveSeats={() => saveSeatLayout().catch((error) => setStatus(errorMessage(error)))}
          onToggleSeats={() => setEditingSeats((value) => !value)}
          onSelectAgent={setSelectedAgentId}
          onChangeSeats={(nextSeats) => {
            setSeatDraft(nextSeats);
            setLayoutDirty(true);
          }}
          onRefresh={refreshAll}
          setStatus={setStatus}
          onOpenTaskDialog={() => setTaskDialogOpen(true)}
        />
      )}

      {taskDialogOpen && (
        <TaskDialog
          agents={agents}
          selectedAgentId={selectedAgentId}
          canCreate={can("task:write")}
          api={api}
          onClose={() => setTaskDialogOpen(false)}
          onDone={refreshAll}
          setStatus={setStatus}
        />
      )}
    </main>
  );
}

function HeaderSelect({
  label,
  value,
  placeholder,
  options,
  alert = false,
  onSelect,
}: {
  label: string;
  value: string;
  placeholder: string;
  options: { value: string; label: string }[];
  alert?: boolean;
  onSelect: (value: string) => void;
}) {
  return (
    <label className={`header-select ${alert ? "alert" : ""}`}>
      <span>{label}</span>
      <select
        value={value}
        onChange={(event) => {
          onSelect(event.target.value);
          event.currentTarget.value = "";
        }}
      >
        <option value="">{placeholder}</option>
        {options.map((option) => (
          <option key={option.value} value={option.value}>
            {option.label}
          </option>
        ))}
      </select>
    </label>
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
  can,
  api,
  onSelectAgent,
  onSelectedAgentModelChange,
  onOrchestratorModelChange,
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
  can: (permission: string) => boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onSelectAgent: (agentId: string, panel?: "tasks" | "chat" | "goals") => void;
  onSelectedAgentModelChange: (route: ModelRoute) => void;
  onOrchestratorModelChange: (route: ModelRoute) => void;
  onRefresh: () => Promise<void>;
  setStatus: (status: string) => void;
  onOpenTaskDialog: () => void;
}) {
  const selectedAgent = cockpit?.agents.find((agent) => agent.agent_id === selectedAgentId) || null;
  return (
    <section className="cockpit">
      <div className="widget-grid">
        <Widget title="Uptime">
          <div className="stat-large">{formatDuration(cockpit?.uptime_seconds || 0)}</div>
          <p className="muted">Started {formatTime(cockpit?.started_at)}</p>
        </Widget>
        <Widget title="Service Health">
          <HealthList checks={cockpit?.datastores || []} />
        </Widget>
        <Widget title="Models Available">
          <ModelSummary cockpit={cockpit} models={models} />
        </Widget>
        <Widget title="Token Usage / Spend">
          <UsageSummary usage={cockpit?.usage || null} />
        </Widget>
        <Widget title="Current Mission" wide>
          <MissionWidget
            mission={cockpit?.mission || null}
            latestReasoning={cockpit?.latest_reasoning || null}
            canEdit={can("mission:write")}
            api={api}
            onDone={onRefresh}
            setStatus={setStatus}
          />
        </Widget>
        <Widget title="Current Alerts">
          <AlertList alerts={cockpit?.alerts || []} />
        </Widget>
        <Widget title="Tasks" wide>
          <TaskBoard
            tasks={cockpit?.tasks || null}
            agents={cockpit?.agents || []}
            canCreate={can("task:write")}
            onOpenTaskDialog={onOpenTaskDialog}
            onSelectAgent={(agentId) => onSelectAgent(agentId, "tasks")}
          />
        </Widget>
        <Widget title="Teams">
          <TeamBoard
            teams={cockpit?.teams || []}
            agents={cockpit?.agents || []}
            canEdit={can("team:write")}
            api={api}
            onDone={onRefresh}
            setStatus={setStatus}
          />
        </Widget>
        <Widget title="Selected Agent">
          <AgentInspector agent={selectedAgent} teams={cockpit?.teams || []} />
          {selectedAgent && (
            <>
              <ModelSelect
                label="Agent model"
                inventory={models}
                route={selectedAgentModel}
                onChange={onSelectedAgentModelChange}
              />
              <div className="button-row">
                <button onClick={() => onSelectAgent(selectedAgent.agent_id, "chat")}>Chat</button>
                <button onClick={() => onSelectAgent(selectedAgent.agent_id, "tasks")}>Tasks</button>
              </div>
            </>
          )}
        </Widget>
        <Widget title="Orchestrator Chat" wide>
          <OrchestratorChat
            canChat={can("chat:write")}
            api={api}
            inventory={models}
            route={orchestratorModel}
            onRouteChange={onOrchestratorModelChange}
            setStatus={setStatus}
          />
        </Widget>
        <Widget title="Settings / Status" wide>
          <SettingsStatus
            settings={settings}
            cockpit={cockpit}
            auth={auth}
            authMessage={authMessage}
            tokenExpired={tokenExpired}
          />
        </Widget>
      </div>
    </section>
  );
}

function Widget({
  title,
  wide = false,
  children,
}: {
  title: string;
  wide?: boolean;
  children: React.ReactNode;
}) {
  return (
    <article className={`widget ${wide ? "wide" : ""}`}>
      <h2>{title}</h2>
      {children}
    </article>
  );
}

function HealthList({ checks }: { checks: { name: string; ok: boolean; detail: string }[] }) {
  if (!checks.length) {
    return <p className="muted">No health checks reported.</p>;
  }
  return (
    <div className="health-list">
      {checks.map((check) => (
        <div key={check.name} className="health-row">
          <span className={`status-light ${check.ok ? "ok" : "bad"}`} />
          <strong>{check.name}</strong>
          <span>{check.detail}</span>
        </div>
      ))}
    </div>
  );
}

function ModelSummary({
  cockpit,
  models,
}: {
  cockpit: CockpitPayload | null;
  models: ModelInventory | null;
}) {
  if (!cockpit) {
    return <p className="muted">Loading model status.</p>;
  }
  const available = models?.options.filter((option) => option.available) || [];
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
      <div className="mini-list">
        {(models?.options || []).slice(0, 5).map((option) => (
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
  const options = inventory?.options || [];
  const value = route ? modelRouteKey(route) : inventory ? modelOptionKey(inventory.recommended) : "";
  return (
    <label className="model-select">
      <span>{label}</span>
      <select
        value={value}
        disabled={!inventory || options.length === 0}
        onChange={(event) => {
          const option = options.find((item) => modelOptionKey(item) === event.target.value);
          const nextRoute = modelRouteFromOption(option || null);
          if (nextRoute) {
            onChange(nextRoute);
          }
        }}
      >
        {!inventory && <option value="">Loading models</option>}
        {options.map((option) => (
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

function AlertList({ alerts }: { alerts: string[] }) {
  if (!alerts.length) {
    return <p className="muted">No current alerts.</p>;
  }
  return (
    <div className="stack-list compact">
      {alerts.slice(-8).map((alert, index) => (
        <article key={`${index}:${alert}`} className="alert-row">
          <p>{alert}</p>
        </article>
      ))}
    </div>
  );
}

function TaskBoard({
  tasks,
  agents,
  canCreate,
  onOpenTaskDialog,
  onSelectAgent,
}: {
  tasks: CockpitPayload["tasks"] | null;
  agents: VisualAgent[];
  canCreate: boolean;
  onOpenTaskDialog: () => void;
  onSelectAgent: (agentId: string) => void;
}) {
  const [filter, setFilter] = useState<"active" | "queued" | "blocked" | "all">("active");
  const source = tasks ? tasks[filter] : [];
  const agentNames = new Map(agents.map((agent) => [agent.agent_id, agent.display_name]));
  return (
    <div className="task-board">
      <div className="toolbar-row">
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
        <button disabled={!canCreate} onClick={onOpenTaskDialog}>
          Add Task
        </button>
      </div>
      <PermissionNotice
        allowed={canCreate}
        permission="task:write"
        action="task creation is disabled"
      />
      <div className="stack-list task-list">
        {source.map((task) => (
          <article
            key={task.assignment_id}
            className={`task-row ${task.status}`}
            onClick={() => onSelectAgent(task.assigned_to)}
          >
            <span>
              {agentNames.get(task.assigned_to) || task.assigned_to} / {task.status} /{" "}
              {task.priority}
            </span>
            <p>{task.assignment}</p>
            {task.blockers.length > 0 && <small>{task.blockers.join("; ")}</small>}
          </article>
        ))}
        {source.length === 0 && <p className="muted">No {filter} tasks.</p>}
      </div>
    </div>
  );
}

function TeamBoard({
  teams,
  agents,
  canEdit,
  api,
  onDone,
  setStatus,
}: {
  teams: Team[];
  agents: VisualAgent[];
  canEdit: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [selectedTeamId, setSelectedTeamId] = useState(teams[0]?.team_id || "");
  const selected = teams.find((team) => team.team_id === selectedTeamId) || teams[0] || null;
  const [displayName, setDisplayName] = useState(selected?.display_name || "");
  const [delegationPolicy, setDelegationPolicy] = useState(selected?.delegation_policy || "chief_only");
  const agentNames = new Map(agents.map((agent) => [agent.agent_id, agent.display_name]));

  useEffect(() => {
    if (!selectedTeamId && teams[0]) {
      setSelectedTeamId(teams[0].team_id);
    }
  }, [selectedTeamId, teams]);

  useEffect(() => {
    setDisplayName(selected?.display_name || "");
    setDelegationPolicy(selected?.delegation_policy || "chief_only");
  }, [selected]);

  async function saveTeam() {
    if (!selected) {
      return;
    }
    setStatus("Saving team");
    await api<Team>(`/api/teams/${encodeURIComponent(selected.team_id)}`, {
      method: "PATCH",
      json: {
        display_name: displayName,
        delegation_policy: delegationPolicy,
      },
    });
    setStatus("Team saved");
    await onDone();
  }

  if (!teams.length) {
    return <p className="muted">No teams configured.</p>;
  }
  return (
    <div className="team-board">
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
            <dd>{selected.members.map((id) => agentNames.get(id) || id).join(", ") || "none"}</dd>
            <dt>Escalation</dt>
            <dd>{selected.escalation_team_id || "none"}</dd>
          </dl>
          <div className="form-stack compact-form">
            <label>
              <span>Display name</span>
              <input
                value={displayName}
                disabled={!canEdit}
                onChange={(event) => setDisplayName(event.target.value)}
              />
            </label>
            <label>
              <span>Delegation policy</span>
              <select
                value={delegationPolicy}
                disabled={!canEdit}
                onChange={(event) => setDelegationPolicy(event.target.value)}
              >
                <option value="chief_only">chief_only</option>
                <option value="open">open</option>
              </select>
            </label>
            <button
              disabled={!canEdit}
              onClick={() => saveTeam().catch((error) => setStatus(errorMessage(error)))}
            >
              Save Team
            </button>
            <PermissionNotice
              allowed={canEdit}
              permission="team:write"
              action="team edits are disabled"
            />
          </div>
        </>
      )}
    </div>
  );
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
      <div className="chat-feed">
        {messages.slice(-8).map((item) => (
          <article key={item.message_id} className="message-row">
            <span>
              {item.sender} -&gt; {item.recipient} / {formatTime(item.created_at)}
            </span>
            {item.sender === "orchestrator" && responseHtmlById[item.message_id] ? (
              <div
                className="message-markdown"
                dangerouslySetInnerHTML={{ __html: responseHtmlById[item.message_id] }}
              />
            ) : (
              <p>{item.content}</p>
            )}
          </article>
        ))}
        {messages.length === 0 && <p className="muted">No orchestrator messages.</p>}
      </div>
      <div className="chat-compose">
        <textarea
          value={message}
          disabled={!canChat || pending}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Message the orchestrator channel"
        />
        <button
          disabled={!canChat || pending || !message.trim()}
          onClick={() => send().catch((error) => setStatus(errorMessage(error)))}
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
  seatDraft,
  editingSeats,
  layoutDirty,
  can,
  api,
  onSaveSeats,
  onToggleSeats,
  onSelectAgent,
  onChangeSeats,
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
  seatDraft: Seat[];
  editingSeats: boolean;
  layoutDirty: boolean;
  can: (permission: string) => boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onSaveSeats: () => void;
  onToggleSeats: () => void;
  onSelectAgent: (agentId: string) => void;
  onChangeSeats: (seats: Seat[]) => void;
  onRefresh: () => Promise<void>;
  setStatus: (status: string) => void;
  onOpenTaskDialog: () => void;
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
            <button className={editingSeats ? "active" : ""} onClick={onToggleSeats}>
              Seats
            </button>
            <button disabled={!layoutDirty} onClick={onSaveSeats}>
              Save Seats
            </button>
          </div>
        </div>
        <OpsRoomCanvas
          snapshot={snapshot}
          seats={seatDraft}
          editingSeats={editingSeats}
          selectedAgentId={selectedAgentId}
          onSelectAgent={onSelectAgent}
          onChangeSeats={onChangeSeats}
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
            api={api}
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
  onOpenTaskDialog: () => void;
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
  api,
  onDone,
  setStatus,
}: {
  snapshot: OpsRoomSnapshot | null;
  selectedAgentId: string;
  modelRoute: ModelRoute | null;
  canChat: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const messages = (snapshot?.messages || []).filter(
    (item) => item.sender === selectedAgentId || item.recipient === selectedAgentId,
  );

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
      setStatus("Chat complete");
      await onDone();
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel-body chat-panel">
      <div className="chat-feed">
        {messages.slice(-12).map((item) => (
          <article key={item.message_id} className="message-row">
            <span>
              {item.sender} -&gt; {item.recipient} / {formatTime(item.created_at)}
            </span>
            <p>{item.content}</p>
          </article>
        ))}
        {messages.length === 0 && <p className="muted">No messages for this agent.</p>}
      </div>
      <div className="chat-compose">
        <textarea
          value={message}
          disabled={!canChat || pending}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Message the selected agent"
        />
        <button
          disabled={!canChat || pending || !message.trim() || !selectedAgentId}
          onClick={() => send().catch((error) => setStatus(errorMessage(error)))}
        >
          {pending ? "Sending" : "Send"}
        </button>
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
  canCreate,
  api,
  onClose,
  onDone,
  setStatus,
}: {
  agents: VisualAgent[];
  selectedAgentId: string;
  canCreate: boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onClose: () => void;
  onDone: () => Promise<void>;
  setStatus: (status: string) => void;
}) {
  const [agentId, setAgentId] = useState(selectedAgentId || agents[0]?.agent_id || "");
  const [assignment, setAssignment] = useState("");
  const [priority, setPriority] = useState("normal");
  const [workMode, setWorkMode] = useState("heartbeat");
  const [goalStatement, setGoalStatement] = useState("");

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

function OpsRoomCanvas({
  snapshot,
  seats,
  editingSeats,
  selectedAgentId,
  onSelectAgent,
  onChangeSeats,
}: {
  snapshot: OpsRoomSnapshot | null;
  seats: Seat[];
  editingSeats: boolean;
  selectedAgentId: string;
  onSelectAgent: (agentId: string) => void;
  onChangeSeats: (seats: Seat[]) => void;
}) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const { assets, error } = useAssets();
  const [zoom, setZoom] = useState(1);
  const [dragAgentId, setDragAgentId] = useState<string | null>(null);
  const resolvedSeats = useMemo(
    () => resolveSeats(snapshot?.agents || [], seats),
    [seats, snapshot?.agents],
  );

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) {
      return;
    }
    const resize = () => {
      const dpr = window.devicePixelRatio || 1;
      const rect = wrap.getBoundingClientRect();
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      canvas.style.width = `${rect.width}px`;
      canvas.style.height = `${rect.height}px`;
    };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(wrap);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }
    const context = canvas.getContext("2d");
    if (!context) {
      return;
    }
    let frame = 0;
    let stopped = false;
    const draw = (time: number) => {
      if (stopped) {
        return;
      }
      if (assets && snapshot) {
        renderRoom(context, canvas, assets, snapshot, resolvedSeats, {
          editingSeats,
          selectedAgentId,
          time,
          zoom,
        });
      } else {
        renderFallbackRoom(context, canvas, snapshot, error);
      }
      frame = window.requestAnimationFrame(draw);
    };
    frame = window.requestAnimationFrame(draw);
    return () => {
      stopped = true;
      window.cancelAnimationFrame(frame);
    };
  }, [assets, editingSeats, error, resolvedSeats, selectedAgentId, snapshot, zoom]);

  const pointerToTile = useCallback(
    (event: React.PointerEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current;
      if (!canvas || !assets) {
        return null;
      }
      const rect = canvas.getBoundingClientRect();
      const metrics = roomMetrics(rect.width, rect.height, assets.layout, zoom);
      const x = (event.clientX - rect.left - metrics.originX) / metrics.scale;
      const y = (event.clientY - rect.top - metrics.originY) / metrics.scale;
      return {
        x: Math.max(0, Math.min(assets.layout.cols - 1, Math.round(x / TILE_SIZE))),
        y: Math.max(0, Math.min(assets.layout.rows - 1, Math.round(y / TILE_SIZE))),
      };
    },
    [assets, zoom],
  );

  const agentAtPointer = useCallback(
    (event: React.PointerEvent<HTMLCanvasElement>) => {
      const tile = pointerToTile(event);
      if (!tile) {
        return null;
      }
      let nearest: { agentId: string; distance: number } | null = null;
      for (const seat of resolvedSeats) {
        const distance = Math.abs(tile.x - seat.x) + Math.abs(tile.y - seat.y);
        if (distance <= 1 && (!nearest || distance < nearest.distance)) {
          nearest = { agentId: seat.agent_id, distance };
        }
      }
      return nearest?.agentId || null;
    },
    [pointerToTile, resolvedSeats],
  );

  function moveSeat(agentId: string, x: number, y: number) {
    const next = resolvedSeats.map((seat) =>
      seat.agent_id === agentId ? { ...seat, x, y } : seat,
    );
    onChangeSeats(next);
  }

  return (
    <div className="canvas-wrap" ref={wrapRef}>
      <canvas
        ref={canvasRef}
        onPointerDown={(event) => {
          const agentId = agentAtPointer(event);
          if (agentId) {
            onSelectAgent(agentId);
          }
          if (editingSeats && agentId) {
            setDragAgentId(agentId);
            event.currentTarget.setPointerCapture(event.pointerId);
          }
        }}
        onPointerMove={(event) => {
          if (!editingSeats || !dragAgentId) {
            return;
          }
          const tile = pointerToTile(event);
          if (tile) {
            moveSeat(dragAgentId, tile.x, tile.y);
          }
        }}
        onPointerUp={(event) => {
          if (dragAgentId) {
            event.currentTarget.releasePointerCapture(event.pointerId);
          }
          setDragAgentId(null);
        }}
      />
      <div className="zoom-controls">
        <button onClick={() => setZoom((value) => Math.max(0.7, value - 0.15))}>-</button>
        <span>{Math.round(zoom * 100)}%</span>
        <button onClick={() => setZoom((value) => Math.min(1.8, value + 0.15))}>+</button>
      </div>
      {!assets && !error && <div className="canvas-loading">Loading room assets</div>}
    </div>
  );
}

function useAssets() {
  const [assets, setAssets] = useState<Assets | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const [layout, floors, characters, furniture] = await Promise.all([
          fetch(`${ASSET_ROOT}/default-layout-1.json`).then((response) => response.json()),
          Promise.all(
            Array.from({ length: 9 }, (_, index) =>
              loadImage(`${ASSET_ROOT}/floors/floor_${index}.png`),
            ),
          ),
          Promise.all(
            Array.from({ length: 6 }, (_, index) =>
              loadImage(`${ASSET_ROOT}/characters/char_${index}.png`),
            ),
          ),
          loadFurnitureImages(),
        ]);
        if (!cancelled) {
          setAssets({ layout, floors, characters, furniture });
        }
      } catch (loadError) {
        if (!cancelled) {
          setError(errorMessage(loadError));
        }
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, []);

  return { assets, error };
}

async function loadFurnitureImages() {
  const entries = await Promise.all(
    Object.entries(FURNITURE_PATHS).map(async ([type, path]) => [
      type,
      await loadImage(`${ASSET_ROOT}/furniture/${path}`),
    ]),
  );
  return Object.fromEntries(entries) as Record<string, HTMLImageElement>;
}

function loadImage(src: string) {
  return new Promise<HTMLImageElement>((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error(`failed to load ${src}`));
    image.src = src;
  });
}

function renderRoom(
  context: CanvasRenderingContext2D,
  canvas: HTMLCanvasElement,
  assets: Assets,
  snapshot: OpsRoomSnapshot,
  seats: Seat[],
  options: {
    editingSeats: boolean;
    selectedAgentId: string;
    time: number;
    zoom: number;
  },
) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#26332b";
  context.fillRect(0, 0, width, height);

  const metrics = roomMetrics(width, height, assets.layout, options.zoom);
  context.save();
  context.translate(metrics.originX, metrics.originY);
  context.scale(metrics.scale, metrics.scale);
  drawTiles(context, assets);
  drawFurniture(context, assets);
  drawSeats(context, seats, options.editingSeats);
  drawAgents(context, assets, snapshot, seats, options);
  context.restore();
}

function renderFallbackRoom(
  context: CanvasRenderingContext2D,
  canvas: HTMLCanvasElement,
  snapshot: OpsRoomSnapshot | null,
  error: string,
) {
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const dpr = window.devicePixelRatio || 1;
  context.setTransform(dpr, 0, 0, dpr, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = "#223129";
  context.fillRect(0, 0, width, height);
  context.fillStyle = "#e8eee9";
  context.font = "14px sans-serif";
  context.fillText(error || "Waiting for room state", 24, 32);
  (snapshot?.agents || []).slice(0, 12).forEach((agent, index) => {
    const x = 28 + (index % 4) * 128;
    const y = 72 + Math.floor(index / 4) * 72;
    context.fillStyle = statusColor(agent.status);
    context.fillRect(x, y, 44, 44);
    context.fillStyle = "#ffffff";
    context.fillText(agent.display_name, x + 54, y + 24);
  });
}

function drawTiles(context: CanvasRenderingContext2D, assets: Assets) {
  const { layout } = assets;
  for (let row = 0; row < layout.rows; row += 1) {
    for (let col = 0; col < layout.cols; col += 1) {
      const tile = layout.tiles[row * layout.cols + col];
      const x = col * TILE_SIZE;
      const y = row * TILE_SIZE;
      if (tile === 255 || tile === undefined) {
        context.fillStyle = "#1c2823";
        context.fillRect(x, y, TILE_SIZE, TILE_SIZE);
        continue;
      }
      const floor = assets.floors[Math.max(0, Math.min(assets.floors.length - 1, tile))];
      context.drawImage(floor, x, y, TILE_SIZE, TILE_SIZE);
      context.strokeStyle = "rgba(29, 38, 34, 0.16)";
      context.strokeRect(x + 0.5, y + 0.5, TILE_SIZE - 1, TILE_SIZE - 1);
    }
  }
}

function drawFurniture(context: CanvasRenderingContext2D, assets: Assets) {
  const sorted = [...assets.layout.furniture].sort((a, b) => a.row - b.row || a.col - b.col);
  for (const item of sorted) {
    const type = item.type.replace(":left", "");
    const image = assets.furniture[type];
    if (!image) {
      continue;
    }
    const x = item.col * TILE_SIZE;
    const y = item.row * TILE_SIZE;
    if (item.type.endsWith(":left")) {
      context.save();
      context.translate(x + image.width, y);
      context.scale(-1, 1);
      context.drawImage(image, 0, 0);
      context.restore();
    } else {
      context.drawImage(image, x, y);
    }
  }
}

function drawSeats(context: CanvasRenderingContext2D, seats: Seat[], editing: boolean) {
  if (!editing) {
    return;
  }
  for (const seat of seats) {
    const x = seat.x * TILE_SIZE;
    const y = seat.y * TILE_SIZE;
    context.fillStyle = "rgba(255, 202, 87, 0.26)";
    context.strokeStyle = "#f0b84c";
    context.lineWidth = 1;
    context.fillRect(x, y, TILE_SIZE, TILE_SIZE);
    context.strokeRect(x + 0.5, y + 0.5, TILE_SIZE - 1, TILE_SIZE - 1);
  }
}

function drawAgents(
  context: CanvasRenderingContext2D,
  assets: Assets,
  snapshot: OpsRoomSnapshot,
  seats: Seat[],
  options: { selectedAgentId: string; time: number },
) {
  const seatMap = new Map(seats.map((seat) => [seat.agent_id, seat]));
  snapshot.agents.forEach((agent, index) => {
    const seat = seatMap.get(agent.agent_id);
    if (!seat) {
      return;
    }
    const sprite = assets.characters[index % assets.characters.length];
    const typing = agent.activity === "typing";
    const attention = agent.activity === "attention" || agent.activity === "blocked";
    const frame = typing ? 3 + (Math.floor(options.time / 280) % 2) : attention ? 5 : 1;
    const bob = typing ? Math.sin(options.time / 120) * 1 : 0;
    const x = seat.x * TILE_SIZE;
    const y = seat.y * TILE_SIZE - 18 + bob;

    if (agent.agent_id === options.selectedAgentId) {
      context.fillStyle = "rgba(238, 181, 67, 0.32)";
      context.beginPath();
      context.ellipse(x + 8, y + 30, 12, 5, 0, 0, Math.PI * 2);
      context.fill();
    }

    context.drawImage(sprite, frame * 16, 0, 16, 32, x, y, 16, 32);
    drawAgentLabel(context, agent, x + 8, y - 5);
  });
}

function drawAgentLabel(
  context: CanvasRenderingContext2D,
  agent: VisualAgent,
  centerX: number,
  y: number,
) {
  const text = `${agent.display_name} ${statusMark(agent.status)}`;
  context.font = "8px sans-serif";
  const width = Math.min(96, context.measureText(text).width + 8);
  context.fillStyle = "rgba(255, 255, 255, 0.92)";
  context.strokeStyle = statusColor(agent.status);
  context.lineWidth = 1;
  roundRect(context, centerX - width / 2, y - 10, width, 12, 3);
  context.fill();
  context.stroke();
  context.fillStyle = "#1e2a24";
  context.fillText(text, centerX - width / 2 + 4, y - 1);
}

function roundRect(
  context: CanvasRenderingContext2D,
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
) {
  context.beginPath();
  context.moveTo(x + radius, y);
  context.lineTo(x + width - radius, y);
  context.quadraticCurveTo(x + width, y, x + width, y + radius);
  context.lineTo(x + width, y + height - radius);
  context.quadraticCurveTo(x + width, y + height, x + width - radius, y + height);
  context.lineTo(x + radius, y + height);
  context.quadraticCurveTo(x, y + height, x, y + height - radius);
  context.lineTo(x, y + radius);
  context.quadraticCurveTo(x, y, x + radius, y);
}

function roomMetrics(width: number, height: number, layout: DefaultLayout, zoom: number) {
  const roomWidth = layout.cols * TILE_SIZE;
  const roomHeight = layout.rows * TILE_SIZE;
  const scale = Math.max(
    1,
    Math.min((width - 24) / roomWidth, (height - 24) / roomHeight) * zoom,
  );
  return {
    scale,
    originX: (width - roomWidth * scale) / 2,
    originY: (height - roomHeight * scale) / 2,
  };
}

function resolveSeats(agents: VisualAgent[], seats: Seat[]) {
  const saved = new Map(seats.map((seat) => [seat.agent_id, seat]));
  return agents.map((agent, index) => {
    const existing = saved.get(agent.agent_id);
    if (existing) {
      return existing;
    }
    const fallback = DEFAULT_SEATS[index % DEFAULT_SEATS.length];
    const cycle = Math.floor(index / DEFAULT_SEATS.length);
    return {
      agent_id: agent.agent_id,
      x: Math.min(19, fallback.x + cycle),
      y: Math.min(20, fallback.y + cycle),
    };
  });
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

function allAgents(cockpit: CockpitPayload | null, snapshot: OpsRoomSnapshot | null) {
  return cockpit?.agents || snapshot?.agents || [];
}

function allTasks(cockpit: CockpitPayload | null, snapshot: OpsRoomSnapshot | null) {
  return cockpit?.tasks.all || snapshot?.assignments || [];
}

function isBlockedTask(task: Assignment) {
  return task.status === "blocked" || task.awaiting_human || task.blockers.length > 0;
}

function taskSortKey(task: Assignment) {
  const rank = task.status === "working" || task.status === "assigned" ? "0" : "1";
  return `${rank}:${task.created_at || task.updated_at || ""}:${task.assignment_id}`;
}

function blockerSummary(agent: VisualAgent) {
  const blockers = agent.current_assignment?.blockers || agent.state?.blockers || [];
  if (agent.status === "awaiting_human") {
    return "Needs user input";
  }
  return blockers[0] || "Blocked";
}

function statusMark(status: string) {
  if (status === "working") {
    return "work";
  }
  if (status === "blocked") {
    return "block";
  }
  if (status === "awaiting_human") {
    return "help";
  }
  return "idle";
}

function statusColor(status: string) {
  if (status === "working") {
    return "#21745e";
  }
  if (status === "blocked") {
    return "#b64634";
  }
  if (status === "awaiting_human") {
    return "#c98d26";
  }
  return "#68766d";
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
