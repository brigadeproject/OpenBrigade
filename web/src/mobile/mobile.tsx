import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./mobile.css";

// ============================================================
// OpenBrigade mobile companion — chat/guidance + approvals +
// status glance. Self-contained second entry point; shares the
// backend API and the brigade_token localStorage key with the
// desktop UI, nothing else.
// ============================================================

const TERMINAL_TASK_STATUSES = ["complete", "failed", "abandoned", "superseded"];
const ORCHESTRATOR = "__orchestrator__";

type ApiOptions = RequestInit & { json?: unknown };

class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

type AuthMe = {
  ok: boolean;
  method: string;
  user: { username: string; role: string } | null;
  permissions: string[];
};

type Assignment = {
  assignment_id: string;
  assignment: string;
  assigned_to: string;
  status: string;
  archived?: boolean;
};

type VisualAgent = {
  agent_id: string;
  display_name: string;
  role: string;
  status: string;
  activity: string;
};

type AlertRecord = {
  message: string;
  count: number;
  last_seen?: string | null;
};

type CockpitPayload = {
  agents: VisualAgent[];
  tasks: { all: Assignment[] };
  counts: {
    agents: number;
    active_tasks: number;
    queued_tasks: number;
    blocked_tasks: number;
    alerts: number;
  };
  alerts: AlertRecord[];
  orchestrator: { agent_id: string; display_name: string; channel: string };
};

type Message = {
  message_id: string;
  channel: string;
  sender: string;
  recipient: string;
  content: string;
  created_at: string;
};

type ChatPayload = {
  messages: Message[];
};

type ProposalRecord = {
  proposal_id: string;
  kind: string;
  status: string;
  title: string;
  agent_id?: string | null;
  details: Record<string, unknown>;
  created_at?: string;
};

type ConnectorApprovalRecord = {
  provider: string;
  external_user_id: string;
  username?: string | null;
  status: string;
  reason?: string | null;
  redacted_metadata?: Record<string, unknown>;
  created_at?: string;
};

type MobileTab = "chat" | "approvals" | "status";

function initialTab(): MobileTab {
  const requested = new URLSearchParams(window.location.search).get("tab");
  return requested === "approvals" || requested === "status" ? requested : "chat";
}

function randomId(prefix: string) {
  return `${prefix}:${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function errorMessage(error: unknown) {
  if (error instanceof Error) {
    return error.message || "Request failed";
  }
  return String(error);
}

function formatTime(value?: string | null) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function statusTone(status: string) {
  if (["working", "assigned"].includes(status)) {
    return "ok";
  }
  if (["blocked", "awaiting_human", "escalated", "failed"].includes(status)) {
    return "bad";
  }
  return "";
}

function App() {
  const [token, setToken] = useState(localStorage.getItem("brigade_token") || "");
  const [tokenDraft, setTokenDraft] = useState("");
  const [auth, setAuth] = useState<AuthMe | null>(null);
  const [authFailed, setAuthFailed] = useState(false);
  const [tab, setTab] = useState<MobileTab>(() => initialTab());
  const [cockpit, setCockpit] = useState<CockpitPayload | null>(null);
  const [proposals, setProposals] = useState<ProposalRecord[]>([]);
  const [connectorApprovals, setConnectorApprovals] = useState<ConnectorApprovalRecord[]>([]);
  const [status, setStatus] = useState("");
  const [statusIsError, setStatusIsError] = useState(false);

  const api = useCallback(
    async <T,>(path: string, options: ApiOptions = {}): Promise<T> => {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (token) {
        headers.Authorization = `Bearer ${token}`;
      }
      const response = await fetch(path, {
        ...options,
        headers: { ...headers, ...((options.headers as Record<string, string>) || {}) },
        body: options.json === undefined ? options.body : JSON.stringify(options.json),
      });
      if (!response.ok) {
        let detail = response.statusText;
        try {
          const payload = await response.json();
          detail = String(payload.detail ?? detail);
        } catch {
          /* keep statusText */
        }
        throw new ApiError(response.status, detail);
      }
      return response.json() as Promise<T>;
    },
    [token],
  );

  const permissions = useMemo(() => new Set(auth?.permissions || []), [auth]);
  const can = useCallback(
    (permission: string) => permissions.has("admin") || permissions.has(permission),
    [permissions],
  );

  const note = useCallback((text: string, isError = false) => {
    setStatus(text);
    setStatusIsError(isError);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const next = await api<CockpitPayload>("/api/cockpit");
      setCockpit(next);
      setAuthFailed(false);
    } catch (error) {
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        setAuthFailed(true);
      } else {
        note(errorMessage(error), true);
      }
    }
    if (can("proposal:read")) {
      try {
        setProposals(await api<ProposalRecord[]>("/api/proposals"));
      } catch {
        /* transient */
      }
    }
    if (permissions.has("admin")) {
      try {
        setConnectorApprovals(
          await api<ConnectorApprovalRecord[]>("/api/connectors/approvals"),
        );
      } catch {
        /* transient */
      }
    }
  }, [api, can, permissions, note]);

  useEffect(() => {
    let cancelled = false;
    async function loadAuth() {
      try {
        const me = await api<AuthMe>("/api/auth/me");
        if (!cancelled) {
          setAuth(me);
          setAuthFailed(false);
        }
      } catch (error) {
        if (!cancelled && error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          setAuthFailed(true);
        }
      }
    }
    loadAuth();
    return () => {
      cancelled = true;
    };
  }, [api]);

  useEffect(() => {
    if (!auth?.ok) {
      return;
    }
    refresh();
    const timer = window.setInterval(refresh, 20000);
    return () => window.clearInterval(timer);
  }, [auth?.ok, refresh]);

  function saveToken() {
    const next = tokenDraft.trim();
    if (!next) {
      return;
    }
    localStorage.setItem("brigade_token", next);
    setToken(next);
    setTokenDraft("");
    setAuth(null);
    setAuthFailed(false);
  }

  const pendingApprovals =
    proposals.filter((item) => item.status === "proposed").length +
    connectorApprovals.filter((item) => item.status === "pending").length;

  const needsLogin = authFailed || (!auth?.ok && !cockpit);

  return (
    <div className="m-app">
      <header className="m-header">
        <div className="m-header-logo">
          <span />
        </div>
        <div className="m-header-title">OpenBrigade</div>
        <div className="m-header-sub">
          <span>{auth?.user?.username || "guest"}</span>
          <span className={`m-status-dot ${cockpit ? "ok" : authFailed ? "bad" : ""}`} />
        </div>
      </header>
      {status && (
        <div
          className={`m-statusline ${statusIsError ? "error" : ""}`}
          onClick={() => note("")}
        >
          {status}
        </div>
      )}
      <main className="m-main">
        {needsLogin ? (
          <LoginScreen
            tokenDraft={tokenDraft}
            setTokenDraft={setTokenDraft}
            onSave={saveToken}
            hasToken={Boolean(token)}
          />
        ) : (
          <>
            {tab === "chat" && (
              <ChatTab api={api} cockpit={cockpit} canChat={can("chat:write")} note={note} />
            )}
            {tab === "approvals" && (
              <ApprovalsTab
                api={api}
                proposals={proposals}
                connectorApprovals={connectorApprovals}
                canDecide={can("proposal:write")}
                isAdmin={permissions.has("admin")}
                onDone={refresh}
                note={note}
              />
            )}
            {tab === "status" && <StatusTab cockpit={cockpit} />}
          </>
        )}
      </main>
      <nav className="m-nav">
        <button className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>
          <span className="m-nav-icon">💬</span>
          Chat
        </button>
        <button
          className={tab === "approvals" ? "active" : ""}
          onClick={() => setTab("approvals")}
        >
          <span className="m-nav-icon">✅</span>
          Approvals
          {pendingApprovals > 0 && <span className="m-nav-badge">{pendingApprovals}</span>}
        </button>
        <button className={tab === "status" ? "active" : ""} onClick={() => setTab("status")}>
          <span className="m-nav-icon">📡</span>
          Status
        </button>
      </nav>
    </div>
  );
}

function LoginScreen({
  tokenDraft,
  setTokenDraft,
  onSave,
  hasToken,
}: {
  tokenDraft: string;
  setTokenDraft: (value: string) => void;
  onSave: () => void;
  hasToken: boolean;
}) {
  return (
    <div className="m-login">
      <h2>Sign in</h2>
      <p>
        {hasToken
          ? "The saved token was rejected. Paste a fresh access token."
          : "Paste an access token to connect to the brigade gateway."}
      </p>
      <input
        className="m-input"
        type="password"
        value={tokenDraft}
        placeholder="Access token"
        autoComplete="off"
        onChange={(event) => setTokenDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            onSave();
          }
        }}
      />
      <button className="m-btn primary" disabled={!tokenDraft.trim()} onClick={onSave}>
        Connect
      </button>
      <a className="m-full-link" href="/">
        Open full interface →
      </a>
    </div>
  );
}

// ============================================================
// CHAT
// ============================================================

function ChatTab({
  api,
  cockpit,
  canChat,
  note,
}: {
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  cockpit: CockpitPayload | null;
  canChat: boolean;
  note: (text: string, isError?: boolean) => void;
}) {
  const [target, setTarget] = useState(ORCHESTRATOR);
  const [messages, setMessages] = useState<Message[]>([]);
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);
  const [guidanceMode, setGuidanceMode] = useState(false);
  const [guidanceTaskId, setGuidanceTaskId] = useState("");
  const feedRef = useRef<HTMLDivElement | null>(null);

  const agents = cockpit?.agents || [];
  const liveTasks = useMemo(
    () =>
      (cockpit?.tasks?.all || []).filter(
        (item) =>
          !item.archived &&
          !TERMINAL_TASK_STATUSES.includes(item.status) &&
          (target === ORCHESTRATOR || item.assigned_to === target),
      ),
    [cockpit, target],
  );

  const loadMessages = useCallback(async () => {
    try {
      const payload = await api<ChatPayload>("/api/chat/messages");
      const all = payload.messages || [];
      setMessages(
        target === ORCHESTRATOR
          ? all.filter((item) => item.channel === "orchestrator")
          : all.filter((item) => item.sender === target || item.recipient === target),
      );
    } catch (error) {
      note(errorMessage(error), true);
    }
  }, [api, target, note]);

  useEffect(() => {
    loadMessages();
    const timer = window.setInterval(loadMessages, 10000);
    return () => window.clearInterval(timer);
  }, [loadMessages]);

  useEffect(() => {
    setGuidanceMode(false);
    setGuidanceTaskId("");
  }, [target]);

  useEffect(() => {
    const feed = feedRef.current;
    if (feed) {
      feed.scrollTop = feed.scrollHeight;
    }
  }, [messages.length, pending]);

  async function send() {
    const content = draft.trim();
    if (!content || pending) {
      return;
    }
    setPending(true);
    try {
      if (guidanceMode) {
        const taskId = guidanceTaskId || liveTasks[0]?.assignment_id || "";
        if (!taskId) {
          note("No live task to attach guidance to", true);
          return;
        }
        await api(`/api/tasks/${encodeURIComponent(taskId)}/guidance`, {
          method: "POST",
          json: { message: content },
        });
        note(`Guidance attached to ${taskId.slice(0, 8)}`);
      } else if (target === ORCHESTRATOR) {
        note("Waiting for the orchestrator…");
        await api("/api/chat/ask-orchestrator", {
          method: "POST",
          json: {
            channel: "orchestrator",
            content,
            idempotency_key: randomId("mobile-orchestrator"),
          },
        });
        note("Orchestrator replied");
      } else {
        note("Waiting for the agent…");
        await api("/api/chat/ask-agent", {
          method: "POST",
          json: {
            agent_id: target,
            content,
            idempotency_key: randomId("mobile-chat"),
          },
        });
        note("Reply received");
      }
      setDraft("");
      setGuidanceMode(false);
      await loadMessages();
    } catch (error) {
      note(errorMessage(error), true);
    } finally {
      setPending(false);
    }
  }

  const meNames = useMemo(() => {
    const names = new Set(["web", "operator", "user", "human"]);
    return names;
  }, []);

  function isMine(message: Message) {
    if (target === ORCHESTRATOR) {
      return message.sender !== "orchestrator";
    }
    return message.sender !== target && (message.recipient === target || meNames.has(message.sender));
  }

  return (
    <div className="m-chat">
      <div className="m-chat-target">
        <select
          className="m-select"
          value={target}
          onChange={(event) => setTarget(event.target.value)}
        >
          <option value={ORCHESTRATOR}>Orchestrator</option>
          {agents.map((agent) => (
            <option key={agent.agent_id} value={agent.agent_id}>
              {agent.display_name} ({agent.status})
            </option>
          ))}
        </select>
      </div>
      <div className="m-chat-feed" ref={feedRef}>
        {messages.length === 0 && <p className="m-muted">No messages yet.</p>}
        {messages.slice(-80).map((item) => (
          <div key={item.message_id} className={`m-msg ${isMine(item) ? "me" : "them"}`}>
            {item.content}
            <span className="m-msg-meta">
              {item.sender} · {formatTime(item.created_at)}
            </span>
          </div>
        ))}
        {pending && <div className="m-pending">waiting for reply…</div>}
      </div>
      <div className="m-guidance-bar">
        <label className="m-guidance-toggle">
          <input
            type="checkbox"
            checked={guidanceMode}
            disabled={!canChat || liveTasks.length === 0}
            onChange={(event) => setGuidanceMode(event.target.checked)}
          />
          <span className={guidanceMode ? "on" : ""}>
            {guidanceMode
              ? "Guidance mode — message steers a task"
              : liveTasks.length > 0
                ? "Send as task guidance"
                : "No live tasks for guidance"}
          </span>
        </label>
        {guidanceMode && (
          <select
            className="m-select"
            value={guidanceTaskId || liveTasks[0]?.assignment_id || ""}
            onChange={(event) => setGuidanceTaskId(event.target.value)}
          >
            {liveTasks.map((task) => (
              <option key={task.assignment_id} value={task.assignment_id}>
                [{task.status}] {task.assignment.slice(0, 60)}
              </option>
            ))}
          </select>
        )}
      </div>
      <div className="m-compose">
        <textarea
          className="m-textarea"
          rows={1}
          value={draft}
          disabled={!canChat || pending}
          placeholder={canChat ? "Message…" : "Token lacks chat permission"}
          onChange={(event) => setDraft(event.target.value)}
        />
        <button
          className="m-btn primary"
          disabled={!canChat || pending || !draft.trim()}
          onClick={send}
        >
          {pending ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}

// ============================================================
// APPROVALS
// ============================================================

function ApprovalsTab({
  api,
  proposals,
  connectorApprovals,
  canDecide,
  isAdmin,
  onDone,
  note,
}: {
  api: <T>(path: string, options?: ApiOptions) => Promise<T>;
  proposals: ProposalRecord[];
  connectorApprovals: ConnectorApprovalRecord[];
  canDecide: boolean;
  isAdmin: boolean;
  onDone: () => Promise<void>;
  note: (text: string, isError?: boolean) => void;
}) {
  const [busyKey, setBusyKey] = useState("");

  const pendingProposals = proposals
    .filter((item) => item.status === "proposed")
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
  const decidedProposals = proposals
    .filter((item) => item.status !== "proposed")
    .sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")))
    .slice(0, 10);
  const pendingConnectors = connectorApprovals.filter((item) => item.status === "pending");

  async function decideProposal(record: ProposalRecord, decision: "approved" | "rejected") {
    setBusyKey(record.proposal_id);
    try {
      await api(`/api/proposals/${encodeURIComponent(record.proposal_id)}/decision`, {
        method: "POST",
        json: { decision },
      });
      note(`${decision === "approved" ? "Approved" : "Rejected"}: ${record.title}`);
      await onDone();
    } catch (error) {
      note(errorMessage(error), true);
    } finally {
      setBusyKey("");
    }
  }

  async function decideConnector(
    record: ConnectorApprovalRecord,
    decision: "approved" | "rejected",
  ) {
    const key = `${record.provider}:${record.external_user_id}`;
    setBusyKey(key);
    try {
      await api("/api/connectors/approvals/decision", {
        method: "POST",
        json: {
          provider: record.provider,
          external_user_id: record.external_user_id,
          decision,
        },
      });
      note(`${decision === "approved" ? "Approved" : "Rejected"} connector ${record.username || record.external_user_id}`);
      await onDone();
    } catch (error) {
      note(errorMessage(error), true);
    } finally {
      setBusyKey("");
    }
  }

  return (
    <div className="m-scroll m-pad">
      {pendingProposals.length === 0 && pendingConnectors.length === 0 && (
        <p className="m-muted">Nothing waiting for approval.</p>
      )}
      {pendingConnectors.length > 0 && isAdmin && (
        <>
          <h3 className="m-section-title">Connector access</h3>
          {pendingConnectors.map((record) => {
            const key = `${record.provider}:${record.external_user_id}`;
            return (
              <div key={key} className="m-card">
                <p className="m-card-title">
                  {record.username || record.external_user_id}
                </p>
                <div className="m-card-meta">
                  <span className="m-badge accent">{record.provider}</span>
                  {record.reason && <span>{record.reason}</span>}
                </div>
                <div className="m-btn-row">
                  <button
                    className="m-btn primary"
                    disabled={busyKey === key}
                    onClick={() => decideConnector(record, "approved")}
                  >
                    Approve
                  </button>
                  <button
                    className="m-btn danger"
                    disabled={busyKey === key}
                    onClick={() => decideConnector(record, "rejected")}
                  >
                    Reject
                  </button>
                </div>
              </div>
            );
          })}
        </>
      )}
      {pendingProposals.length > 0 && (
        <>
          <h3 className="m-section-title">Proposals</h3>
          {pendingProposals.map((record) => (
            <div key={record.proposal_id} className="m-card">
              <p className="m-card-title">{record.title}</p>
              <div className="m-card-meta">
                <span className="m-badge accent">{record.kind}</span>
                {record.agent_id && <span>{record.agent_id}</span>}
                {record.created_at && <span>{formatTime(record.created_at)}</span>}
              </div>
              <details className="m-details">
                <summary>Details</summary>
                <pre>{JSON.stringify(record.details, null, 2)}</pre>
              </details>
              <div className="m-btn-row">
                <button
                  className="m-btn primary"
                  disabled={!canDecide || busyKey === record.proposal_id}
                  onClick={() => decideProposal(record, "approved")}
                >
                  Approve
                </button>
                <button
                  className="m-btn danger"
                  disabled={!canDecide || busyKey === record.proposal_id}
                  onClick={() => decideProposal(record, "rejected")}
                >
                  Reject
                </button>
              </div>
            </div>
          ))}
        </>
      )}
      {decidedProposals.length > 0 && (
        <>
          <h3 className="m-section-title">Recently decided</h3>
          {decidedProposals.map((record) => (
            <div key={record.proposal_id} className="m-card">
              <p className="m-card-title">{record.title}</p>
              <div className="m-card-meta">
                <span
                  className={`m-badge ${record.status === "approved" || record.status === "implemented" ? "ok" : record.status === "rejected" ? "bad" : ""}`}
                >
                  {record.status}
                </span>
                <span className="m-badge accent">{record.kind}</span>
                {record.agent_id && <span>{record.agent_id}</span>}
              </div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

// ============================================================
// STATUS
// ============================================================

function StatusTab({ cockpit }: { cockpit: CockpitPayload | null }) {
  if (!cockpit) {
    return (
      <div className="m-scroll m-pad">
        <p className="m-muted">Loading…</p>
      </div>
    );
  }
  const counts = cockpit.counts;
  return (
    <div className="m-scroll m-pad">
      <h3 className="m-section-title">Tasks</h3>
      <div className="m-counts">
        <div className="m-count-tile">
          <div className="m-count-num">{counts.active_tasks}</div>
          <div className="m-count-label">active</div>
        </div>
        <div className="m-count-tile">
          <div className="m-count-num">{counts.queued_tasks}</div>
          <div className="m-count-label">queued</div>
        </div>
        <div className="m-count-tile">
          <div className="m-count-num">{counts.blocked_tasks}</div>
          <div className="m-count-label">blocked</div>
        </div>
      </div>
      {cockpit.alerts.length > 0 && (
        <>
          <h3 className="m-section-title">Alerts</h3>
          {cockpit.alerts.map((alert) => (
            <div key={alert.message} className="m-alert">
              {alert.message}
              <span className="m-msg-meta">
                {alert.count > 1 ? `×${alert.count} · ` : ""}
                {alert.last_seen ? `last ${formatTime(alert.last_seen)}` : ""}
              </span>
            </div>
          ))}
        </>
      )}
      <h3 className="m-section-title">Agents</h3>
      {cockpit.agents.map((agent) => (
        <div key={agent.agent_id} className="m-agent-row">
          <span className={`m-status-dot ${statusTone(agent.status)}`} />
          <span className="m-agent-name">{agent.display_name}</span>
          <span className="m-agent-activity">{agent.activity || agent.status}</span>
          <span className={`m-badge ${statusTone(agent.status)}`}>{agent.status}</span>
        </div>
      ))}
      <a className="m-full-link" href="/">
        Open full interface →
      </a>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
