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

// ====================================================================
// LONG-TERM SCREENER TAB
// ====================================================================

let ltAll = [];
let ltMeta = {};
let ltLoaded = false;

async function loadLongTerm() {
  if (ltLoaded) return;
  ltLoaded = true;

  let data;
  try {
    data = await fetchJSON("data/long_term_scores.json");
  } catch (e) {
    $("lt-status").innerHTML =
      `<span class="text-amber-700">데이터 파일 없음.</span> ` +
      `<a class="underline" href="https://github.com/melingo-o/danta-screener/actions/workflows/long_term_screener.yml" target="_blank">` +
      `GitHub Actions에서 long-term-screener 워크플로우 한 번 수동 실행</a> 후 새로고침.`;
    ltLoaded = false;  // allow retry
    return;
  }

  ltAll = data.stocks || [];
  ltMeta = data;

  $("lt-status").textContent = `${ltAll.length}개 종목 평가 완료 (KR ${data.kr_scored || 0} · US ${data.us_scored || 0})`;
  $("lt-updated").textContent = data.updated_at || "—";

  $("lt-stat-total").textContent = ltAll.length;
  $("lt-stat-perfect").textContent = ltAll.filter(s => s.score === 7).length;
  $("lt-stat-6plus").textContent = ltAll.filter(s => s.score >= 6).length;

  ["lt-market", "lt-min-score", "lt-sort", "lt-search"].forEach(id => {
    const evt = id === "lt-search" ? "input" : "change";
    $(id).addEventListener(evt, renderLongTermTable);
  });

  renderLongTermTable();
}

function renderLongTermTable() {
  const market = $("lt-market").value;
  const minScore = parseInt($("lt-min-score").value, 10);
  const sort = $("lt-sort").value;
  const search = ($("lt-search").value || "").toLowerCase().trim();

  let rows = ltAll.filter(s => {
    if (market !== "all" && s.market !== market) return false;
    if (s.score < minScore) return false;
    if (search && !(`${s.name} ${s.ticker}`.toLowerCase().includes(search))) return false;
    return true;
  });

  const sortKey = {
    score: s => -(s.score || 0),
    roe: s => -(s.metrics?.roe ?? -999),
    per: s => (s.metrics?.per ?? 9999),
    peg: s => (s.metrics?.peg ?? 9999),
    market_cap: s => -(s.metrics?.market_cap ?? 0),
  };
  rows.sort((a, b) => sortKey[sort](a) - sortKey[sort](b));

  $("lt-stat-filtered").textContent = rows.length;

  const fmtPct = v => (v == null || isNaN(v)) ? "—" : `${(v * 100).toFixed(1)}%`;
  const fmtNum = (v, d = 1) => (v == null || isNaN(v)) ? "—" : Number(v).toFixed(d);
  const fmtCap = v => {
    if (v == null) return "—";
    if (v >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
    if (v >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
    if (v >= 1e6) return `${(v / 1e6).toFixed(0)}M`;
    return v.toString();
  };
  const scoreClass = sc =>
    sc >= 6 ? "bg-green-100 text-green-700"
    : sc >= 4 ? "bg-blue-100 text-blue-700"
    : "bg-slate-100 text-slate-600";
  const cellClass = pass => pass === true ? "text-green-600 font-medium"
                          : pass === false ? "text-slate-400"
                          : "text-slate-400";

  const top = rows.slice(0, 200);
  $("lt-tbody").innerHTML = top.map(s => {
    const m = s.metrics || {};
    const p = s.passes || {};
    return `<tr class="hover:bg-slate-50 cursor-pointer" data-ticker="${s.ticker}">
      <td class="px-3 py-2"><span class="${scoreClass(s.score)} px-2 py-0.5 rounded font-mono text-xs">${s.score}/7</span></td>
      <td class="px-3 py-2 text-xs">${s.market === "KR" ? "🇰🇷" : "🇺🇸"}</td>
      <td class="px-3 py-2">
        <div class="font-medium">${s.name}</div>
        <div class="text-xs text-slate-500 font-mono">${s.ticker}</div>
      </td>
      <td class="px-3 py-2 text-xs text-slate-600">${s.sector || "—"}</td>
      <td class="px-3 py-2 text-right text-xs">${fmtCap(m.market_cap)}</td>
      <td class="px-3 py-2 text-right ${cellClass(p.roe_15)}">${fmtPct(m.roe)}</td>
      <td class="px-3 py-2 text-right ${cellClass(p.fcf_positive)}">${m.fcf == null ? "—" : (m.fcf > 0 ? "✓" : "✗")}</td>
      <td class="px-3 py-2 text-right ${cellClass(p.debt_safe)}">${fmtNum(m.debt_equity, 0)}</td>
      <td class="px-3 py-2 text-right ${cellClass(p.per_reasonable)}">${fmtNum(m.per, 1)}</td>
      <td class="px-3 py-2 text-right ${cellClass(p.peg_attractive)}">${fmtNum(m.peg, 2)}</td>
      <td class="px-3 py-2 text-right ${cellClass(p.revenue_growing)}">${fmtPct(m.revenue_growth)}</td>
      <td class="px-3 py-2 text-right ${cellClass(p.margin_healthy)}">${fmtPct(m.operating_margin)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="12" class="text-center py-6 text-slate-400">조건에 맞는 종목 없음</td></tr>`;

  // Wire row click
  $("lt-tbody").querySelectorAll("tr[data-ticker]").forEach(tr => {
    tr.addEventListener("click", () => openLtDetail(tr.dataset.ticker));
  });
}

function openLtDetail(ticker) {
  const s = ltAll.find(x => x.ticker === ticker);
  if (!s) return;
  const m = s.metrics || {};
  const p = s.passes || {};
  const labels = ltMeta.checklist_labels_kr || {
    roe_15: "ROE ≥ 15%",
    fcf_positive: "FCF > 0",
    debt_safe: "부채비율 ≤ 100%",
    per_reasonable: "PER 합리적 (0~30)",
    peg_attractive: "PEG ≤ 1.5",
    revenue_growing: "매출 성장 (YoY)",
    margin_healthy: "영업이익률 ≥ 5%",
  };
  const fmtPctD = (v) => v != null ? `${(v * 100).toFixed(2)}%` : "—";
  const fmtNumD = (v, d = 1) => v != null ? Number(v).toFixed(d) : "—";

  const scoreColor = s.score >= 6 ? "text-green-600" : s.score >= 4 ? "text-blue-600" : "text-slate-400";

  $("lt-detail-body").innerHTML = `
    <div class="flex items-start justify-between mb-4 gap-4">
      <div>
        <h2 class="text-xl font-bold">${s.market === "KR" ? "🇰🇷" : "🇺🇸"} ${s.name}</h2>
        <p class="text-sm text-slate-500 font-mono mt-1">${s.ticker}${s.yf_ticker !== s.ticker ? ` (${s.yf_ticker})` : ""}</p>
        <p class="text-xs text-slate-500 mt-1">${s.sector || ""} ${s.industry ? "· " + s.industry : ""}</p>
      </div>
      <div class="text-right">
        <div class="text-3xl font-bold ${scoreColor}">${s.score}/7</div>
        <div class="text-xs text-slate-500">정량 점수</div>
      </div>
    </div>

    <div class="mb-4">
      <h3 class="text-sm font-semibold mb-2 text-slate-700">✅ 정량 체크리스트</h3>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2">
        ${Object.entries(labels).map(([k, label]) => `
          <div class="flex items-center gap-2 text-sm ${p[k] ? "" : "text-slate-400"}">
            <span class="text-lg">${p[k] ? "✅" : "⬜"}</span>
            <span>${label}</span>
          </div>
        `).join("")}
      </div>
    </div>

    <div class="mb-4">
      <h3 class="text-sm font-semibold mb-2 text-slate-700">📊 주요 지표</h3>
      <div class="grid grid-cols-2 sm:grid-cols-4 gap-2 text-sm">
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">ROE</div>
          <div class="font-semibold">${fmtPctD(m.roe)}</div>
        </div>
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">PER</div>
          <div class="font-semibold">${fmtNumD(m.per, 1)}</div>
        </div>
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">PEG</div>
          <div class="font-semibold">${fmtNumD(m.peg, 2)}</div>
        </div>
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">D/E</div>
          <div class="font-semibold">${fmtNumD(m.debt_equity, 0)}</div>
        </div>
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">매출성장</div>
          <div class="font-semibold">${fmtPctD(m.revenue_growth)}</div>
        </div>
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">영업이익률</div>
          <div class="font-semibold">${fmtPctD(m.operating_margin)}</div>
        </div>
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">배당수익률</div>
          <div class="font-semibold">${fmtPctD(m.dividend_yield)}</div>
        </div>
        <div class="bg-slate-50 p-2 rounded">
          <div class="text-xs text-slate-500">배당성향</div>
          <div class="font-semibold">${fmtPctD(m.payout_ratio)}</div>
        </div>
      </div>
    </div>

    ${s.summary ? `
      <div class="mb-4">
        <h3 class="text-sm font-semibold mb-2 text-slate-700">📝 사업 설명</h3>
        <p class="text-xs text-slate-600 leading-relaxed">${s.summary}${s.summary.length >= 290 ? "..." : ""}</p>
      </div>
    ` : ""}

    <div class="mb-4 p-3 bg-amber-50 border border-amber-200 rounded">
      <h3 class="text-sm font-semibold mb-2 text-amber-800">⚠️ 정성 평가 (직접 확인)</h3>
      <ul class="text-sm text-amber-900 space-y-1 list-none">
        <li>□ 사업을 한 문장으로 설명 가능한가 — 위 사업 설명 참고</li>
        <li>□ 경제적 해자 — ROE ${fmtPctD(m.roe)}, 영업이익률 ${fmtPctD(m.operating_margin)} (안정성으로 추정)</li>
        <li>□ CEO 자본배분 — 배당성향 ${fmtPctD(m.payout_ratio)} + 자사주매입 추세 확인</li>
        <li>□ 하락 이유 일시적 vs 구조적 — 최근 뉴스 직접 확인</li>
      </ul>
    </div>

    <div class="flex justify-between items-center mt-6">
      <div class="text-xs text-slate-400">
        ${s.website ? `<a href="${s.website}" target="_blank" class="underline">공식 사이트</a>` : ""}
      </div>
      <button onclick="document.getElementById('lt-detail').classList.add('hidden')"
              class="px-4 py-2 bg-slate-100 hover:bg-slate-200 rounded text-sm">닫기</button>
    </div>
  `;
  $("lt-detail").classList.remove("hidden");
  $("lt-detail").classList.add("flex");
}

// Lazy-load long-term tab when first opened
document.querySelector('[data-tab="longterm"]').addEventListener("click", loadLongTerm);

// ---------- Boot ----------
loadDashboard().catch(e => {
  console.error("Dashboard load failed:", e);
  document.body.insertAdjacentHTML("afterbegin",
    `<div class="bg-red-100 text-red-800 p-4 text-sm">로딩 실패: ${e.message}. 콘솔 확인.</div>`);
});
