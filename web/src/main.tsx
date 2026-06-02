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

type OpsRoomSnapshot = {
  version: number;
  generated_at: string;
  mission: Mission | null;
  latest_reasoning?: { decision_summary?: string; cycle_id?: string } | null;
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
          onModelsChange={setModels}
          onSettingsChange={setSettings}
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
          can={can}
          api={api}
          onSelectAgent={setSelectedAgentId}
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
  can: (permission: string) => boolean;
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  onSelectAgent: (agentId: string, panel?: "tasks" | "chat" | "goals") => void;
  onSelectedAgentModelChange: (route: ModelRoute) => void;
  onOrchestratorModelChange: (route: ModelRoute) => void;
  onModelsChange: (models: ModelInventory) => void;
  onSettingsChange: (settings: SettingsPayload) => void;
  onRefresh: () => Promise<void>;
  setStatus: (status: string) => void;
  onOpenTaskDialog: () => void;
}) {
  const selectedAgent = cockpit?.agents.find((agent) => agent.agent_id === selectedAgentId) || null;
  return (
    <section className="cockpit">
      <div className="widget-grid">
        <Widget title="Current Alerts">
          <AlertList
            alerts={cockpit?.alerts || []}
            canClear={can("orchestrator:write")}
            api={api}
            onDone={onRefresh}
            setStatus={setStatus}
          />
        </Widget>
        <Widget title="Uptime / Service Health" className="health-combo">
          <div className="two-column-widget">
            <div>
              <div className="stat-large">{formatDuration(cockpit?.uptime_seconds || 0)}</div>
              <p className="muted">Started {formatTime(cockpit?.started_at)}</p>
            </div>
            <HealthList checks={cockpit?.datastores || []} />
          </div>
        </Widget>
        <Widget title="Models Available">
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
        <Widget title="Tasks" wide>
          <TaskBoard
            tasks={cockpit?.tasks || null}
            agents={cockpit?.agents || []}
            canCreate={can("task:write")}
            onOpenTaskDialog={onOpenTaskDialog}
            onSelectAgent={(agentId) => onSelectAgent(agentId, "tasks")}
          />
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
        <Widget title="Token Usage / Spend">
          <UsageSummary usage={cockpit?.usage || null} />
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
  className = "",
  children,
}: {
  title: string;
  wide?: boolean;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <article className={`widget ${wide ? "wide" : ""} ${className}`}>
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
  const value = route && route.provider !== "fake"
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
          <article key={item.message_id} className="message-row">
            <span>
              {item.sender} -&gt; {item.recipient} / {formatTime(item.created_at)}
            </span>
            {item.sender === "orchestrator" ? (
              <div
                className="message-markdown"
                dangerouslySetInnerHTML={{
                  __html: responseHtmlById[item.message_id] || renderMarkdownHtml(item.content),
                }}
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
      setStatus("Chat complete");
      await onDone();
    } finally {
      setPending(false);
    }
  }

  return (
    <section className="panel-body chat-panel">
      <p className="model-route-note">Agent model: {routeLabel}</p>
      <div className="chat-feed" ref={feedRef}>
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
          const isActiveOrchestrator = isOrchestrator && Boolean(snapshot?.latest_reasoning);
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
                    {isOrchestrator && snapshot?.latest_reasoning?.decision_summary
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

function modelRoutePayload(route: ModelRoute | null): Record<string, string> {
  if (!route || route.provider === "fake") {
    return {};
  }
  return {
    provider: route.provider,
    model: route.model,
    ...(route.base_url ? { base_url: route.base_url } : {}),
  };
}

function visibleModelOptions(inventory: ModelInventory | null) {
  return (inventory?.options || []).filter((option) => option.provider !== "fake");
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
