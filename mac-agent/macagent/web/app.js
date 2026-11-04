// Relay phone app. No framework: state → render(), one delegated click handler,
// one WebSocket for live updates and screen frames.
//
// Everything that came from the server is passed through esc() before it is
// put into HTML: summaries quote emails, web pages and file names, and any of
// those could contain markup.

const $app = document.getElementById("app");
const TOKEN_KEY = "relay_token";
const S = {
  token: localStorage.getItem(TOKEN_KEY),
  state: null, view: "chat", taskId: null, task: null, events: [],
  chat: [], routerBusy: false, chatDraft: "", triggers: null,
  seg: "tasks", incoming: [], wd: null, wdDraft: "", policies: null, rules: null, machineCode: null, policyDraft: "",
  screenMachine: null,
  trig: { kind: "folder", name: "", instruction: "", path: "~/Downloads", pattern: "*", query: "", minutes: 15, battery: "", disk: "" },
  ws: null, connected: false, retry: 0,
  frame: null, frameErr: "", mode: "click", mods: new Set(), lastTap: null, takeover: false,
  draft: "", followup: "", sched: false, runAt: "", repeat: "",
  devices: null, memories: null, inviteCode: null, toast: "", lightbox: null,
};

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ago = (t) => {
  if (!t) return "";
  const s = Date.now() / 1000 - t;
  if (s < 0) return "in " + fmtDur(-s);
  return s < 45 ? "just now" : fmtDur(s) + " ago";
};
const fmtDur = (s) => s < 3600 ? `${Math.round(s / 60)}m` : s < 86400 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`;
const money = (n) => `$${(n || 0).toFixed(n >= 1 ? 2 : 3)}`;
const ACTIVE = new Set(["running", "waiting", "paused", "queued"]);

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${S.token}` },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (r.status === 401 && S.token) { signOut(); throw new Error("This phone is no longer paired."); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(data.detail || r.statusText); e.status = r.status; throw e; }
  return data;
}

function toast(msg) {
  S.toast = msg; render();
  clearTimeout(toast.t); toast.t = setTimeout(() => { S.toast = ""; render(); }, 3200);
}

// ---------------------------------------------------------------- connection
function connect() {
  if (!S.token) return;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/ws?token=${encodeURIComponent(S.token)}`);
  S.ws = ws;
  ws.onopen = () => { S.connected = true; S.retry = 0; if (S.view === "screen") screenOn(true); render(); };
  ws.onclose = (e) => {
    S.connected = false; render();
    if (e.code === 4401) return signOut();
    setTimeout(connect, Math.min(15000, 800 * 2 ** S.retry++));
  };
  ws.onmessage = (e) => onMessage(JSON.parse(e.data));
}

function onMessage(m) {
  if (m.type === "hello") { S.state = m.state; S.routerBusy = !!m.state.router?.busy; render(); loadChat(); return; }
  if (!S.state) return;
  if (m.type === "frame") {
    S.frame = m; S.frameErr = "";
    const img = document.getElementById("screen-img");
    if (img) img.src = `data:${m.media_type};base64,${m.data}`;
    if (m.busy !== S.state.screen_busy) { S.state.screen_busy = m.busy; render(); } else if (!img) render();
    return;
  }
  if (m.type === "frame_error") { S.frameErr = m.error; render(); return; }
  if (m.type === "chat") {
    if (!S.chat.some((x) => x.id === m.message.id)) S.chat.push(m.message);
    if (m.message.role === "relay") S.routerBusy = false;
    const atBottom = innerHeight + scrollY > document.body.scrollHeight - 200;
    render();
    if (S.view === "chat" && atBottom) scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    return;
  }
  if (m.type === "router") {
    S.routerBusy = m.busy;
    if (!m.busy && S.view === "tasks" && S.seg === "events") loadIncoming();
    render(); return;
  }
  if (m.type === "incoming") {
    if (S.view === "tasks" && S.seg === "events") { S.incoming.unshift(m.event); render(); }
    return;
  }
  if (m.type === "machine") { refreshState(); return; }
  if (m.type === "watchdog" || m.type === "watchdog_chat") { if (S.view === "tasks" && S.seg === "watchdog") loadWatchdog(); return; }
  if (m.type === "screen_owner") {
    S.state.screen_owner = m.task_id; S.state.screen_busy = m.task_id != null; render(); return;
  }
  if (m.type === "task") {
    const i = S.state.tasks.findIndex((t) => t.id === m.task.id);
    if (i >= 0) S.state.tasks[i] = m.task; else S.state.tasks.unshift(m.task);
    const live = ["running", "waiting", "paused"].includes(m.task.status);
    const run = new Set(S.state.running || []);
    live ? run.add(m.task.id) : run.delete(m.task.id);
    S.state.running = [...run].sort((a, b) => a - b);
    if (!live && S.state.screen_owner === m.task.id) { S.state.screen_owner = null; S.state.screen_busy = false; }
    if (S.task && S.task.id === m.task.id) S.task = m.task;
    // spend and the schedule change when a task ends; re-read them
    if (["done", "failed", "cancelled", "blocked"].includes(m.task.status)) refreshState();
  }
  if (m.type === "approval") {
    if (!S.state.approvals.some((a) => a.id === m.approval.id)) S.state.approvals.push(m.approval);
    if (navigator.vibrate) navigator.vibrate([80, 60, 80]);
    if (S.view === "chat") { render(); scrollTo({ top: document.body.scrollHeight, behavior: "smooth" }); return; }
  }
  if (m.type === "event") {
    const ev = m.event;
    if (ev.kind === "decision") S.state.approvals = S.state.approvals.filter((a) => a.id !== ev.data.approval_id);
    if (ev.kind === "created" && !S.state.tasks.some((t) => t.id === ev.task_id)) refreshState();
    if (S.view === "task" && S.taskId === ev.task_id && !S.events.some((x) => x.id === ev.id)) {
      S.events.push(ev);
      const nearBottom = innerHeight + scrollY > document.body.scrollHeight - 160;
      render();
      if (nearBottom) scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
      return;
    }
  }
  render();
}

async function loadIncoming() { try { S.incoming = await api("/api/incoming?limit=80"); render(); } catch {} }
async function loadWatchdog() { try { S.wd = await api("/api/watchdog"); render(); } catch {} }

async function loadChat() {
  try { S.chat = await api("/api/chat"); render(); if (S.view === "chat") scrollTo(0, document.body.scrollHeight); } catch {}
}

async function refreshState() {
  try { S.state = await api("/api/state"); render(); } catch (e) { /* surfaced by the connection dot */ }
}

function screenOn(on) {
  if (S.ws && S.ws.readyState === 1) S.ws.send(JSON.stringify({ type: "screen", on, interval: 1, machine: S.screenMachine }));
}

function signOut() {
  localStorage.removeItem(TOKEN_KEY);
  S.token = null; S.state = null;
  try { S.ws && S.ws.close(); } catch {}
  render();
}

// ---------------------------------------------------------------- views
function render() {
  const active = document.activeElement;
  const focusId = active && active.id, sel = active && active.selectionStart;
  $app.innerHTML = !S.token ? viewPair() : !S.state ? `<div class="center muted">Connecting to your Mac…</div>` : shell();
  if (focusId) {
    const el = document.getElementById(focusId);
    if (el) { el.focus(); try { el.setSelectionRange(sel, sel); } catch {} }
  }
  const img = document.getElementById("screen-img");
  if (img && S.frame) img.src = `data:${S.frame.media_type};base64,${S.frame.data}`;
}

function shell() {
  const pending = S.state.approvals.length;
  const body = S.view === "task" ? viewTask() : S.view === "screen" ? viewScreen() : S.view === "settings" ? viewSettings()
    : S.view === "tasks" ? viewHome() : viewChat();
  const composer = S.view === "chat" ? composerChat() : S.view === "tasks" && S.seg === "tasks" ? composerHome() : S.view === "task" ? composerTask() : "";
  return `
    <main class="page ${composer ? "" : "no-composer"}">${body}</main>
    ${composer}
    <nav class="tabs">
      ${tab("chat", "💬", "Chat", pending)}${tab("tasks", "◉", "Tasks", (S.state.running || []).length)}${tab("screen", "▢", "Screen")}${tab("settings", "⚙", "Settings")}
    </nav>
    ${S.toast ? `<div class="toast" role="status">${esc(S.toast)}</div>` : ""}
    ${S.lightbox ? `<div class="lightbox" data-act="close-lightbox"><img src="${esc(S.lightbox)}" alt="screenshot"></div>` : ""}`;
}

function tab(id, ic, label, badge = 0) {
  const on = S.view === id || (id === "tasks" && S.view === "task");
  return `<button class="${on ? "active" : ""}" data-act="tab" data-id="${id}">
    ${badge ? `<span class="badge">${badge}</span>` : ""}<span class="ic">${ic}</span>${label}</button>`;
}

function header(title) {
  const run = (S.state.running || []).map((id) => S.state.tasks.find((t) => t.id === id)).filter(Boolean);
  const dot = !S.connected ? "" : run.length ? "busy" : "on";
  const label = !S.connected ? "offline" : S.state.approvals.length ? "needs you"
    : run.length ? `${run.length} working` : "idle";
  return `<header class="top">
    <div class="brand"><img src="/static/icon.svg" alt="">${esc(title)}</div>
    <span class="conn"><span class="dot ${dot}"></span>${label}</span></header>`;
}

function approvalCard(a) {
  const task = S.state.tasks.find((t) => t.id === a.task_id);
  if (a.kind === "question") {
    return `<div class="card approval">
      <div class="row spread"><span class="label">Relay asks</span><span class="muted small">${esc(ago(a.created_at))}</span></div>
      <div class="summary">${esc(a.summary)}</div>
      ${task ? `<div class="muted small ellipsis">Task: ${esc(task.title)}</div>` : ""}
      <textarea class="answer" id="answer-${a.id}" rows="2" placeholder="Your answer"></textarea>
      <div class="btn-row"><button class="btn primary" data-act="answer" data-id="${a.id}">Send answer</button></div></div>`;
  }
  return `<div class="card approval tier-${esc(a.risk)}-card">
    <div class="row spread"><span class="pill tier-${esc(a.risk)}">${a.risk === "send" ? "Sends a message" : a.risk === "danger" ? "High risk" : "Changes something"}</span>
      <span class="muted small">${esc(ago(a.created_at))}</span></div>
    <div class="summary">${esc(a.summary)}</div>
    ${task ? `<div class="muted small ellipsis">Task: ${esc(task.title)}</div>` : ""}
    <div class="btn-row"><button class="btn bad" data-act="deny" data-id="${a.id}">Deny</button>
      <button class="btn good" data-act="approve" data-id="${a.id}">Approve</button></div></div>`;
}

function taskRow(t) {
  return `<button class="task" style="width:100%;text-align:left" data-act="open" data-id="${t.id}">
    <div class="grow"><div class="title ellipsis">${esc(t.title)}</div>
      <div class="muted small ellipsis">${esc(t.status === "scheduled" ? "runs " + ago(t.run_at) + (t.repeat ? " · " + t.repeat : "") :
        (t.error || t.result || ago(t.updated_at)))}</div></div>
    <span class="pill ${esc(t.status)}">${esc(t.status)}</span></button>`;
}

function runningCard(cur) {
  return `<div class="card" data-act="open" data-id="${cur.id}" style="cursor:pointer">
    <div class="row spread"><span class="label">${S.state.screen_owner === cur.id ? "Now · on screen" : "Now"}</span>
      <span class="pill ${esc(cur.status)}">${esc(cur.status)}</span></div>
    <h2 style="margin-top:6px">${esc(cur.title)}</h2>
    <div class="muted small">#${cur.id} · ${cur.steps} steps · ${money(cur.cost_usd)}</div>
    <div class="btn-row">
      ${cur.status === "paused" ? `<button class="btn small" data-act="resume" data-id="${cur.id}">Resume</button>` :
        `<button class="btn small" data-act="pause" data-id="${cur.id}">Pause</button>`}
      <button class="btn small bad" data-act="cancel" data-id="${cur.id}">Stop</button></div></div>`;
}

function segTabs() {
  return `<div class="seg" style="margin:-6px 0 14px">${[["tasks", "Tasks"], ["events", "Events"], ["watchdog", "Watchdog"]].map(([k, l]) =>
    `<button class="${S.seg === k ? "on" : ""}" data-act="seg" data-id="${k}">${l}</button>`).join("")}</div>`;
}

const VERDICT_TONE = { admit: "done", admitted: "done", pass: "running", dispatch: "running", drop: "failed", ignore: "blocked", candidate: "scheduled" };

function viewEvents() {
  if (!S.incoming.length) return `<div class="card"><p class="muted small">Every message and trigger event appears here with the
    verdicts that decided it: code rules first, then a model against your policies, then the router.</p></div>`;
  return `<div class="card list">${S.incoming.map((e) => `<div class="evrow">
    <div class="row spread"><span class="small"><b>E${e.id}</b> · ${esc(e.source)}${e.sender ? " · " + esc(e.sender) : ""}</span>
      <span class="muted small">${esc(ago(e.created_at))}</span></div>
    <div class="small" style="margin:4px 0">${esc(e.text.length > 180 ? e.text.slice(0, 180) + "…" : e.text)}</div>
    <div class="chips">${e.verdicts.map((v) => `<span class="pill ${VERDICT_TONE[v.verdict] || ""}" title="${esc(v.why)}">${esc(v.by)}: ${esc(v.verdict)}${
      v.verdict !== "admitted" && v.why ? " — " + esc(v.why.slice(0, 40)) : ""}</span>`).join("")}
      ${e.routed_to.map((w) => `<button class="pill running" data-act="open" data-id="${w}">→ #${w}</button>`).join("")}
      ${e.thread ? `<span class="pill">thread ${esc(e.thread.slice(0, 28))}</span>` : ""}</div></div>`).join("")}</div>`;
}

function viewWatchdog() {
  const w = S.wd;
  if (!w) return `<p class="muted">Loading…</p>`;
  const rep = w.reports[w.reports.length - 1];
  const infra = rep ? Object.entries(rep.data.infra).map(([k, v]) => `<div class="row spread small"><span>${esc(k)}</span>
    <span style="color:${v === "ok" ? "var(--ok)" : "var(--bad)"}">${esc(v)}</span></div>`).join("") : `<p class="muted small">No check yet.</p>`;
  const prog = rep && rep.data.progress.length ? rep.data.progress.map((p) => `<div class="row spread small" style="margin-top:6px">
    <button class="taskchip" style="margin:0" data-act="open" data-id="${p.task_id}"><span class="grow ellipsis">#${p.task_id} ${esc(p.title)}</span>
    <span class="pill ${p.verdict === "moving" ? "done" : "failed"}">${esc(p.verdict)}</span></button></div>
    <div class="muted small">${esc(p.why)}</div>`).join("") : `<p class="muted small">No running workers at the last check.</p>`;
  const pending = w.proposals.filter((p) => p.status === "pending");
  return `<div class="card"><div class="row spread"><div class="label">Last check ${rep ? esc(ago(rep.created_at)) : ""}</div>
      <button class="btn small" data-act="wd-check">Check now</button></div>
      <div style="margin-top:8px">${infra}</div>
      <div class="label" style="margin-top:14px">Progress</div>${prog}
      ${rep && rep.data.actions.length ? `<div class="label" style="margin-top:14px">Actions</div>` + rep.data.actions.map((a) => `<div class="small">• ${esc(a)}</div>`).join("") : ""}</div>
    ${pending.map((p) => `<div class="card approval"><div class="label">Watchdog proposes</div>
      <div class="summary">${esc(p.summary)}</div><div class="muted small">${esc(p.kind)}: ${esc(JSON.stringify(p.data).slice(0, 160))}</div>
      <div class="btn-row"><button class="btn bad" data-act="prop-no" data-id="${p.id}">Reject</button>
      <button class="btn good" data-act="prop-yes" data-id="${p.id}">Apply</button></div></div>`).join("")}
    <div class="label section-title">Ask the Watchdog</div>
    <div class="timeline">${w.chat.map((c) => `<div class="bubble ${c.kind === "chat_user" ? "user" : "agent"}">${esc(c.data.text)}</div>`).join("")
      || `<p class="muted small">Ask why something stalled, e.g. “why did #12 keep retrying?”.</p>`}</div>
    <form data-form="wd-chat" class="row" style="margin-top:10px"><input class="field grow" id="wddraft" placeholder="Ask the Watchdog…" value="${esc(S.wdDraft)}">
      <button class="btn primary">Ask</button></form>`;
}

function viewHome() {
  const st = S.state, run = new Set(st.running || []);
  const live = st.tasks.filter((t) => run.has(t.id));
  const tasks = st.tasks.filter((t) => !run.has(t.id));
  const scheduled = tasks.filter((t) => t.status === "scheduled");
  const recent = tasks.filter((t) => t.status !== "scheduled").slice(0, 25);
  if (S.seg === "events") return `${header("Activity")}${segTabs()}${viewEvents()}`;
  if (S.seg === "watchdog") return `${header("Activity")}${segTabs()}${viewWatchdog()}`;
  return `${header("Tasks")}${segTabs()}
    <p class="muted small" style="margin:-2px 4px 12px">${st.workers} workers in parallel · ${st.screen_owner ? `screen: task #${st.screen_owner}` : "screen free"}</p>
    ${live.map(runningCard).join("")}
    ${!st.tasks.length ? `<div class="card"><h2>No tasks yet</h2><p class="muted small">Ask in Chat, or start a task directly below.</p></div>` : ""}
    ${scheduled.length ? `<div class="label section-title">Scheduled</div><div class="card list">${scheduled.map(taskRow).join("")}</div>` : ""}
    ${recent.length ? `<div class="label section-title">Recent</div><div class="card list">${recent.map(taskRow).join("")}</div>` : ""}
    <p class="muted small" style="text-align:center">${money(st.spend_today)} spent today of ${money(st.settings.daily_cap_usd)} cap</p>`;
}

function composerHome() {
  return `<div class="composer"><div class="inner">
    ${S.sched ? `<div class="sched">
      <input class="field" id="run-at" type="datetime-local" value="${esc(S.runAt)}">
      <select class="field" id="repeat" style="max-width:130px">
        ${["", "hourly", "daily", "weekdays", "weekly"].map((r) => `<option value="${r}" ${S.repeat === r ? "selected" : ""}>${r || "once"}</option>`).join("")}
      </select></div>` : ""}
    <form data-form="new-task">
      <button type="button" class="icon-btn ${S.sched ? "on" : ""}" data-act="toggle-sched" aria-label="Schedule">⏰</button>
      <textarea id="draft" rows="1" placeholder="Start a task directly…" enterkeyhint="send">${esc(S.draft)}</textarea>
      <button class="send" aria-label="Send">↑</button></form></div></div>`;
}

const ICON = { read: "👁", act: "🖱", change: "✎", send: "✉", danger: "⚠" };

const SUGGEST = ["What's on my calendar today?", "Summarize my unread email", "Tidy up my Downloads folder by type",
  "Whenever a PDF lands in Downloads, file receipts into Documents/Receipts", "Every weekday at 8:30, brief me on my day"];

function taskChip(id) {
  const t = S.state.tasks.find((x) => x.id === id);
  return `<button class="taskchip" data-act="open" data-id="${id}"><span class="grow ellipsis">#${id} ${esc(t ? t.title : "task")}</span>
    ${t ? `<span class="pill ${esc(t.status)}">${esc(t.status)}</span>` : ""}</button>`;
}

function viewChat() {
  const msgs = S.chat.map((m) => {
    if (m.role === "user") return `<div class="bubble user">${esc(m.text)}</div>`;
    if (m.role === "task") return taskChip(m.task_id);
    return `<div class="bubble agent">${esc(m.text)}${m.task_id ? taskChip(m.task_id) : ""}</div>`;
  }).join("");
  return `${header("Relay")}
    ${!S.chat.length ? `<div class="card"><h2>What should your Mac do?</h2>
        <p class="muted small" style="margin:4px 0 12px">Talk to Relay here. It hands real work to workers on your Mac, runs several at once,
        and asks you before anything that sends, deletes or changes things.</p>
        <div class="chips">${SUGGEST.map((c) => `<button class="chip" data-act="suggest-chat" data-text="${esc(c)}">${esc(c)}</button>`).join("")}</div></div>` : ""}
    <div class="timeline">${msgs}${S.routerBusy ? `<div class="bubble agent typing"><span></span><span></span><span></span></div>` : ""}</div>
    ${S.state.approvals.length ? `<div style="margin-top:14px">${S.state.approvals.map(approvalCard).join("")}</div>` : ""}`;
}

function composerChat() {
  return `<div class="composer"><div class="inner"><form data-form="chat">
    <textarea id="chatdraft" rows="1" placeholder="Message Relay…" enterkeyhint="send">${esc(S.chatDraft)}</textarea>
    <button class="send" aria-label="Send">↑</button></form></div></div>`;
}

function viewTask() {
  const t = S.task || S.state.tasks.find((x) => x.id === S.taskId);
  if (!t) return `<button class="back" data-act="tab" data-id="tasks">‹ Tasks</button><p class="muted">Loading…</p>`;
  const items = S.events.map((ev) => eventItem(ev, t)).join("");
  const live = ["running", "waiting", "paused"].includes(t.status);
  return `<div class="row spread"><button class="back" data-act="tab" data-id="tasks">‹ Tasks</button>
      <span class="pill ${esc(t.status)}">${esc(t.status)}</span></div>
    <h2 style="margin:4px 0 2px;font-size:20px">${esc(t.title)}</h2>
    <div class="muted small" style="margin-bottom:12px">#${t.id} · ${t.steps} steps · ${money(t.cost_usd)} · ${esc(t.model || "")}
      ${t.machine ? " · on " + esc(t.machine) : ""}${t.mode === "silent" ? ` · <span class="pill">silent</span>` : ""}</div>
    ${live || ["queued", "scheduled"].includes(t.status) ? `<div class="btn-row" style="margin:0 0 14px">
      ${t.status === "paused" ? `<button class="btn small" data-act="resume" data-id="${t.id}">Resume</button>` :
        live ? `<button class="btn small" data-act="pause" data-id="${t.id}">Pause</button>` : ""}
      <button class="btn small bad" data-act="cancel" data-id="${t.id}">${t.status === "scheduled" ? "Cancel" : "Stop"}</button></div>` : ""}
    <div class="timeline">${items}</div>
    ${t.status === "done" && t.result ? "" : t.error ? `<div class="card" style="margin-top:12px;border-color:var(--warn)"><b>${esc(t.status)}</b><div class="small">${esc(t.error)}</div></div>` : ""}`;
}

function eventItem(ev, t) {
  const d = ev.data || {};
  switch (ev.kind) {
    case "created": return `<div class="bubble user">${esc(d.prompt)}</div>${d.repeat || (d.run_at && d.run_at > ev.created_at + 5) ?
      `<div class="sys">scheduled ${esc(d.repeat || "")} · ${esc(new Date(d.run_at * 1000).toLocaleString())}</div>` : ""}`;
    case "user": return `<div class="bubble user">${esc(d.text)}</div>`;
    case "event_in": return `<div class="step"><span class="ic">📨</span><div class="grow"><div class="small">Event passed straight to this worker</div>
      <details><summary>show</summary><pre>${esc(d.text)}</pre></details></div></div>`;
    case "job": return `<div class="step"><span class="ic">⏱</span><div class="grow"><pre style="margin:0">${esc(d.text)}</pre></div></div>`;
    case "report": return `<div class="step"><span class="ic">📣</span><div class="grow">Reported to the router: ${esc(d.text)}</div></div>`;
    case "watchdog": return `<div class="step fail"><span class="ic">🐕</span><div class="grow">${esc(d.text)}</div></div>`;
    case "note": return `<div class="note">${esc(d.text)}</div>`;
    case "message": return `<div class="bubble agent">${esc(d.text)}</div>`;
    case "tool": {
      const shot = d.shot ? `/api/shots/${t.id}/${encodeURIComponent(d.shot)}?token=${encodeURIComponent(S.token)}` : "";
      const ic = d.toolset === "computer" ? (d.name === "screenshot" || d.name === "zoom" ? "📷" : "🖱") : ICON[d.tier] || "•";
      return `<div class="step ${d.ok ? "" : "fail"}"><span class="ic">${ic}</span><div class="grow">
        <div>${esc(d.summary)}</div>
        ${d.output && d.output !== "OK" && d.name !== "screenshot" ? `<details><summary>${d.ok ? "output" : "error"}</summary><pre>${esc(d.output)}</pre></details>` : ""}
        ${shot ? `<img class="thumb" loading="lazy" src="${esc(shot)}" data-act="lightbox" data-src="${esc(shot)}" alt="screenshot">` : ""}
        </div></div>`;
    }
    case "approval": {
      const a = d.approval, pending = S.state.approvals.find((x) => x.id === a.id);
      return pending ? approvalCard(pending) : `<div class="step"><span class="ic">🔐</span><div class="grow">${esc(a.kind === "question" ? "Asked: " : "Asked to approve: ")}${esc(a.summary)}</div></div>`;
    }
    case "decision": return `<div class="sys">${esc(d.status)}${d.answer ? ` — “${esc(d.answer)}”` : ""} · via ${esc(d.via)}</div>`;
    case "status": return ["done", "failed", "cancelled", "blocked", "paused", "interrupted", "discarded", "resumed"].includes(d.status) ?
      `<div class="sys">${esc(d.status)}${d.error ? ` — ${esc(d.error)}` : ""}</div>` : "";
    default: return "";
  }
}

function composerTask() {
  const t = S.task || {};
  const ph = ACTIVE.has(t.status) ? "Add a detail…" : "Follow up on this task…";
  return `<div class="composer"><div class="inner"><form data-form="followup">
    <textarea id="followup" rows="1" placeholder="${esc(ph)}" enterkeyhint="send">${esc(S.followup)}</textarea>
    <button class="send" aria-label="Send">↑</button></form></div></div>`;
}

function viewScreen() {
  const busy = S.state.screen_busy;
  const cur = S.state.tasks.find((t) => t.id === (S.state.screen_owner ?? S.state.current));
  return `${header("Screen")}
    ${busy ? `<div class="banner">🤖 <div class="grow">Relay is using the screen${cur ? ` for “${esc(cur.title)}”` : ""}.</div>
      <button class="btn small primary" data-act="takeover">Take over</button></div>` :
      cur && cur.status === "paused" ? `<div class="banner">✋ <div class="grow">You have control. Relay is paused.</div>
      <button class="btn small" data-act="resume" data-id="${cur.id}">Hand back</button></div>` : ""}
    ${(S.state.machines || []).length > 1 ? `<div class="seg" style="margin-bottom:10px">${S.state.machines.map((m) =>
      `<button class="${(S.screenMachine || S.state.machines[0].name) === m.name ? "on" : ""}" data-act="machine" data-id="${esc(m.name)}" ${m.online ? "" : "disabled"}>
      ${m.online ? "●" : "○"} ${esc(m.name)}</button>`).join("")}</div>` : ""}
    <div class="screen-wrap" id="screen">
      ${S.frame ? `<img id="screen-img" alt="Mac screen" draggable="false">` :
        `<div class="screen-empty">${esc(S.frameErr || (S.connected ? "Waiting for the first frame…" : "Offline"))}</div>`}
    </div>
    <div class="seg" style="margin:12px 0">
      ${[["click", "Tap"], ["double_click", "Double"], ["right_click", "Right"], ["move", "Point"]].map(([m, l]) =>
        `<button class="${S.mode === m ? "on" : ""}" data-act="mode" data-id="${m}">${l}</button>`).join("")}
    </div>
    <form data-form="type" class="row" style="margin-bottom:10px">
      <input class="field grow" id="typebox" autocomplete="off" autocapitalize="off" placeholder="${S.mods.size ? "Key to press with " + [...S.mods].join("+") : "Type on the Mac…"}">
      <button class="btn primary">${S.mods.size ? "Press" : "Type"}</button></form>
    <div class="keys">
      ${[["super", "⌘"], ["alt", "⌥"], ["ctrl", "⌃"], ["shift", "⇧"]].map(([k, l]) =>
        `<button class="${S.mods.has(k) ? "on" : ""}" data-act="mod" data-id="${k}">${l}</button>`).join("")}
      <button data-act="key" data-id="Escape">esc</button><button data-act="key" data-id="Tab">tab</button>
      <button data-act="key" data-id="Return">⏎</button><button data-act="key" data-id="BackSpace">⌫</button>
      <button data-act="key" data-id="Left">←</button><button data-act="key" data-id="Up">↑</button>
      <button data-act="key" data-id="Down">↓</button><button data-act="key" data-id="Right">→</button>
      <button data-act="key" data-id="super+space">⌘␣</button><button data-act="key" data-id="super+Tab">⌘⇥</button>
      <button data-act="scroll" data-id="up">scroll ↑</button><button data-act="scroll" data-id="down">scroll ↓</button>
      <button data-act="key" data-id="super+w">⌘W</button><button data-act="key" data-id="super+q">⌘Q</button>
    </div>
    <p class="muted small">Tap the screen to act at that spot. Your manual actions are logged.</p>`;
}

const SOURCES = [["web", "Phone app"], ["telegram", "Telegram (direct)"], ["telegram-group", "Telegram groups"],
  ["trigger", "Automations"], ["trigger:mail", "Mail automations"], ["trigger:webhook", "Webhooks"]];

function viewPolicies() {
  const rules = S.rules || { sources: {}, ignore_senders: [] };
  return `<div class="card"><div class="label">What deserves attention</div>
    <p class="muted small" style="margin:6px 0">Code rules decide first (no model). “Candidate” events are then judged by a light model
      against your policies, and only what's worth acting on reaches the router. See verdicts in Tasks → Events.</p>
    ${SOURCES.map(([k, l]) => `<div class="row spread" style="margin-top:6px"><span class="small">${l}</span>
      <select class="field" style="max-width:150px;padding:6px 8px" data-change="rule" data-id="${k}">
      ${["admit", "candidate", "explicit", "ignore"].map((m) => `<option ${(rules.sources[k] || "candidate") === m ? "selected" : ""}>${m}</option>`).join("")}
      </select></div>`).join("")}
    <label class="small muted" style="display:block;margin-top:10px">Ignore senders (one pattern per line)</label>
    <textarea class="field" id="ignore-senders" rows="2" data-change="ignore">${esc((rules.ignore_senders || []).join("\n"))}</textarea>
    <div class="label" style="margin-top:14px">Policies</div>
    ${(S.policies || []).map((p) => `<div class="trig"><div class="small">${esc(p.text)}</div>
      <div class="row" style="gap:6px"><button class="btn small" data-act="pol-toggle" data-id="${p.id}">${p.enabled ? "On" : "Off"}</button>
      <button class="btn small bad" data-act="pol-delete" data-id="${p.id}">Delete</button><span class="muted small">${esc(p.created_by || "")}</span></div></div>`).join("")}
    <form data-form="policy" class="trigform"><textarea class="field" id="policy-draft" rows="2"
      placeholder="e.g. When a customer emails about billing, draft a reply in the background.">${esc(S.policyDraft)}</textarea>
      <button class="btn primary">Add policy</button></form>
    ${S.wd?.guidance ? `<div class="label" style="margin-top:14px">Worker guidance (applied via the Watchdog)</div><div class="small">${esc(S.wd.guidance)}</div>` : ""}</div>`;
}

function viewMachines() {
  const ms = S.state.machines || [];
  return `<div class="card"><div class="row spread"><div class="label">Machines</div>
      <button class="btn small" data-act="machine-add">Add a machine</button></div>
    <p class="muted small" style="margin:6px 0">The agent loop and its sessions live here; other Macs run a small daemon that keeps no
      state and only executes. If one sleeps, only its tasks wait.</p>
    ${S.machineCode ? `<pre class="hook">${esc(S.machineCode.command)}</pre><p class="muted small">Run that on the other Mac (code valid 10 min).</p>` : ""}
    ${ms.map((m) => `<div class="row spread" style="margin-top:8px"><span><span class="dot ${m.online ? "on" : ""}" style="display:inline-block"></span>
      ${esc(m.name)} <span class="muted small">${m.kind === "local" ? "this Mac" : m.online ? "daemon" : "offline " + esc(ago(m.last_seen))}</span></span>
      ${m.kind === "local" ? "" : `<button class="btn small bad" data-act="machine-revoke" data-id="${esc(m.name)}">Remove</button>`}</div>`).join("")}</div>`;
}

const TRIG = {
  folder: ["📁", "New files in a folder"], mail: ["✉️", "New unread email"], calendar: ["📅", "Event about to start"],
  system: ["🖥", "Disk or battery low"], webhook: ["🔗", "Webhook (Shortcuts, IFTTT…)"],
};

function viewAutomations() {
  const f = S.trig;
  const fields = {
    folder: `<input class="field" id="trig-path" placeholder="Folder, e.g. ~/Downloads" value="${esc(f.path)}">
             <input class="field" id="trig-pattern" placeholder="Files matching, e.g. *.pdf" value="${esc(f.pattern)}">`,
    mail: `<input class="field" id="trig-query" placeholder="Only mail matching (optional)" value="${esc(f.query)}">`,
    calendar: `<input class="field" id="trig-minutes" type="number" min="1" placeholder="Minutes before" value="${esc(f.minutes)}">`,
    system: `<input class="field" id="trig-disk" type="number" placeholder="Disk free below (GB)" value="${esc(f.disk)}">
             <input class="field" id="trig-battery" type="number" placeholder="Battery below (%)" value="${esc(f.battery)}">`,
    webhook: `<p class="muted small" style="margin:0">You get a URL and secret to call from Shortcuts, IFTTT or Home Assistant.</p>`,
  }[f.kind];
  return `<div class="card"><div class="label">Automations</div>
    <p class="muted small" style="margin:6px 0 4px">When something happens on your Mac, Relay's router decides what to do with your instruction.
      You can also just say it in Chat: “whenever…”.</p>
    ${(S.triggers || []).map((t) => `<div class="trig">
      <div class="row spread"><b>${TRIG[t.kind]?.[0] || "⚡"} ${esc(t.name)}</b>
        <span class="row" style="gap:6px"><button class="btn small" data-act="trig-toggle" data-id="${t.id}">${t.enabled ? "On" : "Off"}</button>
        <button class="btn small bad" data-act="trig-delete" data-id="${t.id}">Delete</button></span></div>
      <div class="small">${esc(t.instruction)}</div>
      <div class="muted small">${esc(TRIG[t.kind]?.[1] || t.kind)}${t.config?.path ? " · " + esc(t.config.path) : ""} · fired ${t.fire_count}×${t.last_fired ? ", " + esc(ago(t.last_fired)) : ""}
        ${t.last_error ? `<br><span style="color:var(--bad)">${esc(t.last_error)}</span>` : ""}</div>
      ${t.kind === "webhook" ? `<pre class="hook">POST ${esc(location.origin + t.hook_path)}\nX-Relay-Secret: ${esc(t.secret)}</pre>` : ""}
    </div>`).join("") || `<p class="muted small">No automations yet.</p>`}
    <form data-form="trigger" class="trigform">
      <select class="field" id="trig-kind">${Object.entries(TRIG).map(([k, [ic, l]]) =>
        `<option value="${k}" ${f.kind === k ? "selected" : ""}>${ic} ${l}</option>`).join("")}</select>
      <input class="field" id="trig-name" placeholder="Name" value="${esc(f.name)}">
      ${fields}
      <textarea class="field" id="trig-instruction" rows="2" placeholder="What should Relay do each time?">${esc(f.instruction)}</textarea>
      <button class="btn primary">Add automation</button>
    </form></div>`;
}

const POLICY_TEXT = {
  careful: ["Careful", "Asks before every action except looking — every click and keystroke."],
  balanced: ["Balanced", "Acts on screen freely; asks before anything that changes data, sends, or is risky."],
  autonomous: ["Autonomous", "Asks only before sending messages and high-risk actions."],
};

function viewSettings() {
  const st = S.state.settings;
  return `${header("Settings")}
    <div class="card"><div class="label">When Relay asks first</div>
      ${Object.entries(POLICY_TEXT).map(([k, [n, d]]) => `<button class="policy-opt ${st.policy === k ? "on" : ""}" style="width:100%;text-align:left" data-act="policy" data-id="${k}">
        <b>${n}</b><span class="muted small">${d}</span></button>`).join("")}</div>
    <div class="card"><div class="label">Models</div>
      <p class="small" style="margin:8px 0 0">Router (chat, light): <b>${esc(S.state.router?.model || "—")}</b></p>
      <div class="row spread" style="margin-top:8px"><span class="small">Workers (tasks, ×${S.state.workers}): <b>${esc(st.model)}</b></span>
        <select class="field" id="effort" style="max-width:130px" data-change="effort">
          ${["low", "medium", "high", "xhigh"].map((e) => `<option ${st.effort === e ? "selected" : ""}>${e}</option>`).join("")}</select></div>
      ${st.can_see_screen ? "" : `<p class="muted small">This model can't see the screen, so Relay uses Mac tools only (no clicking).</p>`}
      <p class="muted small">Spent today: ${money(S.state.spend_today)} of ${money(st.daily_cap_usd)} daily cap.</p></div>
    <div class="card"><div class="row spread"><div class="label">Paired devices</div>
      <button class="btn small" data-act="invite">Pair another</button></div>
      ${S.inviteCode ? `<p>Code for the new device: <b style="font-family:var(--mono);letter-spacing:.15em">${esc(S.inviteCode)}</b> <span class="muted small">(10 min)</span></p>` : ""}
      ${(S.devices || []).map((d) => `<div class="row spread" style="margin-top:10px"><div><div>${esc(d.name)}${d.current ? " <span class='muted small'>(this phone)</span>" : ""}</div>
        <div class="muted small">last seen ${esc(ago(d.last_seen))}</div></div>
        ${d.current ? "" : `<button class="btn small bad" data-act="revoke" data-id="${d.id}">Remove</button>`}</div>`).join("") || `<p class="muted small">Loading…</p>`}</div>
    <div class="card"><div class="label">What Relay remembers</div>
      ${(S.memories || []).length ? S.memories.map((m) => `<div class="row spread" style="margin-top:10px"><div class="grow small">${esc(m.text)}</div>
        <button class="btn small" data-act="forget" data-id="${m.id}">Forget</button></div>`).join("") : `<p class="muted small">Nothing yet. Relay saves preferences you mention.</p>`}</div>
    ${viewPolicies()}
    ${viewMachines()}
    ${viewAutomations()}
    <div class="card"><div class="label">Notifications</div>
      <p class="small">Telegram: ${st.telegram ? "on" : "off"} · ntfy: ${st.ntfy ? "on" : "off"}</p>
      <p class="muted small">Approvals also reach you there when this app is closed. Configure on the Mac (see README).</p></div>
    <button class="btn bad" style="width:100%" data-act="signout">Unpair this phone</button>
    <p class="muted small" style="text-align:center">Relay ${esc(S.state.version)} · ${esc(S.state.host.host)} host</p>`;
}

function viewPair() {
  const code = new URLSearchParams(location.search).get("code") || "";
  const name = /iPhone/.test(navigator.userAgent) ? "iPhone" : /Android/.test(navigator.userAgent) ? "Android" : "Phone";
  return `<div class="center"><form class="pair" data-form="pair">
    <img src="/static/icon.svg" width="64" height="64" alt="" style="border-radius:16px">
    <h1 style="margin:12px 0 4px">Pair with your Mac</h1>
    <p class="muted">On the Mac, run <code>python -m macagent pair</code> and enter the code.</p>
    <input class="field code" id="pair-code" maxlength="9" autocomplete="one-time-code" autocapitalize="characters" placeholder="ABCD-EFGH" value="${esc(code)}">
    <input class="field" id="pair-name" style="margin-top:10px" value="${esc(name)}" aria-label="Device name">
    <button class="btn primary" style="width:100%;margin-top:14px">Pair</button>
    ${S.toast ? `<p style="color:var(--bad)">${esc(S.toast)}</p>` : ""}</form></div>`;
}

// ---------------------------------------------------------------- actions
async function go(view, id) {
  if (S.view === "screen" && view !== "screen") screenOn(false);
  S.view = view;
  if (view === "screen") screenOn(true);
  if (view === "settings") loadSettings();
  if (view === "chat") { render(); scrollTo(0, document.body.scrollHeight); return; }
  if (view === "task") {
    S.taskId = id; S.events = []; S.task = S.state.tasks.find((t) => t.id === id) || null; S.followup = "";
    render(); scrollTo(0, 0);
    try { const r = await api(`/api/tasks/${id}`); S.task = r.task; S.events = r.events; } catch (e) { toast(e.message); }
    render(); scrollTo(0, document.body.scrollHeight);
    return;
  }
  render(); scrollTo(0, 0);
}

async function loadSettings() {
  try {
    [S.devices, S.memories, S.triggers, S.policies, S.rules, S.wd] = await Promise.all([api("/api/devices"), api("/api/memories"),
      api("/api/triggers"), api("/api/policies"), api("/api/filter/rules").catch(() => null), api("/api/watchdog").catch(() => null)]);
    render();
  } catch (e) { toast(e.message); }
}

async function control(body) {
  try {
    await api("/api/control", { method: "POST", body: { ...body, force: S.takeover, machine: S.screenMachine } });
  } catch (e) {
    if (e.status === 409) { toast("Relay is using the screen — tap Take over first."); return; }
    toast(e.message);
  }
}

function tapAt(ev) {
  const img = document.getElementById("screen-img");
  if (!img || !S.frame) return;
  const r = img.getBoundingClientRect();
  const x = Math.round(((ev.clientX - r.left) / r.width) * S.frame.w);
  const y = Math.round(((ev.clientY - r.top) / r.height) * S.frame.h);
  S.lastTap = { x, y };
  const dot = document.createElement("div");
  dot.className = "ripple"; dot.style.left = `${ev.clientX - r.left}px`; dot.style.top = `${ev.clientY - r.top}px`;
  document.getElementById("screen").appendChild(dot); setTimeout(() => dot.remove(), 600);
  control({ action: S.mode, x, y });
}

$app.addEventListener("click", async (e) => {
  if (e.target.id === "screen-img") return tapAt(e);
  const el = e.target.closest("[data-act]");
  if (!el) return;
  const { act, id } = el.dataset;
  const n = Number(id);
  try {
    switch (act) {
      case "tab": return go(id);
      case "open": return go("task", n);
      case "suggest": S.draft = el.dataset.text; render(); document.getElementById("draft")?.focus(); return;
      case "suggest-chat": S.chatDraft = el.dataset.text; render(); document.getElementById("chatdraft")?.focus(); return;
      case "trig-toggle": {
        const t = S.triggers.find((x) => x.id === n);
        await api(`/api/triggers/${n}`, { method: "PATCH", body: { enabled: !t.enabled } }); return loadSettings();
      }
      case "trig-delete": if (confirm("Delete this automation?")) { await api(`/api/triggers/${n}`, { method: "DELETE" }); loadSettings(); } return;
      case "toggle-sched": S.sched = !S.sched; render(); return;
      case "approve": case "deny":
        el.disabled = true;
        await api(`/api/approvals/${n}`, { method: "POST", body: { approve: act === "approve" } });
        S.state.approvals = S.state.approvals.filter((a) => a.id !== n);
        toast(act === "approve" ? "Approved" : "Denied"); return;
      case "answer": {
        const v = document.getElementById(`answer-${n}`).value.trim();
        if (!v) return toast("Type an answer first");
        await api(`/api/approvals/${n}`, { method: "POST", body: { approve: true, answer: v } });
        S.state.approvals = S.state.approvals.filter((a) => a.id !== n); toast("Sent"); return;
      }
      case "pause": case "resume": case "cancel":
        await api(`/api/tasks/${n}/${act}`, { method: "POST" });
        if (act === "resume") S.takeover = false;
        return toast(act === "cancel" ? "Stopping" : act === "pause" ? "Paused" : "Resumed");
      case "takeover":
        S.takeover = true;
        if (S.state.screen_owner) await api(`/api/tasks/${S.state.screen_owner}/pause`, { method: "POST" });
        S.state.screen_busy = false; toast("You have control"); return;
      case "mode": S.mode = id; render(); return;
      case "seg": S.seg = id; render(); if (id === "events") loadIncoming(); if (id === "watchdog") loadWatchdog(); return;
      case "wd-check": toast("Checking…"); await api("/api/watchdog/check", { method: "POST" }); return loadWatchdog();
      case "prop-yes": case "prop-no":
        await api(`/api/watchdog/proposals/${n}`, { method: "POST", body: { approve: act === "prop-yes" } });
        toast(act === "prop-yes" ? "Applied" : "Rejected"); return loadWatchdog();
      case "machine": S.screenMachine = id; S.frame = null; screenOn(true); render(); return;
      case "machine-add": S.machineCode = await api("/api/machines/pairing-code", { method: "POST" }); render(); return;
      case "machine-revoke": if (confirm(`Remove ${id}? Its daemon will be disconnected.`)) { await api(`/api/machines/${encodeURIComponent(id)}`, { method: "DELETE" }); refreshState(); } return;
      case "pol-toggle": {
        const p = S.policies.find((x) => x.id === n);
        await api(`/api/policies/${n}`, { method: "PATCH", body: { enabled: !p.enabled } }); return loadSettings();
      }
      case "pol-delete": await api(`/api/policies/${n}`, { method: "DELETE" }); return loadSettings();
      case "mod": S.mods.has(id) ? S.mods.delete(id) : S.mods.add(id); render(); return;
      case "key": {
        const combo = S.mods.size && !id.includes("+") ? [...S.mods, id].join("+") : id;
        S.mods.clear(); render();
        return control({ action: "key", text: combo });
      }
      case "scroll": return control({ action: "scroll", direction: id, amount: 5, ...(S.lastTap || {}) });
      case "policy": S.state.settings = { ...S.state.settings, ...(await api("/api/settings", { method: "PUT", body: { policy: id } })) }; render(); return;
      case "invite": S.inviteCode = (await api("/api/devices/pairing-code", { method: "POST" })).code; render(); return;
      case "revoke": await api(`/api/devices/${n}`, { method: "DELETE" }); return loadSettings();
      case "forget": await api(`/api/memories/${n}`, { method: "DELETE" }); return loadSettings();
      case "signout": if (confirm("Unpair this phone from your Mac?")) signOut(); return;
      case "lightbox": S.lightbox = el.dataset.src; render(); return;
      case "close-lightbox": S.lightbox = null; render(); return;
    }
  } catch (err) { toast(err.message); }
});

$app.addEventListener("change", async (e) => {
  if (e.target.dataset.change === "effort") {
    try { S.state.settings = { ...S.state.settings, ...(await api("/api/settings", { method: "PUT", body: { effort: e.target.value } })) }; toast("Saved"); }
    catch (err) { toast(err.message); }
  }
  if (e.target.id === "run-at") S.runAt = e.target.value;
  if (e.target.id === "trig-kind") { S.trig.kind = e.target.value; render(); }
  if (e.target.dataset.change === "rule" || e.target.dataset.change === "ignore") {
    const rules = S.rules || { sources: {}, ignore_senders: [], admit_senders: [] };
    if (e.target.dataset.change === "rule") rules.sources[e.target.dataset.id] = e.target.value;
    else rules.ignore_senders = e.target.value.split("\n").map((x) => x.trim()).filter(Boolean);
    try { S.rules = await api("/api/filter/rules", { method: "PUT", body: rules }); toast("Saved"); } catch (err) { toast(err.message); }
  }
  if (e.target.id === "repeat") S.repeat = e.target.value;
});

$app.addEventListener("input", (e) => {
  const t = e.target;
  if (t.id === "draft") S.draft = t.value;
  if (t.id === "followup") S.followup = t.value;
  if (t.id === "chatdraft") S.chatDraft = t.value;
  if (t.id === "wddraft") S.wdDraft = t.value;
  if (t.id === "policy-draft") S.policyDraft = t.value;
  const tf = { "trig-name": "name", "trig-instruction": "instruction", "trig-path": "path", "trig-pattern": "pattern",
    "trig-query": "query", "trig-minutes": "minutes", "trig-battery": "battery", "trig-disk": "disk" }[t.id];
  if (tf) S.trig[tf] = t.value;
  if (t.tagName === "TEXTAREA" && t.closest(".composer")) { t.style.height = "auto"; t.style.height = Math.min(140, t.scrollHeight) + "px"; }
});

$app.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && e.target.closest?.(".composer") && e.target.tagName === "TEXTAREA") {
    e.preventDefault(); e.target.form.requestSubmit();
  }
});

$app.addEventListener("submit", async (e) => {
  e.preventDefault();
  const form = e.target.dataset.form;
  try {
    if (form === "pair") {
      const code = document.getElementById("pair-code").value;
      const name = document.getElementById("pair-name").value;
      const r = await fetch("/api/pair", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, name }) });
      const data = await r.json();
      if (!r.ok) { S.toast = data.detail || "Pairing failed"; return render(); }
      localStorage.setItem(TOKEN_KEY, data.token);
      S.token = data.token; S.toast = "";
      history.replaceState(null, "", "/");
      render(); connect(); refreshState();
    } else if (form === "chat") {
      const text = S.chatDraft.trim();
      if (!text) return;
      S.chatDraft = ""; S.routerBusy = true; render();
      const r = await api("/api/chat", { method: "POST", body: { text } });
      if (r && r.id && !S.chat.some((x) => x.id === r.id)) S.chat.push(r);
      render(); scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    } else if (form === "wd-chat") {
      const text = S.wdDraft.trim();
      if (!text) return;
      S.wdDraft = ""; S.wd.chat.push({ kind: "chat_user", data: { text } }); render();
      await api("/api/watchdog/chat", { method: "POST", body: { text } });
      loadWatchdog();
    } else if (form === "policy") {
      if (!S.policyDraft.trim()) return;
      await api("/api/policies", { method: "POST", body: { text: S.policyDraft.trim() } });
      S.policyDraft = ""; toast("Policy added"); loadSettings();
    } else if (form === "trigger") {
      const f = S.trig, config = {};
      if (f.kind === "folder") Object.assign(config, { path: f.path, pattern: f.pattern || "*" });
      if (f.kind === "mail" && f.query) config.query = f.query;
      if (f.kind === "calendar") config.minutes_before = Number(f.minutes) || 15;
      if (f.kind === "system") {
        if (f.disk) config.disk_free_below_gb = Number(f.disk);
        if (f.battery) config.battery_below = Number(f.battery);
      }
      if (!f.instruction.trim()) return toast("Say what Relay should do each time");
      await api("/api/triggers", { method: "POST", body: { name: f.name || TRIG[f.kind][1], kind: f.kind, config, instruction: f.instruction } });
      S.trig = { ...S.trig, name: "", instruction: "" };
      toast("Automation added"); loadSettings();
    } else if (form === "new-task") {
      const prompt = S.draft.trim();
      if (!prompt) return;
      const body = { prompt };
      if (S.sched && S.runAt) body.run_at = new Date(S.runAt).getTime() / 1000;
      if (S.sched && S.repeat) body.repeat = S.repeat;
      const t = await api("/api/tasks", { method: "POST", body });
      S.draft = ""; S.sched = false; S.runAt = ""; S.repeat = "";
      if (!S.state.tasks.some((x) => x.id === t.id)) S.state.tasks.unshift(t);
      go("task", t.id);
    } else if (form === "followup") {
      const text = S.followup.trim();
      if (!text || !S.taskId) return;
      const r = await api(`/api/tasks/${S.taskId}/message`, { method: "POST", body: { text } });
      S.followup = ""; render();
      if (r.result !== "queued") toast("Delivered at the next step");
    } else if (form === "type") {
      const box = document.getElementById("typebox");
      const v = box.value;
      if (!v) return;
      if (S.mods.size) { await control({ action: "key", text: [...S.mods, v.slice(0, 1)].join("+") }); S.mods.clear(); }
      else await control({ action: "type", text: v });
      box.value = ""; render();
    }
  } catch (err) { toast(err.message); }
});

if ("serviceWorker" in navigator && location.protocol === "https:") navigator.serviceWorker.register("/sw.js").catch(() => {});
render();
if (S.token) { connect(); }
