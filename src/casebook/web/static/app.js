"use strict";

// --- routing ---------------------------------------------------------------
// Nested under /project/{id}/ — the project browser lives at /.
function parseRoute() {
  const projectCase = location.pathname.match(/^\/project\/([^/]+)\/case\/(.+)$/);
  if (projectCase) return { mode: "case", projectId: projectCase[1], caseId: decodeURIComponent(projectCase[2]) };
  const projectScratch = location.pathname.match(/^\/project\/([^/]+)\/scratch$/);
  if (projectScratch) return { mode: "scratch", projectId: projectScratch[1], caseId: "scratch" };
  const project = location.pathname.match(/^\/project\/([^/]+)\/?$/);
  if (project) return { mode: "home", projectId: project[1], caseId: null };
  return { mode: "projects", projectId: null, caseId: null };
}
const route = parseRoute();
function isSessionPage() {
  return route.mode === "case" || route.mode === "scratch";
}

// --- URL helpers -----------------------------------------------------------
function projectUrl(projectId) {
  return `/project/${encodeURIComponent(projectId)}/`;
}
function caseUrl(caseId) {
  return `/project/${encodeURIComponent(route.projectId)}/case/${encodeURIComponent(caseId)}`;
}
function scratchUrl() {
  return `/project/${encodeURIComponent(route.projectId)}/scratch`;
}
function apiBase() {
  return `/api/projects/${encodeURIComponent(route.projectId)}`;
}

// --- state ----------------------------------------------------------------
const state = {
  ws: null,
  activeCase: route.caseId,
  agents: new Map(),
  transcripts: new Map(),
  configOptions: new Map(),
  commands: new Map(),
  usage: new Map(),
  panes: new Map(),
  sidebarFocusedAgent: null,
  paneFocusedAgent: null,
  focusRegion: "sidebar",  // "sidebar" | "panes"
  focusedCase: null,
  focusedProject: null,
  cases: [],
  projects: [],
  eventCounts: new Map(),
  hotkeyByKey: new Map(),
  widths: [],
  widthIndex: -1,
  projectName: null,  // for tab titles; set from /ui
  caseTitle: null,    // resolved title of the case page, for the tab title
  prevUsage: new Map(),  // agent_id → {input_tokens, output_tokens, total_tokens} for computing deltas
};

// Tab title: "{project} · {label} — casebook" (label omitted on the home page).
// Any part not yet loaded is skipped, so the title fills in as data arrives.
function updateTitle() {
  let label;
  if (route.mode === "scratch") label = "scratch";
  else if (route.mode === "case") label = state.caseTitle || route.caseId;
  else if (route.mode === "home") label = null;
  else return void (document.title = "Casebook");  // project browser
  const parts = [state.projectName, label].filter(Boolean);
  document.title = parts.length ? `${parts.join(" · ")} — casebook` : "casebook";
}

const el = (id) => document.getElementById(id);

marked.setOptions({ gfm: true, breaks: true });
function renderMarkdown(text) {
  return DOMPurify.sanitize(marked.parse(text || ""));
}

// --- websocket (project-scoped) -------------------------------------------
function connect() {
  if (!route.projectId) return; // project browser has no websocket
  // Scope the initial snapshot to the case being viewed: session pages only need
  // their own case's transcripts, not the whole project's. Home pages send none.
  const query = isSessionPage() ? `?case=${encodeURIComponent(route.caseId)}` : "";
  const ws = new WebSocket(`ws://${location.host}/ws/${encodeURIComponent(route.projectId)}${query}`);
  state.ws = ws;
  ws.onopen = () => setConnection(true);
  ws.onclose = () => {
    setConnection(false);
    setTimeout(connect, 1000);
  };
  ws.onmessage = (msg) => handleEvent(JSON.parse(msg.data));
}

function send(action) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(action));
  } else {
    toast("Not connected — action ignored. Retrying the connection…");
  }
}

// --- toasts ----------------------------------------------------------------
function toast(message, kind = "info") {
  const node = document.createElement("div");
  node.className = `toast ${kind}`;
  node.textContent = message;
  node.onclick = () => node.remove();
  el("toasts").appendChild(node);
  setTimeout(() => node.remove(), 6000);
}

function setConnection(connected) {
  const node = el("connection");
  node.textContent = connected ? "connected" : "disconnected";
  node.className = connected ? "connected" : "disconnected";
}

// --- event handling --------------------------------------------------------
function handleEvent(event) {
  if (event.type === "snapshot") return applySnapshot(event);
  if (event.type === "config_changed") {
    loadHotkeys();
    loadUi();
    if (isSessionPage()) loadBackends();
    toast("Config reloaded");
    return;
  }
  if (route.mode === "home") {
    if (event.type === "case_created" || event.type === "case_deleted") loadCases();
    return;
  }
  if (event.type === "case_deleted" && event.case_id === route.caseId) {
    location.href = projectUrl(route.projectId);
    return;
  }
  if (event.case_id && event.case_id !== route.caseId) return;
  switch (event.type) {
    case "agent_added":
    case "agent_updated":
      return upsertAgent(event);
    case "agent_removed":
      return removeAgent(event.agent_id);
    case "config_options":
      state.configOptions.set(event.agent_id, event.options || []);
      return renderConfigOptions(event.agent_id);
    case "commands":
      state.commands.set(event.agent_id, event.commands || []);
      return renderCommands(event.agent_id);
    case "usage": {
      const usage = state.usage.get(event.agent_id) || {};
      for (const key of ["used", "size", "input_tokens", "output_tokens", "total_tokens", "cost_amount", "cost_currency"]) {
        if (event[key] != null) usage[key] = event[key];
      }
      state.usage.set(event.agent_id, usage);
      renderUsage(event.agent_id);
      return applyToTranscript(event);
    }
    case "notice":
      if (event.agent_id && state.transcripts.has(event.agent_id)) {
        return applyToTranscript(event);
      }
      return toast(event.message);
    case "transcript_reset":
      return applyTranscriptReset(event);
    case "files_changed":
      if (event.case_id === state.activeCase) renderFiles(event.files);
      return;
    default:
      return applyToTranscript(event);
  }
}

function applySnapshot(snapshot) {
  state.agents.clear();
  state.transcripts.clear();
  state.eventCounts.clear();
  state.configOptions.clear();
  state.commands.clear();
  state.prevUsage.clear();
  for (const [agentId, pane] of state.panes) pane.root.remove();
  state.panes.clear();
  state._snapshotLoading = true;
  for (const agent of snapshot.agents) upsertAgent(agent);
  state._snapshotLoading = false;
  for (const [agentId, options] of Object.entries(snapshot.config_options || {})) {
    state.configOptions.set(agentId, options);
    renderConfigOptions(agentId);
  }
  for (const [agentId, commands] of Object.entries(snapshot.commands || {})) {
    state.commands.set(agentId, commands);
    renderCommands(agentId);
  }
  for (const [agentId, usage] of Object.entries(snapshot.usage || {})) {
    state.usage.set(agentId, usage);
    renderUsage(agentId);
  }
  for (const [agentId, events] of Object.entries(snapshot.transcripts || {})) {
    for (const event of events) applyToTranscript(event);
  }
  const ids = sessionIds();
  state.sidebarFocusedAgent = ids[0] || null;
  const pids = paneIds();
  state.paneFocusedAgent = pids[0] || null;
  // Open the navigator on arrival only when there's no live session to work in.
  state.focusRegion = state.panes.size === 0 ? "sidebar" : "panes";
  renderSessionList();
  applyFocusVisibility();
  scrollSidebarToFocused();
}

function upsertAgent(agent) {
  if (!isSessionPage() || agent.case_id !== route.caseId) return;
  const isNew = !state.agents.has(agent.agent_id) && !state._snapshotLoading;
  state.agents.set(agent.agent_id, agent);
  if (!state.transcripts.has(agent.agent_id)) state.transcripts.set(agent.agent_id, []);
  if (agent.live) {
    buildPane(agent);
    updateHead(agent.agent_id);
  } else {
    removePaneOnly(agent.agent_id);
  }
  const shouldFocus = !state._snapshotLoading && (isNew || (agent.live && agent.agent_id === state.sidebarFocusedAgent));
  if (shouldFocus) {
    state.sidebarFocusedAgent = agent.agent_id;
    // Opening a live session moves into its pane, dismissing the navigator.
    if (agent.live) {
      state.paneFocusedAgent = agent.agent_id;
      state.focusRegion = "panes";
    }
  } else {
    if (!state.sidebarFocusedAgent) state.sidebarFocusedAgent = agent.agent_id;
    if (!state.paneFocusedAgent && agent.live) state.paneFocusedAgent = agent.agent_id;
  }
  autoOpenCaseNavIfEmpty();
  renderSessionList();
  applyFocusVisibility();
  if (shouldFocus && agent.live) {
    scrollSidebarToFocused();
    const pane = state.panes.get(agent.agent_id);
    if (pane) {
      pane.root.scrollIntoView({ inline: "nearest", block: "nearest" });
      pane.input.focus();
    }
  }
}

function removePaneOnly(agentId) {
  const pane = state.panes.get(agentId);
  if (pane) pane.root.remove();
  state.panes.delete(agentId);
}

function removeAgent(agentId) {
  removePaneOnly(agentId);
  state.agents.delete(agentId);
  state.transcripts.delete(agentId);
  state.eventCounts.delete(agentId);
  state.configOptions.delete(agentId);
  state.commands.delete(agentId);
  state.usage.delete(agentId);
  state.prevUsage.delete(agentId);
  if (state.sidebarFocusedAgent === agentId) state.sidebarFocusedAgent = sessionIds()[0] || null;
  if (state.paneFocusedAgent === agentId) state.paneFocusedAgent = paneIds()[0] || null;
  autoOpenCaseNavIfEmpty();
  renderSessionList();
  applyFocusVisibility();
}

function attachUsageToLastAgentMessage(agentId, event) {
  const items = state.transcripts.get(agentId);
  if (!items) return;
  // Walk backward to find the last agent message to attach usage to.
  let target = null;
  for (let index = items.length - 1; index >= 0; index--) {
    if (items[index].kind === "message" && items[index].role === "agent") {
      target = items[index];
      break;
    }
  }
  if (!target) return;
  // Compute deltas from previous cumulative totals.
  const prev = state.prevUsage.get(agentId) || {};
  const usage = {};
  if (event.input_tokens != null) {
    usage.input_tokens = event.input_tokens;
    usage.delta_input = prev.input_tokens != null ? event.input_tokens - prev.input_tokens : null;
  }
  if (event.output_tokens != null) {
    usage.output_tokens = event.output_tokens;
    usage.delta_output = prev.output_tokens != null ? event.output_tokens - prev.output_tokens : null;
  }
  if (event.used != null) usage.used = event.used;
  if (event.size != null) usage.size = event.size;
  if (event.cost_amount != null) usage.cost_amount = event.cost_amount;
  if (event.cost_currency != null) usage.cost_currency = event.cost_currency;
  // Merge: multiple usage events per turn (streaming + post-prompt) contribute different fields.
  target.usage = Object.assign(target.usage || {}, usage);
  // Update previous cumulative totals for next delta computation.
  const updated = { ...prev };
  if (event.input_tokens != null) updated.input_tokens = event.input_tokens;
  if (event.output_tokens != null) updated.output_tokens = event.output_tokens;
  if (event.total_tokens != null) updated.total_tokens = event.total_tokens;
  state.prevUsage.set(agentId, updated);
}

// Append one replayable transcript event to an items array, coalescing streamed
// agent message chunks into one bubble and merging tool_call updates by id. This
// is the single source of truth for both live streaming (applyToTranscript) and
// bulk replay (applyRawEventToItems), so the two can't render the same transcript
// differently. Returns true if it consumed the event.
function appendItem(items, event, eventIndex) {
  if (event.type === "message") {
    const last = items[items.length - 1];
    const streaming = event.role !== "user";
    if (streaming && last && last.kind === "message" && last.role === event.role && last.streaming) {
      last.text += event.text;
    } else {
      items.push({ kind: "message", role: event.role, text: event.text, streaming, system: event.system, eventIndex, ts: event.ts });
    }
    return true;
  }
  if (event.type === "tool_call") {
    const existing = items.find((item) => item.kind === "tool" && item.id === event.tool_call_id);
    if (existing) {
      if (event.title) existing.title = event.title;
      if (event.tool_kind) existing.tool_kind = event.tool_kind;
      existing.status = event.status || existing.status;
    } else {
      items.push({ kind: "tool", id: event.tool_call_id, title: event.title, tool_kind: event.tool_kind, status: event.status });
    }
    return true;
  }
  if (event.type === "notice") {
    items.push({ kind: "notice", message: event.message, level: event.level || "info" });
    return true;
  }
  return false;
}

function applyToTranscript(event) {
  const agentId = event.agent_id;
  if (event.type === "agent_state") {
    const agent = state.agents.get(agentId);
    if (agent) {
      agent.state = event.state;
      updateHead(agentId);
    }
    return;
  }
  const items = state.transcripts.get(agentId);
  if (!items) return;

  const eventIndex = state.eventCounts.get(agentId) || 0;

  if (appendItem(items, event, eventIndex)) {
    state.eventCounts.set(agentId, eventIndex + 1);
  } else if (event.type === "permission_request") {
    items.push({ kind: "permission", request_id: event.request_id, tool_call: event.tool_call, options: event.options, resolved: false });
  } else if (event.type === "permission_resolved") {
    const perm = items.find((item) => item.kind === "permission" && item.request_id === event.request_id);
    if (perm) {
      perm.resolved = true;
      perm.chosen_option_id = event.option_id ?? null;
    }
  } else if (event.type === "usage") {
    attachUsageToLastAgentMessage(agentId, event);
  } else {
    return;
  }
  if (event.type === "message") {
    const agent = state.agents.get(agentId);
    if (agent) {
      agent.last_active = event.ts || agent.last_active;
      renderSessionList();
    }
  }
  renderTranscript(agentId);
}

function applyTranscriptReset(event) {
  const agentId = event.agent_id;
  if (!state.transcripts.has(agentId)) state.transcripts.set(agentId, []);
  const items = [];
  const rawEvents = event.transcript || [];
  state.prevUsage.delete(agentId);
  for (let index = 0; index < rawEvents.length; index++) {
    applyRawEventToItems(agentId, items, rawEvents[index], index);
  }
  state.transcripts.set(agentId, items);
  state.eventCounts.set(agentId, rawEvents.length);
  renderTranscript(agentId);
  renderSessionList();
}

function applyRawEventToItems(agentId, items, event, eventIndex) {
  if (appendItem(items, event, eventIndex)) return;
  if (event.type === "usage") {
    // During replay, attach to the last agent message in the items array built so far.
    // Reuse the same delta logic via attachUsageToLastAgentMessage — it reads from
    // state.transcripts, so temporarily set the items we're building.
    const prev = state.transcripts.get(agentId);
    state.transcripts.set(agentId, items);
    attachUsageToLastAgentMessage(agentId, event);
    if (prev !== undefined) state.transcripts.set(agentId, prev);
  }
}

// --- panes / rendering ----------------------------------------------------
function buildPane(agent) {
  if (state.panes.get(agent.agent_id)) return;
  const root = document.createElement("div");
  root.className = "agent-pane";
  root.innerHTML = `
    <div class="agent-head">
      <div class="agent-head-title"><span class="label"></span></div>
      <div class="agent-head-controls">
        <span class="state"></span>
        <div class="config-wrap">
          <button class="commands-btn" title="slash commands" hidden>/</button>
          <div class="commands-popover" hidden></div>
        </div>
        <div class="config-wrap">
          <button class="config-btn" title="session options" hidden>&#x2699;</button>
          <div class="config-popover" hidden></div>
        </div>
        <label class="allow" title="auto-allow this session's permission requests"><input type="checkbox" /> allow</label>
        <button class="rename" title="rename session">&#x270E;</button>
        <button class="autoname" title="autoname session">&#x2728;</button>
        <button class="fork" title="fork (duplicate) this session">&#x2442;</button>
        <button class="promote" title="promote into a new case" hidden>&#x2191; case</button>
        <button class="resume" hidden>Resume</button>
        <button class="close" title="close session (keep history)">&#xd7;</button>
        <button class="delete" title="delete session and history">&#x1F5D1;</button>
      </div>
      <div class="agent-usage"></div>
    </div>
    <div class="transcript"></div>
    <button class="scroll-bottom" hidden title="Jump to bottom">&#x2193;</button>
    <div class="composer">
      <div class="command-menu" hidden></div>
      <textarea rows="1" placeholder="Message this session…"></textarea>
      <button class="send">Send</button>
      <button class="cancel" hidden>Stop</button>
    </div>`;
  el("agent-panes").appendChild(root);

  const input = root.querySelector("textarea");
  const sendBtn = root.querySelector(".send");
  const cancelBtn = root.querySelector(".cancel");
  const doSend = () => {
    if (sendBtn.disabled) return;
    const text = input.value.trim();
    if (!text) return;
    send({ action: "send", agent_id: agent.agent_id, text });
    input.value = "";
    input.style.height = "auto";
  };
  const autoResize = () => {
    input.style.height = "auto";
    input.style.height = input.scrollHeight + "px";
  };
  input.addEventListener("input", () => {
    autoResize();
    updateAutocomplete(agent.agent_id);
  });
  // Clicking a menu row (mousedown, preventDefault) selects without blurring; any
  // other blur just dismisses the menu.
  input.addEventListener("blur", () => closeAutocomplete(agent.agent_id));
  sendBtn.onclick = doSend;
  input.onkeydown = (event) => {
    if (handleAutocompleteKey(agent.agent_id, event)) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      doSend();
    }
  };
  cancelBtn.onclick = () => send({ action: "cancel", agent_id: agent.agent_id });
  const allowInput = root.querySelector(".allow input");
  allowInput.onchange = () => send({ action: "set_always_allow", agent_id: agent.agent_id, value: allowInput.checked });
  root.querySelector(".rename").onclick = () => sessionRename(agent.agent_id);
  root.querySelector(".autoname").onclick = () => send({ action: "name_agent", agent_id: agent.agent_id });
  root.querySelector(".fork").onclick = () => send({ action: "fork_agent", agent_id: agent.agent_id });
  root.querySelector(".resume").onclick = () => send({ action: "resume_agent", agent_id: agent.agent_id });
  root.querySelector(".close").onclick = () => send({ action: "close_agent", agent_id: agent.agent_id });
  root.querySelector(".delete").onclick = () => sessionDelete(agent.agent_id);
  const promoteBtn = root.querySelector(".promote");
  promoteBtn.hidden = route.mode !== "scratch";
  promoteBtn.onclick = () => promoteSession(agent.agent_id);
  root.addEventListener("mousedown", () => {
    state.paneFocusedAgent = agent.agent_id;
    state.focusRegion = "panes";
    applyFocusVisibility();
  });

  const pane = {
    root,
    transcript: root.querySelector(".transcript"),
    scrollBottomBtn: root.querySelector(".scroll-bottom"),
    input,
    sendBtn,
    cancelBtn,
    composer: root.querySelector(".composer"),
    resumeBtn: root.querySelector(".resume"),
    configBtn: root.querySelector(".config-btn"),
    configPopover: root.querySelector(".config-popover"),
    commandsBtn: root.querySelector(".commands-btn"),
    commandsPopover: root.querySelector(".commands-popover"),
    commandMenu: root.querySelector(".command-menu"),
    autocomplete: [],   // currently-filtered commands (empty ⇒ menu closed)
    autocompleteIndex: 0,
    allowInput,
    usageEl: root.querySelector(".agent-usage"),
    stateEl: root.querySelector(".state"),
    labelEl: root.querySelector(".label"),
  };
  pane.configBtn.onclick = (event) => {
    event.stopPropagation();
    toggleConfigPopover(agent.agent_id);
  };
  pane.commandsBtn.onclick = (event) => {
    event.stopPropagation();
    toggleCommandsPopover(agent.agent_id);
  };
  pane.scrollBottomBtn.onclick = () => {
    pane.transcript.scrollTop = pane.transcript.scrollHeight;
  };
  pane.transcript.addEventListener("scroll", () => {
    const nearBottom = pane.transcript.scrollHeight - pane.transcript.scrollTop - pane.transcript.clientHeight < 80;
    pane.scrollBottomBtn.hidden = nearBottom;
  });
  state.panes.set(agent.agent_id, pane);
  applyFocusVisibility();
  renderTranscript(agent.agent_id);
  renderConfigOptions(agent.agent_id);
  renderCommands(agent.agent_id);
  renderUsage(agent.agent_id);
}

function fmtTokens(count) {
  if (count == null) return null;
  if (count >= 1e6) return (count / 1e6).toFixed(1) + "M";
  if (count >= 1e3) return (count / 1e3).toFixed(1) + "k";
  return String(count);
}

function renderUsage(agentId) {
  const pane = state.panes.get(agentId);
  if (!pane) return;
  const usage = state.usage.get(agentId);
  const parts = [];
  if (usage) {
    if (usage.used != null && usage.size != null) {
      const pct = usage.size ? Math.round((usage.used / usage.size) * 100) : 0;
      parts.push(`context ${fmtTokens(usage.used)}/${fmtTokens(usage.size)} (${pct}%)`);
    } else if (usage.used != null) {
      parts.push(`context ${fmtTokens(usage.used)}`);
    }
    if (usage.total_tokens != null) parts.push(`${fmtTokens(usage.total_tokens)} tokens`);
    if (usage.cost_amount != null) parts.push(`${usage.cost_currency || ""} ${usage.cost_amount.toFixed(2)}`.trim());
  }
  pane.usageEl.textContent = parts.join("  \u00b7  ");
  pane.usageEl.hidden = parts.length === 0;
}

// All config options — model included, no special case — collapse behind a
// single gear button (the header is crowded and bare dropdowns give no hint what
// they control); the popover gives each a visible label + a copy-ready config
// snippet next to its control.
function renderConfigOptions(agentId) {
  const pane = state.panes.get(agentId);
  if (!pane) return;
  const options = state.configOptions.get(agentId) || [];
  pane.configBtn.hidden = options.length === 0;
  if (options.length === 0) pane.configPopover.hidden = true;
  pane.configPopover.replaceChildren(
    ...options.map((option) => buildConfigRow(agentId, option))
  );
}

function toggleConfigPopover(agentId) {
  const pane = state.panes.get(agentId);
  if (pane) toggleExclusivePopover(pane.configPopover, pane.configBtn);
}

// Header popovers (session options, slash commands) are mutually exclusive: open
// one and every other closes, and a click outside dismisses it.
function toggleExclusivePopover(popover, anchor) {
  const show = popover.hidden;
  for (const other of state.panes.values()) {
    other.configPopover.hidden = true;
    other.commandsPopover.hidden = true;
  }
  popover.hidden = !show;
  if (show) {
    const close = (event) => {
      if (popover.contains(event.target) || anchor.contains(event.target)) return;
      popover.hidden = true;
      document.removeEventListener("mousedown", close, true);
    };
    document.addEventListener("mousedown", close, true);
  }
}

function buildConfigRow(agentId, option) {
  const row = document.createElement("div");
  row.className = "config-row";
  const top = document.createElement("div");
  top.className = "config-row-top";
  const label = document.createElement("span");
  label.className = "config-label";
  label.textContent = option.name;
  if (option.description) label.title = option.description;
  top.append(label, buildConfigControl(agentId, option));
  // The exact, copy-ready config.toml line for the current value — so a user can
  // set it in the UI, read off the key/value, and paste it as a default.
  const hint = document.createElement("code");
  hint.className = "config-hint";
  hint.textContent = configHint(option);
  row.append(top, hint);
  return row;
}

// A TOML-valid `id = value` snippet for the option's current value.
function configHint(option) {
  const value = option.type === "boolean"
    ? String(!!option.current_value)
    : JSON.stringify(option.current_value == null ? "" : String(option.current_value));
  return `${option.id} = ${value}`;
}

function buildConfigControl(agentId, option) {
  if (option.type === "boolean") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "config-control";
    input.title = configHint(option);
    input.checked = !!option.current_value;
    input.onchange = () => send({
      action: "set_config_option", agent_id: agentId,
      config_id: option.id, value: input.checked,
    });
    return input;
  }
  const select = document.createElement("select");
  select.className = "config-control";
  select.title = configHint(option);
  select.replaceChildren(...(option.options || []).map((choice) => {
    const el = document.createElement("option");
    el.value = choice.value;
    el.textContent = choice.name || choice.value;
    if (choice.description) el.title = choice.description;
    return el;
  }));
  if (option.current_value != null) select.value = option.current_value;
  select.onchange = () => send({
    action: "set_config_option", agent_id: agentId,
    config_id: option.id, value: select.value,
  });
  return select;
}

// --- slash commands -------------------------------------------------------
// Two discoverability surfaces over the same list: a browsable popover (the "/"
// header button) for when you don't know the command, and inline autocomplete
// on the composer for when you do. Invocation itself is just prompt text.

// The browse popover: every command with its description and input hint. Clicking
// a row drops it into the composer, ready to run.
function renderCommands(agentId) {
  const pane = state.panes.get(agentId);
  if (!pane) return;
  const commands = state.commands.get(agentId) || [];
  pane.commandsBtn.hidden = commands.length === 0;
  if (commands.length === 0) pane.commandsPopover.hidden = true;
  pane.commandsPopover.replaceChildren(
    ...commands.map((command) =>
      buildCommandRow(command, false, () => {
        pane.commandsPopover.hidden = true;
        applyCommand(pane, command);
      }))
  );
}

function toggleCommandsPopover(agentId) {
  const pane = state.panes.get(agentId);
  // Guard the hotkey path: no pane / no advertised commands ⇒ nothing to show.
  if (!pane || (state.commands.get(agentId) || []).length === 0) return;
  toggleExclusivePopover(pane.commandsPopover, pane.commandsBtn);
}

// One row shared by the browse popover and the autocomplete menu: `/name`, its
// description, and (if any) the argument hint.
function buildCommandRow(command, active, onPick) {
  const row = document.createElement("div");
  row.className = "command-row" + (active ? " active" : "");
  const name = document.createElement("code");
  name.className = "command-name";
  name.textContent = "/" + command.name;
  row.append(name);
  if (command.description) {
    const desc = document.createElement("span");
    desc.className = "command-desc";
    desc.textContent = command.description;
    row.append(desc);
  }
  if (command.input_hint) {
    const hint = document.createElement("span");
    hint.className = "command-hint";
    hint.textContent = command.input_hint;
    row.append(hint);
  }
  // mousedown (not click) so selection fires before the textarea's blur.
  row.addEventListener("mousedown", (event) => {
    event.preventDefault();
    onPick();
  });
  return row;
}

// Put `/name ` in the composer and focus it, ready for arguments. A trailing
// space means the input no longer matches the autocomplete trigger, so the menu
// closes on its own.
function applyCommand(pane, command) {
  pane.input.value = "/" + command.name + " ";
  pane.input.focus();
  pane.input.dispatchEvent(new Event("input"));
}

// Autocomplete fires only while the whole message is a bare `/word` — a slash at
// position 0 with no space yet (the command-name phase). That intentionally
// mirrors how backends recognize a command (leading, on its own) and avoids
// false positives on paths or dates mid-message.
function updateAutocomplete(agentId) {
  const pane = state.panes.get(agentId);
  if (!pane) return;
  const match = /^\/(\S*)$/.exec(pane.input.value);
  const commands = state.commands.get(agentId) || [];
  const filtered = match
    ? commands.filter((command) =>
        command.name.toLowerCase().startsWith(match[1].toLowerCase()))
    : [];
  pane.autocomplete = filtered;
  if (pane.autocompleteIndex >= filtered.length) pane.autocompleteIndex = 0;
  if (filtered.length === 0) {
    pane.commandMenu.hidden = true;
    return;
  }
  pane.commandMenu.replaceChildren(
    ...filtered.map((command, index) =>
      buildCommandRow(command, index === pane.autocompleteIndex,
        () => applyCommand(pane, command)))
  );
  pane.commandMenu.hidden = false;
}

function closeAutocomplete(agentId) {
  const pane = state.panes.get(agentId);
  if (!pane) return;
  pane.autocomplete = [];
  pane.autocompleteIndex = 0;
  pane.commandMenu.hidden = true;
}

// Intercept navigation keys while the menu is open; returns true if the key was
// handled (so the caller skips its own Enter-to-send).
function handleAutocompleteKey(agentId, event) {
  const pane = state.panes.get(agentId);
  if (!pane || pane.commandMenu.hidden || pane.autocomplete.length === 0) return false;
  const count = pane.autocomplete.length;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    pane.autocompleteIndex = (pane.autocompleteIndex + 1) % count;
    updateAutocomplete(agentId);
    return true;
  }
  if (event.key === "ArrowUp") {
    event.preventDefault();
    pane.autocompleteIndex = (pane.autocompleteIndex - 1 + count) % count;
    updateAutocomplete(agentId);
    return true;
  }
  if (event.key === "Enter" || event.key === "Tab") {
    event.preventDefault();
    applyCommand(pane, pane.autocomplete[pane.autocompleteIndex]);
    return true;
  }
  if (event.key === "Escape") {
    event.preventDefault();
    closeAutocomplete(agentId);
    return true;
  }
  return false;
}

function updateHead(agentId) {
  const pane = state.panes.get(agentId);
  const agent = state.agents.get(agentId);
  if (!pane || !agent) return;
  pane.labelEl.textContent = `${agent.label}  \u00b7  ${agent.backend || ""}`;
  const live = !!agent.live;
  const working = agent.state === "working" || agent.state === "starting";
  pane.stateEl.textContent = agent.state || "";
  pane.stateEl.className = "state" + (working ? " working" : "");
  pane.resumeBtn.hidden = live;
  pane.composer.hidden = !live;
  pane.allowInput.checked = !!agent.always_allow;
  pane.sendBtn.disabled = working;
  pane.cancelBtn.hidden = agent.state !== "working";
}

function renderTranscript(agentId) {
  const pane = state.panes.get(agentId);
  if (!pane) return;
  const el = pane.transcript;
  const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  const items = state.transcripts.get(agentId) || [];
  el.replaceChildren(...items.map((item) => renderItem(agentId, item)));
  if (nearBottom) {
    el.scrollTop = el.scrollHeight;
    pane.scrollBottomBtn.hidden = true;
  } else {
    pane.scrollBottomBtn.hidden = false;
  }
}

function renderItem(agentId, item) {
  if (item.kind === "message") {
    const node = document.createElement("div");
    node.className = `bubble ${item.role}` + (item.system ? " system" : "");
    const header = document.createElement("div");
    header.className = "bubble-header";
    const role = document.createElement("span");
    role.className = "role";
    role.textContent = item.system ? "system" : item.role;
    header.appendChild(role);
    if (item.ts) {
      const ts = document.createElement("span");
      ts.className = "ts";
      ts.textContent = new Date(item.ts).toLocaleTimeString(undefined, { hour12: false });
      ts.title = new Date(item.ts).toLocaleString(undefined, { hour12: false });
      header.appendChild(ts);
    }
    if (item.role === "user" && !item.system && item.eventIndex != null) {
      const actions = document.createElement("span");
      actions.className = "bubble-actions";
      const revertBtn = document.createElement("button");
      revertBtn.className = "revert";
      revertBtn.title = "revert to before this message";
      revertBtn.textContent = "\u21A9 revert";
      revertBtn.onclick = (event) => {
        event.stopPropagation();
        if (confirm("Revert session to before this message? Everything from here onward will be removed.")) {
          send({ action: "revert_agent", agent_id: agentId, event_index: item.eventIndex });
        }
      };
      const forkBtn = document.createElement("button");
      forkBtn.className = "fork-from";
      forkBtn.title = "fork session from before this message";
      forkBtn.textContent = "\u2442 fork";
      forkBtn.onclick = (event) => {
        event.stopPropagation();
        send({ action: "fork_agent", agent_id: agentId, event_index: item.eventIndex });
      };
      actions.appendChild(revertBtn);
      actions.appendChild(forkBtn);
      header.appendChild(actions);
    }
    node.appendChild(header);
    const body = document.createElement("div");
    if (item.role === "user") {
      body.className = "content";
      body.textContent = item.text;
    } else {
      body.className = "content markdown";
      body.innerHTML = renderMarkdown(item.text);
    }
    node.appendChild(body);
    if (item.usage) {
      const usageEl = document.createElement("div");
      usageEl.className = "bubble-usage";
      const parts = [];
      if (item.usage.delta_output != null) parts.push(`+${fmtTokens(item.usage.delta_output)} out`);
      else if (item.usage.output_tokens != null) parts.push(`${fmtTokens(item.usage.output_tokens)} out`);
      if (item.usage.delta_input != null) parts.push(`${fmtTokens(item.usage.delta_input)} in`);
      else if (item.usage.input_tokens != null) parts.push(`${fmtTokens(item.usage.input_tokens)} in`);
      if (item.usage.used != null && item.usage.size != null) {
        const pct = item.usage.size ? Math.round((item.usage.used / item.usage.size) * 100) : 0;
        parts.push(`ctx ${fmtTokens(item.usage.used)}/${fmtTokens(item.usage.size)} (${pct}%)`);
      } else if (item.usage.used != null) {
        parts.push(`ctx ${fmtTokens(item.usage.used)}`);
      }
      if (item.usage.cost_amount != null) {
        parts.push(`${item.usage.cost_currency || ""} ${item.usage.cost_amount.toFixed(2)}`.trim());
      }
      if (parts.length > 0) {
        usageEl.textContent = parts.join("  \u00b7  ");
        node.appendChild(usageEl);
      }
    }
    return node;
  }
  if (item.kind === "tool") {
    const node = document.createElement("div");
    node.className = "tool" + (item.status === "failed" ? " failed" : "");
    node.innerHTML = `<span class="status ${item.status || ""}">${item.status || ""}</span>` +
      `<span class="tk">${item.tool_kind || "tool"}</span> ` +
      `<span class="title"></span>`;
    node.querySelector(".title").textContent = item.title || "";
    return node;
  }
  if (item.kind === "notice") {
    const node = document.createElement("div");
    node.className = "notice" + (item.level === "error" ? " error" : "");
    node.textContent = item.message;
    return node;
  }
  if (item.kind === "permission") {
    const node = document.createElement("div");
    node.className = "permission" + (item.resolved ? " resolved" : "");
    const question = document.createElement("div");
    question.className = "q";
    const toolCall = item.tool_call || {};
    question.textContent = `Permission: ${toolCall.title || "tool call"}${toolCall.kind ? ` (${toolCall.kind})` : ""}`;
    node.appendChild(question);
    const opts = document.createElement("div");
    opts.className = "options";
    for (const option of item.options) {
      const btn = document.createElement("button");
      btn.textContent = option.name;
      if (item.resolved && item.chosen_option_id === option.option_id) btn.classList.add("chosen");
      btn.onclick = () => send({ action: "permission", request_id: item.request_id, option_id: option.option_id });
      opts.appendChild(btn);
    }
    const deny = document.createElement("button");
    deny.textContent = "Cancel";
    if (item.resolved && item.chosen_option_id === null) deny.classList.add("chosen");
    deny.onclick = () => send({ action: "permission", request_id: item.request_id, option_id: null });
    opts.appendChild(deny);
    node.appendChild(opts);
    return node;
  }
  return document.createElement("div");
}

function sessionIds() {
  return [...state.agents.keys()].sort((a, b) => {
    const ta = state.agents.get(a).last_active || "";
    const tb = state.agents.get(b).last_active || "";
    return tb < ta ? -1 : tb > ta ? 1 : 0;
  });
}

function paneIds() {
  return [...state.panes.keys()];
}

function activeAgentId() {
  return state.focusRegion === "panes" ? state.paneFocusedAgent : state.sidebarFocusedAgent;
}

function renderSessionList() {
  const list = el("session-list");
  if (!list) return;
  list.replaceChildren();
  for (const agentId of sessionIds()) {
    const agent = state.agents.get(agentId);
    const li = document.createElement("li");
    li.dataset.agentId = agentId;
    li.className = "session-item" + (agentId === state.sidebarFocusedAgent ? " focused" : "");
    const dot = `<span class="dot ${agent.state || ""}"></span>`;
    const meta = agent.live ? (agent.state || "live") : "closed";
    li.innerHTML =
      `<button class="open">${dot}<span class="name"></span>` +
      `<span class="session-meta">${meta}</span></button>` +
      `<button class="rename" title="rename">&#x270E;</button>` +
      `<button class="trash" title="delete session and history">&#x1F5D1;</button>`;
    li.querySelector(".name").textContent = agent.label;
    li.querySelector(".open").onclick = () => activateSession(agentId);
    li.querySelector(".rename").onclick = () => sessionRename(agentId);
    li.querySelector(".trash").onclick = () => sessionDelete(agentId);
    list.appendChild(li);
  }
}

function activateSession(agentId) {
  const agent = state.agents.get(agentId);
  if (!agent) return;
  state.sidebarFocusedAgent = agentId;
  if (agent.live) {
    state.paneFocusedAgent = agentId;
    state.focusRegion = "panes";
    const pane = state.panes.get(agentId);
    if (pane) {
      pane.root.scrollIntoView({ inline: "nearest", block: "nearest" });
      pane.input.focus();
    }
  } else {
    state.focusRegion = "sidebar";
    send({ action: "resume_agent", agent_id: agentId });
  }
  applyFocusVisibility();
}

function applyFocusVisibility() {
  for (const [agentId, pane] of state.panes) {
    pane.root.classList.toggle("focused", agentId === state.paneFocusedAgent);
  }
  for (const li of document.querySelectorAll("#session-list li")) {
    li.classList.toggle("focused", li.dataset.agentId === state.sidebarFocusedAgent);
  }
  const sidebar = el("sidebar");
  const panes = el("agent-panes");
  if (sidebar) sidebar.classList.toggle("active-region", state.focusRegion === "sidebar");
  if (panes) panes.classList.toggle("active-region", state.focusRegion === "panes");
  const hint = el("no-open-sessions");
  if (hint) hint.hidden = state.panes.size > 0;
  syncCaseModal();
}

// The session navigator is a centered modal on session pages, shown whenever the
// focus region is the sidebar. It stays open while there are no live panes, since
// there's nothing behind it to work in. The close control hides in that case.
function syncCaseModal() {
  const modal = el("case-modal");
  if (!modal) return;
  modal.hidden = !(isSessionPage() && state.focusRegion === "sidebar");
  el("case-modal-close").hidden = state.panes.size === 0;
}

function focusActivePaneInput() {
  const pane = state.paneFocusedAgent && state.panes.get(state.paneFocusedAgent);
  if (pane) pane.input.focus();
}

// Open the navigator. Stealing focus off the composer is deliberate: it lets the
// keyboard nav (arrows / Enter, which bail out while typing) work the instant the
// modal appears, even when it was popped open from the prompt box via Tab.
function openCaseNav() {
  state.focusRegion = "sidebar";
  if (isTyping()) document.activeElement.blur();
  applyFocusVisibility();
  scrollSidebarToFocused();
}

// Toggle the navigator: open it, or close it into the panes — unless there are
// none, in which case it stays open (there's nothing behind it to work in).
function toggleCaseNav() {
  if (state.focusRegion === "sidebar") closeCaseNav();
  else openCaseNav();
}

function closeCaseNav() {
  if (state.panes.size === 0) return;
  state.focusRegion = "panes";
  applyFocusVisibility();
  focusActivePaneInput();
}

// When the last live pane goes away there's nothing to work in, so surface the
// navigator automatically.
function autoOpenCaseNavIfEmpty() {
  if (!isSessionPage() || state._snapshotLoading) return;
  if (state.panes.size === 0 && state.focusRegion !== "sidebar") {
    openCaseNav();
  }
}

function scrollSidebarToFocused() {
  if (!state.sidebarFocusedAgent) return;
  const li = document.querySelector(`#session-list li[data-agent-id="${CSS.escape(state.sidebarFocusedAgent)}"]`);
  if (li) li.scrollIntoView({ block: "nearest" });
}

// --- keyboard focus + shortcuts -------------------------------------------
function focusSidebarSession(agentId) {
  if (!state.agents.has(agentId)) return;
  state.sidebarFocusedAgent = agentId;
  applyFocusVisibility();
  const li = document.querySelector(`#session-list li[data-agent-id="${CSS.escape(agentId)}"]`);
  if (li) li.scrollIntoView({ block: "nearest" });
}

function focusPane(agentId) {
  if (!state.panes.has(agentId)) return;
  state.paneFocusedAgent = agentId;
  applyFocusVisibility();
  const pane = state.panes.get(agentId);
  if (pane) pane.root.scrollIntoView({ inline: "nearest", block: "nearest" });
}

function focusSidebarStep(delta) {
  const ids = sessionIds();
  if (ids.length === 0) return;
  const current = ids.indexOf(state.sidebarFocusedAgent);
  const next = current < 0 ? 0 : (current + delta + ids.length) % ids.length;
  focusSidebarSession(ids[next]);
}

function focusPaneStep(delta) {
  const ids = paneIds();
  if (ids.length === 0) return;
  const current = ids.indexOf(state.paneFocusedAgent);
  const next = current < 0 ? 0 : (current + delta + ids.length) % ids.length;
  focusPane(ids[next]);
}

function focusStep(delta) {
  if (route.mode === "home") return focusCaseStep(delta);
  if (route.mode === "projects") return focusProjectStep(delta);
  if (state.focusRegion === "panes") return focusPaneStep(delta);
  return focusSidebarStep(delta);
}

function caseIds() {
  return [...document.querySelectorAll("#case-list li")].map((li) => li.dataset.caseId);
}

function focusCase(caseId) {
  state.focusedCase = caseId;
  for (const li of document.querySelectorAll("#case-list li")) {
    li.classList.toggle("focused", li.dataset.caseId === caseId);
  }
  if (caseId) renderCaseDetail(caseId);
  else {
    el("case-detail").hidden = true;
    el("placeholder").hidden = false;
  }
}

function focusCaseStep(delta) {
  const ids = caseIds();
  if (ids.length === 0) return;
  const current = ids.indexOf(state.focusedCase);
  const next = current < 0 ? 0 : (current + delta + ids.length) % ids.length;
  focusCase(ids[next]);
  const li = document.querySelector(`#case-list li[data-case-id="${CSS.escape(ids[next])}"]`);
  if (li) li.scrollIntoView({ block: "nearest" });
}

// --- project browser focus -------------------------------------------------
function projectIds() {
  return [...document.querySelectorAll("#project-list li")].map((li) => li.dataset.projectId);
}

function focusProject(projectId) {
  state.focusedProject = projectId;
  for (const li of document.querySelectorAll("#project-list li")) {
    li.classList.toggle("focused", li.dataset.projectId === projectId);
  }
  if (projectId) renderProjectDetail(projectId);
  else {
    el("project-detail").hidden = true;
    el("project-placeholder").hidden = false;
  }
}

function focusProjectStep(delta) {
  const ids = projectIds();
  if (ids.length === 0) return;
  const current = ids.indexOf(state.focusedProject);
  const next = current < 0 ? 0 : (current + delta + ids.length) % ids.length;
  focusProject(ids[next]);
  const li = document.querySelector(`#project-list li[data-project-id="${CSS.escape(ids[next])}"]`);
  if (li) li.scrollIntoView({ block: "nearest" });
}

function renderProjectDetail(projectId) {
  const project = state.projects.find((entry) => entry.id === projectId);
  if (!project) {
    el("project-detail").hidden = true;
    el("project-placeholder").hidden = false;
    return;
  }
  el("project-placeholder").hidden = true;
  el("project-detail").hidden = false;
  el("pd-name").textContent = project.name;
  el("pd-path").textContent = project.path;
  el("pd-meta").textContent = [
    `${project.cases || 0} case${project.cases === 1 ? "" : "s"}`,
    project.last_opened ? `opened ${new Date(project.last_opened).toLocaleString(undefined, { hour12: false })}` : null,
  ].filter(Boolean).join("  \u00b7  ");
  el("pd-open").href = projectUrl(projectId);
  el("pd-remove").onclick = () => removeProject(projectId, project.name);
}

async function removeProject(projectId, name) {
  if (!confirm(`Remove "${name}" from the project list?`)) return;
  const response = await fetch(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
  if (response.ok) {
    await loadProjects();
  } else {
    const data = await response.json();
    toast(data.error || "Failed to remove project", "error");
  }
}

// Session actions shared by buttons and hotkeys.
function sessionRename(agentId) {
  const current = (state.agents.get(agentId) || {}).label || "";
  const label = prompt("Session name:", current);
  if (label && label.trim()) send({ action: "rename_agent", agent_id: agentId, label: label.trim() });
}
function sessionDelete(agentId) {
  if (confirm("Delete this session and its history?")) send({ action: "delete_agent", agent_id: agentId });
}
function sessionToggleAllow(agentId) {
  const pane = state.panes.get(agentId);
  if (!pane) return;
  pane.allowInput.checked = !pane.allowInput.checked;
  send({ action: "set_always_allow", agent_id: agentId, value: pane.allowInput.checked });
}

function newSession() {
  if (state.activeCase) {
    send({ action: "add_agent", case_id: state.activeCase, backend: el("backend-select").value });
  }
}

async function newCase() {
  const title = prompt("New case title:", "Unnamed case");
  if (title === null) return;
  const summary = await fetch(`${apiBase()}/cases`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: title.trim() || "Unnamed case" }),
  }).then((response) => response.json());
  if (summary.case_id) location.href = caseUrl(summary.case_id);
}

async function deleteCase(caseId, title) {
  if (!caseId) return;
  if (!confirm(`Delete case "${title || caseId}" and all its sessions? This cannot be undone.`)) return;
  await fetch(`${apiBase()}/cases/${encodeURIComponent(caseId)}`, { method: "DELETE" });
  loadCases();
}

function deleteFocusedCase() {
  const li = document.querySelector(`#case-list li[data-case-id="${CSS.escape(state.focusedCase || "")}"]`);
  const title = li ? li.querySelector(".case-title-row").textContent : "";
  deleteCase(state.focusedCase, title);
}

function openFocused() {
  if (route.mode === "projects") {
    if (state.focusedProject) location.href = projectUrl(state.focusedProject);
    return;
  }
  if (route.mode === "home") {
    if (state.focusedCase) location.href = caseUrl(state.focusedCase);
    return;
  }
  if (state.focusRegion === "panes") {
    const pane = state.paneFocusedAgent && state.panes.get(state.paneFocusedAgent);
    if (pane) pane.input.focus();
    return;
  }
  // Sidebar region: activate the focused session — activateSession dismisses the
  // navigator into the pane (live) or resumes a closed session.
  const agentId = state.sidebarFocusedAgent;
  if (agentId && state.agents.get(agentId)) activateSession(agentId);
}

function runAction(action) {
  const agentId = activeAgentId();
  const agent = agentId && state.agents.get(agentId);
  switch (action) {
    case "new_session":
      if (isSessionPage()) return newSession();
      if (route.mode === "home") return newCase();
      return;
    case "delete_session": if (route.mode === "home") return deleteFocusedCase(); break;
    case "home":
      if (isSessionPage()) return location.href = projectUrl(route.projectId);
      if (route.mode === "home") return location.href = "/";
      return;
    case "scratch":
      if (route.projectId && route.mode !== "scratch") return location.href = scratchUrl();
      return;
    case "focus_next": return focusStep(1);
    case "focus_prev": return focusStep(-1);
    case "open_focused": return openFocused();
    case "cycle_width": return cycleWidth();
    case "cancel_turn": return cancelTurn();
    case "help": return toggleHelp();
  }
  if (!isSessionPage() || !agent) return;
  switch (action) {
    case "rename_session": return sessionRename(agentId);
    case "autoname_session": return send({ action: "name_agent", agent_id: agentId });
    case "close_session": if (agent.live) send({ action: "close_agent", agent_id: agentId }); return;
    case "delete_session": return sessionDelete(agentId);
    case "toggle_allow": return sessionToggleAllow(agentId);
    case "toggle_commands": return toggleCommandsPopover(agentId);
  }
}

function cancelTurn() {
  const agentId = activeAgentId();
  if (isSessionPage() && agentId) {
    send({ action: "cancel", agent_id: agentId });
  }
}

function isTyping() {
  const node = document.activeElement;
  if (!node) return false;
  const tag = node.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || node.isContentEditable;
}

function onKeydown(event) {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.key === "Escape") {
    if (!el("file-modal").hidden) return (el("file-modal").hidden = true);
    if (!el("hotkey-help").hidden) return toggleHelp();
    if (isSessionPage() && state.focusRegion === "sidebar" && state.panes.size > 0) return closeCaseNav();
    if (isTyping()) return document.activeElement.blur();
  }
  // Tab pops the session navigator in and out — usable even while composing.
  if (event.key === "Tab" && isSessionPage()) {
    event.preventDefault();
    return toggleCaseNav();
  }
  if (isTyping()) return;
  const action = state.hotkeyByKey.get(event.key);
  if (!action) return;
  event.preventDefault();
  runAction(action);
}

async function loadHotkeys() {
  const url = route.projectId ? `${apiBase()}/hotkeys` : "/api/hotkeys";
  const map = await fetch(url).then((response) => response.json());
  state.hotkeyByKey = new Map();
  for (const [action, keys] of Object.entries(map)) {
    for (const key of Array.isArray(keys) ? keys : [keys]) state.hotkeyByKey.set(key, action);
  }
  const rows = Object.entries(map)
    .map(([action, keys]) => {
      const shown = (Array.isArray(keys) ? keys : [keys]).join(" / ");
      return `<tr><td>${shown}</td><td>${action.replace(/_/g, " ")}</td></tr>`;
    })
    .join("");
  el("hotkey-help-body").innerHTML = `<table>${rows}</table>`;
}

function toggleHelp() {
  el("hotkey-help").hidden = !el("hotkey-help").hidden;
}

async function loadUi() {
  if (!route.projectId) return;
  const ui = await fetch(`${apiBase()}/ui`).then((response) => response.json());
  const style = document.documentElement.style;
  if (ui.session_min_width) style.setProperty("--session-min-width", ui.session_min_width);
  if (ui.session_max_width) style.setProperty("--session-max-width", ui.session_max_width);
  state.widths = Array.isArray(ui.session_widths) ? ui.session_widths : [];
  // Publish case colors as CSS variables rather than resolving them in JS: the
  // case list may already be rendered (loadCases races this fetch), and driving
  // the color through a variable lets those links restyle live once it lands.
  for (const [status, color] of Object.entries(ui.case_colors || {})) {
    style.setProperty(`--case-color-${status}`, color);
  }
  const saved = localStorage.getItem("casebook.sessionWidth");
  const width = saved || ui.session_width;
  if (width) style.setProperty("--session-width", width);
  state.widthIndex = state.widths.indexOf(width);
  state.projectName = ui.project_name || null;
  updateTitle();
}

function cycleWidth() {
  if (!state.widths || state.widths.length === 0) return;
  state.widthIndex = (state.widthIndex + 1 + state.widths.length) % state.widths.length;
  const width = state.widths[state.widthIndex];
  document.documentElement.style.setProperty("--session-width", width);
  localStorage.setItem("casebook.sessionWidth", width);
  toast(`Session width: ${width}`);
}

// --- projects (project browser) -------------------------------------------
async function loadProjects() {
  state.projects = await fetch("/api/projects").then((response) => response.json());
  const list = el("project-list");
  list.replaceChildren();
  for (const project of state.projects) {
    const li = document.createElement("li");
    li.dataset.projectId = project.id;
    li.className = "project-item";
    const link = document.createElement("a");
    link.className = "open";
    link.href = projectUrl(project.id);
    link.title = project.path;
    link.innerHTML = `<span class="project-name"></span><span class="project-path muted"></span>`;
    link.querySelector(".project-name").textContent = project.name;
    link.querySelector(".project-path").textContent = project.path;
    link.onclick = (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      focusProject(project.id);
    };
    li.appendChild(link);
    list.appendChild(li);
  }
  const ids = projectIds();
  state.focusedProject = ids.includes(state.focusedProject) ? state.focusedProject : (ids[0] || null);
  focusProject(state.focusedProject);
}

async function openProjectPath(path) {
  if (!path.trim()) return;
  const response = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: path.trim() }),
  });
  const data = await response.json();
  if (response.ok) {
    location.href = projectUrl(data.id);
  } else {
    toast(data.error || "Failed to open project", "error");
  }
}

// --- cases (project home page) --------------------------------------------
async function loadCases() {
  state.cases = await fetch(`${apiBase()}/cases`).then((response) => response.json());
  state.cases.sort((a, b) => {
    const ta = a.last_active || a.created || "";
    const tb = b.last_active || b.created || "";
    return tb < ta ? -1 : tb > ta ? 1 : 0;
  });
  const list = el("case-list");
  list.replaceChildren();
  for (const caseEntry of state.cases) {
    const li = document.createElement("li");
    li.dataset.caseId = caseEntry.case_id;
    li.className = "case-item";
    const link = document.createElement("a");
    link.className = "open";
    link.href = caseUrl(caseEntry.case_id);
    link.title = caseEntry.title;
    link.textContent = caseEntry.title;
    // A status with no configured color leaves the variable undefined, so the
    // declaration is invalid and the link inherits the default text color.
    link.style.color = `var(--case-color-${caseEntry.status})`;
    link.onclick = (event) => {
      if (event.metaKey || event.ctrlKey || event.shiftKey) return;
      event.preventDefault();
      focusCase(caseEntry.case_id);
    };
    li.appendChild(link);
    list.appendChild(li);
  }
  const ids = caseIds();
  state.focusedCase = ids.includes(state.focusedCase) ? state.focusedCase : (ids[0] || null);
  focusCase(state.focusedCase);
}

let caseDetailToken = 0;
async function renderCaseDetail(caseId) {
  const summary = state.cases.find((caseEntry) => caseEntry.case_id === caseId);
  if (!summary) {
    el("case-detail").hidden = true;
    el("placeholder").hidden = false;
    return;
  }
  el("placeholder").hidden = true;
  el("case-detail").hidden = false;
  el("cd-title").textContent = summary.title;
  el("cd-open").href = caseUrl(caseId);
  el("cd-delete").onclick = () => deleteCase(caseId, summary.title);
  el("cd-meta").textContent = [
    summary.status,
    `${summary.sessions || 0} session${summary.sessions === 1 ? "" : "s"}`,
    summary.created ? new Date(summary.created).toLocaleString(undefined, { hour12: false }) : null,
  ].filter(Boolean).join("  \u00b7  ");
  el("cd-id").textContent = caseId;
  el("cd-keywords").innerHTML = (summary.keywords || []).map((keyword) => `<span class="kw">${keyword}</span>`).join("");
  el("cd-files").innerHTML = "";
  el("cd-sessions").innerHTML = "";
  const token = ++caseDetailToken;
  const detail = await fetch(`${apiBase()}/cases/${encodeURIComponent(caseId)}`).then((response) => response.json()).catch(() => null);
  if (token !== caseDetailToken || !detail) return;
  if ((detail.files || []).length) {
    el("cd-files").innerHTML = "<h4>Files</h4>" + detail.files.map((filename) => `<span class="file">${filename}</span>`).join("");
  }
  if ((detail.agents || []).length) {
    el("cd-sessions").innerHTML = "<h4>Sessions</h4>" +
      detail.agents.map((agent) => `<div class="cd-session">${agent.label} <span class="muted">(${agent.live ? agent.state : "closed"})</span></div>`).join("");
  }
}

// --- case page -------------------------------------------------------------
async function openCaseView(caseId) {
  const detail = await fetch(`${apiBase()}/cases/${caseId}`).then((response) => response.json()).catch(() => null);
  el("case-title").textContent = (detail && detail.title) || caseId;
  state.caseTitle = (detail && detail.title) || caseId;
  updateTitle();
  renderFiles((detail && detail.files) || []);
}

function renderFiles(files) {
  const list = el("file-list");
  list.replaceChildren();
  for (const name of files) {
    const li = document.createElement("li");
    li.textContent = name;
    li.onclick = () => openFile(name);
    list.appendChild(li);
  }
}

async function openFile(name) {
  const text = await fetch(`${apiBase()}/cases/${state.activeCase}/files/${encodeURIComponent(name)}`).then((response) => response.text());
  el("file-modal-name").textContent = name;
  const body = el("file-modal-content");
  if (/\.(md|markdown)$/i.test(name)) {
    body.className = "filebody markdown";
    body.innerHTML = renderMarkdown(text);
  } else {
    body.className = "filebody plain";
    body.textContent = text;
  }
  el("file-modal").hidden = false;
}

// --- backends --------------------------------------------------------------
async function loadBackends() {
  const info = await fetch(`${apiBase()}/backends`).then((response) => response.json());
  const select = el("backend-select");
  select.replaceChildren(...info.backends.map((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    return option;
  }));
  if (info.default) select.value = info.default;
  select.hidden = info.backends.length <= 1;
}

async function promoteSession(agentId) {
  const title = prompt("Promote this session into a new case — title:", "Unnamed case");
  if (title === null) return;
  const response = await fetch(`${apiBase()}/promote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agent_id: agentId, title: title.trim() || "Unnamed case" }),
  }).then((response) => response.json()).catch(() => null);
  if (response && response.case_id) location.href = caseUrl(response.case_id);
}

function applyRoute() {
  const isProjects = route.mode === "projects";
  const isHome = route.mode === "home";
  const isScratch = route.mode === "scratch";
  const isCase = route.mode === "case";
  const hasProject = !!route.projectId;

  document.body.classList.toggle("projects", isProjects);
  document.body.classList.toggle("home", isHome);
  document.body.classList.toggle("case", isCase || isScratch);

  // Sidebar sections. The session navigator lives in #case-modal (a centered
  // modal) rather than the inline sidebar, so the sidebar itself only serves the
  // project and case browsers — hide it entirely on session pages.
  el("sidebar").hidden = isCase || isScratch;
  el("projects-nav").hidden = !isProjects;
  el("cases-nav").hidden = !isHome;
  el("case-nav").hidden = !(isCase || isScratch);
  el("files-section").hidden = !route.caseId || isScratch;

  // Main sections
  el("project-detail").hidden = true;
  el("project-placeholder").hidden = !isProjects;
  el("agents").hidden = !(isCase || isScratch);
  el("case-detail").hidden = true;
  el("placeholder").hidden = !isHome;

  // Top bar
  el("back-projects").hidden = !hasProject;
  el("back-cases").hidden = !(isCase || isScratch);
  if (hasProject) {
    el("back-cases").href = projectUrl(route.projectId);
  }
  el("case-title").hidden = isProjects || isHome;

  // Scratch link
  const scratchLink = el("scratch-link");
  if (scratchLink && hasProject) {
    scratchLink.href = scratchUrl();
  }

  // Connection indicator: hide on project browser (no websocket)
  el("connection").hidden = isProjects;
  el("reload-config").hidden = isProjects;
}

// --- wiring ---------------------------------------------------------------
el("add-agent").onclick = newSession;
el("new-case").onclick = newCase;
el("file-modal-close").onclick = () => (el("file-modal").hidden = true);
el("file-modal").onclick = (event) => {
  if (event.target.id === "file-modal") el("file-modal").hidden = true;
};
el("case-modal-close").onclick = closeCaseNav;
el("case-modal").onclick = (event) => {
  if (event.target.id === "case-modal") closeCaseNav();
};
el("hotkey-help-close").onclick = toggleHelp;
el("hotkey-hint").onclick = toggleHelp;
el("reload-config").onclick = async () => {
  await fetch("/api/reload", { method: "POST" });
};

el("open-project").onclick = () => openProjectPath(el("project-path-input").value);
el("project-path-input").onkeydown = (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    openProjectPath(el("project-path-input").value);
  }
};
document.addEventListener("keydown", onKeydown);

async function loadVersion() {
  try {
    const { version } = await fetch("/api/version").then((response) => response.json());
    if (version) el("version").textContent = version;
  } catch {
    // The version indicator is cosmetic; ignore fetch failures.
  }
}

applyRoute();
loadVersion();
if (route.mode === "projects") {
  loadHotkeys();
  loadProjects();
  const pathParam = new URLSearchParams(location.search).get("path");
  if (pathParam) openProjectPath(pathParam);
} else if (route.mode === "home") {
  loadHotkeys();
  loadUi();
  connect();
  loadCases();
} else if (route.mode === "scratch") {
  loadHotkeys();
  loadUi();
  connect();
  loadBackends();
  el("case-title").textContent = "Scratch sessions";
  updateTitle();
} else {
  loadHotkeys();
  loadUi();
  connect();
  loadBackends();
  openCaseView(route.caseId);
}
