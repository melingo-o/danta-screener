// Static dashboard for danta-screener.
// Fetches CSV/JSON from GitHub raw (same repo, main branch) — no backend.

const RAW = "https://raw.githubusercontent.com/melingo-o/danta-screener/main";

const $ = (id) => document.getElementById(id);

// ---------- Tab switching ----------
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll(".tab-btn").forEach(b => {
      const active = b === btn;
      b.classList.toggle("border-blue-500", active);
      b.classList.toggle("border-transparent", !active);
      b.classList.toggle("text-slate-500", !active);
      b.classList.toggle("font-medium", active);
    });
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.add("hidden"));
    document.getElementById(`tab-${tab}`).classList.remove("hidden");
  });
});

// ---------- Fetch helpers ----------
async function fetchCSV(path) {
  const url = `${RAW}/${path}?t=${Date.now()}`; // cache bust
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed: ${url} (${res.status})`);
  const text = await res.text();
  return new Promise((resolve, reject) => {
    Papa.parse(text, {
      header: true,
      dynamicTyping: true,
      skipEmptyLines: true,
      complete: r => resolve(r.data),
      error: reject,
    });
  });
}

async function fetchJSON(path) {
  const res = await fetch(`${RAW}/${path}?t=${Date.now()}`);
  if (!res.ok) throw new Error(`Failed: ${path}`);
  return res.json();
}

// Find newest backtest summary file by listing data/ via GitHub contents API.
async function fetchLatestBacktestSummary() {
  try {
    const res = await fetch(`https://api.github.com/repos/melingo-o/danta-screener/contents/data?t=${Date.now()}`);
    if (!res.ok) throw new Error(`contents API ${res.status}`);
    const list = await res.json();
    const summaries = list
      .filter(f => f.name && f.name.startsWith("backtest_summary_") && f.name.endsWith(".csv"))
      .sort((a, b) => (a.name < b.name ? 1 : -1));
    if (!summaries.length) return [];
    return fetchCSV(`data/${summaries[0].name}`);
  } catch (e) {
    console.warn("Could not list backtest files, falling back to hardcoded path:", e);
    return fetchCSV("data/backtest_summary_20260513.csv").catch(() => []);
  }
}

// ---------- Render utilities ----------
function fmtPct(v, decimals = 2) {
  if (v == null || isNaN(v)) return "—";
  const sign = v >= 0 ? "+" : "";
  return `${sign}${(v * 100).toFixed(decimals)}%`;
}

// KR market convention: red = up, blue = down
function colorClassForReturn(v) {
  if (v == null || isNaN(v)) return "text-slate-400";
  return v > 0 ? "text-red-600" : v < 0 ? "text-blue-600" : "text-slate-600";
}

function isHit(v) {
  return v === true || v === "True" || v === "true";
}

// ---------- Main loader ----------
async function loadDashboard() {
  let picks = [], results = [], backtestSummary = [], scannerState = {};

  // Parallel fetches; each individually fault-tolerant.
  [picks, results, backtestSummary, scannerState] = await Promise.all([
    fetchCSV("data/journal/picks.csv").catch(e => { console.warn("picks.csv:", e); return []; }),
    fetchCSV("data/journal/results.csv").catch(e => { console.warn("results.csv:", e); return []; }),
    fetchLatestBacktestSummary(),
    fetchJSON("data/scanner_state.json").catch(e => { console.warn("scanner_state.json:", e); return {}; }),
  ]);

  renderStatsCards(picks, results, backtestSummary);
  renderCumulativeChart(results);
  renderStrategiesChart(backtestSummary);
  renderDriversChart(picks);
  renderTodayAlerted(scannerState);
  renderPicksTable(picks, results);
}

// ---------- Stats Cards ----------
function renderStatsCards(picks, results, backtestSummary) {
  const totalResults = results.length;
  const hits = results.filter(r => isHit(r.hit_gross)).length;
  const hitRate = totalResults ? hits / totalResults : null;
  $("stat-hitrate").textContent = hitRate != null ? `${(hitRate * 100).toFixed(1)}%` : "—";
  $("stat-hitrate-sub").textContent = `${totalResults}건 누적 (${hits} hit)`;

  const meanPred = picks.length
    ? picks.reduce((a, p) => a + (Number(p.predicted_gap) || 0), 0) / picks.length
    : null;
  $("stat-pred").textContent = meanPred != null ? fmtPct(meanPred, 2) : "—";

  const meanAct = totalResults
    ? results.reduce((a, r) => a + (Number(r.actual_gap) || 0), 0) / totalResults
    : null;
  $("stat-actual").textContent = meanAct != null ? fmtPct(meanAct, 2) : "—";

  const tier1 = backtestSummary.find(r => r.strategy === "tier1");
  $("stat-sharpe").textContent = tier1 && tier1.annualized_sharpe != null
    ? Number(tier1.annualized_sharpe).toFixed(2)
    : "—";
}

// ---------- Cumulative gap chart ----------
function renderCumulativeChart(results) {
  const dailyByDate = {};
  results.forEach(r => {
    const d = r.date;
    const g = Number(r.actual_gap);
    if (d && !isNaN(g)) {
      (dailyByDate[d] = dailyByDate[d] || []).push(g);
    }
  });
  const dates = Object.keys(dailyByDate).sort();
  const dailyMeans = dates.map(d => {
    const arr = dailyByDate[d];
    return arr.reduce((a, b) => a + b, 0) / arr.length;
  });

  let cum = 1;
  const cumulative = dailyMeans.map(d => { cum *= (1 + d); return (cum - 1) * 100; });

  new Chart($("chart-cumulative"), {
    type: "line",
    data: {
      labels: dates,
      datasets: [{
        label: "누적 실현 갭 (%)",
        data: cumulative,
        borderColor: "rgb(59, 130, 246)",
        backgroundColor: "rgba(59, 130, 246, 0.15)",
        fill: true,
        tension: 0.25,
        pointRadius: 2,
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { ticks: { callback: v => v.toFixed(1) + "%" } },
        x: { ticks: { maxRotation: 0, autoSkip: true } },
      },
    },
  });
}

// ---------- Backtest strategies bar chart ----------
function renderStrategiesChart(backtestSummary) {
  const strats = backtestSummary.filter(r =>
    r.cum_return_gross_pct != null && r.strategy && !r.strategy.includes("random") && !r.strategy.includes("universe")
  );
  const baselines = backtestSummary.filter(r =>
    r.strategy && (r.strategy.includes("random") || r.strategy.includes("universe"))
  );
  const allRows = [...strats, ...baselines];
  if (!allRows.length) return;

  new Chart($("chart-strategies"), {
    type: "bar",
    data: {
      labels: allRows.map(s => s.strategy),
      datasets: [
        {
          label: "Gross %",
          data: allRows.map(s => Number(s.cum_return_gross_pct ?? s.mean_cum_return_pct ?? s.cum_return_pct ?? 0)),
          backgroundColor: "rgba(59, 130, 246, 0.75)",
        },
        {
          label: "Net %",
          data: allRows.map(s => Number(s.cum_return_net_pct ?? 0)),
          backgroundColor: "rgba(34, 197, 94, 0.75)",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      scales: {
        y: { ticks: { callback: v => v.toFixed(0) + "%" } },
      },
    },
  });
}

// ---------- Primary driver pie ----------
function renderDriversChart(picks) {
  const driverCounts = {};
  picks.forEach(p => {
    const d = p.primary_driver;
    if (d) driverCounts[d] = (driverCounts[d] || 0) + 1;
  });
  const sorted = Object.entries(driverCounts).sort((a, b) => b[1] - a[1]);
  if (!sorted.length) return;

  new Chart($("chart-drivers"), {
    type: "doughnut",
    data: {
      labels: sorted.map(e => e[0]),
      datasets: [{
        data: sorted.map(e => e[1]),
        backgroundColor: [
          "#3b82f6", "#22c55e", "#f59e0b", "#ef4444", "#8b5cf6",
          "#ec4899", "#06b6d4", "#84cc16", "#f97316", "#6366f1",
          "#14b8a6", "#a855f7", "#fb7185", "#fbbf24",
        ],
      }],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { position: "right", labels: { boxWidth: 14, font: { size: 11 } } } },
    },
  });
}

// ---------- Today's alerted list ----------
function renderTodayAlerted(state) {
  const alerted = Array.isArray(state.alerted) ? state.alerted : [];
  const date = state.date || "—";
  const html = alerted.length
    ? `<div class="mb-2 text-xs text-slate-500">scanner_state.json (${date}) · ${alerted.length}건</div>
       <div class="flex flex-wrap gap-2">${alerted.map(t =>
         `<span class="px-2 py-1 bg-slate-100 rounded text-xs font-mono">${t}</span>`
       ).join("")}</div>`
    : `<div class="text-slate-400">${date} · 알람 기록 없음</div>`;
  $("today-alerted").innerHTML = html;
}

// ---------- Recent picks table (join picks + results) ----------
function renderPicksTable(picks, results) {
  const key = (r) => `${r.date}|${r.ticker6}`;
  const resultMap = new Map(results.map(r => [key(r), r]));
  const merged = picks.map(p => {
    const r = resultMap.get(key(p)) || {};
    return { ...p, actual_gap: r.actual_gap, hit_gross: r.hit_gross };
  });
  // Newest first by date, then by rank
  merged.sort((a, b) => {
    if (a.date < b.date) return 1;
    if (a.date > b.date) return -1;
    return (a.rank || 0) - (b.rank || 0);
  });
  const recent = merged.slice(0, 50);

  $("picks-tbody").innerHTML = recent.map(r => {
    const pred = Number(r.predicted_gap);
    const act = Number(r.actual_gap);
    const predClass = colorClassForReturn(pred);
    const actClass = colorClassForReturn(act);
    const hit = r.hit_gross == null ? null : isHit(r.hit_gross);
    const hitCell = hit == null ? "—" : (hit ? "✅" : "❌");
    const predStr = isFinite(pred) ? `${(pred * 100).toFixed(2)}%` : "—";
    const actStr = isFinite(act) ? `${(act * 100).toFixed(2)}%` : "—";
    return `<tr class="hover:bg-slate-50">
      <td class="py-2 pr-3 font-mono text-xs text-slate-500">${r.date || ""}</td>
      <td class="pr-3">${r.rank ?? ""}</td>
      <td class="pr-3 font-medium">${r.name || ""}</td>
      <td class="pr-3 font-mono text-xs text-slate-500">${r.ticker6 || ""}</td>
      <td class="pr-3 text-right ${predClass}">${predStr}</td>
      <td class="pr-3 text-right ${actClass}">${actStr}</td>
      <td class="pr-3 text-center">${hitCell}</td>
      <td class="pr-3 text-xs text-slate-600">${r.primary_driver || ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="text-center py-6 text-slate-400">데이터 없음</td></tr>`;
}

// ---------- Boot ----------
loadDashboard().catch(e => {
  console.error("Dashboard load failed:", e);
  document.body.insertAdjacentHTML("afterbegin",
    `<div class="bg-red-100 text-red-800 p-4 text-sm">로딩 실패: ${e.message}. 콘솔 확인.</div>`);
});
