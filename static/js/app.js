// Vanilla JS front-end: talks to the Flask API using fetch()
const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const page = document.body.dataset.page;

function esc(v) {  // escape anything that comes from the server before putting it in HTML
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(url, method = "GET", body) {
  try {
    const res = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    return await res.json();
  } catch (e) {
    return { success: false, message: "Could not reach the server" };
  }
}
function toast(msg, type = "") {
  const t = document.createElement("div");
  t.className = "toast " + type;
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 3500);
}
const badge = (s) => `<span class="badge b-${esc(s)}">${esc(s)}</span>`;
const money = (n) => "\u20B9" + Number(n).toLocaleString("en-IN");
let isOnline = true;

// ---------- connectivity ----------
async function refreshStatus() {
  const r = await api("/api/status");
  if (!r.success) return;
  isOnline = r.connectivity === "ONLINE";
  const b = $("#connBadge");
  b.textContent = "\u25CF " + r.connectivity;
  b.className = "conn " + (isOnline ? "online" : "offline");
  const banner = $("#offlineBanner");
  if (banner) banner.classList.toggle("hidden", isOnline);
  if (typeof updateRaiseHint === "function") updateRaiseHint();
  const pipe = $("#pipeline");
  if (pipe) {
    pipe.classList.toggle("offline", !isOnline);
    $("#linkLabel").textContent = isOnline ? "Connected" : "No connection - local queue";
  }
}

// ---------- dashboard ----------
const SEGMENTS = [  // [stat key, label, colour]
  ["reconciled", "Reconciled", "#16a34a"], ["pending", "Pending / Synced", "#2563eb"],
  ["offline", "Offline queue", "#9ca3af"], ["duplicates", "Duplicates", "#f59e0b"],
  ["delayed", "Delayed", "#fb923c"], ["conflicts", "Conflicts", "#dc2626"], ["failed", "Failed", "#7f1d1d"],
];
function countUp(el, to) {   // animate numbers instead of jumping
  const from = parseInt(el.textContent, 10) || 0;
  if (from === to) return;
  el.closest(".stat")?.classList.remove("bump");
  void el.offsetWidth;
  el.closest(".stat")?.classList.add("bump");
  const start = performance.now();
  const tick = (now) => {
    const k = Math.min((now - start) / 450, 1);
    el.textContent = Math.round(from + (to - from) * k);
    if (k < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}
function renderChart(st) {   // SVG donut drawn with stroke-dasharray (circumference ~ 100)
  const g = $("#donutSegs");
  if (!g) return;
  const total = st.total || 0;
  let offset = 0, segs = "", legend = "";
  for (const [key, label, color] of SEGMENTS) {
    const pct = total ? (st[key] / total) * 100 : 0;
    segs += `<circle class="seg" cx="21" cy="21" r="15.9155" fill="none" stroke="${color}" stroke-width="5"
      stroke-dasharray="${pct} ${100 - pct}" stroke-dashoffset="${-offset}"></circle>`;
    offset += pct;
    legend += `<div><span class="dot" style="background:${color}"></span>${label}<b>${st[key]}</b></div>`;
  }
  g.innerHTML = segs;
  $("#legend").innerHTML = legend;
  $("#donutPct").textContent = (total ? Math.round((st.reconciled / total) * 100) : 0) + "%";
  $("#queueCount").textContent = st.offline + " queued";
  $("#syncedCount").textContent = (st.reconciled + st.pending) + " at server";
}
async function refreshStats() {
  const r = await api("/api/statistics");
  if (!r.success) return;
  for (const [k, v] of Object.entries(r.statistics)) {
    const el = $("#stat-" + k);
    if (el) countUp(el, v);
  }
  renderChart(r.statistics);
}
async function refreshRecent() {
  const body = $("#recentBody");
  if (!body) return;
  const r = await api("/api/transactions?limit=8");
  if (!r.success || !r.transactions.length) {
    body.innerHTML = '<tr><td colspan="5" class="empty">No transactions yet</td></tr>';
    return;
  }
  const seen = new Set([...body.querySelectorAll("tr[data-id]")].map((x) => x.dataset.id));
  body.innerHTML = r.transactions.map((t) => `<tr class="clickable ${seen.size && !seen.has(t.transaction_id) ? "fresh" : ""}" data-id="${esc(t.transaction_id)}">
    <td>${esc(t.transaction_id)}</td><td>${esc(t.device_id)}</td><td class="num">${money(t.amount)}</td>
    <td>${badge(t.status)}</td><td>${t.retry_count}</td></tr>`).join("");
}
async function refreshAll() {
  await Promise.all([refreshStatus(), refreshStats(), refreshRecent()]);
}

function log(msg, cls = "") {
  const box = $("#activityLog");
  if (!box) return;
  if (box.querySelector(".muted")) box.innerHTML = "";
  const d = document.createElement("div");
  d.innerHTML = `<span class="t">${new Date().toLocaleTimeString("en-GB")}</span><span class="${cls}">${esc(msg)}</span>`;
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
}

// Call an action endpoint, show a toast + log line, refresh the screen
async function action(url, body, okClass = "success") {
  const r = await api(url, "POST", body);
  toast(r.message, r.success ? okClass : "error");
  log(r.message, r.success ? "" : "err");
  await refreshAll();
  return r;
}

async function runSync() {
  const wrap = $("#syncProgress"), bar = $("#syncBar"), text = $("#syncText");
  wrap.classList.remove("hidden");
  bar.style.width = "0";
  text.textContent = "Synchronizing...";
  const poll = setInterval(async () => {   // read live progress while the POST is running
    const p = (await api("/api/sync-progress")).progress;
    if (p && p.total) {
      text.textContent = `Synchronizing...  ${p.done} / ${p.total} transactions processed`;
      bar.style.width = Math.round((p.done / p.total) * 100) + "%";
    }
  }, 120);
  const r = await api("/api/synchronize", "POST");
  clearInterval(poll);
  if (r.success) {
    bar.style.width = "100%";
    text.textContent = `Done: ${r.synced} synchronized, ${r.failed} failed (will be retried)`;
    (r.synced_ids || []).forEach((id) => log(id + " synchronized", "good"));
    (r.failed_ids || []).forEach((id) => log(id + " failed - will retry", "err"));
  }
  toast(r.message, r.success ? "success" : "error");
  if (!r.success) { log(r.message, "err"); wrap.classList.add("hidden"); }
  else setTimeout(() => wrap.classList.add("hidden"), 2500);
  await refreshAll();
  return r;
}

async function runReconcile() {
  const r = await action("/api/reconcile");
  if (r.success) {
    log(`${r.reconciled} transactions reconciled`, "good");
    if (r.duplicates) log(`${r.duplicates} duplicates detected`, "warn");
    if (r.conflicts) log(`${r.conflicts} conflicts detected`, "warn");
    if (r.delayed) log(`${r.delayed} delayed transactions flagged`, "warn");
    if (r.failed) log(`${r.failed} failed transactions`, "err");
  }
  return r;
}

async function runDemo() {
  const btn = $("#btnDemo");
  btn.disabled = true;
  const step = async (msg, fn, cls = "") => { log(msg, cls); await fn(); await refreshAll(); await sleep(700); };
  const gen = async (count, type, label) => {
    const r = await api("/api/transactions/generate-batch", "POST", { count, type });
    (r.ids || []).forEach((id) => log("Generated " + id));
    log(label, "warn");
  };
  const st = (await api("/api/status")).connectivity;
  if (st === "ONLINE") await step("System went OFFLINE", () => api("/api/offline", "POST"), "err");
  await gen(12, "NORMAL", "12 normal transactions queued");
  await gen(2, "DUPLICATE", "Duplicate records created (2 pairs)");
  await gen(2, "DELAYED", "Delayed records created (old timestamps)");
  await gen(2, "CONFLICT", "Conflicting records created (amount mismatch)");
  await gen(2, "FAILED", "Records that will fail synchronization created");
  await refreshAll(); await sleep(800);
  await step("Connectivity restored", () => api("/api/online", "POST"), "good");
  log("Synchronization started");
  await runSync(); await sleep(700);
  for (let i = 1; i <= 2; i++) {      // failed ones need 2 retries
    log("Retrying failed transactions (round " + i + ")", "warn");
    await runSync(); await sleep(700);
  }
  log("Reconciliation started");
  await runReconcile();
  log("Demo complete - see final statistics above", "good");
  toast("Demo complete", "success");
  btn.disabled = false;
}

// ---------- manual "Raise a Transaction" form ----------
const TYPE_HINTS = {
  NORMAL: "A single payment that syncs and reconciles normally.",
  DUPLICATE: "Creates the payment plus a second copy (-DUP) that reconciliation will flag as DUPLICATE.",
  DELAYED: "Backdates the payment so it reaches the server late and is flagged DELAYED.",
  CONFLICT: "The server holds a different amount, so reconciliation will flag a CONFLICT.",
  FAILED: "Sync fails twice (retry count 2) and succeeds on the third attempt.",
};
function updateRaiseHint() {
  const h = $("#raiseHint");
  if (!h) return;
  const where = isOnline ? "System is ONLINE: it is sent to the server immediately."
                         : "System is OFFLINE: it is stored in the local queue until you synchronize.";
  h.textContent = TYPE_HINTS[$("#fType").value] + " " + where;
}
function initRaiseForm() {
  const type = $("#fType");
  const toggle = () => {
    $("#fDelayWrap").classList.toggle("hidden", type.value !== "DELAYED");
    $("#fServerWrap").classList.toggle("hidden", type.value !== "CONFLICT");
    updateRaiseHint();
  };
  type.onchange = toggle;
  toggle();
  $("#raiseForm").onsubmit = async (e) => {
    e.preventDefault();
    const amount = $("#fAmount"), server = $("#fServer");
    [amount, server].forEach((i) => i.classList.remove("invalid"));
    const body = { type: type.value, device_id: $("#fDevice").value, payment_method: $("#fMethod").value, amount: amount.value };
    if (type.value === "DELAYED") body.delay_minutes = parseInt($("#fDelay").value, 10);
    if (type.value === "CONFLICT") body.server_amount = server.value;
    const r = await api("/api/transactions/create", "POST", body);
    toast(r.message, r.success ? "success" : "error");
    log(r.message, r.success ? "good" : "err");
    if (r.success) { amount.value = ""; server.value = ""; }
    else if (/amount/i.test(r.message)) (/server/i.test(r.message) ? server : amount).classList.add("invalid");
    await refreshAll();
  };
}

function initDashboard() {
  initRaiseForm();
  $("#btnOffline").onclick = () => action("/api/offline", null, "warn");
  $("#btnOnline").onclick = () => action("/api/online");
  $("#btnGenerate").onclick = () => action("/api/transactions/generate", { type: "NORMAL" });
  $("#btnBatch").onclick = () => action("/api/transactions/generate-batch", { count: 10, type: "NORMAL" });
  $("#btnSync").onclick = runSync;
  $("#btnReconcile").onclick = runReconcile;
  $("#btnDemo").onclick = runDemo;
  $("#btnReset").onclick = async () => {
    if (!confirm("Delete all transactions and audit logs and reset the simulator?")) return;
    await action("/api/reset");
    $("#activityLog").innerHTML = '<div class="muted">Waiting for activity...</div>';
  };
  document.querySelectorAll("[data-type]").forEach((b) => {
    b.onclick = () => action("/api/transactions/generate", { type: b.dataset.type });
  });
  refreshAll();
  setInterval(refreshStatus, 5000);
}

// ---------- transactions page ----------
let allTx = [], activeFilter = "ALL";
async function loadTransactions() {
  const r = await api("/api/transactions");
  allTx = r.transactions || [];
  renderTransactions();
}
function renderTransactions() {
  const q = $("#search").value.trim().toLowerCase();
  const rows = allTx.filter((t) => (activeFilter === "ALL" || t.status === activeFilter) &&
    (!q || t.transaction_id.toLowerCase().includes(q) || t.device_id.toLowerCase().includes(q)));
  $("#txBody").innerHTML = rows.length ? rows.map((t) => `<tr class="clickable" data-id="${esc(t.transaction_id)}">
    <td>${esc(t.transaction_id)}</td><td>${esc(t.device_id)}</td><td class="num">${money(t.amount)}</td>
    <td>${esc(t.payment_method)}</td><td>${esc(t.created_at)}</td><td>${badge(t.status)}</td><td>${t.retry_count}</td></tr>`).join("")
    : '<tr><td colspan="7" class="empty">No transactions found</td></tr>';
}
function initTransactions() {
  document.querySelectorAll(".chip").forEach((c) => c.onclick = () => {
    document.querySelectorAll(".chip").forEach((x) => x.classList.remove("active"));
    c.classList.add("active");
    activeFilter = c.dataset.filter;
    renderTransactions();
  });
  $("#search").oninput = renderTransactions;
  loadTransactions();
  refreshStatus();
}

// ---------- audit page ----------
async function initAudit() {
  refreshStatus();
  const r = await api("/api/audit");
  const rows = r.audit || [];
  $("#auditBody").innerHTML = rows.length ? rows.map((a) => `<tr>
    <td>${a.id}</td><td>${esc(a.timestamp)}</td><td>${esc(a.transaction_id)}</td><td>${esc(a.action)}</td>
    <td>${a.old_status ? badge(a.old_status) + " \u2192 " : ""}${a.new_status ? badge(a.new_status) : ""}</td>
    <td>${esc(a.message)}</td></tr>`).join("")
    : '<tr><td colspan="6" class="empty">No audit records yet</td></tr>';
}

// ---------- details modal ----------
async function showDetails(id) {
  const r = await api("/api/transactions/" + encodeURIComponent(id));
  if (!r.success) return toast(r.message, "error");
  const t = r.transaction;
  let html = `<h2>Transaction Details</h2>
    <dl class="kv">
      <dt>Transaction ID</dt><dd>${esc(t.transaction_id)}</dd>
      <dt>Device</dt><dd>${esc(t.device_id)}</dd>
      <dt>Amount</dt><dd>${money(t.amount)}</dd>
      <dt>Payment Method</dt><dd>${esc(t.payment_method)}</dd>
      <dt>Created</dt><dd>${esc(t.created_at)}</dd>
      <dt>Received</dt><dd>${esc(t.received_at || "-")}</dd>
      <dt>Delay</dt><dd>${esc(t.delay_text)}</dd>
      <dt>Status</dt><dd>${badge(t.status)}</dd>
      <dt>Sync Status</dt><dd>${esc(t.sync_status)}</dd>
      <dt>Retry Count</dt><dd>${t.retry_count}</dd>
      <dt>Type</dt><dd>${esc(t.transaction_type)}</dd></dl>`;
  if (t.status === "DUPLICATE") html += `<div class="alert">\u26A0 Duplicate transaction detected (original: ${esc(t.original_transaction_id)})</div>`;
  if (t.conflict_reason) html += `<div class="alert">${esc(t.conflict_reason)}</div>`;
  if (t.failure_reason) html += `<div class="alert">Failure: ${esc(t.failure_reason)}</div>`;
  html += "<h3>Verification</h3>" + r.verification.map((v) =>
    `<div class="check ${v.ok ? "ok" : "bad"}">${v.ok ? "\u2713" : "\u2717"} ${esc(v.label)}</div>`).join("");
  html += `<h3>Audit History</h3><table><tbody>${r.audit.map((a) => `<tr><td>${esc(a.timestamp)}</td>
    <td>${esc(a.old_status || "-")} \u2192 ${esc(a.new_status || "-")}</td><td>${esc(a.message)}</td></tr>`).join("")}</tbody></table>`;
  $("#modalBody").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
$("#modalClose").onclick = () => $("#modal").classList.add("hidden");
$("#modal").onclick = (e) => { if (e.target.id === "modal") e.target.classList.add("hidden"); };
document.addEventListener("click", (e) => {   // row click (works for rows added later too)
  const row = e.target.closest("tr[data-id]");
  if (row) showDetails(row.dataset.id);
});

if (page === "dashboard") initDashboard();
else if (page === "transactions") initTransactions();
else if (page === "audit") initAudit();
