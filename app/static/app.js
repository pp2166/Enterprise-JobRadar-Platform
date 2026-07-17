const form = document.getElementById("search-form");
const results = document.getElementById("results");
const meta = document.getElementById("meta");
const pager = document.getElementById("pager");
const navTabs = document.querySelectorAll(".nav-tab");
const views = {
  search: document.getElementById("search-view"),
  "crawl-runs": document.getElementById("crawl-runs-view"),
};
const refreshRuns = document.getElementById("refresh-runs");
const runsStatus = document.getElementById("runs-status");
const runsError = document.getElementById("runs-error");
const runsEmpty = document.getElementById("runs-empty");
const runsBody = document.getElementById("runs-body");
const runsPager = document.getElementById("runs-pager");
const runsActionMessage = document.getElementById("runs-action-message");
const runsFilterForm = document.getElementById("runs-filter-form");
const runsSourceFilter = document.getElementById("runs-source-filter");
const runsStatusFilter = document.getElementById("runs-status-filter");
const resetRunsFilter = document.getElementById("reset-runs-filter");
const crawlSourceSelect = document.getElementById("crawl-source-select");
const startCrawlButton = document.getElementById("start-crawl");

let currentPage = 1;
let runsPage = 1;
let runsLoaded = false;
let sourcesLoaded = false;
let runFilters = { source: "", status: "" };
const PAGE_SIZE = 20;
const RUNS_PAGE_SIZE = 20;

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"']/g, c => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function fmtDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  if (days < 1) return "today";
  if (days === 1) return "1 day ago";
  if (days < 30) return `${days} days ago`;
  return d.toLocaleDateString();
}

function fmtLocalDateTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "-";
  return d.toLocaleString();
}

function dash(value) {
  if (value === null || value === undefined || value === "") return "-";
  return value;
}

function fmtSalary(j) {
  if (!j.salary_min && !j.salary_max) return "";
  const cur = j.salary_currency || "";
  const fmt = n => n ? `${cur} ${n.toLocaleString()}` : "";
  if (j.salary_min && j.salary_max) return `${fmt(j.salary_min)} - ${fmt(j.salary_max)}`;
  return fmt(j.salary_min || j.salary_max);
}

async function extractErrorMessage(response) {
  const fallback = `HTTP ${response.status}`;
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && typeof detail.message === "string") {
      return detail.message;
    }
    return fallback;
  } catch {
    return fallback;
  }
}

function setActionMessage(type, message) {
  runsActionMessage.hidden = false;
  runsActionMessage.className = `action-message ${type}`;
  runsActionMessage.textContent = message;
}

function clearActionMessage() {
  runsActionMessage.hidden = true;
  runsActionMessage.className = "action-message";
  runsActionMessage.textContent = "";
}

function resetSelect(select, firstLabel) {
  select.replaceChildren();
  const option = document.createElement("option");
  option.value = "";
  option.textContent = firstLabel;
  select.appendChild(option);
}

function appendSourceOptions(select, sources) {
  for (const source of sources) {
    const option = document.createElement("option");
    option.value = source;
    option.textContent = source;
    select.appendChild(option);
  }
}

async function loadSources() {
  if (sourcesLoaded) return;

  try {
    const response = await fetch("/admin/sources");
    if (!response.ok) throw new Error(await extractErrorMessage(response));
    const body = await response.json();
    const sources = Array.isArray(body.sources) ? body.sources : [];

    resetSelect(crawlSourceSelect, "选择来源");
    resetSelect(runsSourceFilter, "全部来源");
    appendSourceOptions(crawlSourceSelect, sources);
    appendSourceOptions(runsSourceFilter, sources);
    sourcesLoaded = true;
  } catch (e) {
    setActionMessage("error", `加载来源失败：${e.message}`);
  }
}

function renderJob(j) {
  const tags = (j.tags || "").split(",").map(s => s.trim()).filter(Boolean).slice(0, 6);
  const snippet = (j.description || "").slice(0, 240);
  return `
    <article class="job">
      <h3><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></h3>
      <div class="meta">
        <strong>${esc(j.company)}</strong>
        ${j.location ? " / " + esc(j.location) : ""}
        ${j.remote ? " / remote" : ""}
        ${j.experience_level ? " / " + esc(j.experience_level) : ""}
        / <span title="source">${esc(j.source)}</span>
        ${j.posted_at ? " / " + esc(fmtDate(j.posted_at)) : ""}
        ${fmtSalary(j) ? " / " + esc(fmtSalary(j)) : ""}
      </div>
      <div class="snippet">${esc(snippet)}${j.description && j.description.length > 240 ? "..." : ""}</div>
      <div style="margin-top:6px">
        ${tags.map(t => `<span class="tag">${esc(t)}</span>`).join("")}
      </div>
    </article>`;
}

async function runSearch(page = 1) {
  currentPage = page;
  const data = new FormData(form);
  const params = new URLSearchParams();
  for (const [k, v] of data.entries()) {
    if (v !== "" && v !== false && v !== "false") params.set(k, v);
  }
  if (form.remote.checked) params.set("remote", "true"); else params.delete("remote");
  params.set("page", String(page));
  params.set("page_size", String(PAGE_SIZE));

  meta.textContent = "正在搜索...";
  results.innerHTML = "";
  pager.innerHTML = "";

  try {
    const r = await fetch(`/search?${params.toString()}`);
    if (!r.ok) throw new Error(await extractErrorMessage(r));
    const body = await r.json();
    meta.textContent = `${body.total.toLocaleString()} 个职位 / 第 ${body.page} 页`;
    results.innerHTML = body.results.map(renderJob).join("") ||
      `<p class="muted-line">没有匹配结果。可以放宽筛选条件，或通过 POST /admin/crawl 触发采集。</p>`;
    const totalPages = Math.max(1, Math.ceil(body.total / body.page_size));
    pager.innerHTML = `
      <button ${page <= 1 ? "disabled" : ""} id="prev">上一页</button>
      <span>${page} / ${totalPages}</span>
      <button ${page >= totalPages ? "disabled" : ""} id="next">下一页</button>`;
    document.getElementById("prev")?.addEventListener("click", () => runSearch(page - 1));
    document.getElementById("next")?.addEventListener("click", () => runSearch(page + 1));
  } catch (e) {
    meta.textContent = `搜索失败：${e.message}`;
  }
}

function renderStatus(status) {
  const normalized = (status || "").toLowerCase();
  const known = ["queued", "running", "retrying", "succeeded", "failed"];
  const cls = known.includes(normalized) ? `status-${normalized}` : "";
  return `<span class="status-badge ${cls}">${esc(status || "-")}</span>`;
}

function renderRunAction(run) {
  if (run.status !== "failed") return "-";
  return `<button type="button" class="action-button retry-run" data-run-id="${esc(run.run_id)}">重新执行</button>`;
}

function renderRunRow(run) {
  return `
    <tr>
      <td>${esc(run.run_id)}</td>
      <td>${esc(run.source)}</td>
      <td>${renderStatus(run.status)}</td>
      <td>${esc(dash(run.trigger_type))}</td>
      <td>${esc(dash(run.attempt_count))}</td>
      <td>${esc(dash(run.received))}</td>
      <td>${esc(dash(run.inserted))}</td>
      <td>${esc(dash(run.updated))}</td>
      <td>${esc(dash(run.duplicates))}</td>
      <td class="error-cell">${esc(dash(run.error_message))}</td>
      <td>${esc(fmtLocalDateTime(run.created_at))}</td>
      <td>${esc(fmtLocalDateTime(run.started_at))}</td>
      <td>${esc(fmtLocalDateTime(run.finished_at))}</td>
      <td>${renderRunAction(run)}</td>
    </tr>`;
}

function setRunsLoading() {
  runsStatus.textContent = "正在加载任务...";
  runsError.hidden = true;
  runsError.textContent = "";
  runsEmpty.hidden = true;
  runsBody.innerHTML = "";
  runsPager.innerHTML = "";
}

function renderRunsPager(body) {
  const totalPages = Math.max(1, Math.ceil(body.total / body.page_size));
  runsPager.innerHTML = `
    <button ${body.page <= 1 ? "disabled" : ""} id="runs-prev">上一页</button>
    <span>第 ${body.page} / ${totalPages} 页，共 ${body.total.toLocaleString()} 条</span>
    <button ${body.page >= totalPages ? "disabled" : ""} id="runs-next">下一页</button>`;
  document.getElementById("runs-prev")?.addEventListener("click", () => loadRuns(body.page - 1));
  document.getElementById("runs-next")?.addEventListener("click", () => loadRuns(body.page + 1));
}

function buildRunsParams(page) {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(RUNS_PAGE_SIZE),
  });
  if (runFilters.source) params.set("source", runFilters.source);
  if (runFilters.status) params.set("status", runFilters.status);
  return params;
}

async function loadRuns(page = 1) {
  runsPage = page;
  setRunsLoading();

  try {
    const response = await fetch(`/admin/crawl-runs?${buildRunsParams(page).toString()}`);
    if (!response.ok) throw new Error(await extractErrorMessage(response));
    const body = await response.json();
    runsLoaded = true;
    runsStatus.textContent = `已加载第 ${body.page} 页`;
    runsEmpty.hidden = body.runs.length > 0;
    runsBody.innerHTML = body.runs.map(renderRunRow).join("");
    renderRunsPager(body);
  } catch (e) {
    runsStatus.textContent = "";
    runsError.hidden = false;
    runsError.textContent = `加载采集任务失败：${e.message}`;
  }
}

async function startCrawl() {
  const source = crawlSourceSelect.value;
  if (!source) {
    setActionMessage("error", "请选择一个具体来源");
    return;
  }

  startCrawlButton.disabled = true;
  setActionMessage("loading", "正在创建采集任务...");

  try {
    const response = await fetch("/admin/crawl", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ source }),
    });
    if (!response.ok) throw new Error(await extractErrorMessage(response));
    const body = await response.json();
    const run = body.runs?.[0];
    const runId = run?.run_id ?? "-";
    setActionMessage("success", `已创建 ${source} 采集任务，run_id=${runId}`);
    await loadRuns(1);
  } catch (e) {
    setActionMessage("error", `创建采集任务失败：${e.message}`);
  } finally {
    startCrawlButton.disabled = false;
  }
}

async function retryRun(runId, button) {
  button.disabled = true;
  setActionMessage("loading", `正在重试任务 ${runId}...`);

  try {
    const response = await fetch(`/admin/crawl-runs/${encodeURIComponent(runId)}/retry`, {
      method: "POST",
    });
    if (!response.ok) throw new Error(await extractErrorMessage(response));
    const body = await response.json();
    setActionMessage("success", `已创建重试任务，run_id=${body.run_id}`);
    await loadRuns(1);
  } catch (e) {
    setActionMessage("error", `重试任务失败：${e.message}`);
    button.disabled = false;
  }
}

function switchView(viewName) {
  for (const [name, section] of Object.entries(views)) {
    section.classList.toggle("active", name === viewName);
  }
  navTabs.forEach(tab => {
    tab.classList.toggle("active", tab.dataset.view === viewName);
  });

  if (viewName === "crawl-runs") {
    loadSources();
    if (!runsLoaded) loadRuns(1);
  }
}

navTabs.forEach(tab => {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
});

refreshRuns.addEventListener("click", () => loadRuns(runsPage));

runsFilterForm.addEventListener("submit", e => {
  e.preventDefault();
  runFilters = {
    source: runsSourceFilter.value,
    status: runsStatusFilter.value,
  };
  loadRuns(1);
});

resetRunsFilter.addEventListener("click", () => {
  runsSourceFilter.value = "";
  runsStatusFilter.value = "";
  runFilters = { source: "", status: "" };
  loadRuns(1);
});

startCrawlButton.addEventListener("click", startCrawl);

runsBody.addEventListener("click", e => {
  const button = e.target.closest(".retry-run");
  if (!button) return;
  retryRun(button.dataset.runId, button);
});

form.addEventListener("submit", e => {
  e.preventDefault();
  runSearch(1);
});

runSearch(1);
