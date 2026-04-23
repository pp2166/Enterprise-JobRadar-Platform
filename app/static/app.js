const form = document.getElementById("search-form");
const results = document.getElementById("results");
const meta = document.getElementById("meta");
const pager = document.getElementById("pager");

let currentPage = 1;
const PAGE_SIZE = 20;

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

function fmtSalary(j) {
  if (!j.salary_min && !j.salary_max) return "";
  const cur = j.salary_currency || "";
  const fmt = n => n ? `${cur} ${n.toLocaleString()}` : "";
  if (j.salary_min && j.salary_max) return `${fmt(j.salary_min)} – ${fmt(j.salary_max)}`;
  return fmt(j.salary_min || j.salary_max);
}

function renderJob(j) {
  const tags = (j.tags || "").split(",").map(s => s.trim()).filter(Boolean).slice(0, 6);
  const snippet = (j.description || "").slice(0, 240);
  return `
    <article class="job">
      <h3><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></h3>
      <div class="meta">
        <strong>${esc(j.company)}</strong>
        ${j.location ? " · " + esc(j.location) : ""}
        ${j.remote ? " · remote" : ""}
        ${j.experience_level ? " · " + esc(j.experience_level) : ""}
        · <span title="source">${esc(j.source)}</span>
        ${j.posted_at ? " · " + esc(fmtDate(j.posted_at)) : ""}
        ${fmtSalary(j) ? " · " + esc(fmtSalary(j)) : ""}
      </div>
      <div class="snippet">${esc(snippet)}${j.description && j.description.length > 240 ? "…" : ""}</div>
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

  meta.textContent = "searching…";
  results.innerHTML = "";
  pager.innerHTML = "";

  try {
    const r = await fetch(`/search?${params.toString()}`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const body = await r.json();
    meta.textContent = `${body.total.toLocaleString()} jobs · page ${body.page}`;
    results.innerHTML = body.results.map(renderJob).join("") ||
      `<p style="color:#666">No matches. Try relaxing filters, or POST /admin/crawl to fetch jobs.</p>`;
    const totalPages = Math.max(1, Math.ceil(body.total / body.page_size));
    pager.innerHTML = `
      <button ${page <= 1 ? "disabled" : ""} id="prev">← prev</button>
      <span style="align-self:center;color:#666">${page} / ${totalPages}</span>
      <button ${page >= totalPages ? "disabled" : ""} id="next">next →</button>`;
    document.getElementById("prev")?.addEventListener("click", () => runSearch(page - 1));
    document.getElementById("next")?.addEventListener("click", () => runSearch(page + 1));
  } catch (e) {
    meta.textContent = `error: ${e.message}`;
  }
}

form.addEventListener("submit", e => {
  e.preventDefault();
  runSearch(1);
});

runSearch(1);
