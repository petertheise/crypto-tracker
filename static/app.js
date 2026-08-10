/* Crypto Tracker frontend */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);

// if the session expires, send the user to the login page instead of failing silently
const _fetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const r = await _fetch(...args);
  if (r.status === 401) { location.href = "/login"; throw new Error("auth required"); }
  return r;
};

// escape text before it goes into innerHTML — coin names/images come from
// CoinGecko and Coinbase, so they are not trusted markup
const esc = (v) => String(v ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const fmtUSD = (v, digits) => {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const abs = Math.abs(v);
  const d = digits !== undefined ? digits : (abs >= 1000 ? 0 : abs >= 1 ? 2 : abs >= 0.01 ? 4 : 6);
  return (v < 0 ? "-$" : "$") + abs.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
};
const fmtNum = (v) => {
  if (v === null || v === undefined || isNaN(v)) return "—";
  const abs = Math.abs(v);
  const d = abs >= 1000 ? 2 : abs >= 1 ? 4 : 8;
  return v.toLocaleString("en-US", { maximumFractionDigits: d });
};
const fmtPct = (v) => (v === null || v === undefined || isNaN(v)) ? "—"
  : (v >= 0 ? "+" : "") + v.toFixed(2) + "%";
const pctClass = (v) => (v === null || v === undefined) ? "" : v >= 0 ? "pos" : "neg";

const PALETTE = ["#f5a623","#4aa3ff","#2ecc71","#e74c3c","#9b59b6","#1abc9c","#e67e22","#f1c40f",
                 "#3498db","#e91e63","#00bcd4","#8bc34a","#ff7043","#7986cb","#26a69a","#d4e157"];
Chart.defaults.color = "#8b98a9";
if (window.matchMedia("(max-width: 640px)").matches) {
  // phones: slimmer legends leave more room for the plot itself
  Chart.defaults.plugins.legend.labels.boxWidth = 16;
  Chart.defaults.plugins.legend.labels.boxHeight = 10;
  Chart.defaults.plugins.legend.labels.padding = 8;
  Chart.defaults.font.size = 10;
}
Chart.defaults.borderColor = "#2a3442";

let state = { portfolio: null, coins: [], charts: {}, pfDays: 365, coinDays: 365, fngDays: 30 };

/* ---------------------------------------------------------------- tabs */
$$(".tab").forEach((btn) => btn.addEventListener("click", () => {
  $$(".tab").forEach((b) => b.classList.remove("active"));
  $$(".tabpane").forEach((p) => p.classList.remove("active"));
  btn.classList.add("active");
  $("#tab-" + btn.dataset.tab).classList.add("active");
  if (btn.dataset.tab === "market" && !state.marketLoaded) loadMarket();
  if (btn.dataset.tab === "charts" && !state.coinChartLoaded) { loadCoinChart(1); loadCoinChart(2); }
  if (btn.dataset.tab === "stocks" && !state.stocksLoaded) { loadStocks(); loadStocksHistory(); loadStocksEach(); }
}));

function makeChart(id, cfg) {
  if (state.charts[id]) state.charts[id].destroy();
  state.charts[id] = new Chart($(id), cfg);
  return state.charts[id];
}

/* ---------------------------------------------------------------- wiring helpers */
// range button strips all behave the same: highlight the clicked button, park the
// day count on state, re-render. rangeDays() also handles data-days="ytd".
function wireRange(sel, stateKey, onChange) {
  $(sel).addEventListener("click", (e) => {
    if (e.target.tagName !== "BUTTON") return;
    $$(sel + " button").forEach((b) => b.classList.remove("active"));
    e.target.classList.add("active");
    state[stateKey] = rangeDays(e.target.dataset.days);
    onChange();
  });
}

// checkbox that remembers itself in localStorage. defaultOn flips the restore test:
// "not 0" (on unless turned off) vs "is 1" (off unless turned on).
function wireToggle(elementId, storageKey, defaultOn, onChange) {
  const el = $("#" + elementId);
  el.addEventListener("change", () => {
    localStorage.setItem(storageKey, el.checked ? "1" : "0");
    onChange();
  });
  el.checked = defaultOn
    ? localStorage.getItem(storageKey) !== "0"
    : localStorage.getItem(storageKey) === "1";
}

const getJSON = async (url) => (await fetch(url)).json();

/* ---------------------------------------------------------------- chart options */
// shared option fragments. Each call returns fresh objects so no two charts share one.
const usdTicks = (d) => ({ ticks: { callback: (v) => fmtUSD(v, d) } });
const tip = (label) => ({ callbacks: { label } });
// shell for the line/bar charts: hover mode, optional legend, one tooltip line
// formatter, thinned x axis; the y scale is passed in per chart.
const lineOpts = ({ hover = "index", legend, tooltip, maxX = 10, y }) => ({
  maintainAspectRatio: false,
  ...(hover ? { interaction: { mode: hover, intersect: false } } : {}),
  plugins: { ...(legend ? { legend } : {}), tooltip: tip(tooltip) },
  scales: { x: { ticks: { maxTicksLimit: maxX } }, y },
});
// the four doughnuts are identical: legend on the right, USD tooltip
const donutOpts = () => ({
  plugins: { legend: { position: "right" }, tooltip: tip((c) => ` ${c.label}: ${fmtUSD(c.parsed, 0)}`) },
});

/* ---------------------------------------------------------------- dashboard */
async function loadPortfolio() {
  const r = await fetch("/api/portfolio");
  const p = await r.json();
  state.portfolio = p;
  $("#asof").textContent = "Prices as of " + p.as_of + "  ·  auto-refreshes every 2 min";

  const pl = p.total_pl, plc = pctClass(pl);
  const active = p.holdings.filter((h) => !h.closed);
  const best = active.slice().sort((a, b) => (b.change_24h ?? -999) - (a.change_24h ?? -999))[0];
  $("#summary-cards").innerHTML = `
    <div class="card"><div class="label">Portfolio Value</div>
      <div class="value">${fmtUSD(p.total_value, 2)}</div></div>
    <div class="card"><div class="label">Net Invested</div>
      <div class="value">${fmtUSD(p.total_cost, 2)}</div></div>
    <div class="card"><div class="label">Profit / Loss</div>
      <div class="value ${plc}">${fmtUSD(pl, 2)}</div>
      <div class="sub ${plc}">${fmtPct(p.total_pl_pct)}</div>
      <div class="sub">realized ${fmtUSD(p.total_realized, 0)} · unrealized ${fmtUSD(p.total_unrealized, 0)}</div></div>
    <div class="card"><div class="label">Annualized Return</div>
      <div class="value ${pctClass(p.xirr)}">${p.xirr != null ? fmtPct(p.xirr) : "—"}</div>
      <div class="sub">money-weighted (XIRR)</div></div>
    <div class="card"><div class="label">Cold Storage</div>
      <div class="value">${fmtUSD(p.total_cold, 2)}</div>
      <div class="sub">${p.cold_pct != null ? p.cold_pct.toFixed(1) + "% of portfolio" : ""}</div></div>
    <div class="card"><div class="label">Positions</div>
      <div class="value">${active.length}</div>
      <div class="sub">${p.holdings.length - active.length} closed</div></div>
    <div class="card"><div class="label">Best 24h Mover</div>
      <div class="value">${best ? esc(best.symbol) : "—"}</div>
      <div class="sub ${best ? pctClass(best.change_24h) : ""}">${best ? fmtPct(best.change_24h) : ""}</div></div>`;
  // Net Worth is masked by default on every load; click the eye to reveal for this session.
  const addNetWorth = (s) => {
    const total = p.total_value + s.total_value;
    $("#summary-cards").insertAdjacentHTML("beforeend", `
    <div class="card" id="nw-card"><div class="label">Net Worth
        <span id="nw-eye" title="Show / hide" style="cursor:pointer;float:right;color:var(--muted)">&#128065;</span></div>
      <div class="value" id="nw-value">••••••</div>
      <div class="sub" id="nw-sub">hidden — tap the eye to show</div></div>`);
    const reveal = (on) => {
      state.nwShown = on;
      $("#nw-value").textContent = on ? fmtUSD(total, 0) : "••••••";
      $("#nw-sub").innerHTML = on
        ? `crypto ${fmtUSD(p.total_value, 0)} + stocks ${fmtUSD(s.total_value, 0)}`
        : "hidden — tap the eye to show";
    };
    reveal(state.nwShown === true); // stays revealed across refreshes within a session, hidden on a fresh load
    $("#nw-eye").addEventListener("click", () => reveal(!state.nwShown));
  };
  if (state.stocks) addNetWorth(state.stocks);
  else fetch("/api/stocks").then((r) => r.json())
    .then((s) => { if (s.total_value) { state.stocks = s; addNetWorth(s); } })
    .catch(() => {});

  renderHoldings();
  populateChartSelects();
  renderYearly(p.yearly);
  renderAllocation(active);
  renderPLChart(p.holdings);
  renderRebalance(active);
  renderBreakEven(p);
  loadBestWorst();
  if (!$("#tax-year").options.length) {
    $("#tax-year").innerHTML = `<option value="all">All years</option>` +
      [...p.yearly].reverse().map((y) => `<option>${y.year}</option>`).join("");
  }
}

/* ---------------------------------------------------------------- break-even & best/worst */
function renderBreakEven(p) {
  const rows = p.holdings
    .filter((h) => !h.closed && h.quantity > 1e-9 && h.net_cost > 0 && h.value >= 5 && h.price > 0)
    .map((h) => ({ ...h, need: (h.cost_avg / h.price - 1) * 100 }))
    .sort((a, b) => a.need - b.need);
  $("#be-table tbody").innerHTML = rows.map((h) => `
    <tr>
      <td>${esc(h.symbol)}</td>
      <td class="r">${fmtUSD(h.price)}</td>
      <td class="r">${fmtUSD(h.cost_avg)}</td>
      <td class="r ${h.need <= 0 ? "pos" : "neg"}">${h.need <= 0 ? "in profit ✓" : "+" + h.need.toFixed(1) + "%"}</td>
    </tr>`).join("");
  const btc = p.holdings.find((h) => h.symbol === "BTC");
  if (btc && btc.quantity > 1e-9 && btc.price > 0) {
    const needP = (p.total_cost - (p.total_value - btc.value)) / btc.quantity;
    $("#be-note").textContent = needP > btc.price
      ? `Whole portfolio breaks even if BTC reaches ${fmtUSD(needP, 0)} (+${((needP / btc.price - 1) * 100).toFixed(1)}%), with everything else at today's prices.`
      : "Portfolio is above break-even at current prices.";
  }
}

async function loadBestWorst() {
  const txs = await getJSON("/api/transactions");
  const prices = {};
  (state.portfolio?.holdings || []).forEach((h) => { prices[h.symbol.toLowerCase()] = h.price; });
  const buys = txs
    .filter((t) => t.side === "buy" && t.total >= 5 && prices[t.symbol] > 0)
    .map((t) => {
      const worth = t.quantity * prices[t.symbol];
      return { ...t, worth, ret: ((worth - t.total) / t.total) * 100 };
    })
    .sort((a, b) => b.ret - a.ret);
  if (!buys.length) return;
  const row = (t) => `
    <tr>
      <td>${esc(t.date)}</td>
      <td>${esc(t.symbol.toUpperCase())}</td>
      <td class="r">${fmtUSD(t.total, 2)}</td>
      <td class="r">${fmtUSD(t.worth, 2)}</td>
      <td class="r ${pctClass(t.ret)}">${fmtPct(t.ret)}</td>
    </tr>`;
  $("#bw-table tbody").innerHTML =
    buys.slice(0, 5).map(row).join("") +
    `<tr><td colspan="5" style="text-align:center;color:var(--muted)">· · ·</td></tr>` +
    buys.slice(-5).reverse().map(row).join("");
}

/* ---------------------------------------------------------------- rebalance preview */
function renderRebalance(active) {
  const V = active.reduce((s, h) => s + h.value, 0);
  const tbody = $("#rebal-table tbody");
  const existing = [...tbody.querySelectorAll("tr")].map((r) => r.dataset.sym).join(",");
  const want = active.map((h) => h.symbol).join(",");
  if (existing !== want) {
    // build rows once; later refreshes only update prices so typing isn't interrupted
    tbody.innerHTML = active.map((h) => {
      const t = state.targets[h.symbol] ?? +((h.value / V) * 100).toFixed(1);
      return `<tr data-sym="${esc(h.symbol)}">
        <td>${esc(h.symbol)}</td>
        <td class="r cell-value"></td>
        <td class="r cell-cur"></td>
        <td class="r"><input class="rebal-in" type="number" min="0" max="100" step="0.1" value="${t}"></td>
        <td class="r cell-target"></td>
        <td class="r cell-action"></td>
      </tr>`;
    }).join("");
  }
  tbody.querySelectorAll("tr").forEach((row) => {
    const h = active.find((x) => x.symbol === row.dataset.sym);
    if (h) row.dataset.value = h.value;
  });
  recalcRebalance();
}

function recalcRebalance() {
  const rows = [...$("#rebal-table tbody").querySelectorAll("tr")];
  const V = rows.reduce((s, r) => s + (+r.dataset.value || 0), 0);
  let sum = 0;
  rows.forEach((r) => {
    const v = +r.dataset.value || 0;
    const pct = parseFloat(r.querySelector("input").value) || 0;
    sum += pct;
    const tgt = (pct / 100) * V;
    const diff = tgt - v;
    r.querySelector(".cell-value").textContent = fmtUSD(v, 0);
    r.querySelector(".cell-cur").textContent = (V ? (v / V) * 100 : 0).toFixed(1) + "%";
    r.querySelector(".cell-target").textContent = fmtUSD(tgt, 0);
    const a = r.querySelector(".cell-action");
    if (Math.abs(diff) < 5) a.innerHTML = "—";
    else if (diff > 0) a.innerHTML = `<span class="pos">Buy ${fmtUSD(diff, 0)}</span>`;
    else a.innerHTML = `<span class="neg">Sell ${fmtUSD(-diff, 0)}</span>`;
  });
  const sumEl = $("#rebal-sum");
  const ok = Math.abs(sum - 100) <= 0.5;
  sumEl.textContent = `Targets total ${sum.toFixed(1)}%` + (ok ? " ✓" : " — should be 100%");
  sumEl.className = ok ? "pos" : "neg";
}

$("#rebal-table").addEventListener("input", (e) => {
  if (!e.target.classList.contains("rebal-in")) return;
  const sym = e.target.closest("tr").dataset.sym;
  state.targets[sym] = parseFloat(e.target.value) || 0;
  recalcRebalance();
});
$("#rebal-current").addEventListener("click", () => {
  const rows = [...$("#rebal-table tbody").querySelectorAll("tr")];
  const V = rows.reduce((s, r) => s + (+r.dataset.value || 0), 0);
  rows.forEach((r) => {
    const pct = +(((+r.dataset.value || 0) / V) * 100).toFixed(1);
    r.querySelector("input").value = pct;
    state.targets[r.dataset.sym] = pct;
  });
  recalcRebalance();
});
$("#rebal-equal").addEventListener("click", () => {
  const rows = [...$("#rebal-table tbody").querySelectorAll("tr")];
  const pct = +(100 / rows.length).toFixed(1);
  rows.forEach((r) => {
    r.querySelector("input").value = pct;
    state.targets[r.dataset.sym] = pct;
  });
  recalcRebalance();
});
$("#rebal-save").addEventListener("click", async () => {
  const targets = {};
  $$("#rebal-table tbody tr").forEach((r) => {
    targets[r.dataset.sym] = parseFloat(r.querySelector("input").value) || 0;
  });
  await fetch("/api/targets", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ targets }),
  });
  const msg = $("#rebal-msg");
  msg.textContent = "Saved ✓";
  msg.className = "pos";
  setTimeout(() => (msg.textContent = ""), 3000);
});

function renderPLChart(holdings) {
  const rows = holdings.filter((h) => Math.abs(h.pl) > 0.5)
    .sort((a, b) => b.pl - a.pl);
  makeChart("#pl-chart", {
    type: "bar",
    data: {
      labels: rows.map((h) => h.symbol),
      datasets: [{
        data: rows.map((h) => h.pl),
        backgroundColor: rows.map((h) => (h.pl >= 0 ? "rgba(46,204,113,.75)" : "rgba(231,76,60,.75)")),
        borderWidth: 0,
      }],
    },
    options: {
      indexAxis: "y",
      maintainAspectRatio: false,
      plugins: { legend: { display: false }, tooltip: tip((c) => " " + fmtUSD(c.parsed.x, 2)) },
      scales: { x: usdTicks(0) },
    },
  });
}

async function loadMonthlyChart() {
  const rows = await getJSON("/api/history/monthly");
  makeChart("#monthly-chart", {
    type: "bar",
    data: {
      labels: rows.map((r) => r.ym),
      datasets: [
        { label: "Invested", data: rows.map((r) => r.invested),
          backgroundColor: "rgba(245,166,35,.8)", stack: "s" },
        { label: "Sold", data: rows.map((r) => -r.proceeds),
          backgroundColor: "rgba(231,76,60,.8)", stack: "s" },
      ],
    },
    options: lineOpts({
      tooltip: (c) => ` ${c.dataset.label}: ${fmtUSD(Math.abs(c.parsed.y), 2)}`,
      maxX: 12, y: usdTicks(0),
    }),
  });
}

async function loadAllocationHistory() {
  const d = await getJSON("/api/history/allocation?days=" + (state.allocDays || 365));
  makeChart("#alloc-time-chart", {
    type: "line",
    data: {
      labels: d.dates,
      datasets: d.series.map((s, i) => {
        const col = s.label === "Other" ? "#8b98a9" : PALETTE[i % PALETTE.length];
        return { label: s.label, data: s.values, fill: true, stack: "v",
          backgroundColor: col + "cc", borderColor: col,
          pointRadius: 0, borderWidth: 1, tension: .2 };
      }),
    },
    options: lineOpts({
      tooltip: (c) => ` ${c.dataset.label}: ${fmtUSD(c.parsed.y, 0)}`,
      y: { stacked: true, min: 0, ...usdTicks(0) },
    }),
  });
}
wireRange("#alloc-range", "allocDays", loadAllocationHistory);

function renderHoldings() {
  const showClosed = $("#show-closed").checked;
  const hideDust = $("#hide-dust").checked;
  const rows = state.portfolio.holdings.filter((h) =>
    (showClosed || !h.closed) && (!hideDust || Math.abs(h.value) >= 5));
  $("#holdings-table tbody").innerHTML = rows.map((h) => `
    <tr>
      <td><div class="coin-cell">${h.image ? `<img src="${esc(h.image)}">` : ""}
        <div>${esc(h.name)}<div class="sym">${esc(h.symbol)}</div></div></div></td>
      <td><span class="badge">${esc(h.category)}</span></td>
      <td class="r">${fmtNum(h.quantity)}</td>
      <td class="r">${h.cold_pct > 0.05 ? h.cold_pct.toFixed(0) + "%" : "—"}</td>
      <td class="r">${h.cost_avg !== null ? fmtUSD(h.cost_avg) : "—"}</td>
      <td class="r">${fmtUSD(h.price)}</td>
      <td class="r ${pctClass(h.change_24h)}">${fmtPct(h.change_24h)}</td>
      <td class="r ${pctClass(h.change_7d)}">${fmtPct(h.change_7d)}</td>
      <td class="r">${fmtUSD(h.value, 2)}</td>
      <td class="r">${fmtUSD(h.net_cost, 2)}</td>
      <td class="r ${pctClass(h.pl)}">${fmtUSD(h.pl, 2)}</td>
      <td class="r ${pctClass(h.pl)}">${fmtPct(h.pl_pct)}</td>
    </tr>`).join("");
}
$("#show-closed").addEventListener("change", renderHoldings);
wireToggle("hide-dust", "hideDust", false, renderHoldings);

function renderYearly(years) {
  $("#yearly-table tbody").innerHTML = years.map((y) => {
    const net = y.invested - y.proceeds;
    return `<tr><td>${y.year}</td>
      <td class="r">${fmtUSD(y.invested, 2)}</td>
      <td class="r">${fmtUSD(y.proceeds, 2)}</td>
      <td class="r">${fmtUSD(net, 2)}</td>
      <td class="r ${pctClass(y.realized)}">${y.realized ? fmtUSD(y.realized, 2) : "—"}</td></tr>`;
  }).join("");
}

function renderAllocation(active) {
  const byCoin = active.filter((h) => h.value > 0.5).sort((a, b) => b.value - a.value);
  makeChart("#alloc-chart", {
    type: "doughnut",
    data: { labels: byCoin.map((h) => h.symbol),
      datasets: [{ data: byCoin.map((h) => h.value), backgroundColor: PALETTE, borderWidth: 0 }] },
    options: donutOpts(),
  });
  const cats = {};
  active.forEach((h) => { if (h.value > 0) cats[h.category] = (cats[h.category] || 0) + h.value; });
  const names = Object.keys(cats);
  makeChart("#cat-chart", {
    type: "doughnut",
    data: { labels: names,
      datasets: [{ data: names.map((n) => cats[n]), backgroundColor: PALETTE.slice(4), borderWidth: 0 }] },
    options: donutOpts(),
  });
}

async function loadPortfolioHistory() {
  const r = await fetch("/api/history/portfolio?days=" + state.pfDays);
  state.pfData = await r.json();
  renderPfChart();
  renderDrawdown(state.pfData);
}

function renderPfChart() {
  const data = state.pfData || [];
  const datasets = [
    { label: "Value", data: data.map((d) => d.value), borderColor: "#f5a623",
      backgroundColor: "rgba(245,166,35,.08)", fill: true, pointRadius: 0, tension: .2, borderWidth: 2 },
    { label: "Net invested", data: data.map((d) => d.cost), borderColor: "#4aa3ff",
      borderDash: [5, 4], pointRadius: 0, tension: 0, borderWidth: 1.5 },
  ];
  if ($("#show-bench").checked) {
    datasets.push({ label: "Same $ into BTC only", data: data.map((d) => d.bench), borderColor: "#2ecc71",
      borderDash: [3, 3], pointRadius: 0, tension: .15, borderWidth: 1.5 });
  }
  if ($("#show-bench-eth").checked) {
    datasets.push({ label: "Same $ into ETH only", data: data.map((d) => d.bench_eth), borderColor: "#9b59b6",
      borderDash: [3, 3], pointRadius: 0, tension: .15, borderWidth: 1.5 });
  }
  if ($("#show-bench-spx").checked) {
    datasets.push({ label: "Same $ into S&P 500", data: data.map((d) => d.bench_spx), borderColor: "#f1c40f",
      borderDash: [3, 3], pointRadius: 0, tension: .15, borderWidth: 1.5 });
  }
  if ($("#show-bench-ndq").checked) {
    datasets.push({ label: "Same $ into Nasdaq", data: data.map((d) => d.bench_ndq), borderColor: "#e91e63",
      borderDash: [3, 3], pointRadius: 0, tension: .15, borderWidth: 1.5 });
  }
  makeChart("#pf-chart", {
    type: "line",
    data: { labels: data.map((d) => d.date), datasets },
    options: lineOpts({
      tooltip: (c) => ` ${c.dataset.label}: ${fmtUSD(c.parsed.y, 0)}`,
      y: usdTicks(0),
    }),
  });
}
wireToggle("show-bench", "showBench", true, renderPfChart);
wireToggle("show-bench-eth", "showBenchEth", true, renderPfChart);
wireToggle("show-bench-spx", "showBenchSpx", true, renderPfChart);
wireToggle("show-bench-ndq", "showBenchNdq", true, renderPfChart);

function renderDrawdown(data) {
  let peak = 0;
  const dd = data.map((d) => {
    peak = Math.max(peak, d.value);
    return peak > 0 ? ((d.value - peak) / peak) * 100 : 0;
  });
  const now = dd[dd.length - 1];
  $("#dd-now").textContent = now !== undefined
    ? `currently ${now.toFixed(1)}% below the ${state.pfDays}-day peak` : "";
  makeChart("#dd-chart", {
    type: "line",
    data: { labels: data.map((d) => d.date),
      datasets: [{ data: dd, borderColor: "#e74c3c", backgroundColor: "rgba(231,76,60,.18)",
        fill: true, pointRadius: 0, tension: .2, borderWidth: 1.5 }] },
    options: lineOpts({
      hover: null, legend: { display: false },
      tooltip: (c) => " " + c.parsed.y.toFixed(1) + "%",
      y: { max: 0, ticks: { callback: (v) => v + "%" } },
    }),
  });
}
wireRange("#pf-range", "pfDays", loadPortfolioHistory);

/* ---------------------------------------------------------------- transactions */
async function loadCoins() {
  const r = await fetch("/api/coins");
  state.coins = await r.json();
  const opts = state.coins.map((c) => `<option value="${esc(c.symbol)}">${esc(c.symbol.toUpperCase())} — ${esc(c.name)}</option>`).join("");
  $("#tx-symbol").innerHTML = opts;
  $("#cv-from").innerHTML = opts;
  $("#cv-to").innerHTML = opts;
  $("#tf-symbol").innerHTML = opts;
  $("#al-symbol").innerHTML = opts;
  $("#tx-filter").innerHTML = `<option value="">All coins</option>` + opts;
  populateChartSelects();
  $("#coins-table tbody").innerHTML = state.coins.map((c) => `
    <tr><td>${esc(c.symbol.toUpperCase())}</td><td>${esc(c.name)}</td><td>${esc(c.coingecko_id)}</td>
    <td><span class="badge">${esc(c.category)}</span></td></tr>`).join("");
}

async function loadTransactions() {
  const sym = $("#tx-filter").value;
  const r = await fetch("/api/transactions" + (sym ? "?symbol=" + encodeURIComponent(sym) : ""));
  const txs = await r.json();
  state.txs = txs; // kept so the edit button can look a row up by id
  // live prices come from the portfolio payload (includes closed positions)
  const prices = {};
  (state.portfolio?.holdings || []).forEach((h) => { prices[h.symbol.toLowerCase()] = h.price; });
  $("#tx-table tbody").innerHTML = txs.map((t) => {
    const now = prices[t.symbol];
    let pl = null, ret = null;
    if (now != null && now > 0 && t.total > 0) {
      const valueNow = t.quantity * now;
      // buys: gain from holding; sells: gain from having sold vs holding
      pl = t.side === "buy" ? valueNow - t.total : t.total - valueNow;
      ret = (pl / t.total) * 100;
    }
    return `
    <tr>
      <td>${esc(t.date)}</td>
      <td>${esc(t.symbol.toUpperCase())}</td>
      <td><span class="badge ${esc(t.side)}">${esc(t.side.toUpperCase())}</span></td>
      <td class="r">${fmtNum(t.quantity)}</td>
      <td class="r">${fmtUSD(t.price)}</td>
      <td class="r">${fmtUSD(t.total, 2)}</td>
      <td class="r">${now != null && now > 0 ? fmtUSD(now) : "—"}</td>
      <td class="r ${pctClass(pl)}">${pl !== null ? fmtUSD(pl, 2) : "—"}</td>
      <td class="r ${pctClass(ret)}">${fmtPct(ret)}</td>
      <td class="tx-notes">${esc(t.notes)}</td>
      <td>
        <button class="small" onclick="editTx(${+t.id})">edit</button>
        <button class="small danger" onclick="deleteTx(${+t.id})">delete</button>
      </td>
    </tr>`;
  }).join("");
}
$("#tx-filter").addEventListener("change", loadTransactions);

window.editTx = (id) => {
  const t = (state.txs || []).find((x) => x.id === id);
  if (!t) return;
  $("#tx-details").open = true; // the form is collapsed by default now
  $("#tx-id").value = t.id;
  $("#tx-side").value = t.side;
  $("#tx-date").value = t.date;
  $("#tx-symbol").value = t.symbol;
  $("#tx-qty").value = t.quantity;
  $("#tx-price").value = t.price;
  $("#tx-fee").value = t.fee || 0;
  $("#tx-total").value = t.total;
  $("#tx-exchange").value = t.exchange || "";
  $("#tx-notes").value = t.notes || "";
  $("#tx-form-title").textContent = "Edit Transaction #" + t.id;
  $("#tx-submit").textContent = "Save";
  $("#tx-cancel").classList.remove("hidden");
  window.scrollTo({ top: 0, behavior: "smooth" });
};

window.deleteTx = async (id) => {
  if (!confirm("Delete transaction #" + id + "?")) return;
  await fetch("/api/transactions/" + id, { method: "DELETE" });
  loadTransactions(); loadPortfolio(); loadPortfolioHistory();
};

// price auto-populates from total ÷ quantity (how the old spreadsheet did it)
function autoPrice() {
  const qty = parseFloat($("#tx-qty").value);
  const total = parseFloat($("#tx-total").value);
  if (qty > 0 && total > 0) {
    $("#tx-price").value = total / qty;
  }
}
$("#tx-qty").addEventListener("input", autoPrice);
$("#tx-total").addEventListener("input", autoPrice);

function resetTxForm() {
  $("#tx-form").reset();
  $("#tx-id").value = "";
  $("#tx-date").value = new Date().toISOString().slice(0, 10);
  $("#cv-date").value = new Date().toISOString().slice(0, 10);
  $("#tf-date").value = new Date().toISOString().slice(0, 10);
  $("#tx-exchange").value = "COINBASE";
  $("#tx-form-title").textContent = "Add Transaction";
  $("#tx-submit").textContent = "Add";
  $("#tx-cancel").classList.add("hidden");
}
$("#tx-cancel").addEventListener("click", resetTxForm);

$("#tx-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#tx-id").value;
  const body = {
    date: $("#tx-date").value,
    symbol: $("#tx-symbol").value,
    side: $("#tx-side").value,
    quantity: $("#tx-qty").value,
    price: $("#tx-price").value,
    fee: $("#tx-fee").value || 0,
    total: $("#tx-total").value === "" ? null
      : $("#tx-total").value,
    exchange: $("#tx-exchange").value,
    notes: $("#tx-notes").value,
  };
  if (id && body.total === null) {
    body.total = $("#tx-side").value === "buy"
      ? +body.quantity * +body.price + +body.fee
      : +body.quantity * +body.price - +body.fee;
  }
  const r = await fetch(id ? "/api/transactions/" + id : "/api/transactions", {
    method: id ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const res = await r.json();
  const msg = $("#tx-msg");
  if (res.error) { msg.textContent = res.error; msg.className = "neg"; return; }
  msg.textContent = id ? "Saved ✓" : "Added ✓";
  msg.className = "pos";
  setTimeout(() => (msg.textContent = ""), 3000);
  resetTxForm();
  loadTransactions(); loadPortfolio(); loadPortfolioHistory();
});

/* ---------------------------------------------------------------- cold storage transfers */
async function loadTransfers() {
  const rows = await getJSON("/api/transfers");
  $("#tf-table tbody").innerHTML = rows.map((t) => `
    <tr>
      <td>${esc(t.date)}</td>
      <td>${esc(t.symbol.toUpperCase())}</td>
      <td>${t.direction === "to_cold"
        ? '<span class="badge">Coinbase → Cold</span>'
        : '<span class="badge">Cold → Coinbase</span>'}</td>
      <td class="r">${fmtNum(t.quantity)}</td>
      <td>${esc(t.notes)}</td>
      <td><button class="small danger" onclick="deleteTransfer(${t.id})">delete</button></td>
    </tr>`).join("");
}

window.deleteTransfer = async (id) => {
  if (!confirm("Delete transfer #" + id + "?")) return;
  await fetch("/api/transfers/" + id, { method: "DELETE" });
  loadTransfers(); loadPortfolio();
};

$("#tf-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#tf-msg");
  const r = await fetch("/api/transfers", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      date: $("#tf-date").value,
      symbol: $("#tf-symbol").value,
      quantity: $("#tf-qty").value,
      direction: $("#tf-direction").value,
      notes: $("#tf-notes").value,
    }),
  });
  const res = await r.json();
  if (res.error) { msg.textContent = res.error; msg.className = "neg"; return; }
  msg.textContent = "Transfer recorded ✓";
  msg.className = "pos";
  setTimeout(() => (msg.textContent = ""), 4000);
  $("#tf-qty").value = $("#tf-notes").value = "";
  loadTransfers(); loadPortfolio();
});

/* ---------------------------------------------------------------- conversions */
$("#cv-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#cv-msg");
  const r = await fetch("/api/convert", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      date: $("#cv-date").value,
      from_symbol: $("#cv-from").value,
      from_qty: $("#cv-from-qty").value,
      to_symbol: $("#cv-to").value,
      to_qty: $("#cv-to-qty").value,
      usd: $("#cv-usd").value,
      notes: $("#cv-notes").value,
    }),
  });
  const res = await r.json();
  if (res.error) { msg.textContent = res.error; msg.className = "neg"; return; }
  msg.textContent = "Conversion recorded ✓" +
    (res.adjustment ? ` (plus a ${res.adjustment.toFixed(8)} zero-cost balance adjustment)` : "");
  msg.className = "pos";
  setTimeout(() => (msg.textContent = ""), 6000);
  $("#cv-from-qty").value = $("#cv-to-qty").value = $("#cv-usd").value = $("#cv-notes").value = "";
  loadTransactions(); loadPortfolio(); loadPortfolioHistory();
});

/* ---------------------------------------------------------------- coin chart */
// two price panels: 1 = active holdings, 2 = retired coins
const COIN_PANELS = {
  1: { select: "#chart-coin",  canvas: "#coin-chart",  stats: "#coin-chart-stats",  stateKey: "coinDays"  },
  2: { select: "#chart-coin2", canvas: "#coin-chart2", stats: "#coin-chart-stats2", stateKey: "coinDays2" },
};

// active coins -> chart 1, retired (closed) coins -> chart 2. keeps selection on refresh.
function populateChartSelects() {
  if (!state.coins) return;
  const holdings = state.portfolio?.holdings || [];
  const closedIds = new Set(holdings.filter((h) => h.closed).map((h) => h.coingecko_id));
  const openIds = new Set(holdings.filter((h) => !h.closed).map((h) => h.coingecko_id));
  const opt = (c) => `<option value="${esc(c.coingecko_id)}">${esc(c.name)} (${esc(c.symbol.toUpperCase())})</option>`;
  // until portfolio data arrives, everything goes in chart 1
  let active = state.coins.filter((c) => openIds.has(c.coingecko_id) || (!openIds.size && !closedIds.size));
  const retired = state.coins.filter((c) => closedIds.has(c.coingecko_id));
  // actives ordered by position value (largest first) so the default chart is a real holding
  const valueOf = (c) => holdings.find((h) => h.coingecko_id === c.coingecko_id)?.value ?? 0;
  active = active.sort((a, b) => valueOf(b) - valueOf(a));
  const keep = (sel, list) => {
    const el = $(sel);
    const prev = el.value;
    el.innerHTML = list.map(opt).join("");
    if (prev && [...el.options].some((o) => o.value === prev)) el.value = prev;
  };
  keep("#chart-coin", active);
  keep("#chart-coin2", retired);
}

async function loadCoinChart(panel = 1) {
  const cfg = COIN_PANELS[panel];
  state.coinChartLoaded = true;
  const id = $(cfg.select).value;
  if (!id) { $(cfg.stats).innerHTML = "<span>No coins in this group.</span>"; return; }
  const coin = state.coins.find((c) => c.coingecko_id === id);
  const [data, txs] = await Promise.all([
    fetch(`/api/history/coin/${id}?days=${state[cfg.stateKey] || 365}`).then((r) => r.json()),
    coin ? fetch(`/api/transactions?symbol=${coin.symbol}`).then((r) => r.json()) : [],
  ]);
  const labels = data.map((d) => d.date);
  const inRange = new Set(labels);
  const markers = (side) => txs
    .filter((t) => t.side === side && inRange.has(t.date) && t.price > 0) // skip $0-basis adjustments
    .map((t) => ({ x: t.date, y: t.price, r: Math.min(4 + Math.sqrt(t.total) / 2, 12), tx: t }));
  const buys = markers("buy"), sells = markers("sell");
  const datasets = [
    { label: "Price", type: "line", data: data.map((d) => d.price), borderColor: "#4aa3ff",
      backgroundColor: "rgba(74,163,255,.08)", fill: true, pointRadius: 0, tension: .2,
      borderWidth: 2, order: 3 },
  ];
  if (buys.length) datasets.push({ label: "My buys", type: "bubble", data: buys,
    backgroundColor: "rgba(46,204,113,.85)", borderColor: "#2ecc71", order: 1 });
  if (sells.length) datasets.push({ label: "My sells", type: "bubble", data: sells,
    backgroundColor: "rgba(231,76,60,.85)", borderColor: "#e74c3c", order: 1 });
  // average cost line (only while the position is open)
  const h = (state.portfolio?.holdings || []).find((x) => x.coingecko_id === id);
  if (h && h.cost_avg !== null && h.quantity > 1e-9) {
    datasets.push({ label: "My avg cost", type: "line", data: labels.map(() => h.cost_avg),
      borderColor: "#f5a623", borderDash: [6, 5], pointRadius: 0, borderWidth: 1.5, order: 2 });
  }
  makeChart(cfg.canvas, {
    data: { labels, datasets },
    options: {
      maintainAspectRatio: false,  // fill the chart-box, don't overflow it
      interaction: { mode: "nearest", intersect: false },
      plugins: { legend: { display: true },
        tooltip: { callbacks: { label: (c) => {
          const t = c.raw && c.raw.tx;
          if (t) return ` ${t.side === "buy" ? "Bought" : "Sold"} ${fmtNum(t.quantity)} for ${fmtUSD(t.total, 2)} @ ${fmtUSD(t.price)}`;
          return ` ${c.dataset.label}: ${fmtUSD(c.parsed.y)}`;
        } } } },
      scales: { y: { ticks: { callback: (v) => fmtUSD(v) } }, x: { ticks: { maxTicksLimit: 10 } } },
    },
  });
  if (data.length) {
    const prices = data.map((d) => d.price);
    const first = prices[0], last = prices[prices.length - 1];
    const chg = ((last - first) / first) * 100;
    $(cfg.stats).innerHTML = `
      <span>Current: <b>${fmtUSD(last)}</b></span>
      <span>Period change: <b class="${pctClass(chg)}">${fmtPct(chg)}</b></span>
      <span>High: <b>${fmtUSD(Math.max(...prices))}</b></span>
      <span>Low: <b>${fmtUSD(Math.min(...prices))}</b></span>`;
  } else {
    $(cfg.stats).innerHTML = "<span>No history available yet — try again in a minute (API rate limit).</span>";
  }
}
$("#chart-coin").addEventListener("change", () => loadCoinChart(1));
$("#chart-coin2").addEventListener("change", () => loadCoinChart(2));
wireRange("#coin-range", "coinDays", () => loadCoinChart(1));
wireRange("#coin-range2", "coinDays2", () => loadCoinChart(2));

/* ---------------------------------------------------------------- stocks */
function rangeDays(d) {
  return d === "ytd"
    ? Math.max(2, Math.ceil((Date.now() - new Date(new Date().getFullYear(), 0, 1)) / 86400000))
    : +d;
}
async function loadStocks() {
  state.stocksLoaded = true;
  const s = await getJSON("/api/stocks");
  state.stocks = s;
  const gc = pctClass(s.total_gain);
  const gainPct = s.total_invested ? (s.total_gain / s.total_invested) * 100 : null;
  $("#stk-cards").innerHTML = `
    <div class="card"><div class="label">Stock Portfolio</div>
      <div class="value">${fmtUSD(s.total_value, 2)}</div></div>
    <div class="card"><div class="label">Today</div>
      <div class="value ${pctClass(s.day_change)}">${fmtUSD(s.day_change, 2)}</div>
      <div class="sub ${pctClass(s.day_change)}">${fmtPct(s.day_pct)}</div></div>
    <div class="card"><div class="label">This Week</div>
      <div class="value ${pctClass(s.week_change)}">${fmtUSD(s.week_change, 2)}</div>
      <div class="sub ${pctClass(s.week_change)}">${fmtPct(s.week_pct)}</div></div>
    <div class="card"><div class="label">Invested</div>
      <div class="value">${fmtUSD(s.total_invested, 2)}</div></div>
    <div class="card"><div class="label">Total Gain</div>
      <div class="value ${gc}">${fmtUSD(s.total_gain, 2)}</div>
      <div class="sub ${gc}">${fmtPct(gainPct)}</div></div>
    <div class="card"><div class="label">Est. Annual Income</div>
      <div class="value">${fmtUSD(s.total_income, 0)}</div>
      <div class="sub">dividends & interest</div></div>` +
    (s.fees ? `
    <div class="card"><div class="label">Advisory Fees (YTD)</div>
      <div class="value neg">${fmtUSD(s.fees.ytd, 2)}</div>
      <div class="sub">${s.fees.quarter_label || "Q"}: ${fmtUSD(s.fees.quarter, 0)} · ~${fmtUSD(s.fees.expected_annual, 0)}/yr at ${s.fees.rate}%</div></div>` : "");
  $("#stk-asof").textContent = s.as_of ? `positions as of ${s.as_of} import` : "";
  // by-portfolio table + donut
  $("#stk-accts tbody").innerHTML = s.accounts.map((a) => `
    <tr>
      <td>${esc(a.account)}</td>
      <td class="r">${fmtUSD(a.value, 2)}</td>
      <td class="r ${pctClass(a.day_change)}">${fmtUSD(a.day_change, 2)}<div class="sub ${pctClass(a.day_change)}">${fmtPct(a.day_pct)}</div></td>
      <td class="r ${pctClass(a.week_change)}">${fmtUSD(a.week_change, 2)}<div class="sub ${pctClass(a.week_change)}">${fmtPct(a.week_pct)}</div></td>
      <td class="r">${fmtUSD(a.invested, 2)}</td>
      <td class="r ${pctClass(a.gain)}">${fmtUSD(a.gain, 2)}</td>
      <td class="r">${a.income ? fmtUSD(a.income, 0) : "—"}</td>
    </tr>`).join("");
  makeChart("#stk-acct-donut", {
    type: "doughnut",
    data: { labels: s.accounts.map((a) => a.account),
      datasets: [{ data: s.accounts.map((a) => a.value), backgroundColor: PALETTE, borderWidth: 0 }] },
    options: donutOpts(),
  });
  // portfolio filter (populate once)
  const filt = $("#stk-acct-filter");
  if (filt.options.length <= 1) {
    filt.innerHTML = `<option value="">All portfolios</option>` +
      s.accounts.map((a) => `<option>${esc(a.account)}</option>`).join("");
  }
  renderStockHoldings();
  const agg = aggregateStocks(s.holdings);
  const top = agg.filter((h) => h.value > 0);
  makeChart("#stk-alloc", {
    type: "doughnut",
    data: { labels: top.map((h) => h.symbol),
      datasets: [{ data: top.map((h) => h.value), backgroundColor: PALETTE, borderWidth: 0 }] },
    options: donutOpts(),
  });
}

function aggregateStocks(holdings) {
  const agg = {};
  for (const h of holdings) {
    const a = agg[h.symbol] || (agg[h.symbol] = { ...h, quantity: 0, value: 0, invested: 0, income: 0, gain: 0, day_change: 0, week_change: 0, n: 0 });
    a.quantity += h.quantity; a.value += h.value; a.invested += h.invested;
    a.income += h.income; a.gain += h.gain; a.n += 1;
    a.day_change += h.day_change || 0; a.week_change += h.week_change || 0;
  }
  return Object.values(agg).map((a) => ({ ...a,
    gain_pct: a.invested ? (a.gain / a.invested) * 100 : null,
    account: a.n > 1 ? a.n + " portfolios" : a.account,
  })).sort((x, y) => y.value - x.value);
}

function renderStockHoldings() {
  const s = state.stocks;
  if (!s) return;
  const sel = $("#stk-acct-filter").value;
  const rows = sel
    ? s.holdings.filter((h) => h.account === sel).sort((x, y) => y.value - x.value)
    : aggregateStocks(s.holdings);
  $("#stk-table tbody").innerHTML = rows.map((h) => `
    <tr>
      <td style="white-space:normal">${esc(h.name)}</td>
      <td>${h.symbol}</td>
      <td><span class="badge">${esc(h.account)}</span></td>
      <td class="r">${fmtNum(h.quantity)}</td>
      <td class="r">${fmtUSD(h.price)}${h.live ? "" : " *"}</td>
      <td class="r">${fmtUSD(h.value, 2)}</td>
      <td class="r ${pctClass(h.day_change)}">${fmtUSD(h.day_change, 2)}<div class="sub ${pctClass(h.day_change)}">${fmtPct(h.day_pct)}</div></td>
      <td class="r ${pctClass(h.week_change)}">${fmtUSD(h.week_change, 2)}<div class="sub ${pctClass(h.week_change)}">${fmtPct(h.week_pct)}</div></td>
      <td class="r">${fmtUSD(h.invested, 2)}</td>
      <td class="r ${pctClass(h.gain)}">${fmtUSD(h.gain, 2)}</td>
      <td class="r ${pctClass(h.gain)}">${fmtPct(h.gain_pct)}</td>
      <td class="r">${h.income ? fmtUSD(h.income, 0) : "—"}</td>
    </tr>`).join("");
}
$("#stk-acct-filter").addEventListener("change", renderStockHoldings);

async function loadStocksHistory() {
  const days = state.stkDays || 365;
  const d = await getJSON("/api/history/stocks?days=" + days);
  makeChart("#stk-chart", {
    type: "line",
    data: { labels: d.points.map((p) => p.date),
      datasets: [{ label: "Positions value", data: d.points.map((p) => p.value),
        borderColor: "#2ecc71", backgroundColor: "rgba(46,204,113,.08)",
        fill: true, pointRadius: 0, tension: .2, borderWidth: 2 }] },
    options: lineOpts({
      hover: null, legend: { display: false },
      tooltip: (c) => " " + fmtUSD(c.parsed.y, 0),
      y: usdTicks(0),
    }),
  });
}
async function loadStocksEach() {
  const days = state.stkeDays || 365;
  const d = await getJSON("/api/history/stocks/each?days=" + days);
  makeChart("#stke-chart", {
    type: "line",
    data: {
      labels: d.dates,
      datasets: d.series.map((s, i) => {
        const first = s.values.find((v) => v != null);
        return {
          label: s.symbol,
          data: s.values.map((v) => (v != null && first ? ((v / first) - 1) * 100 : null)),
          borderColor: PALETTE[i % PALETTE.length],
          pointRadius: 0, borderWidth: 1.8, tension: .2, spanGaps: true,
        };
      }),
    },
    options: lineOpts({
      hover: "nearest", legend: { position: "bottom" },
      tooltip: (c) => ` ${c.dataset.label}: ${fmtPct(c.parsed.y)}`,
      y: { ticks: { callback: (v) => (v >= 0 ? "+" : "") + v + "%" } },
    }),
  });
}
wireRange("#stke-range", "stkeDays", loadStocksEach);
wireRange("#stk-range", "stkDays", loadStocksHistory);
$("#stk-upload").addEventListener("click", async () => {
  const msg = $("#stk-msg");
  const files = $("#stk-file").files;
  if (!files.length) { msg.textContent = "Choose the statement PDFs first."; msg.className = "neg"; return; }
  msg.textContent = `Parsing ${files.length} statement${files.length > 1 ? "s" : ""}…`;
  msg.className = "";
  const fd = new FormData();
  [...files].forEach((f) => fd.append("files", f));
  const res = await (await fetch("/api/stocks/import_statements", { method: "POST", body: fd })).json();
  if (res.error) { msg.textContent = res.error; msg.className = "neg"; return; }
  const ok = res.applied.length;
  const bad = res.results.filter((r) => !r.ok);
  msg.textContent = `${ok} account${ok === 1 ? "" : "s"} updated ✓` + (bad.length ? ` — ${bad.length} skipped` : "");
  msg.className = bad.length ? "neg" : "pos";
  $("#stk-import-results").innerHTML = res.results.map((r) => r.ok
    ? `<li class="pos">✓ ${esc(r.account)} — reconciled to ${fmtUSD(r.closing, 2)}</li>`
    : `<li class="neg">✗ ${esc(r.account || r.file)} — ${esc(r.error)}</li>`).join("");
  $("#stk-file").value = "";
  loadStocks(); loadStocksHistory(); loadStocksEach();
});

/* ---------------------------------------------------------------- market */
const FNG_COLORS = (v) => v <= 25 ? "#e74c3c" : v <= 45 ? "#e67e22" : v <= 55 ? "#f1c40f" : v <= 75 ? "#8bc34a" : "#2ecc71";

async function loadMarket() {
  const r = await fetch("/api/market");
  const m = await r.json();
  const g = m.global;
  state.marketLoaded = !!g; // retry on next tab visit if the API was rate-limited
  const btcPrice = (state.portfolio?.holdings || []).find((h) => h.symbol === "BTC")?.price;
  let extraCards = "";
  if (m.btc_fees) {
    const f = m.btc_fees;
    const usd = btcPrice ? fmtUSD((f.hourFee * 140 / 1e8) * btcPrice, 2) : "?";
    extraCards += `
    <div class="card" title="What it costs to send Bitcoin on the blockchain right now — for example, moving BTC from Coinbase to your cold wallet. The network charges by transaction size (satoshis per virtual byte), not by dollar amount, so moving $100 or $10,000 costs the same. When this is low, it's a cheap time to move coins.">
      <div class="label">BTC Transfer Fee</div>
      <div class="value">${f.hourFee} sat/vB</div>
      <div class="sub">moving BTC to cold storage now ≈ ${usd}</div></div>`;
  }
  if (m.halving) {
    extraCards += `
    <div class="card"><div class="label">Next BTC Halving</div>
      <div class="value">${(m.halving.days / 365).toFixed(1)}y</div>
      <div class="sub">~${m.halving.estimated_date} (${m.halving.days} days)</div></div>`;
  }
  $("#market-cards").innerHTML = (g ? `
    <div class="card"><div class="label">Total Market Cap</div>
      <div class="value">${fmtUSD(g.total_market_cap / 1e12, 2)}T</div>
      <div class="sub ${pctClass(g.market_cap_change_24h)}">${fmtPct(g.market_cap_change_24h)} (24h)</div></div>
    <div class="card"><div class="label">24h Volume</div>
      <div class="value">${fmtUSD(g.total_volume / 1e9, 1)}B</div></div>
    <div class="card"><div class="label">BTC Dominance</div>
      <div class="value">${g.btc_dominance.toFixed(1)}%</div></div>
    <div class="card"><div class="label">ETH Dominance</div>
      <div class="value">${g.eth_dominance.toFixed(1)}%</div></div>` :
    `<div class="card"><div class="label">Market data unavailable</div></div>`) + extraCards;
  loadRatioChart();
  loadDominance();

  if (m.fear_greed) {
    const f = m.fear_greed;
    $("#fng-gauge").innerHTML = `
      <div class="fng-value" style="color:${FNG_COLORS(f.value)}">${+f.value}</div>
      <div class="fng-label">${esc(f.label)}</div>`;
    state.fngHistory = f.history; // full history, chronological (one point per day)
    renderFngChart();
    if (!state.btcMap) {
      // BTC daily closes for the overlay + long-term trend chart
      fetch("/api/history/btc?days=4000").then((r) => r.json()).then((rows) => {
        state.btcRows = rows;
        state.btcMap = Object.fromEntries(rows.map((r) => [r.date, r.price]));
        renderFngChart();
        renderBtcMA();
      });
    }
    if (!state.stabRows) {
      fetch("/api/stablecoins").then((r) => r.json()).then((rows) => {
        state.stabRows = rows;
        renderStabChart();
        if (rows.length > 30) {
          const cur = rows[rows.length - 1].mcap;
          const prev = rows[rows.length - 31].mcap;
          const chg = ((cur - prev) / prev) * 100;
          $("#market-cards").insertAdjacentHTML("beforeend", `
            <div class="card"><div class="label">Stablecoin Supply</div>
              <div class="value">${fmtUSD(cur / 1e9, 1)}B</div>
              <div class="sub ${pctClass(chg)}">${fmtPct(chg)} (30d)</div></div>`);
        }
      });
    }
  } else {
    $("#fng-gauge").innerHTML = `<div class="fng-label">Fear & Greed unavailable</div>`;
  }

  $("#trending-table tbody").innerHTML = (m.trending || []).map((t, i) => `
    <tr><td>${i + 1}</td>
      <td><div class="coin-cell">${t.thumb ? `<img src="${esc(t.thumb)}">` : ""}${esc(t.name)} <span class="sym">${esc(t.symbol)}</span></div></td>
      <td class="r">${esc(t.rank ?? "—")}</td>
      <td class="r">${t.price != null ? fmtUSD(+t.price) : "—"}</td>
      <td class="r ${pctClass(t.change_24h)}">${fmtPct(t.change_24h)}</td></tr>`).join("");
}

function renderFngChart() {
  const hist = (state.fngHistory || []).slice(-state.fngDays);
  const datasets = [{ label: "Fear & Greed", data: hist.map((h) => h.value),
    borderColor: "#f5a623", pointRadius: 0, tension: .3, borderWidth: 2, yAxisID: "y" }];
  const scales = { y: { min: 0, max: 100 }, x: { ticks: { maxTicksLimit: 8 } } };
  if (state.btcMap) {
    let last = null; // carry the last known close over any gap days
    datasets.push({ label: "BTC price", yAxisID: "y1",
      data: hist.map((h) => (state.btcMap[h.date] != null ? (last = state.btcMap[h.date]) : last)),
      borderColor: "#4aa3ff", pointRadius: 0, tension: .2, borderWidth: 1.5 });
    scales.y1 = { position: "right", grid: { drawOnChartArea: false },
      ticks: { callback: (v) => fmtUSD(v, 0) } };
  }
  makeChart("#fng-chart", {
    type: "line",
    data: { labels: hist.map((h) => h.date), datasets },
    options: {
      interaction: { mode: "index", intersect: false },
      plugins: { legend: { display: !!state.btcMap },
        tooltip: { callbacks: { label: (c) =>
          " " + c.dataset.label + ": " + (c.dataset.yAxisID === "y1" ? fmtUSD(c.parsed.y, 0) : c.parsed.y) } } },
      scales,
    },
  });
}
wireRange("#fng-range", "fngDays", renderFngChart);

function movingAvg(vals, w) {
  const out = new Array(vals.length).fill(null);
  let sum = 0;
  for (let i = 0; i < vals.length; i++) {
    sum += vals[i];
    if (i >= w) sum -= vals[i - w];
    if (i >= w - 1) out[i] = sum / w;
  }
  return out;
}

function renderBtcMA() {
  if (!state.btcRows || !state.btcRows.length) return;
  const rows = state.btcRows;
  const prices = rows.map((r) => r.price);
  const ma200d = movingAvg(prices, 200);
  const ma200w = movingAvg(prices, 1400); // 200 weeks of daily closes
  const days = state.btcMaDays || 1095;
  const s = Math.max(0, rows.length - days);
  makeChart("#btc-ma-chart", {
    type: "line",
    data: {
      labels: rows.slice(s).map((r) => r.date),
      datasets: [
        { label: "BTC", data: prices.slice(s), borderColor: "#4aa3ff",
          pointRadius: 0, borderWidth: 2, tension: .2 },
        { label: "200-day MA", data: ma200d.slice(s), borderColor: "#f5a623",
          borderDash: [6, 4], pointRadius: 0, borderWidth: 1.5, tension: .2 },
        { label: "200-week MA", data: ma200w.slice(s), borderColor: "#2ecc71",
          borderDash: [2, 3], pointRadius: 0, borderWidth: 1.5, tension: .2 },
      ],
    },
    options: lineOpts({
      tooltip: (c) => ` ${c.dataset.label}: ${c.parsed.y != null ? fmtUSD(c.parsed.y, 0) : "—"}`,
      maxX: 8, y: usdTicks(0),
    }),
  });
}
wireRange("#btcma-range", "btcMaDays", renderBtcMA);

function renderStabChart() {
  if (!state.stabRows || !state.stabRows.length) return;
  const days = state.stabDays || 1095;
  const rows = state.stabRows.slice(-days);
  makeChart("#stab-chart", {
    type: "line",
    data: { labels: rows.map((r) => r.date),
      datasets: [{ label: "Total stablecoin market cap", data: rows.map((r) => r.mcap),
        borderColor: "#1abc9c", backgroundColor: "rgba(26,188,156,.12)",
        fill: true, pointRadius: 0, borderWidth: 2, tension: .2 }] },
    options: lineOpts({
      hover: null, legend: { display: false },
      tooltip: (c) => " " + fmtUSD(c.parsed.y / 1e9, 1) + "B",
      maxX: 8, y: { ticks: { callback: (v) => fmtUSD(v / 1e9, 0) + "B" } },
    }),
  });
}
wireRange("#stab-range", "stabDays", renderStabChart);

async function loadRatioChart() {
  const days = state.ratioDays || 1095;
  const rows = await getJSON("/api/history/ratio?days=" + days);
  makeChart("#ratio-chart", {
    type: "line",
    data: { labels: rows.map((r) => r.date),
      datasets: [{ data: rows.map((r) => r.ratio), borderColor: "#9b59b6",
        backgroundColor: "rgba(155,89,182,.10)", fill: true, pointRadius: 0, borderWidth: 2, tension: .2 }] },
    options: lineOpts({
      hover: null, legend: { display: false },
      tooltip: (c) => " " + c.parsed.y.toFixed(5) + " BTC per ETH",
      maxX: 8, y: { ticks: { callback: (v) => v.toFixed(3) } },
    }),
  });
}
wireRange("#ratio-range", "ratioDays", loadRatioChart);

async function loadDominance() {
  const rows = await getJSON("/api/history/dominance");
  const note = $("#dom-note");
  if (rows.length < 2) {
    note.classList.remove("hidden");
    note.textContent = rows.length
      ? `Collecting: 1 reading so far (${rows[0].pct.toFixed(1)}% on ${rows[0].date}). The line appears once there are a few days of data.`
      : "No readings yet — one is recorded each day the app fetches market data.";
    return;
  }
  note.classList.add("hidden");
  makeChart("#dom-chart", {
    type: "line",
    data: { labels: rows.map((r) => r.date),
      datasets: [{ data: rows.map((r) => r.pct), borderColor: "#f5a623",
        backgroundColor: "rgba(245,166,35,.10)", fill: true, pointRadius: 2, borderWidth: 2, tension: .2 }] },
    options: lineOpts({
      hover: null, legend: { display: false },
      tooltip: (c) => " " + c.parsed.y.toFixed(2) + "%",
      maxX: 8, y: { ticks: { callback: (v) => v + "%" } },
    }),
  });
}

/* ---------------------------------------------------------------- price alerts */
async function loadAlerts() {
  const rows = await getJSON("/api/alerts");
  $("#al-table tbody").innerHTML = rows.map((a) => `
    <tr style="${a.active ? "" : "opacity:.5"}">
      <td>${esc(a.symbol.toUpperCase())}</td>
      <td>${esc(a.condition)}</td>
      <td class="r">${fmtUSD(a.price)}</td>
      <td>${a.active ? '<span class="badge">watching</span>'
                     : `<span class="badge sell">triggered ${esc(a.triggered_at)}</span>`}</td>
      <td><button class="small danger" onclick="deleteAlert(${a.id})">delete</button></td>
    </tr>`).join("") ||
    `<tr><td colspan="5" class="loading">No alerts yet — add one above.</td></tr>`;
}

window.deleteAlert = async (id) => {
  await fetch("/api/alerts/" + id, { method: "DELETE" });
  loadAlerts();
};

$("#al-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#al-msg");
  const r = await fetch("/api/alerts", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol: $("#al-symbol").value,
      condition: $("#al-cond").value,
      price: $("#al-price").value,
    }),
  });
  const res = await r.json();
  if (res.error) { msg.textContent = res.error; msg.className = "neg"; return; }
  msg.textContent = "Alert set ✓";
  msg.className = "pos";
  setTimeout(() => (msg.textContent = ""), 3000);
  $("#al-price").value = "";
  loadAlerts();
});

/* ---------------------------------------------------------------- coinbase sync */
async function loadCbStatus() {
  const s = await getJSON("/api/coinbase/status");
  const el = $("#cb-status");
  const table = $("#cb-balances");
  if (!s.connected) {
    el.textContent = "Not connected — put coinbase_key.json (a view-only API key) in the app folder.";
    table.classList.add("hidden");
    return;
  }
  el.textContent = "Connected (view-only key). " +
    (s.last_sync ? `Last sync: ${s.last_sync}.` : "Never synced yet.") +
    " Auto-syncs every 6 hours; new buys, sells, converts, rewards and sends are imported automatically.";
  if (s.balances) {
    table.classList.remove("hidden");
    table.querySelector("tbody").innerHTML = s.balances.map((b) => `
      <tr>
        <td>${esc(b.symbol)}</td>
        <td class="r">${fmtNum(b.app_hot)}</td>
        <td class="r">${fmtNum(b.coinbase)}</td>
        <td class="r">${b.ok ? '<span class="pos">✓</span>' : '<span class="neg">mismatch</span>'}</td>
      </tr>`).join("");
  } else if (s.balance_error) {
    el.textContent += " (Balance check failed: " + s.balance_error + ")";
  }
}
$("#cb-sync").addEventListener("click", async () => {
  const msg = $("#cb-msg");
  msg.textContent = "Syncing…";
  msg.className = "";
  const r = await (await fetch("/api/coinbase/sync", { method: "POST" })).json();
  if (r.error) { msg.textContent = r.error; msg.className = "neg"; return; }
  const i = r.imported;
  msg.textContent = `Done ✓ imported ${i.buys} buys, ${i.sells} sells, ${i.transfers} transfers` +
    (r.warnings && r.warnings.length ? " — " + r.warnings.join(" ") : "");
  msg.className = r.warnings && r.warnings.length ? "neg" : "pos";
  loadCbStatus(); loadPortfolio(); loadTransactions(); loadTransfers(); loadPortfolioHistory();
});

/* ---------------------------------------------------------------- passkeys (Face ID) */
const _b64uToBuf = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));
const _bufToB64u = (b) => btoa(String.fromCharCode(...new Uint8Array(b)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

async function loadPasskeys() {
  const rows = await getJSON("/api/passkey/list");
  $("#pk-table tbody").innerHTML = rows.length ? rows.map((p) => `
    <tr>
      <td>${esc(p.device_name)}</td>
      <td>${esc(p.created)}</td>
      <td>${esc(p.last_used || "never")}</td>
      <td><button class="small danger" onclick="deletePasskey(${+p.id})">remove</button></td>
    </tr>`).join("") : `<tr><td colspan="4" class="loading">No devices yet — add one below.</td></tr>`;
  if (!window.PublicKeyCredential) {
    $("#pk-add").disabled = true;
    $("#pk-msg").textContent = "This browser doesn't support passkeys.";
    $("#pk-msg").className = "neg";
  }
}

window.deletePasskey = async (id) => {
  if (!confirm("Remove this passkey? That device will need the password to sign in.")) return;
  await fetch("/api/passkey/" + id, { method: "DELETE" });
  loadPasskeys();
};

$("#pk-add").addEventListener("click", async () => {
  const msg = $("#pk-msg");
  msg.textContent = "Follow the prompt…"; msg.className = "";
  try {
    const opts = await (await fetch("/api/passkey/register/options", { method: "POST" })).json();
    if (opts.error) { msg.textContent = opts.error; msg.className = "neg"; return; }
    opts.challenge = _b64uToBuf(opts.challenge);
    opts.user.id = _b64uToBuf(opts.user.id);
    (opts.excludeCredentials || []).forEach((c) => { c.id = _b64uToBuf(c.id); });
    const cred = await navigator.credentials.create({ publicKey: opts });
    const payload = {
      device_name: $("#pk-name").value.trim() || "This device",
      credential: {
        id: cred.id, rawId: _bufToB64u(cred.rawId), type: cred.type,
        response: {
          clientDataJSON: _bufToB64u(cred.response.clientDataJSON),
          attestationObject: _bufToB64u(cred.response.attestationObject),
        },
        clientExtensionResults: cred.getClientExtensionResults(),
      },
    };
    const res = await (await fetch("/api/passkey/register/verify", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })).json();
    if (res.ok) {
      msg.textContent = "Added ✓ — you can now sign in with Face ID on this device.";
      msg.className = "pos";
      $("#pk-name").value = "";
      loadPasskeys();
    } else { msg.textContent = res.error || "Failed"; msg.className = "neg"; }
  } catch (e) {
    if (e.name === "NotAllowedError") { msg.textContent = "Cancelled."; msg.className = "neg"; }
    else { msg.textContent = e.message; msg.className = "neg"; }
  }
});

/* ---------------------------------------------------------------- backups & tax report */
async function loadBackupInfo() {
  const b = await getJSON("/api/backup");
  const where = b.icloud ? "iCloud Drive (Crypto Tracker Backups)" : b.dir;
  $("#backup-info").textContent = b.count
    ? `${b.count} backup${b.count > 1 ? "s" : ""} in ${where} — latest: ${b.last}. A new one is made daily while the app runs.`
    : `Backups save to ${where} automatically every day while the app runs. None yet — make the first one now.`;
}
$("#backup-now").addEventListener("click", async () => {
  const msg = $("#backup-msg");
  const res = await (await fetch("/api/backup", { method: "POST" })).json();
  msg.textContent = res.ok ? "Backed up ✓" : (res.error || "Backup failed");
  msg.className = res.ok ? "pos" : "neg";
  setTimeout(() => (msg.textContent = ""), 4000);
  loadBackupInfo();
});
$("#tax-dl").addEventListener("click", () => {
  const y = $("#tax-year").value;
  location.href = "/api/export/realized" + (y === "all" ? "" : "?year=" + y);
});
$("#tax-8949-dl").addEventListener("click", () => {
  const y = $("#tax-year").value;
  location.href = "/api/export/tax8949" + (y === "all" ? "" : "?year=" + y);
});

/* ---------------------------------------------------------------- to-do list */
async function loadTodos() {
  const rows = await getJSON("/api/todos");
  $("#todo-table tbody").innerHTML = rows.map((t) => `
    <tr style="${t.done ? "opacity:.45" : ""}">
      <td><input type="checkbox" ${t.done ? "checked" : ""} onchange="toggleTodo(${t.id})"></td>
      <td style="white-space:normal; ${t.done ? "text-decoration:line-through" : ""}">${esc(t.text)}</td>
      <td>${esc(t.created)}</td>
      <td><button class="small danger" onclick="deleteTodo(${t.id})">delete</button></td>
    </tr>`).join("") || `<tr><td colspan="4" class="loading">Nothing here yet — add your first idea above.</td></tr>`;
}

window.toggleTodo = async (id) => {
  await fetch("/api/todos/" + id, { method: "PUT" });
  loadTodos();
};
window.deleteTodo = async (id) => {
  if (!confirm("Delete this to-do?")) return;
  await fetch("/api/todos/" + id, { method: "DELETE" });
  loadTodos();
};
$("#todo-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = $("#todo-text").value.trim();
  if (!text) return;
  await fetch("/api/todos", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  $("#todo-text").value = "";
  loadTodos();
});

/* ---------------------------------------------------------------- coins tab */
let searchTimer;
$("#coin-search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(async () => {
    const q = $("#coin-search").value.trim();
    if (!q) { $("#coin-results").innerHTML = ""; return; }
    $("#coin-results").innerHTML = `<div class="loading">Searching…</div>`;
    const r = await fetch("/api/search?q=" + encodeURIComponent(q));
    const results = await r.json();
    state.searchResults = results; // looked up by index when "+ Track" is clicked
    $("#coin-results").innerHTML = results.map((c, i) => `
      <div class="coin-result">
        ${c.thumb ? `<img src="${esc(c.thumb)}">` : ""}
        <div class="grow">${esc(c.name)} <span class="sym">${esc(c.symbol)}</span>
          ${c.market_cap_rank ? `<span class="badge">rank ${esc(c.market_cap_rank)}</span>` : ""}</div>
        <select id="cat-${i}">
          <option>Core</option><option>AI</option><option>Infra</option><option>Meme</option>
          <option selected>Other</option>
        </select>
        <button class="small" onclick="addCoin(${i})">+ Track</button>
      </div>`).join("") || `<div class="loading">No results</div>`;
  }, 450);
});

window.addCoin = async (i) => {
  const c = (state.searchResults || [])[i];
  if (!c) return;
  const category = $("#cat-" + i) ? $("#cat-" + i).value : "Other";
  await fetch("/api/coins", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: c.symbol.toLowerCase(), coingecko_id: c.id, name: c.name, category }),
  });
  $("#coin-search").value = "";
  $("#coin-results").innerHTML = "";
  await loadCoins();
};

/* ---------------------------------------------------------------- settings */
$("#pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#pw-msg");
  if ($("#pw-new").value !== $("#pw-new2").value) {
    msg.textContent = "New passwords don't match.";
    msg.className = "neg";
    return;
  }
  const r = await fetch("/api/change_password", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current: $("#pw-current").value, new: $("#pw-new").value }),
  });
  const res = await r.json();
  if (res.error) { msg.textContent = res.error; msg.className = "neg"; return; }
  msg.textContent = "Password changed ✓";
  msg.className = "pos";
  $("#pw-form").reset();
});
$("#net-url").textContent = "http://" + location.hostname + ":" + (location.port || 80);

/* ---------------------------------------------------------------- init */
async function init() {
  resetTxForm();
  state.targets = await fetch("/api/targets").then((r) => r.json()).catch(() => ({}));
  await loadCoins();
  await loadPortfolio();
  loadTransactions();
  loadTransfers();
  loadTodos();
  loadBackupInfo();
  loadCbStatus();
  loadAlerts();
  loadPasskeys();
  loadPortfolioHistory();
  loadMonthlyChart();
  loadAllocationHistory();
  setInterval(async () => { await loadPortfolio(); loadTransactions(); }, 120000); // refresh prices every 2 min
}
init();
