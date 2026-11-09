// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

document.addEventListener("DOMContentLoaded", () => {
  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  function unescapeHtmlEntities(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#039;/g, "'")
      .replace(/&amp;/g, "&");
  }

  function splitTopLevelItems(rawVal) {
    if (Array.isArray(rawVal)) {
      return rawVal
        .map((x) => String(x === null || x === undefined ? "" : x).trim())
        .filter(Boolean);
    }
    const text = String(
      rawVal === null || rawVal === undefined ? "" : rawVal,
    ).trim();
    if (!text) return [];
    const out = [];
    let buf = "";
    let depth = 0;
    let quoteChar = null;
    let escaped = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text.charAt(i);
      if (escaped) {
        buf += ch;
        escaped = false;
        continue;
      }
      if (quoteChar !== null) {
        buf += ch;
        if (ch === "\\") {
          escaped = true;
        } else if (ch === quoteChar) {
          quoteChar = null;
        }
        continue;
      }
      if (ch === "'" || ch === '"') {
        quoteChar = ch;
        buf += ch;
      } else if (ch === "(" || ch === "[" || ch === "{") {
        depth += 1;
        buf += ch;
      } else if (ch === ")" || ch === "]" || ch === "}") {
        depth = Math.max(0, depth - 1);
        buf += ch;
      } else if (ch === "," && depth === 0) {
        const piece = buf.trim();
        if (piece) out.push(piece);
        buf = "";
      } else {
        buf += ch;
      }
    }
    const tail = buf.trim();
    if (tail) out.push(tail);
    return out;
  }

  window.splitTopLevelItems = splitTopLevelItems;

  // ==========================================
  // ADK LIVE SYNC POLLING ENGINE (BigQuery Enabled)
  // ==========================================
  const adkLiveTableBody = document.getElementById("adk-live-table-body");
  const adkSessionSelect = document.getElementById("adk-session-select");
  const adkAppSelect = document.getElementById("adk-app-select");

  // Main Tab Navigation Switcher
  const tabBtnTelemetry = document.getElementById("tab-btn-telemetry");
  const tabBtnBq = document.getElementById("tab-btn-bq");
  const tabPaneTelemetry = document.getElementById("tab-pane-telemetry");
  const tabPaneBq = document.getElementById("tab-pane-bq");
  let activeMainTab = "telemetry";
  let configuredBqBadgeText = "";

  function switchTab(activeTab) {
    activeMainTab = activeTab;
    [tabBtnTelemetry, tabBtnBq].forEach((btn) => {
      if (btn) btn.classList.remove("active");
    });
    [tabPaneTelemetry, tabPaneBq].forEach((pane) => {
      if (pane) {
        pane.classList.add("hidden");
        pane.style.display = "none";
      }
    });

    if (activeTab === "telemetry" && tabBtnTelemetry && tabPaneTelemetry) {
      tabBtnTelemetry.classList.add("active");
      tabPaneTelemetry.classList.remove("hidden");
      tabPaneTelemetry.style.display = "block";
      setTimeout(() => {
        if (costLineChart) costLineChart.resize();
        if (whatIfBarChart) whatIfBarChart.resize();
      }, 50);
    } else if (activeTab === "bq" && tabBtnBq && tabPaneBq) {
      tabBtnBq.classList.add("active");
      tabPaneBq.classList.remove("hidden");
      tabPaneBq.style.display = "block";
      loadBqExplorerView();
    }
  }

  if (tabBtnTelemetry)
    tabBtnTelemetry.addEventListener("click", () => switchTab("telemetry"));
  if (tabBtnBq) tabBtnBq.addEventListener("click", () => switchTab("bq"));

  const LOCAL_METRICS_POLL_MS = 1500;
  const LOCAL_SESSIONS_POLL_MS = 4000;
  const BQ_METRICS_POLL_MS = 15000;
  const BQ_SESSIONS_POLL_MS = 30000;

  let pollIntervalId = null;
  let sessionIntervalId = null;
  let activeMetricsPollMs = LOCAL_METRICS_POLL_MS;
  let currentDataSource = "local_traces";

  let pollSeq = 0;
  let pollAbortController = null;
  let sessionsSeq = 0;
  let sessionsAbortController = null;

  let costLineChart = null;
  let whatIfBarChart = null;

  function initCharts() {
    if (typeof Chart === "undefined") {
      return;
    }
    const lineCanvas = document.getElementById("costLineChart");
    const barCanvas = document.getElementById("whatIfBarChart");

    // Custom Chart.js defaults for Google Material 3 Dark theme
    if (Chart.defaults) {
      Chart.defaults.color = "#9aa0a6";
      if (Chart.defaults.font) {
        Chart.defaults.font.family = "'Google Sans', 'Inter', sans-serif";
      }
    }

    if (lineCanvas) {
      const lineCtx = lineCanvas.getContext("2d");
      costLineChart = new Chart(lineCtx, {
        type: "line",
        data: {
          labels: [],
          datasets: [
            {
              label: "Cumulative Session Cost ($)",
              data: [],
              borderColor: "#8ab4f8",
              backgroundColor: "rgba(138, 180, 248, 0.1)",
              borderWidth: 2,
              fill: true,
              tension: 0.15,
              pointBackgroundColor: "#8ab4f8",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          scales: {
            x: {
              ticks: { font: { size: 9 } },
              grid: { color: "rgba(255, 255, 255, 0.04)" },
            },
            y: {
              ticks: { font: { size: 9 } },
              grid: { color: "rgba(255, 255, 255, 0.04)" },
            },
          },
          plugins: {
            legend: {
              position: "top",
              labels: {
                color: "#e3e3e3",
                font: { size: 10, weight: "500" },
                boxWidth: 10,
              },
            },
          },
        },
      });
    }

    if (!barCanvas) return;
    const barCtx = barCanvas.getContext("2d");

    // Horizontal Stacked Bar Chart: Sub-Agent Token & Cost Attribution
    whatIfBarChart = new Chart(barCtx, {
      type: "bar",
      data: {
        labels: [],
        datasets: [
          {
            label: "Fresh Input",
            data: [],
            backgroundColor: "rgba(138, 180, 248, 0.85)",
            borderRadius: 4,
            borderWidth: 0,
          },
          {
            label: "Cached Input",
            data: [],
            backgroundColor: "rgba(129, 201, 149, 0.85)",
            borderRadius: 4,
            borderWidth: 0,
          },
          {
            label: "Output + Thinking",
            data: [],
            backgroundColor: "rgba(197, 138, 249, 0.85)",
            borderRadius: 4,
            borderWidth: 0,
          },
        ],
      },
      options: {
        indexAxis: "y",
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          x: {
            stacked: true,
            ticks: { font: { size: 9 } },
            grid: { color: "rgba(255, 255, 255, 0.05)" },
          },
          y: {
            stacked: true,
            ticks: { color: "#e3e3e3", font: { size: 10, weight: "600" } },
            grid: { display: false },
          },
        },
        plugins: {
          legend: {
            display: true,
            position: "top",
            labels: {
              color: "#e3e3e3",
              font: { size: 10, weight: "500" },
              boxWidth: 10,
            },
          },
        },
      },
    });

    const detailsToolsArena = document.getElementById("details-tools-arena");
    if (detailsToolsArena) {
      detailsToolsArena.addEventListener("toggle", () => {
        if (detailsToolsArena.open) {
          setTimeout(() => {
            if (costLineChart) costLineChart.resize();
            if (whatIfBarChart) whatIfBarChart.resize();
          }, 30);
        }
      });
    }
  }

  let hasAutoSwitchedForLocalTrace = false;
  const btnSessionLatest = document.getElementById("btn-session-latest");

  function formatRelativeSessionTime(isoStr) {
    if (!isoStr) return "recent";
    const rawTime = new Date(isoStr);
    if (Number.isNaN(rawTime.getTime())) return "recent";
    const diffSec = Math.max(
      0,
      Math.floor((Date.now() - rawTime.getTime()) / 1000),
    );
    if (diffSec < 60) return "just now";
    if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
    if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
    return (
      rawTime.toLocaleDateString([], { month: "short", day: "numeric" }) +
      " " +
      rawTime.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    );
  }

  function syncLatestButtonState() {
    if (!btnSessionLatest || !adkSessionSelect) return;
    const isFollowingLatest = adkSessionSelect.value === "latest";
    if (isFollowingLatest) {
      btnSessionLatest.style.background = "rgba(16,185,129,0.22)";
      btnSessionLatest.style.borderColor = "rgba(16,185,129,0.55)";
      btnSessionLatest.style.color = "#6ee7b7";
      btnSessionLatest.textContent = "🟢 Following Latest";
    } else {
      btnSessionLatest.style.background = "rgba(59,130,246,0.14)";
      btnSessionLatest.style.borderColor = "rgba(59,130,246,0.4)";
      btnSessionLatest.style.color = "#60a5fa";
      btnSessionLatest.textContent = "⚡ Jump to Latest";
    }
  }

  if (btnSessionLatest && adkSessionSelect) {
    btnSessionLatest.addEventListener("click", () => {
      adkSessionSelect.value = "latest";
      syncLatestButtonState();
      pollMetrics();
    });
  }

  function pollSessions() {
    const seq = ++sessionsSeq;
    if (sessionsAbortController) {
      try {
        sessionsAbortController.abort();
      } catch (_) {}
    }
    let fetchOpts;
    if (typeof AbortController !== "undefined") {
      sessionsAbortController = new AbortController();
      fetchOpts = { signal: sessionsAbortController.signal };
    }

    fetch("/api/sessions", fetchOpts)
      .then((response) => response.json())
      .then((sessions) => {
        if (seq !== sessionsSeq) return;
        if (!adkSessionSelect || !Array.isArray(sessions)) return;
        const currentVal = adkSessionSelect.value || "latest";

        let latestContextSuffix = "Live Auto-Follow";
        if (
          sessions.length > 0 &&
          typeof sessions[0] === "object" &&
          sessions[0]
        ) {
          const top = sessions[0];
          const topSid = String(top.session_id || "").substring(0, 8);
          const topPreview = String(
            top.first_query || top.agent_name || topSid,
          ).trim();
          const shortPreview =
            topPreview.length > 28
              ? `${topPreview.substring(0, 26)}…`
              : topPreview;
          const topTurns = top.turn_count || 1;
          latestContextSuffix = `Auto-Follow → "${shortPreview}" • ${topTurns}t`;
        }

        let optionsHtml = `<option value="latest">🟢 Latest Active Session (${escapeHtml(latestContextSuffix)})</option>`;
        optionsHtml += `<option value="global">🌐 All Sessions (Global Aggregate • ${sessions.length})</option>`;

        sessions.forEach((s, idx) => {
          const sid =
            typeof s === "string"
              ? s
              : s?.session_id
                ? String(s.session_id)
                : "";
          if (!sid || sid === "global" || sid === "latest") return;
          const relTime =
            typeof s === "object" && s
              ? formatRelativeSessionTime(s.last_active_time || s.start_time)
              : "local";
          const turnCount =
            typeof s === "object" && s && s.turn_count
              ? Number(s.turn_count)
              : 1;
          const turnLabel = `${turnCount}t`;
          const rawPreview =
            typeof s === "object" && s && s.first_query
              ? String(s.first_query).trim()
              : typeof s === "object" && s && s.agent_name
                ? String(s.agent_name).trim()
                : "";
          const shortPreview =
            rawPreview.length > 38
              ? `${rawPreview.substring(0, 36)}…`
              : rawPreview;
          const shortId = sid.substring(0, 8);
          const prefix =
            idx === 0 ? `🟢 [LATEST • ${relTime}]` : `🕒 [${relTime}]`;
          const labelBody = shortPreview
            ? `"${shortPreview}"`
            : `Session ${shortId}`;
          optionsHtml += `<option value="${escapeHtml(sid)}">${escapeHtml(prefix)} ${escapeHtml(labelBody)} (${escapeHtml(turnLabel)} • ${escapeHtml(shortId)})</option>`;
        });

        adkSessionSelect.innerHTML = optionsHtml;

        // Restore selection if option still exists, otherwise default to 'latest'
        if (
          Array.from(adkSessionSelect.options).some(
            (opt) => opt.value === currentVal,
          )
        ) {
          adkSessionSelect.value = currentVal;
        } else {
          adkSessionSelect.value = "latest";
        }
        syncLatestButtonState();
      })
      .catch((err) => {
        if (err && err.name === "AbortError") return;
        console.warn("Failed to fetch session list:", err);
      });
  }

  function applyPollBackoffForSource(source) {
    if (source !== "bigquery" && source !== "local_traces") return;
    currentDataSource = source;
    const desiredMetricsMs =
      source === "bigquery" ? BQ_METRICS_POLL_MS : LOCAL_METRICS_POLL_MS;
    const desiredSessionsMs =
      source === "bigquery" ? BQ_SESSIONS_POLL_MS : LOCAL_SESSIONS_POLL_MS;
    if (pollIntervalId && activeMetricsPollMs !== desiredMetricsMs) {
      clearInterval(pollIntervalId);
      clearInterval(sessionIntervalId);
      activeMetricsPollMs = desiredMetricsMs;
      pollIntervalId = setInterval(pollMetrics, desiredMetricsMs);
      sessionIntervalId = setInterval(pollSessions, desiredSessionsMs);
    }
  }

  function pollMetrics() {
    const seq = ++pollSeq;
    const selectedSession = adkSessionSelect
      ? adkSessionSelect.value || "latest"
      : "latest";
    syncLatestButtonState();

    if (pollAbortController) {
      try {
        pollAbortController.abort();
      } catch (_) {}
    }
    let fetchOpts;
    if (typeof AbortController !== "undefined") {
      pollAbortController = new AbortController();
      fetchOpts = { signal: pollAbortController.signal };
    }

    fetch(
      `/agent-nexus/live_metrics.json?session_id=${encodeURIComponent(selectedSession)}`,
      fetchOpts,
    )
      .then((response) => {
        if (!response.ok) {
          throw new Error("Metrics log is empty or uninitialized.");
        }
        return response.json();
      })
      .then((data) => {
        if (seq !== pollSeq) return;
        const currentSelected = adkSessionSelect
          ? adkSessionSelect.value || "latest"
          : "latest";
        if (currentSelected !== selectedSession) return;
        if (
          selectedSession !== "latest" &&
          selectedSession !== "global" &&
          data?.active_session_id &&
          data.active_session_id !== selectedSession
        ) {
          return;
        }

        if (data?.source) {
          applyPollBackoffForSource(data.source);
        }

        applyRegistryRatesToMetrics(data);
        const metrics = data.metrics || data;
        const turns = data.turns || [];
        const simulations = data.simulations || {};

        if (adkAppSelect && metrics && typeof metrics === "object") {
          const prevSelected = adkAppSelect.value || "all";
          adkAppSelect.disabled = false;
          const existingValues = new Set(["all"]);
          let appOpts = '<option value="all">All Agents</option>';
          Object.keys(metrics).forEach((k) => {
            if (
              !existingValues.has(k) &&
              metrics[k] &&
              typeof metrics[k] === "object"
            ) {
              existingValues.add(k);
              const label = metrics[k].name || k;
              appOpts += `<option value="${escapeHtml(k)}">${escapeHtml(label)}</option>`;
            }
          });
          adkAppSelect.innerHTML = appOpts;
          if (existingValues.has(prevSelected)) {
            adkAppSelect.value = prevSelected;
          }
          const bqAppSel = document.getElementById("bq-filter-app");
          if (bqAppSel) {
            const prevBq = bqAppSel.value || "all";
            bqAppSel.innerHTML = appOpts;
            if (existingValues.has(prevBq)) {
              bqAppSel.value = prevBq;
            }
          }
        }

        // Update read-only toolbar telemetry badges (scoped to active sub-agent filter when set)
        const selectedApp = adkAppSelect ? adkAppSelect.value || "all" : "all";
        const scopedTurns =
          selectedApp === "all"
            ? turns
            : turns.filter((t) => t && t.app_name === selectedApp);
        const toolbarTurnsEl = document.getElementById("toolbar-turns-badge");
        const toolbarCostEl = document.getElementById("toolbar-cost-badge");

        const seenModelKeys = new Set();
        const scopedRawModels = [];
        const scopedShortModels = [];
        const pushModelCandidate = (rawVal) => {
          const rawStr = String(rawVal || "").trim();
          if (!rawStr) return;
          const shortStr = stripProviderPrefixes(rawStr) || rawStr;
          const dedupeKey = shortStr.toLowerCase();
          if (!seenModelKeys.has(dedupeKey)) {
            seenModelKeys.add(dedupeKey);
            scopedRawModels.push(rawStr);
            scopedShortModels.push(shortStr);
          } else {
            const prevIdx = scopedShortModels.findIndex(
              (s) => s.toLowerCase() === dedupeKey,
            );
            if (prevIdx >= 0) {
              scopedRawModels.splice(prevIdx, 1);
              scopedShortModels.splice(prevIdx, 1);
              scopedRawModels.push(rawStr);
              scopedShortModels.push(shortStr);
            }
          }
        };

        scopedTurns.forEach((t) => {
          if (t?.model_name) pushModelCandidate(t.model_name);
        });
        if (
          scopedShortModels.length === 0 &&
          metrics &&
          typeof metrics === "object"
        ) {
          const metricEntries =
            selectedApp === "all"
              ? Object.values(metrics)
              : metrics[selectedApp]
                ? [metrics[selectedApp]]
                : [];
          metricEntries.forEach((mEntry) => {
            if (!mEntry || typeof mEntry !== "object") return;
            if (Array.isArray(mEntry.models) && mEntry.models.length > 0) {
              mEntry.models.forEach(pushModelCandidate);
            } else if (mEntry.model_name) {
              pushModelCandidate(mEntry.model_name);
            }
          });
        }

        activeSessionModelIds = scopedRawModels.slice();

        if (toolbarTurnsEl) {
          let displayTurns = scopedTurns.length;
          if (metrics && typeof metrics === "object") {
            if (selectedApp === "all") {
              const aggTurns = Object.values(metrics).reduce(
                (acc, m) => acc + Number(m?.turns || 0),
                0,
              );
              if (aggTurns > displayTurns) displayTurns = aggTurns;
            } else if (
              metrics[selectedApp] &&
              typeof metrics[selectedApp] === "object"
            ) {
              const subTurns = Number(metrics[selectedApp].turns || 0);
              if (subTurns > displayTurns) displayTurns = subTurns;
            }
          }
          toolbarTurnsEl.textContent = `${displayTurns} ${displayTurns === 1 ? "turn" : "turns"}`;
        }
        if (toolbarCostEl && metrics && typeof metrics === "object") {
          let totSpend = 0;
          if (selectedApp === "all") {
            Object.values(metrics).forEach((m) => {
              totSpend += Number(m?.cost || 0);
            });
          } else if (
            metrics[selectedApp] &&
            typeof metrics[selectedApp] === "object"
          ) {
            totSpend = Number(metrics[selectedApp].cost || 0);
          }
          toolbarCostEl.textContent = `$${totSpend.toFixed(5)}`;
        }

        const srcIcon = document.getElementById("header-source-icon");
        const bqBadge = document.getElementById("header-bq-dataset-badge");
        if (data.source === "local_traces") {
          if (srcIcon) srcIcon.textContent = "📁 Local Trace:";
          if (bqBadge) {
            const parts = String(data.traces_path || "artifacts/traces/")
              .split("/")
              .filter(Boolean);
            bqBadge.textContent = parts.pop() || "artifacts/traces/";
          }
          if (
            !hasAutoSwitchedForLocalTrace &&
            turns.length > 0 &&
            activeMainTab !== "bq"
          ) {
            hasAutoSwitchedForLocalTrace = true;
            switchTab("telemetry");
          }
        } else if (data.source === "bigquery") {
          if (srcIcon) srcIcon.textContent = "🗄️ BigQuery:";
          if (bqBadge && configuredBqBadgeText) {
            bqBadge.textContent = configuredBqBadgeText;
          }
        }

        renderLiveMetrics(metrics);
        updateLineChart(turns);
        updateBarChart(simulations, metrics);
        updateKpiCards(metrics);
        renderSessionToolsTable(data.tool_counts, turns);
        renderTurnsHistoryTable(turns);
      })
      .catch((err) => {
        if (err && err.name === "AbortError") return;
        console.warn("Sync server poll warning:", err.message);
      });
  }

  // ==========================================
  // GENERIC ANALYTICS TABLE SORTING ENGINE
  // ==========================================
  const tableSortState = {
    "adk-live-table-body": {
      colIndex: 6,
      dir: "desc",
      colTypes: ["text", "num", "num", "num", "num", "num", "num", "num"],
      defaultDirs: [
        "asc",
        "desc",
        "desc",
        "desc",
        "desc",
        "desc",
        "desc",
        "desc",
      ],
    },
    "session-turns-history-table-body": {
      colIndex: 0,
      dir: "asc",
      colTypes: [
        "num",
        "num",
        "text",
        "text",
        "text",
        "num",
        "num",
        "num",
        "text",
        "text",
      ],
      defaultDirs: [
        "asc",
        "asc",
        "asc",
        "asc",
        "asc",
        "desc",
        "desc",
        "desc",
        "asc",
        "asc",
      ],
    },
    "session-tools-table-body": {
      colIndex: 2,
      dir: "desc",
      colTypes: ["text", "text", "num", "num"],
      defaultDirs: ["asc", "asc", "desc", "desc"],
    },
  };

  function updateTableSortHeaders(tbodyId) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    const table = tbody.closest("table");
    if (!table) return;
    const state = tableSortState[tbodyId];
    if (!state) return;

    const headers = table.querySelectorAll("thead th");
    headers.forEach((th, idx) => {
      const ind = th.querySelector(".sort-indicator");
      if (idx === state.colIndex) {
        th.classList.add("sort-active");
        if (ind) ind.textContent = state.dir === "asc" ? "↑" : "↓";
      } else {
        th.classList.remove("sort-active");
        if (ind) ind.textContent = "↕";
      }
    });
  }

  function applyTableSort(tbodyId) {
    const tbody = document.getElementById(tbodyId);
    if (!tbody) return;
    const state = tableSortState[tbodyId];
    if (!state) return;

    updateTableSortHeaders(tbodyId);

    const rows = Array.from(tbody.querySelectorAll("tr"));
    if (rows.length <= 1) return;
    if (rows[0].querySelector("td[colspan]")) return;

    const colIdx = state.colIndex;
    const isNum = state.colTypes && state.colTypes[colIdx] === "num";
    const mult = state.dir === "asc" ? 1 : -1;

    rows.sort((rowA, rowB) => {
      const cellA = rowA.children[colIdx];
      const cellB = rowB.children[colIdx];
      if (!cellA || !cellB) return 0;

      const rawA = cellA.getAttribute("data-sort-val");
      const rawB = cellB.getAttribute("data-sort-val");

      if (isNum) {
        const parsedA =
          rawA !== null
            ? Number(rawA)
            : parseFloat(cellA.textContent.replace(/[^0-9.-]+/g, ""));
        const parsedB =
          rawB !== null
            ? Number(rawB)
            : parseFloat(cellB.textContent.replace(/[^0-9.-]+/g, ""));
        const numA = Number.isFinite(parsedA) ? parsedA : 0;
        const numB = Number.isFinite(parsedB) ? parsedB : 0;
        if (numA !== numB) return (numA - numB) * mult;
        return 0;
      }

      const strA = unescapeHtmlEntities(
        rawA !== null ? rawA : cellA.textContent,
      )
        .trim()
        .toLowerCase();
      const strB = unescapeHtmlEntities(
        rawB !== null ? rawB : cellB.textContent,
      )
        .trim()
        .toLowerCase();
      return (
        strA.localeCompare(strB, undefined, {
          numeric: true,
          sensitivity: "base",
        }) * mult
      );
    });

    rows.forEach((r) => {
      tbody.appendChild(r);
    });
  }

  function initSortableTables() {
    Object.keys(tableSortState).forEach((tbodyId) => {
      const tbody = document.getElementById(tbodyId);
      if (!tbody) return;
      const table = tbody.closest("table");
      if (!table) return;
      const state = tableSortState[tbodyId];

      const headers = table.querySelectorAll("thead th");
      headers.forEach((th, idx) => {
        if (th.classList.contains("sortable-th")) return;
        th.classList.add("sortable-th");
        th.title = "Click to sort column";
        const indicator = document.createElement("span");
        indicator.className = "sort-indicator";
        indicator.textContent = "↕";
        th.appendChild(indicator);

        th.addEventListener("click", () => {
          if (state.colIndex === idx) {
            state.dir = state.dir === "asc" ? "desc" : "asc";
          } else {
            state.colIndex = idx;
            state.dir = state.defaultDirs?.[idx]
              ? state.defaultDirs[idx]
              : "desc";
          }
          applyTableSort(tbodyId);
        });
      });

      updateTableSortHeaders(tbodyId);
    });
  }

  function extractTurnToolsList(turn) {
    if (!turn) return [];
    const raw = Array.isArray(turn.tools_called)
      ? turn.tools_called
      : turn.invoked_tools;
    return splitTopLevelItems(raw).filter(
      (x) => x && x !== "None (Direct Text)" && x !== "None",
    );
  }

  function extractTurnSkillsList(turn) {
    if (!turn) return [];
    const raw = Array.isArray(turn.skills_active)
      ? turn.skills_active
      : turn.invoked_skills;
    return splitTopLevelItems(raw).filter(
      (x) => x && x !== "None" && x !== "unload",
    );
  }

  function renderSessionToolsTable(toolCounts, turns) {
    const tableBody = document.getElementById("session-tools-table-body");
    const badgeTotal = document.getElementById("session-tools-total-badge");
    if (!tableBody) return;

    const selectedApp = adkAppSelect ? adkAppSelect.value || "all" : "all";
    const hasServerCounts = Boolean(
      toolCounts &&
        typeof toolCounts === "object" &&
        Object.keys(toolCounts).length > 0,
    );
    const effectiveMap = {};

    if (hasServerCounts && selectedApp === "all") {
      Object.entries(toolCounts).forEach(([k, v]) => {
        if (v && typeof v === "object") {
          effectiveMap[k] = Object.assign({}, v, {
            targets:
              v.targets && typeof v.targets === "object"
                ? Object.assign({}, v.targets)
                : {},
          });
        }
      });
      // Ensure activated skills from turns are present even if server payload omitted skill entries in tool_counts
      if (Array.isArray(turns)) {
        const existingSkillNames = new Set(
          Object.values(effectiveMap)
            .filter(
              (item) => item && (item.kind === "skill" || item.icon === "⚡"),
            )
            .map((item) => String(item.name || "")),
        );
        turns.forEach((t) => {
          const appKey = t?.app_name ? String(t.app_name) : "agent";
          extractTurnSkillsList(t).forEach((sName) => {
            if (existingSkillNames.has(sName)) return;
            const skKey =
              effectiveMap[sName] &&
              effectiveMap[sName].kind !== "skill" &&
              effectiveMap[sName].icon !== "⚡"
                ? `skill:${sName}`
                : sName;
            if (!effectiveMap[skKey]) {
              effectiveMap[skKey] = {
                name: sName,
                kind: "skill",
                label: `Activated Skill (${appKey})`,
                icon: "⚡",
                count: 0,
                targets: {},
              };
            }
            effectiveMap[skKey].count += 1;
            effectiveMap[skKey].targets[appKey] =
              (effectiveMap[skKey].targets[appKey] || 0) + 1;
          });
        });
      }
    } else if (Array.isArray(turns) && turns.length > 0) {
      const scopedTurns =
        selectedApp === "all"
          ? turns
          : turns.filter((t) => t && t.app_name === selectedApp);
      scopedTurns.forEach((t) => {
        const appKey = t?.app_name ? String(t.app_name) : "agent";
        extractTurnToolsList(t).forEach((tName) => {
          if (!effectiveMap[tName]) {
            effectiveMap[tName] = {
              name: tName,
              kind: "tool",
              label: `Sub-Agent: ${appKey}`,
              icon: "🛠️",
              count: 0,
              targets: {},
            };
          }
          effectiveMap[tName].count += 1;
          effectiveMap[tName].targets[appKey] =
            (effectiveMap[tName].targets[appKey] || 0) + 1;
        });
        extractTurnSkillsList(t).forEach((sName) => {
          const skKey =
            effectiveMap[sName] && effectiveMap[sName].kind !== "skill"
              ? `skill:${sName}`
              : sName;
          if (!effectiveMap[skKey]) {
            effectiveMap[skKey] = {
              name: sName,
              kind: "skill",
              label: `Activated Skill (${appKey})`,
              icon: "⚡",
              count: 0,
              targets: {},
            };
          }
          effectiveMap[skKey].count += 1;
          effectiveMap[skKey].targets[appKey] =
            (effectiveMap[skKey].targets[appKey] || 0) + 1;
        });
      });
    }

    const toolEntries = Object.values(effectiveMap);
    if (toolEntries.length === 0) {
      tableBody.innerHTML = `
        <tr>
          <td colspan="4" style="text-align: center; color: var(--color-text-muted); padding: 1.5rem;">
            No tool or skill invocations recorded in this session yet.
          </td>
        </tr>
      `;
      if (badgeTotal) {
        badgeTotal.textContent = "0 Tool Invocations";
        badgeTotal.className = "badge badge-primary";
      }
      return;
    }

    let totalInvocations = 0;
    let skillEntriesCount = 0;
    let html = "";

    toolEntries.forEach((t) => {
      const safeCount = Number(t.count || 0);
      totalInvocations += safeCount;
      const isCalled = safeCount > 0;
      const isSkill = t.kind === "skill" || t.icon === "⚡";
      if (isSkill && isCalled) skillEntriesCount += 1;

      let targetsHtml = "";
      const targetKeys = t.targets ? Object.keys(t.targets) : [];
      if (targetKeys.length > 0) {
        const targetBadges = Object.entries(t.targets)
          .map(([tgt, cnt]) => {
            return `<span class="badge ${isSkill ? "badge-success" : "badge-primary"}" style="font-size:0.75rem; margin-right:4px; margin-bottom:2px;">${escapeHtml(tgt)} (${Number(cnt || 0)}x)</span>`;
          })
          .join(" ");
        targetsHtml = targetBadges;
      } else if (isCalled) {
        targetsHtml = `<span style="color:var(--color-text-muted); font-size:0.8rem;">Standard execution</span>`;
      } else {
        targetsHtml = `<span style="color:var(--color-text-muted); font-size:0.8rem;">—</span>`;
      }

      const categoryLabel =
        targetKeys.length > 1
          ? isSkill
            ? `Shared Skill (${targetKeys.length} Sub-Agents)`
            : `Shared Tool (${targetKeys.length} Sub-Agents)`
          : t.label || (isSkill ? "Activated Skill" : "Tool");

      html += `
        <tr>
          <td data-sort-val="${escapeHtml(String(t.name || "").toLowerCase())}">
            <div style="display:flex; align-items:center; gap:0.5rem;">
              <span style="font-size:1.1rem;">${escapeHtml(t.icon || (isSkill ? "⚡" : "🛠️"))}</span>
              <strong style="font-family:monospace; font-size:0.85rem; color:${isCalled ? (isSkill ? "#81c995" : "#8ab4f8") : "var(--color-text)"};">${escapeHtml(t.name || "")}</strong>
            </div>
          </td>
          <td data-sort-val="${escapeHtml(categoryLabel.toLowerCase())}"><span class="badge badge-secondary" style="font-size:0.75rem;">${escapeHtml(categoryLabel)}</span></td>
          <td data-sort-val="${safeCount}" style="text-align:center;">
            <span class="badge ${isCalled ? "badge-success" : "badge-secondary"}" style="font-weight:700; font-size:0.8rem;">
              ${safeCount} ${safeCount === 1 ? "call" : "calls"}
            </span>
          </td>
          <td data-sort-val="${targetKeys.length}">${targetsHtml}</td>
        </tr>
      `;
    });

    tableBody.innerHTML = html;
    applyTableSort("session-tools-table-body");
    if (badgeTotal) {
      const kindLabel = skillEntriesCount > 0 ? "Tool & Skill" : "Tool";
      badgeTotal.textContent = `${totalInvocations} ${kindLabel} ${totalInvocations === 1 ? "Invocation" : "Invocations"}`;
      badgeTotal.className =
        totalInvocations > 0 ? "badge badge-success" : "badge badge-primary";
    }
  }

  let latestFilteredTurns = [];

  function cleanTurnUserQuery(rawQ) {
    let txt = String(rawQ || "").trim();
    while (txt.startsWith("[") && txt.includes("]")) {
      txt = txt.substring(txt.indexOf("]") + 1).trim();
    }
    return txt;
  }

  function renderTurnsHistoryTable(turns) {
    const tableBody = document.getElementById(
      "session-turns-history-table-body",
    );
    const countBadge = document.getElementById("session-turns-count-badge");
    if (!tableBody) return;

    if (!turns || turns.length === 0) {
      latestFilteredTurns = [];
      tableBody.innerHTML = `
        <tr>
          <td colspan="10" style="text-align: center; color: var(--color-text-muted); padding: 2rem;">
            No turns recorded in this session yet.
          </td>
        </tr>
      `;
      if (countBadge) countBadge.textContent = "0 Turns";
      return;
    }

    // Pre-pass across all turns in the session to resolve clean user prompts and post-tool final response status
    const lastActivePromptBySession = {};
    const pendingToolSynthesisByAgent = {};
    turns.forEach((t) => {
      const sid = t.session_id || "default";
      const agentKey = `${sid}::${t.app_name || "agent"}`;
      const cleaned = cleanTurnUserQuery(t.user_query);
      if (
        cleaned &&
        cleaned !== "Turn" &&
        cleaned !== "—" &&
        !cleaned.startsWith("For context:")
      ) {
        lastActivePromptBySession[sid] = cleaned;
        t._resolved_user_query = cleaned;
      } else if (lastActivePromptBySession[sid]) {
        t._resolved_user_query = lastActivePromptBySession[sid];
      } else {
        t._resolved_user_query = cleaned || "—";
      }

      const hasTools = extractTurnToolsList(t).length > 0;
      if (hasTools) {
        pendingToolSynthesisByAgent[agentKey] = true;
        t._is_post_tool_final = false;
      } else {
        t._is_post_tool_final = Boolean(pendingToolSynthesisByAgent[agentKey]);
        pendingToolSynthesisByAgent[agentKey] = false;
      }
    });

    const selectedApp = adkAppSelect ? adkAppSelect.value : "all";
    const filteredTurns =
      selectedApp === "all"
        ? turns
        : turns.filter((t) => t.app_name === selectedApp);
    latestFilteredTurns = filteredTurns;

    if (countBadge)
      countBadge.textContent = `${filteredTurns.length} ${filteredTurns.length === 1 ? "Turn" : "Turns"}`;

    let html = "";
    filteredTurns.forEach((turn, idx) => {
      const timeStr = turn.timestamp
        ? new Date(turn.timestamp).toLocaleTimeString()
        : `Turn #${idx + 1}`;
      const timeSortVal = turn.timestamp
        ? new Date(turn.timestamp).getTime() || idx + 1
        : idx + 1;
      const appName = turn.app_name || "agent";
      const cleanQ =
        turn._resolved_user_query || cleanTurnUserQuery(turn.user_query) || "—";
      const userQ = escapeHtml(cleanQ);
      const toolList = extractTurnToolsList(turn);
      const skillList = extractTurnSkillsList(turn);
      let rawResp = (turn.agent_response || "").trim();
      if ((!rawResp || rawResp === "—") && toolList.length > 0) {
        rawResp = `🛠️ Tool call: ${toolList.join(", ")}`;
      }
      const agentResp =
        escapeHtml(rawResp.substring(0, 75)) +
        (rawResp.length > 75 ? "..." : "");
      const rawCost = Number(
        turn.estimated_cost !== undefined
          ? turn.estimated_cost
          : turn.cost || 0,
      );
      const costStr = `$${rawCost.toFixed(5)}`;
      const rawLatencyMs = Number(turn.latency_ms || 0);
      const latencyStr =
        rawLatencyMs > 0 ? `${rawLatencyMs.toLocaleString()} ms` : "—";

      const promptTok = Number(turn.prompt_tokens || 0);
      const cachedTok = Number(turn.cached_tokens || 0);
      const freshTok =
        turn.input_tokens !== undefined
          ? Number(turn.input_tokens || 0)
          : Math.max(0, promptTok - cachedTok);
      const outTok = Number(turn.output_tokens || 0);
      const thinkTok = Number(turn.thinking_tokens || 0);
      const totalTurnTokens = freshTok + cachedTok + outTok + thinkTok;
      const tokStr = `<span style="font-family:monospace; font-size:0.75rem;"><strong style="color:#8ab4f8;">${freshTok}</strong> / <span style="color:#81c995;">${cachedTok}</span> / <span style="color:#c58af9;">${outTok}</span> / <span style="color:#fdd663;">${thinkTok}</span></span>`;

      // Invoked Tools Badges
      let toolsHtml = turn._is_post_tool_final
        ? '<span class="badge badge-secondary" style="font-size:0.75rem; color:#81c995; border-color:rgba(129,201,149,0.35); background:rgba(129,201,149,0.1);" title="Sub-agent finished tool execution and emitted its final response">✅ Final Response</span>'
        : '<span class="badge badge-secondary" style="font-size:0.75rem; color:#9aa0a6;" title="Direct text response without tool calls">💬 Direct Reply</span>';
      if (toolList.length > 0) {
        toolsHtml = toolList
          .map((tClean) => {
            const icon =
              tClean.includes("load_skill") || tClean.includes("activate_skill")
                ? "⚡"
                : tClean.includes("search_travel")
                  ? "📖"
                  : tClean.includes("weather")
                    ? "🌤️"
                    : tClean.includes("time")
                      ? "⏰"
                      : "🌐";
            return `<span class="badge badge-primary" style="font-size:0.75rem; margin-right:3px; margin-bottom:2px;">${icon} ${escapeHtml(tClean)}</span>`;
          })
          .join(" ");
      }

      // Invoked Skills Badges
      let skillsHtml =
        '<span style="color:var(--color-text-muted); font-size:0.75rem;">None</span>';
      if (skillList.length > 0) {
        skillsHtml = skillList
          .map((sClean) => {
            return `<span class="badge badge-success" style="font-size:0.75rem; margin-right:3px; margin-bottom:2px;">⚡ ${escapeHtml(sClean)}</span>`;
          })
          .join(" ");
      }

      const flowBadge = formatOrchestrationBadge(turn.orchestration_flow);
      const turnModelBadge = formatModelPillBadge(turn.model_name);

      html += `
        <tr style="cursor:pointer;" title="Click to inspect full prompt, response &amp; JSON" onclick="window.inspectSessionTurn(${idx})">
          <td data-sort-val="${idx + 1}"><span style="font-family:monospace; color:var(--color-text-muted); font-size:0.8rem;">#${idx + 1}</span></td>
          <td data-sort-val="${timeSortVal}"><span style="font-family:monospace; font-size:0.75rem; color:var(--color-text-muted);">${timeStr}</span></td>
          <td data-sort-val="${escapeHtml(String(appName).toLowerCase())}">
            <div style="display:flex; align-items:center; gap:0.35rem; flex-wrap:wrap;">
              <span class="badge badge-primary" style="font-size:0.75rem;">${escapeHtml(appName)}</span>
              ${flowBadge}
              ${turnModelBadge}
            </div>
          </td>
          <td data-sort-val="${escapeHtml(String(cleanQ).toLowerCase())}" style="min-width:190px; max-width:300px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:#e3e3e3;" title="${userQ}">
            <strong>${userQ}</strong>
          </td>
          <td data-sort-val="${escapeHtml(String(rawResp).toLowerCase())}" style="max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--color-text-muted); font-size:0.8rem;" title="${escapeHtml(rawResp)}">
            ${agentResp || "—"}
          </td>
          <td data-sort-val="${totalTurnTokens}">${tokStr}</td>
          <td data-sort-val="${rawLatencyMs}" style="font-family:monospace; font-size:0.78rem; color:#94a3b8; white-space:nowrap;">${escapeHtml(latencyStr)}</td>
          <td data-sort-val="${rawCost}" style="font-family:monospace; font-size:0.8rem; color:var(--color-success); font-weight:600;">${costStr}</td>
          <td>${toolsHtml}</td>
          <td>${skillsHtml}</td>
        </tr>
      `;
    });

    tableBody.innerHTML = html;
    applyTableSort("session-turns-history-table-body");
  }

  window.inspectSessionTurn = (index) => {
    const row = latestFilteredTurns[index];
    if (!row || !bqRowModalOverlay) return;

    if (modalBqApp) modalBqApp.textContent = row.app_name || "-";
    if (modalBqModel)
      modalBqModel.textContent = row.model_name || "Gemini 3.8 Flash";
    const costVal =
      row.estimated_cost !== undefined ? row.estimated_cost : row.cost || 0;
    if (modalBqCost) modalBqCost.textContent = `$${Number(costVal).toFixed(5)}`;
    const latMs = Number(row.latency_ms || 0);
    if (modalBqLatency)
      modalBqLatency.textContent =
        latMs > 0 ? `${latMs.toLocaleString()} ms` : "—";
    if (modalBqTime)
      modalBqTime.textContent = row.timestamp || `Turn #${index + 1}`;
    const cleanQ =
      row._resolved_user_query || cleanTurnUserQuery(row.user_query);
    if (modalBqQuery)
      modalBqQuery.textContent = cleanQ || row.user_query || "(empty prompt)";
    if (modalBqResponse)
      modalBqResponse.textContent = row.agent_response || "(empty response)";
    if (modalBqRawJson)
      modalBqRawJson.textContent = JSON.stringify(row, null, 2);

    bqRowModalOverlay.classList.remove("hidden");
    bqRowModalOverlay.style.display = "flex";
  };

  function updateKpiCards(metrics) {
    let totalCost = 0;
    let totalTurns = 0;
    let totalInput = 0;
    let totalCached = 0;
    let totalOutput = 0;
    let totalThinking = 0;
    let totalPerTurnSavings = 0;
    let hasPerAppSavings = false;

    Object.values(metrics || {}).forEach((app) => {
      if (!app || typeof app !== "object") return;
      totalCost += Number(app.cost || 0);
      totalTurns += Number(app.turns || 0);
      totalInput += Number(app.input || 0);
      totalCached += Number(app.cached || 0);
      totalOutput += Number(app.output || 0);
      totalThinking += Number(app.thinking || 0);
      if (app.savings_dollars !== undefined) {
        hasPerAppSavings = true;
        totalPerTurnSavings += Number(app.savings_dollars || 0);
      }
    });

    // 1. Total cost + Avg per turn
    const kpiTotalCostEl = document.getElementById("kpi-total-cost");
    if (kpiTotalCostEl) {
      kpiTotalCostEl.textContent = `$${totalCost.toFixed(5)}`;
    }
    const kpiCostDescEl = document.getElementById("kpi-cost-desc");
    if (kpiCostDescEl) {
      const avgTurnCost = totalTurns > 0 ? totalCost / totalTurns : 0;
      const avgTurnStr = (
        Math.round((avgTurnCost + Number.EPSILON) * 100000) / 100000
      ).toFixed(5);
      kpiCostDescEl.textContent =
        totalTurns > 0
          ? `Avg $${avgTurnStr} / turn (${totalTurns} ${totalTurns === 1 ? "turn" : "turns"})`
          : "Active session footprint";
    }

    // 2. Context Cache Efficiency (Hit ratio + true per-turn Rate Card $ and % savings)
    const ratio = totalInput > 0 ? (totalCached / totalInput) * 100 : 0.0;
    const kpiCacheRatioEl = document.getElementById("kpi-cache-ratio");
    const kpiCacheBarEl = document.getElementById("kpi-cache-bar");
    if (kpiCacheRatioEl) kpiCacheRatioEl.textContent = `${ratio.toFixed(1)}%`;
    if (kpiCacheBarEl) kpiCacheBarEl.style.width = `${ratio}%`;

    let savedDollars = 0;
    if (hasPerAppSavings || totalCached === 0) {
      savedDollars = Math.max(0, totalPerTurnSavings);
    } else {
      const activeId = getActiveToolbarModelId();
      const activeModel = findMatchingModelInRegistry(activeId);
      const inRate = activeModel?.pricing
        ? Number(activeModel.pricing.input ?? 0.75)
        : 0.75;
      const cacRate = activeModel?.pricing
        ? Number(activeModel.pricing.cached ?? 0.075)
        : 0.075;
      savedDollars = Math.max(
        0,
        (totalCached * Math.max(0, inRate - cacRate)) / 1000000,
      );
    }
    const uncachedTotalCost = totalCost + savedDollars;
    const billSavingsPct =
      uncachedTotalCost > 0 ? (savedDollars / uncachedTotalCost) * 100.0 : 0.0;

    const kpiCacheDescEl = document.getElementById("kpi-cache-desc");
    if (kpiCacheDescEl) {
      kpiCacheDescEl.textContent = `Saved $${savedDollars.toFixed(5)} (${billSavingsPct.toFixed(1)}% off total bill)`;
    }

    // 3. Input Context Tokens (Total Input + Fresh vs Cached breakdown)
    const totalFresh = Math.max(0, totalInput - totalCached);
    const kpiMaxSavingsEl = document.getElementById("kpi-max-savings");
    if (kpiMaxSavingsEl) {
      kpiMaxSavingsEl.textContent = totalInput.toLocaleString();
    }
    const kpiSavingsDescEl = document.getElementById("kpi-savings-desc");
    if (kpiSavingsDescEl) {
      kpiSavingsDescEl.textContent = `Fresh: ${totalFresh.toLocaleString()} | Cached: ${totalCached.toLocaleString()}`;
    }

    // 4. Reasoning & Output Tokens breakdown (output_tokens is visible candidates; thinking_tokens is separate)
    const totalCompletion = totalOutput + totalThinking;
    const visibleOutput = totalOutput;
    const thinkingRatio =
      totalCompletion > 0 ? (totalThinking / totalCompletion) * 100 : 0.0;

    const kpiOutputEl = document.getElementById("kpi-output-tokens");
    const kpiOutputDescEl = document.getElementById("kpi-output-desc");
    if (kpiOutputEl) {
      kpiOutputEl.textContent = totalCompletion.toLocaleString();
    }
    if (kpiOutputDescEl) {
      kpiOutputDescEl.textContent = `Visible: ${visibleOutput.toLocaleString()} | Thoughts: ${totalThinking.toLocaleString()} (${thinkingRatio.toFixed(1)}%)`;
    }
  }

  function updateLineChart(turns) {
    if (!costLineChart) return;

    const getCumulative = (list) => {
      let sum = 0;
      return list.map((t) => {
        sum += Number(t.estimated_cost || t.total_cost || 0);
        return sum;
      });
    };

    const selectedApp = adkAppSelect ? adkAppSelect.value : "all";
    const filteredTurns =
      selectedApp === "all"
        ? turns || []
        : (turns || []).filter((t) => t.app_name === selectedApp);

    costLineChart.data.labels = filteredTurns.map(
      (t, i) => `T${i + 1} (${t.app_name || "agent"})`,
    );
    costLineChart.data.datasets[0].label =
      selectedApp === "all"
        ? "Cumulative Session Cost ($)"
        : `${selectedApp} Cost ($)`;
    costLineChart.data.datasets[0].data = getCumulative(filteredTurns);
    costLineChart.update();
  }

  function updateBarChart(_simulations, metrics) {
    if (!whatIfBarChart || !metrics || typeof metrics !== "object") return;

    const selectedApp = adkAppSelect ? adkAppSelect.value : "all";
    const allKeys = Object.keys(metrics).filter(
      (k) => metrics[k] && typeof metrics[k] === "object",
    );
    const keysToShow =
      selectedApp !== "all" && metrics[selectedApp] ? [selectedApp] : allKeys;

    const labels = [];
    const freshData = [];
    const cachedData = [];
    const outputData = [];

    keysToShow.forEach((k) => {
      const app = metrics[k];
      const name = app.name || k;
      const turns = app.turns || 0;
      const cost = Number(app.cost || 0);
      const input = Number(app.input || 0);
      const cached = Number(app.cached || 0);
      const output = Number(app.output || 0);
      const thinking = Number(app.thinking || 0);
      const fresh = Math.max(0, input - cached);

      labels.push(`${name} (${turns}t • $${cost.toFixed(4)})`);
      freshData.push(fresh);
      cachedData.push(cached);
      outputData.push(output + thinking);
    });

    whatIfBarChart.data.labels = labels;
    if (whatIfBarChart.data.datasets.length >= 3) {
      whatIfBarChart.data.datasets[0].data = freshData;
      whatIfBarChart.data.datasets[1].data = cachedData;
      whatIfBarChart.data.datasets[2].data = outputData;
    }
    whatIfBarChart.update();
  }

  const btnResetSubagentFilter = document.getElementById(
    "btn-reset-subagent-filter",
  );

  function syncResetSubagentFilterButton() {
    if (!btnResetSubagentFilter) return;
    const selectedApp = adkAppSelect ? adkAppSelect.value : "all";
    if (selectedApp && selectedApp !== "all") {
      btnResetSubagentFilter.style.display = "inline-block";
      btnResetSubagentFilter.textContent = `✕ Filter: ${selectedApp}`;
    } else {
      btnResetSubagentFilter.style.display = "none";
    }
  }

  if (btnResetSubagentFilter) {
    btnResetSubagentFilter.addEventListener("click", () => {
      if (adkAppSelect) {
        adkAppSelect.value = "all";
      }
      syncResetSubagentFilterButton();
      pollMetrics();
    });
  }

  function formatOrchestrationBadge(flow) {
    const f = String(flow || "")
      .trim()
      .replace(/\s*•\s*/g, " • ");
    if (!f) return "";
    if (f === "Root") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#fbbf24; border-color:rgba(245,158,11,0.35); background:rgba(245,158,11,0.12);" title="Root entry / router agent">🎯 Root</span>';
    }
    if (f === "AgentTool • Parallel • Loop") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#c084fc; border-color:rgba(168,85,247,0.38); background:rgba(168,85,247,0.14);" title="AgentTool wrapping ParallelAgent inside a LoopAgent">🧰 AgentTool • ⚡ Parallel • 🔁 Loop</span>';
    }
    if (f === "AgentTool • Parallel") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#38bdf8; border-color:rgba(56,189,248,0.38); background:rgba(56,189,248,0.14);" title="AgentTool wrapping concurrent ParallelAgent sub-agent">🧰 AgentTool • ⚡ Parallel</span>';
    }
    if (f === "AgentTool • Loop") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#f472b6; border-color:rgba(244,114,182,0.38); background:rgba(244,114,182,0.14);" title="AgentTool wrapping iterative LoopAgent sub-agent">🧰 AgentTool • 🔁 Loop</span>';
    }
    if (f === "AgentTool • Sequential") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#6ee7b7; border-color:rgba(16,185,129,0.38); background:rgba(16,185,129,0.14);" title="AgentTool wrapping SequentialAgent pipeline stage">🧰 AgentTool • ➡️ Sequential</span>';
    }
    if (f === "AgentTool") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#a78bfa; border-color:rgba(167,139,250,0.38); background:rgba(167,139,250,0.14);" title="Agent invoked as an ADK AgentTool">🧰 AgentTool</span>';
    }
    if (f === "Parallel • Loop") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#c084fc; border-color:rgba(168,85,247,0.35); background:rgba(168,85,247,0.12);" title="ParallelAgent sub-agent inside a LoopAgent">⚡ Parallel • 🔁 Loop</span>';
    }
    if (f === "Parallel") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#38bdf8; border-color:rgba(56,189,248,0.35); background:rgba(56,189,248,0.12);" title="Concurrent ParallelAgent sub-agent">⚡ Parallel</span>';
    }
    if (f === "Loop") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#f472b6; border-color:rgba(244,114,182,0.35); background:rgba(244,114,182,0.12);" title="Iterative LoopAgent sub-agent">🔁 Loop</span>';
    }
    if (f === "Sequential") {
      return '<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#6ee7b7; border-color:rgba(16,185,129,0.35); background:rgba(16,185,129,0.12);" title="SequentialAgent pipeline stage">➡️ Sequential</span>';
    }
    return `<span class="badge badge-secondary" style="font-size:0.68rem; padding:0.12rem 0.42rem; color:#94a3b8;">🤖 ${escapeHtml(f)}</span>`;
  }

  function formatModelPillBadge(modelsOrStr) {
    const rawItems = Array.isArray(modelsOrStr)
      ? modelsOrStr
      : modelsOrStr
        ? [modelsOrStr]
        : [];
    const seen = new Set();
    const shorts = [];
    const raws = [];
    rawItems.forEach((item) => {
      const raw = String(item || "").trim();
      if (!raw) return;
      const shortId = stripProviderPrefixes(raw) || raw;
      const key = shortId.toLowerCase();
      if (!seen.has(key)) {
        seen.add(key);
        shorts.push(shortId);
        raws.push(raw);
      }
    });
    if (shorts.length === 0) return "";
    const primaryShort = shorts[shorts.length - 1];
    const displayTxt =
      shorts.length === 1
        ? primaryShort
        : `${primaryShort} (+${shorts.length - 1})`;
    const titleTxt =
      shorts.length === 1
        ? `Model: ${raws[0]}`
        : `Models: ${shorts.join(", ")}`;
    return `<span class="badge badge-secondary" style="font-family:var(--font-mono); font-size:0.68rem; padding:0.12rem 0.42rem; color:#93c5fd; border-color:rgba(147,197,253,0.3); background:rgba(59,130,246,0.1);" title="${escapeHtml(titleTxt)}">${escapeHtml(displayTxt)}</span>`;
  }

  window.formatOrchestrationBadge = formatOrchestrationBadge;
  window.formatModelPillBadge = formatModelPillBadge;

  function renderLiveMetrics(data) {
    adkLiveTableBody.innerHTML = "";
    const selectedApp = adkAppSelect ? adkAppSelect.value : "all";
    syncResetSubagentFilterButton();

    const keys =
      data && typeof data === "object"
        ? Object.keys(data).filter(
            (k) => data[k] && typeof data[k] === "object",
          )
        : [];
    if (keys.length === 0) {
      adkLiveTableBody.innerHTML = `
        <tr>
          <td colspan="8" style="text-align: center; color: var(--color-text-muted); padding: 2.25rem;">
            No agent turns recorded in this session yet.
          </td>
        </tr>
      `;
      return;
    }

    let totalSessionCost = 0;
    keys.forEach((k) => {
      const d = data[k];
      totalSessionCost += Number.isFinite(Number(d.cost)) ? Number(d.cost) : 0;
    });

    keys.forEach((key) => {
      const appData = data[key];
      const freshIn = Math.max(
        0,
        Number(appData.input || 0) - Number(appData.cached || 0),
      );
      const thinkingVal = appData.thinking || 0;
      const isSelected = selectedApp !== "all" && key === selectedApp;

      const row = document.createElement("tr");
      row.style.cursor = "pointer";
      row.title = isSelected
        ? "Click to clear sub-agent filter"
        : `Click to filter charts & turns by ${appData.name}`;
      if (isSelected) {
        row.style.background = "rgba(138, 180, 248, 0.14)";
        row.style.borderLeft = "3px solid #8ab4f8";
      } else if (selectedApp !== "all") {
        row.style.opacity = "0.55";
      } else if (appData.turns > 0) {
        row.style.background = "rgba(129, 201, 149, 0.03)";
        row.style.borderLeft = "3px solid var(--color-success)";
      }

      row.addEventListener("click", () => {
        if (!adkAppSelect) return;
        adkAppSelect.value = adkAppSelect.value === key ? "all" : key;
        syncResetSubagentFilterButton();
        pollMetrics();
      });

      const flowBadge = formatOrchestrationBadge(appData.orchestration_flow);
      const appModels =
        Array.isArray(appData.models) && appData.models.length > 0
          ? appData.models
          : appData.model_name
            ? [appData.model_name]
            : [];
      const modelBadge = formatModelPillBadge(appModels);
      const safeTurns = Number(appData.turns || 0);
      const safeCached = Number(appData.cached || 0);
      const safeOutput = Number(appData.output || 0);
      const safeThinking = Number(thinkingVal || 0);
      const safeCost = Number.isFinite(Number(appData.cost))
        ? Number(appData.cost)
        : 0;
      const fallbackSharePct =
        appData.cost_share_pct !== undefined &&
        Number.isFinite(Number(appData.cost_share_pct))
          ? Number(appData.cost_share_pct)
          : 0.0;
      const sharePct =
        totalSessionCost > 0
          ? (safeCost / totalSessionCost) * 100.0
          : fallbackSharePct;

      row.innerHTML = `
        <td data-sort-val="${escapeHtml(String(appData.name || key).toLowerCase())}">
          <div style="display:flex; align-items:center; gap:0.4rem; flex-wrap:wrap;">
            <span style="font-weight:600; color:var(--color-primary);">${escapeHtml(appData.name)}</span>
            ${flowBadge}
            ${modelBadge}
          </div>
        </td>
        <td data-sort-val="${safeTurns}" style="text-align:center; font-weight:600;">${safeTurns}</td>
        <td data-sort-val="${freshIn}">${freshIn.toLocaleString()}</td>
        <td data-sort-val="${safeCached}" style="color: var(--color-success); font-weight:600;">${safeCached.toLocaleString()}</td>
        <td data-sort-val="${safeOutput}">${safeOutput.toLocaleString()}</td>
        <td data-sort-val="${safeThinking}" style="color: #c58af9; font-weight:600;">🧠 ${safeThinking.toLocaleString()}</td>
        <td data-sort-val="${safeCost}" style="font-weight:700; color:var(--color-success);">$${safeCost.toFixed(5)}</td>
        <td data-sort-val="${sharePct.toFixed(4)}" style="font-family:var(--font-mono); font-weight:600; color:var(--color-primary-light);">${sharePct.toFixed(1)}%</td>
      `;
      adkLiveTableBody.appendChild(row);
    });
    applyTableSort("adk-live-table-body");
  }

  const btnHeaderPollToggle = document.getElementById("btn-header-poll-toggle");
  const headerPollDot = document.getElementById("header-poll-dot");
  const headerPollLabel = document.getElementById("header-poll-label");

  function updatePollUiState(isPolling) {
    if (headerPollDot) {
      headerPollDot.className = isPolling ? "pulse-dot green" : "pulse-dot";
      headerPollDot.style.background = isPolling ? "" : "#94a3b8";
      headerPollDot.style.boxShadow = isPolling ? "" : "none";
    }
    if (headerPollLabel) {
      headerPollLabel.textContent = isPolling
        ? "Live Polling (Click to Pause)"
        : "⏸ Poll Paused (Click to Poll)";
    }
  }

  function startPolling() {
    if (pollIntervalId) clearInterval(pollIntervalId);
    if (sessionIntervalId) clearInterval(sessionIntervalId);

    const metricsMs =
      currentDataSource === "bigquery"
        ? BQ_METRICS_POLL_MS
        : LOCAL_METRICS_POLL_MS;
    const sessionsMs =
      currentDataSource === "bigquery"
        ? BQ_SESSIONS_POLL_MS
        : LOCAL_SESSIONS_POLL_MS;
    activeMetricsPollMs = metricsMs;
    pollIntervalId = setInterval(pollMetrics, metricsMs);
    sessionIntervalId = setInterval(pollSessions, sessionsMs);
    updatePollUiState(true);

    pollSessions();
    pollMetrics();
  }

  function stopPolling() {
    if (pollIntervalId) {
      clearInterval(pollIntervalId);
      pollIntervalId = null;
    }
    if (sessionIntervalId) {
      clearInterval(sessionIntervalId);
      sessionIntervalId = null;
    }
    updatePollUiState(false);
  }

  if (btnHeaderPollToggle) {
    btnHeaderPollToggle.addEventListener("click", () => {
      if (pollIntervalId) {
        stopPolling();
      } else {
        startPolling();
      }
    });
  }

  if (adkSessionSelect) {
    adkSessionSelect.addEventListener("change", () => {
      pollMetrics();
    });
  }

  if (adkAppSelect) {
    adkAppSelect.addEventListener("change", () => {
      syncResetSubagentFilterButton();
      pollMetrics();
    });
  }

  let globalModelsRegistry = [];
  let globalModelsDeletedIds = [];
  let globalModelsReplaceDefaults = false;
  let globalModelsConfigPath = "";
  let activeSessionModelIds = [];

  // Modal Elements
  const btnManageModels = document.getElementById("btn-manage-models");
  const modelModalOverlay = document.getElementById("model-modal-overlay");
  const btnCloseModal = document.getElementById("btn-close-modal");
  const btnCancelEdit = document.getElementById("btn-cancel-edit");
  const btnAddNewModel = document.getElementById("btn-add-new-model");
  const modalModelsTableContainer = document.getElementById(
    "modal-models-table-container",
  );
  const modalModelsTableBody = document.getElementById(
    "modal-models-table-body",
  );
  const modalFormSection = document.getElementById("modal-form-section");
  const modelEditorForm = document.getElementById("model-editor-form");
  const modalFormTitle = document.getElementById("modal-form-title");
  const modalModelsConfigPath = document.getElementById(
    "modal-models-config-path",
  );

  function showModelFormSection() {
    if (modalFormSection) {
      modalFormSection.classList.remove("hidden");
      modalFormSection.style.display = "block";
    }
    if (modalModelsTableContainer) {
      modalModelsTableContainer.style.maxHeight = "230px";
    }
  }

  function hideModelFormSection() {
    if (modelEditorForm) {
      modelEditorForm.reset();
    }
    if (modalFormSection) {
      modalFormSection.classList.add("hidden");
      modalFormSection.style.display = "none";
    }
    if (modalModelsTableContainer) {
      modalModelsTableContainer.style.maxHeight = "460px";
    }
  }
  hideModelFormSection();

  // Tab Elements
  const tabBtnVisual = document.getElementById("tab-btn-visual");
  const tabBtnRaw = document.getElementById("tab-btn-raw");
  const modalTabVisual = document.getElementById("modal-tab-visual");
  const modalTabRaw = document.getElementById("modal-tab-raw");

  const rawJsonTextarea = document.getElementById("raw-json-editor-textarea");
  const btnFormatRawJson = document.getElementById("btn-format-raw-json");
  const btnCopyRawJson = document.getElementById("btn-copy-raw-json");
  const btnSaveRawJson = document.getElementById("btn-save-raw-json");
  const rawJsonStatusMsg = document.getElementById("raw-json-status-msg");

  function buildRawRegistryPayloadObj() {
    const obj = { models: globalModelsRegistry };
    if (
      Array.isArray(globalModelsDeletedIds) &&
      globalModelsDeletedIds.length > 0
    ) {
      obj.deleted_ids = globalModelsDeletedIds;
    }
    if (globalModelsReplaceDefaults) {
      obj.replace_defaults = true;
    }
    return obj;
  }

  if (tabBtnVisual && tabBtnRaw) {
    tabBtnVisual.addEventListener("click", () => {
      tabBtnVisual.style.background = "var(--color-primary)";
      tabBtnVisual.style.color = "#fff";
      tabBtnRaw.style.background = "rgba(255,255,255,0.08)";
      tabBtnRaw.style.color = "var(--color-text-muted)";
      if (modalTabVisual) modalTabVisual.style.display = "block";
      if (modalTabRaw) modalTabRaw.style.display = "none";
    });

    tabBtnRaw.addEventListener("click", () => {
      tabBtnRaw.style.background = "var(--color-primary)";
      tabBtnRaw.style.color = "#fff";
      tabBtnVisual.style.background = "rgba(255,255,255,0.08)";
      tabBtnVisual.style.color = "var(--color-text-muted)";
      if (modalTabRaw) modalTabRaw.style.display = "block";
      if (modalTabVisual) modalTabVisual.style.display = "none";
      if (rawJsonTextarea) {
        rawJsonTextarea.value = JSON.stringify(
          buildRawRegistryPayloadObj(),
          null,
          2,
        );
      }
    });
  }

  if (btnFormatRawJson && rawJsonTextarea) {
    btnFormatRawJson.addEventListener("click", () => {
      try {
        const parsed = JSON.parse(rawJsonTextarea.value);
        rawJsonTextarea.value = JSON.stringify(parsed, null, 2);
        if (rawJsonStatusMsg) {
          rawJsonStatusMsg.style.color = "#10b981";
          rawJsonStatusMsg.textContent = "✓ JSON formatted successfully";
        }
      } catch (err) {
        if (rawJsonStatusMsg) {
          rawJsonStatusMsg.style.color = "#ef4444";
          rawJsonStatusMsg.textContent = `⚠ Invalid JSON Syntax: ${err.message}`;
        }
      }
    });
  }

  if (btnCopyRawJson && rawJsonTextarea) {
    btnCopyRawJson.addEventListener("click", () => {
      navigator.clipboard.writeText(rawJsonTextarea.value).then(() => {
        btnCopyRawJson.textContent = "✅ Copied!";
        setTimeout(() => (btnCopyRawJson.textContent = "📋 Copy"), 2000);
      });
    });
  }

  const LOCAL_MODELS_STORAGE_KEY = "adk_tokenomics_models_config_v1";

  function formatRateVal(v) {
    const num = Number(v || 0);
    if (Math.abs(num * 100 - Math.round(num * 100)) > 1e-6) {
      return num.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    }
    return num.toFixed(2);
  }

  function parseValidRateClient(rawVal, fieldName) {
    if (
      rawVal === null ||
      rawVal === undefined ||
      typeof rawVal === "boolean" ||
      (typeof rawVal === "string" && rawVal.trim() === "")
    ) {
      throw new Error(
        `Invalid '${fieldName}' rate: must be a finite number between 0 and 10000.`,
      );
    }
    const num = Number(rawVal);
    if (!Number.isFinite(num) || num < 0 || num > 10000) {
      throw new Error(
        `Rate '${fieldName}' out of range: must be finite and between 0 and 10000.`,
      );
    }
    return num;
  }

  function hasOwnKey(obj, key) {
    return Boolean(obj && Object.hasOwn(obj, key));
  }

  function parseThinkingBudgetClient(rawTb) {
    if (typeof rawTb === "number" && Number.isFinite(rawTb)) {
      return Math.trunc(rawTb);
    }
    const s = String(
      rawTb !== undefined && rawTb !== null ? rawTb : "0",
    ).trim();
    if (/^-?\d+$/.test(s)) {
      return parseInt(s, 10);
    }
    const low = s.toLowerCase();
    if (["off", "low", "medium", "high", "dynamic"].includes(low)) {
      return low;
    }
    return s || 0;
  }

  function normalizeModelEntryClient(raw) {
    if (!raw || typeof raw !== "object") {
      throw new Error("Model configuration entry must be an object.");
    }
    const m = Object.assign({}, raw);
    const id = String(m.id || "").trim();
    if (!id) {
      throw new Error("Model configuration requires a non-empty 'id'.");
    }
    m.id = id;
    m.name = String(m.name || id.split("/").pop() || id).trim();
    m.provider = String(m.provider || "google")
      .trim()
      .toLowerCase();
    const p =
      m.pricing && typeof m.pricing === "object"
        ? Object.assign({}, m.pricing)
        : {};
    const rawIn = hasOwnKey(p, "input")
      ? p.input
      : hasOwnKey(p, "input_per_million")
        ? p.input_per_million
        : hasOwnKey(m, "input_rate")
          ? m.input_rate
          : 0.75;
    const inR = parseValidRateClient(rawIn, "input");

    const rawCac = hasOwnKey(p, "cached")
      ? p.cached
      : hasOwnKey(p, "cached_per_million")
        ? p.cached_per_million
        : hasOwnKey(m, "cached_rate")
          ? m.cached_rate
          : Number((inR * 0.25).toFixed(6));
    const cacR = parseValidRateClient(rawCac, "cached");

    const rawOut = hasOwnKey(p, "output")
      ? p.output
      : hasOwnKey(p, "output_per_million")
        ? p.output_per_million
        : hasOwnKey(m, "output_rate")
          ? m.output_rate
          : 3.75;
    const outR = parseValidRateClient(rawOut, "output");

    m.pricing = {
      input: inR,
      cached: cacR,
      output: outR,
      input_per_million: inR,
      cached_per_million: cacR,
      output_per_million: outR,
    };
    m.thinking_budget =
      m.thinking_budget !== undefined
        ? parseThinkingBudgetClient(m.thinking_budget)
        : 0;
    const parsedMaxTok = parseInt(
      String(m.max_output_tokens !== undefined ? m.max_output_tokens : 8192),
      10,
    );
    m.max_output_tokens =
      Number.isFinite(parsedMaxTok) && parsedMaxTok > 0 ? parsedMaxTok : 8192;
    return m;
  }

  window.normalizeModelEntryClient = normalizeModelEntryClient;

  function safeNormalizeModelsList(list) {
    if (!Array.isArray(list)) return [];
    const out = [];
    list.forEach((item) => {
      try {
        const norm = normalizeModelEntryClient(item);
        if (norm?.id) out.push(norm);
      } catch (_) {
        // Skip corrupt persisted entry during passive load
      }
    });
    return out;
  }

  function loadLocalModelsOverride() {
    try {
      const raw = localStorage.getItem(LOCAL_MODELS_STORAGE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      const list = Array.isArray(parsed)
        ? parsed
        : parsed && Array.isArray(parsed.models)
          ? parsed.models
          : null;
      if (!list || list.length === 0) return null;
      const valid = safeNormalizeModelsList(list);
      return valid.length > 0 ? valid : null;
    } catch (_e) {
      return null;
    }
  }

  function saveLocalModelsOverride(modelsList) {
    try {
      const normalized = safeNormalizeModelsList(modelsList || []);
      const payload = { models: normalized };
      if (
        Array.isArray(globalModelsDeletedIds) &&
        globalModelsDeletedIds.length > 0
      ) {
        payload.deleted_ids = globalModelsDeletedIds;
      }
      if (globalModelsReplaceDefaults) {
        payload.replace_defaults = true;
      }
      localStorage.setItem(
        LOCAL_MODELS_STORAGE_KEY,
        JSON.stringify(payload, null, 2),
      );
      return normalized;
    } catch (_e) {
      return modelsList || [];
    }
  }

  function syncRegistryViews() {
    if (modalModelsConfigPath && globalModelsConfigPath) {
      modalModelsConfigPath.textContent = globalModelsConfigPath;
    }
    if (rawJsonTextarea) {
      rawJsonTextarea.value = JSON.stringify(
        buildRawRegistryPayloadObj(),
        null,
        2,
      );
    }
    renderModalModelsTable();
  }

  const TIER_SUFFIX_TOKENS = new Set([
    "mini",
    "nano",
    "lite",
    "small",
    "flash",
    "haiku",
    "micro",
    "pro",
    "opus",
    "sonnet",
  ]);
  const PRO_TOKEN_RE = /(?:^|[-_.])pro(?:$|[-_.])/;
  const OPENAI_REASONING_RE = /^(?:o1|o3|o4)(?:$|[-_:@])/;

  function stripProviderPrefixes(modelStr) {
    return String(modelStr || "")
      .trim()
      .replace(
        /^(?:projects\/[^/]+\/locations\/[^/]+\/|publishers\/[^/]+\/models\/|models\/|vertex_ai\/|anthropic\/|openai\/|together_ai\/|fireworks_ai\/|groq\/|bedrock\/|azure\/|huggingface\/)+/,
        "",
      );
  }

  function isModelMatchingActive(m, activeId) {
    if (!m || !activeId) return false;
    const mId = String(m.id || "").trim();
    const shortId = stripProviderPrefixes(mId);
    const shortAt = shortId.includes("@") ? shortId.split("@")[0] : shortId;
    const shortTail = shortId.includes("/")
      ? shortId.split("/").pop()
      : shortId;
    const mName = String(m.name || "").trim();
    const cleanActive = stripProviderPrefixes(activeId);
    const cleanSeg = cleanActive.includes("/")
      ? cleanActive.split("/").pop()
      : cleanActive;
    const activeLower = String(activeId || "")
      .trim()
      .toLowerCase();
    const cleanLower = cleanActive.toLowerCase();
    const segLower = String(cleanSeg || "").toLowerCase();
    return (
      mId === activeId ||
      shortId === cleanActive ||
      mId.toLowerCase() === activeLower ||
      shortId.toLowerCase() === cleanLower ||
      shortAt.toLowerCase() === cleanLower ||
      shortTail.toLowerCase() === segLower ||
      mName === activeId ||
      mName.toLowerCase() === cleanLower
    );
  }

  function findMatchingModelInRegistry(modelId) {
    if (
      !modelId ||
      !Array.isArray(globalModelsRegistry) ||
      globalModelsRegistry.length === 0
    ) {
      return null;
    }
    const exact = globalModelsRegistry.find((m) =>
      isModelMatchingActive(m, modelId),
    );
    if (exact) return exact;

    const rawLower = String(modelId || "")
      .trim()
      .toLowerCase();
    const normLower = stripProviderPrefixes(modelId).toLowerCase();
    const segLower =
      (normLower.includes("/") ? normLower.split("/").pop() : normLower) || "";
    if (!segLower) return null;

    // Longest-prefix match at version/date boundary delimiters (-, @, :, _), skipping tier suffix tokens
    const prefixCandidates = [];
    globalModelsRegistry.forEach((m) => {
      const rawK = String(m.id || "")
        .trim()
        .toLowerCase();
      const shortK = stripProviderPrefixes(rawK).toLowerCase();
      const tailK = shortK.includes("/") ? shortK.split("/").pop() : shortK;
      const atK = shortK.includes("@") ? shortK.split("@")[0] : shortK;
      [rawK, shortK, tailK, atK].forEach((k) => {
        if (k && !k.includes(" ")) prefixCandidates.push([k, m]);
      });
    });
    prefixCandidates.sort((a, b) => b[0].length - a[0].length);
    for (let i = 0; i < prefixCandidates.length; i++) {
      const kLow = prefixCandidates[i][0];
      const candModel = prefixCandidates[i][1];
      const targets =
        normLower !== segLower ? [normLower, segLower] : [segLower];
      for (let j = 0; j < targets.length; j++) {
        const target = targets[j];
        if (
          target.length > kLow.length &&
          target.startsWith(kLow) &&
          "-@:_".includes(target.charAt(kLow.length))
        ) {
          const rem = target.slice(kLow.length).replace(/^[-@:_]+/, "");
          const firstRemToken = rem ? rem.split(/[-@:_.]/)[0] : "";
          if (TIER_SUFFIX_TOKENS.has(firstRemToken)) continue;
          return candModel;
        }
      }
    }

    if (
      rawLower.startsWith("ollama/") ||
      rawLower.startsWith("vllm/") ||
      rawLower.startsWith("hosted_vllm/") ||
      rawLower.startsWith("local/")
    ) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("ollama"),
        ) || null
      );
    }

    // Family fallback heuristics on the final '/' path segment (aligned with core/pricing.py)
    if (segLower.includes("haiku")) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("haiku"),
        ) || null
      );
    }
    if (segLower.includes("opus")) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("opus"),
        ) || null
      );
    }
    if (segLower.includes("sonnet") || segLower.includes("claude")) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("sonnet"),
        ) || null
      );
    }
    if (segLower.includes("llama")) {
      if (segLower.includes("8b")) {
        return (
          globalModelsRegistry.find(
            (m) =>
              String(m.id || "")
                .toLowerCase()
                .includes("llama") &&
              String(m.id || "")
                .toLowerCase()
                .includes("8b"),
          ) || null
        );
      }
      if (segLower.includes("405b")) {
        return (
          globalModelsRegistry.find(
            (m) =>
              String(m.id || "")
                .toLowerCase()
                .includes("llama") &&
              String(m.id || "")
                .toLowerCase()
                .includes("405b"),
          ) || null
        );
      }
      return (
        globalModelsRegistry.find(
          (m) =>
            String(m.id || "")
              .toLowerCase()
              .includes("llama") &&
            String(m.id || "")
              .toLowerCase()
              .includes("70b"),
        ) || null
      );
    }
    if (segLower.includes("deepseek")) {
      if (segLower.includes("r1")) {
        return (
          globalModelsRegistry.find((m) =>
            String(m.id || "")
              .toLowerCase()
              .includes("deepseek-r1"),
          ) || null
        );
      }
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("deepseek-v3"),
        ) || null
      );
    }
    if (segLower.includes("mistral") || segLower.includes("mixtral")) {
      if (segLower.includes("small")) {
        return (
          globalModelsRegistry.find((m) =>
            String(m.id || "")
              .toLowerCase()
              .includes("mistral-small"),
          ) || null
        );
      }
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("mistral-large"),
        ) || null
      );
    }
    if (segLower.includes("qwen")) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("qwen"),
        ) || null
      );
    }
    if (segLower.includes("gemma")) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("gemma"),
        ) || null
      );
    }
    if (segLower.includes("gpt") || OPENAI_REASONING_RE.test(segLower)) {
      if (segLower.includes("mini") || segLower.includes("nano")) {
        return (
          globalModelsRegistry.find((m) =>
            String(m.id || "")
              .toLowerCase()
              .includes("gpt-4o-mini"),
          ) || null
        );
      }
      return (
        globalModelsRegistry.find(
          (m) => String(m.id || "").toLowerCase() === "gpt-4o",
        ) || null
      );
    }
    if (segLower.includes("argon") || PRO_TOKEN_RE.test(segLower)) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("argon"),
        ) || null
      );
    }
    if (segLower.includes("flash")) {
      return (
        globalModelsRegistry.find((m) =>
          String(m.id || "")
            .toLowerCase()
            .includes("flash"),
        ) || null
      );
    }
    return null;
  }

  function applyRegistryRatesToMetrics(data) {
    if (!data) return;
    const turns = Array.isArray(data.turns) ? data.turns : [];
    const metrics =
      data.metrics && typeof data.metrics === "object" ? data.metrics : {};
    const simulations =
      data.simulations && typeof data.simulations === "object"
        ? data.simulations
        : {};
    data.simulations = simulations;

    if (turns.length > 0) {
      const hasRegistry =
        Array.isArray(globalModelsRegistry) && globalModelsRegistry.length > 0;
      const costByApp = {};
      const savingsByApp = {};
      const turnCountByApp = {};
      const serverTurnCostByApp = {};
      const modelsByApp = {};

      turns.forEach((t) => {
        const modelId = t.model_name || getActiveToolbarModelId() || "";
        const matchedModel = hasRegistry
          ? findMatchingModelInRegistry(modelId)
          : null;
        const cTok = Number(t.cached_tokens || 0);
        const pTok = Number(t.prompt_tokens || 0);
        const freshTok =
          t.input_tokens !== undefined
            ? Number(t.input_tokens)
            : Math.max(0, pTok - cTok);
        const oTok = Number(t.output_tokens || 0);
        const thTok = Number(t.thinking_tokens || 0);
        const origTurnCost = Number(
          t.estimated_cost ?? t.total_cost ?? t.cost ?? 0,
        );

        if (matchedModel?.pricing) {
          const inRatePerM = Number(matchedModel.pricing.input ?? 0.75);
          const cacRatePerM = Number(matchedModel.pricing.cached ?? 0.075);
          const outRatePerM = Number(matchedModel.pricing.output ?? 3.75);
          const newCost =
            (freshTok * inRatePerM +
              cTok * cacRatePerM +
              (oTok + thTok) * outRatePerM) /
            1000000;
          const turnSavings =
            (cTok * Math.max(0, inRatePerM - cacRatePerM)) / 1000000;
          t.estimated_cost = newCost;
          t.total_cost = newCost;
          t.cost = newCost;
          t.savings_dollars = turnSavings;
          t.uncached_cost = newCost + turnSavings;
        } else if (t.savings_dollars === undefined) {
          const fallbackSavings = (cTok * Math.max(0, 0.75 - 0.075)) / 1000000;
          t.savings_dollars = fallbackSavings;
          t.uncached_cost =
            Number(t.estimated_cost || t.total_cost || 0) + fallbackSavings;
        }

        const appKey = t.app_name || "agent";
        if (t.model_name) {
          const cleanTurnModel = String(t.model_name).trim();
          if (cleanTurnModel) {
            if (!modelsByApp[appKey]) modelsByApp[appKey] = [];
            if (!modelsByApp[appKey].includes(cleanTurnModel)) {
              modelsByApp[appKey].push(cleanTurnModel);
            }
          }
        }
        turnCountByApp[appKey] = (turnCountByApp[appKey] || 0) + 1;
        serverTurnCostByApp[appKey] =
          (serverTurnCostByApp[appKey] || 0) + origTurnCost;
        costByApp[appKey] =
          (costByApp[appKey] || 0) +
          Number(t.estimated_cost || t.total_cost || 0);
        savingsByApp[appKey] =
          (savingsByApp[appKey] || 0) + Number(t.savings_dollars || 0);
      });

      Object.keys(costByApp).forEach((appKey) => {
        const appData = metrics[appKey];
        if (appData && typeof appData === "object") {
          if (modelsByApp[appKey] && modelsByApp[appKey].length > 0) {
            const mergedModels = Array.isArray(appData.models)
              ? appData.models.slice()
              : [];
            modelsByApp[appKey].forEach((mStr) => {
              if (!mergedModels.includes(mStr)) mergedModels.push(mStr);
            });
            appData.models = mergedModels;
            if (!appData.model_name) {
              appData.model_name = mergedModels[mergedModels.length - 1];
            }
          }
          const reportedTurns = Number(appData.turns || 0);
          const returnedTurns = turnCountByApp[appKey] || 0;
          const isAppTruncated =
            Boolean(data.turns_truncated) || reportedTurns > returnedTurns;
          if (!isAppTruncated) {
            if (hasRegistry) {
              appData.cost = costByApp[appKey];
            }
            appData.savings_dollars = savingsByApp[appKey] || 0;
          } else {
            // Preserve server's full-window aggregate cost & savings unless client-only rates diverge
            const srvSampleCost = serverTurnCostByApp[appKey] || 0;
            const cliSampleCost = costByApp[appKey] || 0;
            if (
              hasRegistry &&
              srvSampleCost > 0 &&
              Math.abs(cliSampleCost - srvSampleCost) > 1e-6
            ) {
              const ratio = cliSampleCost / srvSampleCost;
              appData.cost = Number(appData.cost || 0) * ratio;
            }
            if (appData.savings_dollars === undefined) {
              const turnScale =
                returnedTurns > 0 && reportedTurns > returnedTurns
                  ? reportedTurns / returnedTurns
                  : 1;
              appData.savings_dollars = (savingsByApp[appKey] || 0) * turnScale;
            }
          }
        }
      });

      if (hasRegistry) {
        const totalMetricTurns = Object.values(metrics).reduce(
          (acc, m) => acc + Number(m?.turns || 0),
          0,
        );
        const useAggregateTokensForSim =
          Boolean(data.turns_truncated) || totalMetricTurns > turns.length;
        globalModelsRegistry.forEach((m) => {
          const p = m.pricing || { input: 0.75, cached: 0.075, output: 3.75 };
          const inRate = Number(p.input || 0) / 1000000;
          const cacRate = Number(p.cached || 0) / 1000000;
          const outRate = Number(p.output || 0) / 1000000;
          let simCost = 0;
          if (useAggregateTokensForSim) {
            Object.values(metrics).forEach((app) => {
              if (!app || typeof app !== "object") return;
              const cTok = Number(app.cached || 0);
              const pTok = Number(app.input || 0);
              const freshTok = Math.max(0, pTok - cTok);
              const oTok = Number(app.output || 0);
              const thTok = Number(app.thinking || 0);
              simCost +=
                freshTok * inRate + cTok * cacRate + (oTok + thTok) * outRate;
            });
          } else {
            turns.forEach((t) => {
              const cTok = Number(t.cached_tokens || 0);
              const pTok = Number(t.prompt_tokens || 0);
              const freshTok =
                t.input_tokens !== undefined
                  ? Number(t.input_tokens)
                  : Math.max(0, pTok - cTok);
              const oTok = Number(t.output_tokens || 0);
              const thTok = Number(t.thinking_tokens || 0);
              simCost +=
                freshTok * inRate + cTok * cacRate + (oTok + thTok) * outRate;
            });
          }
          simulations[m.name] = simCost;
        });
      }
    }
  }

  function closeModelModal() {
    hideModelFormSection();
    if (modelModalOverlay) {
      modelModalOverlay.classList.add("hidden");
      modelModalOverlay.style.display = "none";
    }
  }

  function showModelToast(message, isError = false) {
    let toast = document.getElementById("model-save-toast");
    if (!toast) {
      toast = document.createElement("div");
      toast.id = "model-save-toast";
      toast.style.cssText = [
        "position:fixed",
        "top:1.25rem",
        "right:1.25rem",
        "z-index:9999",
        "background:rgba(16, 185, 129, 0.95)",
        "color:#ffffff",
        "padding:0.75rem 1.15rem",
        "border-radius:10px",
        "font-size:0.85rem",
        "font-weight:600",
        "box-shadow:0 10px 25px rgba(0,0,0,0.45)",
        "border:1px solid rgba(255,255,255,0.2)",
        "transition:opacity 0.25s ease, transform 0.25s ease",
        "opacity:0",
        "transform:translateY(-8px)",
        "pointer-events:none",
      ].join(";");
      document.body.appendChild(toast);
    }
    toast.style.background = isError
      ? "rgba(239, 68, 68, 0.95)"
      : "rgba(16, 185, 129, 0.95)";
    toast.textContent = message;
    toast.style.opacity = "1";
    toast.style.transform = "translateY(0)";
    if (toast._hideTimer) clearTimeout(toast._hideTimer);
    toast._hideTimer = setTimeout(() => {
      toast.style.opacity = "0";
      toast.style.transform = "translateY(-8px)";
    }, 3000);
  }

  function applyServerModelsMeta(data) {
    if (!data || typeof data !== "object") return;
    const cfgPath = data.models_config_path || data.config_path || "";
    if (cfgPath) {
      globalModelsConfigPath = String(cfgPath);
    }
    if (Array.isArray(data.deleted_ids)) {
      globalModelsDeletedIds = data.deleted_ids
        .map((x) => String(x || "").trim())
        .filter(Boolean);
    } else if (!Object.hasOwn(data, "deleted_ids")) {
      globalModelsDeletedIds = [];
    }
    globalModelsReplaceDefaults = Boolean(data.replace_defaults);
  }

  function refreshTelemetryAfterRateCardChange() {
    pollMetrics();
    const isBqTabVisible =
      activeMainTab === "bq" ||
      tabBtnBq?.classList.contains("active") ||
      (tabPaneBq &&
        !tabPaneBq.classList.contains("hidden") &&
        tabPaneBq.style.display !== "none");
    if (
      isBqTabVisible ||
      (Array.isArray(bqCachedRows) && bqCachedRows.length > 0)
    ) {
      fetchBqStats();
      fetchBqLogs(bqCurrentOffset);
    }
  }

  if (btnSaveRawJson && rawJsonTextarea) {
    btnSaveRawJson.addEventListener("click", () => {
      let parsed;
      try {
        parsed = JSON.parse(rawJsonTextarea.value);
      } catch (err) {
        if (rawJsonStatusMsg) {
          rawJsonStatusMsg.style.color = "#ef4444";
          rawJsonStatusMsg.textContent = `⚠ Cannot Save: Invalid JSON Syntax (${err.message})`;
        }
        return;
      }

      const rawList = Array.isArray(parsed)
        ? parsed
        : parsed && Array.isArray(parsed.models)
          ? parsed.models
          : null;
      if (!rawList) {
        if (rawJsonStatusMsg) {
          rawJsonStatusMsg.style.color = "#ef4444";
          rawJsonStatusMsg.textContent =
            '⚠ Cannot Save: JSON must contain a "models" array.';
        }
        return;
      }

      let validatedList;
      try {
        validatedList = rawList.map(normalizeModelEntryClient);
      } catch (err) {
        if (rawJsonStatusMsg) {
          rawJsonStatusMsg.style.color = "#ef4444";
          rawJsonStatusMsg.textContent = `⚠ Validation Error: ${err.message}`;
        }
        showModelToast(`⚠ ${err.message}`, true);
        return;
      }

      const rawObjToSave = { models: validatedList };
      if (parsed && !Array.isArray(parsed)) {
        if (Array.isArray(parsed.deleted_ids)) {
          rawObjToSave.deleted_ids = parsed.deleted_ids;
        } else if (globalModelsDeletedIds.length > 0) {
          rawObjToSave.deleted_ids = globalModelsDeletedIds;
        }
        if (parsed.replace_defaults !== undefined) {
          rawObjToSave.replace_defaults = Boolean(parsed.replace_defaults);
        } else if (globalModelsReplaceDefaults) {
          rawObjToSave.replace_defaults = true;
        }
      }

      const prevRegistry = globalModelsRegistry.slice();
      fetch("/api/models_config_raw", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          raw_json: JSON.stringify(rawObjToSave, null, 2),
        }),
      })
        .then(async (res) => {
          const text = await res.text();
          let data = null;
          try {
            data = text ? JSON.parse(text) : null;
          } catch (_) {
            data = null;
          }
          if (
            !res.ok ||
            !data ||
            data.status !== "success" ||
            !Array.isArray(data.models)
          ) {
            globalModelsRegistry = prevRegistry;
            syncRegistryViews();
            const errMsg = data?.message ? data.message : `HTTP ${res.status}`;
            if (rawJsonStatusMsg) {
              rawJsonStatusMsg.style.color = "#ef4444";
              rawJsonStatusMsg.textContent = `⚠ Save Rejected: ${errMsg}`;
            }
            showModelToast(`⚠ Failed to save rate card: ${errMsg}`, true);
            return;
          }
          applyServerModelsMeta(data);
          globalModelsRegistry = data.models.map(normalizeModelEntryClient);
          saveLocalModelsOverride(globalModelsRegistry);
          syncRegistryViews();
          closeModelModal();
          showModelToast(
            `✅ Saved ${globalModelsRegistry.length} models to rate card`,
          );
          refreshTelemetryAfterRateCardChange();
        })
        .catch((err) => {
          globalModelsRegistry = prevRegistry;
          syncRegistryViews();
          const errMsg = err?.message ? err.message : "Network error";
          if (rawJsonStatusMsg) {
            rawJsonStatusMsg.style.color = "#ef4444";
            rawJsonStatusMsg.textContent = `⚠ Save Failed: ${errMsg}`;
          }
          showModelToast(`⚠ Failed to save rate card: ${errMsg}`, true);
        });
    });
  }

  function fetchModelsRegistry() {
    return fetch("/api/models")
      .then(async (res) => {
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        const text = await res.text();
        const data = text ? JSON.parse(text) : {};
        if (!Array.isArray(data.models)) {
          throw new Error("Malformed /api/models payload");
        }
        applyServerModelsMeta(data);
        const serverModels = safeNormalizeModelsList(data.models);
        globalModelsRegistry = serverModels;
        saveLocalModelsOverride(serverModels);
        syncRegistryViews();
      })
      .catch((err) => {
        console.warn(
          "Server /api/models unreachable; using offline cached models fallback:",
          err,
        );
        const localFallback = loadLocalModelsOverride();
        if (localFallback && localFallback.length > 0) {
          globalModelsRegistry = localFallback;
          syncRegistryViews();
        }
      });
  }

  function getActiveToolbarModelId() {
    if (
      Array.isArray(activeSessionModelIds) &&
      activeSessionModelIds.length > 0
    ) {
      const latestRaw = String(
        activeSessionModelIds[activeSessionModelIds.length - 1] || "",
      ).trim();
      return stripProviderPrefixes(latestRaw) || latestRaw;
    }
    return "";
  }

  function getProviderBadgeClass(provider) {
    const p = String(provider || "").toLowerCase();
    if (p === "anthropic") return "badge-purple";
    if (p === "openai") return "badge-primary";
    if (p === "open_weight") return "badge-warning";
    if (p === "custom") return "badge-secondary";
    return "badge-success";
  }

  function renderModalModelsTable() {
    if (!modalModelsTableBody) return;
    modalModelsTableBody.innerHTML = "";
    const fallbackToolbarId = getActiveToolbarModelId();
    const activeIds =
      Array.isArray(activeSessionModelIds) && activeSessionModelIds.length > 0
        ? activeSessionModelIds
        : fallbackToolbarId
          ? [fallbackToolbarId]
          : [];

    globalModelsRegistry.forEach((m) => {
      const tr = document.createElement("tr");
      const pricing = m.pricing || { input: 0.75, cached: 0.075, output: 3.75 };
      const isActive = activeIds.some((aid) => isModelMatchingActive(m, aid));

      if (isActive) {
        tr.style.background = "rgba(16, 185, 129, 0.08)";
        tr.style.borderLeft = "3px solid var(--color-success)";
      }

      tr.style.cursor = "pointer";
      tr.title = `Click to edit ${m.name}`;
      tr.innerHTML = `
        <td style="font-family:monospace; font-size:0.75rem; color:var(--color-primary);">
          ${escapeHtml(m.id)} ${isActive ? '<span class="badge badge-success" style="margin-left:0.35rem;">Active 🟢</span>' : ""}
        </td>
        <td style="font-weight:600;">${escapeHtml(m.name)}</td>
        <td><span class="badge ${getProviderBadgeClass(m.provider)}">${escapeHtml(m.provider)}</span></td>
        <td>$${formatRateVal(pricing.input)}</td>
        <td>$${formatRateVal(pricing.cached)}</td>
        <td>$${formatRateVal(pricing.output)}</td>
        <td style="text-align:center;">
          <button type="button" class="btn btn-edit-model" data-id="${escapeHtml(m.id)}" style="padding:0.25rem 0.5rem; font-size:0.75rem; background:rgba(59,130,246,0.15); color:#60a5fa; border:1px solid rgba(59,130,246,0.3); border-radius:6px; margin-right:0.25rem; cursor:pointer;">Edit</button>
          <button type="button" class="btn btn-delete-model" data-id="${escapeHtml(m.id)}" style="padding:0.25rem 0.5rem; font-size:0.75rem; background:rgba(239,68,68,0.15); color:#f87171; border:1px solid rgba(239,68,68,0.3); border-radius:6px; cursor:pointer;">Delete</button>
        </td>
      `;
      tr.addEventListener("click", (ev) => {
        const tgt = ev?.target;
        if (
          tgt?.className &&
          String(tgt.className).includes("btn-delete-model")
        ) {
          return;
        }
        populateEditForm(m);
        if (modelEditorForm)
          modelEditorForm.scrollIntoView({
            behavior: "smooth",
            block: "nearest",
          });
      });
      modalModelsTableBody.appendChild(tr);
    });

    document.querySelectorAll(".btn-edit-model").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        if (e && typeof e.stopPropagation === "function") e.stopPropagation();
        const id = e.currentTarget.getAttribute("data-id");
        const m = globalModelsRegistry.find((x) => x.id === id);
        if (m) {
          populateEditForm(m);
          if (modelEditorForm)
            modelEditorForm.scrollIntoView({
              behavior: "smooth",
              block: "nearest",
            });
        }
      });
    });

    document.querySelectorAll(".btn-delete-model").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        if (e && typeof e.stopPropagation === "function") e.stopPropagation();
        const id = e.currentTarget.getAttribute("data-id");
        if (confirm(`Are you sure you want to delete model '${id}'?`)) {
          deleteModel(id);
        }
      });
    });
  }

  function ensureSelectOption(selectEl, valStr, labelStr) {
    if (!selectEl) return;
    const opts = Array.isArray(selectEl.options)
      ? selectEl.options
      : Array.from(selectEl.options || []);
    if (!opts.some((o) => o && o.value === valStr)) {
      const opt = document.createElement("option");
      opt.value = valStr;
      opt.textContent = labelStr || valStr;
      if (typeof selectEl.appendChild === "function") {
        selectEl.appendChild(opt);
      }
      if (Array.isArray(selectEl.options) && !selectEl.options.includes(opt)) {
        selectEl.options.push(opt);
      }
    }
    selectEl.value = valStr;
  }

  function populateEditForm(m) {
    if (!modelEditorForm) return;
    showModelFormSection();
    document.getElementById("form-model-id").value = m.id;
    document.getElementById("form-model-name").value = m.name;
    const provSel = document.getElementById("form-model-provider");
    const provVal = String(m.provider || "google")
      .trim()
      .toLowerCase();
    ensureSelectOption(provSel, provVal, provVal);

    document.getElementById("form-price-input").value = m.pricing
      ? m.pricing.input
      : 0.75;
    document.getElementById("form-price-cached").value = m.pricing
      ? m.pricing.cached
      : 0.075;
    document.getElementById("form-price-output").value = m.pricing
      ? m.pricing.output
      : 3.75;

    const tbSel = document.getElementById("form-thinking-budget");
    const rawTb =
      m.thinking_budget !== undefined && m.thinking_budget !== null
        ? String(m.thinking_budget).trim()
        : "0";
    ensureSelectOption(tbSel, rawTb, rawTb);

    const maxSel = document.getElementById("form-max-tokens");
    const rawMax =
      m.max_output_tokens !== undefined && m.max_output_tokens !== null
        ? String(m.max_output_tokens).trim()
        : "8192";
    ensureSelectOption(maxSel, rawMax, `${rawMax} Tokens`);

    if (modalFormTitle) modalFormTitle.textContent = `Edit Model: ${m.name}`;
  }

  function resetEditForm() {
    if (modelEditorForm) modelEditorForm.reset();
    if (modalFormTitle)
      modalFormTitle.textContent = "Add New Model Configuration";
  }

  function deleteModel(modelId) {
    const prevRegistry = globalModelsRegistry.slice();
    fetch(`/api/models?id=${encodeURIComponent(modelId)}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
    })
      .then(async (res) => {
        const text = await res.text();
        let data = null;
        try {
          data = text ? JSON.parse(text) : null;
        } catch (_) {
          data = null;
        }
        if (
          !res.ok ||
          !data ||
          data.status !== "success" ||
          !Array.isArray(data.models)
        ) {
          globalModelsRegistry = prevRegistry;
          syncRegistryViews();
          const errMsg = data?.message ? data.message : `HTTP ${res.status}`;
          if (modalFormTitle) {
            modalFormTitle.innerHTML = `<span style="color:#ef4444;">⚠ Delete failed: ${escapeHtml(errMsg)}</span>`;
          }
          showModelToast(`⚠ Failed to delete ${modelId}: ${errMsg}`, true);
          return;
        }
        applyServerModelsMeta(data);
        globalModelsRegistry = data.models.map(normalizeModelEntryClient);
        saveLocalModelsOverride(globalModelsRegistry);
        syncRegistryViews();
        if (modalFormTitle) {
          modalFormTitle.innerHTML = `<span style="color:#10b981;">✓ Deleted ${escapeHtml(modelId)}</span>`;
        }
        showModelToast(`✅ Deleted ${modelId}`);
        refreshTelemetryAfterRateCardChange();
      })
      .catch((err) => {
        globalModelsRegistry = prevRegistry;
        syncRegistryViews();
        const errMsg = err?.message ? err.message : "Network error";
        if (modalFormTitle) {
          modalFormTitle.innerHTML = `<span style="color:#ef4444;">⚠ Delete failed: ${escapeHtml(errMsg)}</span>`;
        }
        showModelToast(`⚠ Failed to delete ${modelId}: ${errMsg}`, true);
      });
  }

  if (btnManageModels) {
    btnManageModels.addEventListener("click", (e) => {
      e.preventDefault();
      if (rawJsonStatusMsg) rawJsonStatusMsg.textContent = "";
      hideModelFormSection();
      fetchModelsRegistry();
      if (modelModalOverlay) {
        modelModalOverlay.classList.remove("hidden");
        modelModalOverlay.style.display = "flex";
      }
    });
  }

  if (btnAddNewModel) {
    btnAddNewModel.addEventListener("click", (e) => {
      e.preventDefault();
      if (tabBtnVisual && tabBtnRaw && modalTabVisual && modalTabRaw) {
        tabBtnVisual.style.background = "var(--color-primary)";
        tabBtnVisual.style.color = "#fff";
        tabBtnRaw.style.background = "rgba(255,255,255,0.08)";
        tabBtnRaw.style.color = "var(--color-text-muted)";
        modalTabVisual.style.display = "block";
        modalTabRaw.style.display = "none";
      }
      resetEditForm();
      showModelFormSection();
      if (modelEditorForm)
        modelEditorForm.scrollIntoView({
          behavior: "smooth",
          block: "nearest",
        });
    });
  }

  if (btnCloseModal) {
    btnCloseModal.addEventListener("click", (e) => {
      e.preventDefault();
      closeModelModal();
    });
  }

  if (btnCancelEdit) {
    btnCancelEdit.addEventListener("click", (e) => {
      e.preventDefault();
      hideModelFormSection();
    });
  }

  if (modelModalOverlay) {
    modelModalOverlay.addEventListener("click", (e) => {
      if (e.target === modelModalOverlay) {
        closeModelModal();
      }
    });
  }

  function stripGoogleOnlyPrefix(idStr) {
    const s = String(idStr || "").trim();
    const low = s.toLowerCase();
    if (low.startsWith("publishers/google/models/"))
      return s.slice("publishers/google/models/".length);
    if (low.startsWith("models/")) return s.slice("models/".length);
    return s;
  }

  if (modelEditorForm) {
    modelEditorForm.addEventListener("submit", (e) => {
      e.preventDefault();
      const rawId = document.getElementById("form-model-id").value.trim();
      const rawIdLow = rawId.toLowerCase();
      const strippedRawLow = stripGoogleOnlyPrefix(rawId).toLowerCase();
      const existingModel = globalModelsRegistry.find((x) => {
        const xId = String(x.id || "").trim();
        const xLow = xId.toLowerCase();
        if (xLow === rawIdLow) return true;
        if (
          xLow.startsWith("publishers/google/models/") &&
          stripGoogleOnlyPrefix(xId).toLowerCase() === strippedRawLow &&
          (rawIdLow === strippedRawLow ||
            rawIdLow.startsWith("models/") ||
            rawIdLow.startsWith("publishers/google/models/"))
        ) {
          return true;
        }
        return false;
      });

      const tbVal = parseThinkingBudgetClient(
        document.getElementById("form-thinking-budget").value,
      );
      const maxTokVal =
        parseInt(document.getElementById("form-max-tokens").value, 10) || 8192;

      const modelEntry = Object.assign({}, existingModel || {}, {
        id: rawId,
        name: document.getElementById("form-model-name").value.trim(),
        provider: document.getElementById("form-model-provider").value,
        pricing: {
          input: document.getElementById("form-price-input").value,
          cached: document.getElementById("form-price-cached").value,
          output: document.getElementById("form-price-output").value,
        },
        thinking_budget: tbVal,
        max_output_tokens: maxTokVal,
      });

      if (
        existingModel &&
        Array.isArray(existingModel.allowed_thinking_budgets)
      ) {
        const copyTb = existingModel.allowed_thinking_budgets.slice();
        if (!copyTb.includes(tbVal)) copyTb.push(tbVal);
        modelEntry.allowed_thinking_budgets = copyTb;
      } else if (!existingModel) {
        modelEntry.allowed_thinking_budgets =
          typeof tbVal === "string"
            ? ["off", "low", "medium", "high"]
            : Array.from(new Set([0, 1024, 4096, -1, tbVal]));
      }

      if (
        existingModel &&
        Array.isArray(existingModel.allowed_max_output_tokens)
      ) {
        const copyMax = existingModel.allowed_max_output_tokens.slice();
        if (!copyMax.includes(maxTokVal)) copyMax.push(maxTokVal);
        modelEntry.allowed_max_output_tokens = copyMax;
      } else if (!existingModel) {
        modelEntry.allowed_max_output_tokens = Array.from(
          new Set([1024, 2048, 4096, 8192, 16384, 32768, 65536, maxTokVal]),
        );
      }

      let normEntry;
      try {
        normEntry = normalizeModelEntryClient(modelEntry);
      } catch (err) {
        showModelToast(`⚠ ${err.message}`, true);
        return;
      }

      const prevRegistry = globalModelsRegistry.slice();
      fetch("/api/models", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(normEntry),
      })
        .then(async (res) => {
          const text = await res.text();
          let data = null;
          try {
            data = text ? JSON.parse(text) : null;
          } catch (_) {
            data = null;
          }
          if (
            !res.ok ||
            !data ||
            data.status !== "success" ||
            !Array.isArray(data.models)
          ) {
            globalModelsRegistry = prevRegistry;
            syncRegistryViews();
            const errMsg = data?.message ? data.message : `HTTP ${res.status}`;
            showModelToast(
              `⚠ Failed to save ${normEntry.name}: ${errMsg}`,
              true,
            );
            return;
          }
          applyServerModelsMeta(data);
          globalModelsRegistry = data.models.map(normalizeModelEntryClient);
          saveLocalModelsOverride(globalModelsRegistry);
          syncRegistryViews();
          closeModelModal();
          showModelToast(
            `✅ Saved ${normEntry.name} ($${formatRateVal(normEntry.pricing.input)} / $${formatRateVal(normEntry.pricing.cached)} / $${formatRateVal(normEntry.pricing.output)})`,
          );
          refreshTelemetryAfterRateCardChange();
        })
        .catch((err) => {
          globalModelsRegistry = prevRegistry;
          syncRegistryViews();
          const errMsg = err?.message ? err.message : "Network error";
          showModelToast(`⚠ Failed to save ${normEntry.name}: ${errMsg}`, true);
        });
    });
  }

  // Initializing Dashboard Sync
  initCharts();
  initSortableTables();
  fetchModelsRegistry().then(() => {
    startPolling();
  });

  // ==========================================
  // BIGQUERY TABLE LOGS EXPLORER ENGINE
  // ==========================================
  let bqCurrentOffset = 0;
  let bqCurrentLimit = 50;
  let bqTotalRows = 0;
  let bqCachedRows = [];
  let bqLogsSeq = 0;
  let bqLogsAbortController = null;
  let bqStatsSeq = 0;

  const bqFilterApp = document.getElementById("bq-filter-app");
  const bqSearchQuery = document.getElementById("bq-search-query");
  const bqFilterLimit = document.getElementById("bq-filter-limit");
  const btnBqApplyFilter = document.getElementById("btn-bq-apply-filter");
  const btnBqRefresh = document.getElementById("btn-bq-refresh");
  const btnBqExportCsv = document.getElementById("btn-bq-export-csv");
  const btnBqExportJson = document.getElementById("btn-bq-export-json");
  const bqLogsTableBody = document.getElementById("bq-logs-table-body");
  const bqTableCountBadge = document.getElementById("bq-table-count-badge");
  const bqPageRange = document.getElementById("bq-page-range");
  const bqTotalCount = document.getElementById("bq-total-count");
  const bqPageCurrent = document.getElementById("bq-page-current");
  const btnBqPrevPage = document.getElementById("btn-bq-prev-page");
  const btnBqNextPage = document.getElementById("btn-bq-next-page");

  const bqRowModalOverlay = document.getElementById("bq-row-modal-overlay");
  const btnCloseBqModal = document.getElementById("btn-close-bq-modal");
  const modalBqApp = document.getElementById("modal-bq-app");
  const modalBqModel = document.getElementById("modal-bq-model");
  const modalBqCost = document.getElementById("modal-bq-cost");
  const modalBqLatency = document.getElementById("modal-bq-latency");
  const modalBqTime = document.getElementById("modal-bq-time");
  const modalBqQuery = document.getElementById("modal-bq-query");
  const modalBqResponse = document.getElementById("modal-bq-response");
  const modalBqRawJson = document.getElementById("modal-bq-raw-json");
  const btnCopyBqJson = document.getElementById("btn-copy-bq-json");

  if (btnCloseBqModal && bqRowModalOverlay) {
    btnCloseBqModal.addEventListener("click", () => {
      bqRowModalOverlay.classList.add("hidden");
      bqRowModalOverlay.style.display = "none";
    });
    bqRowModalOverlay.addEventListener("click", (e) => {
      if (e.target === bqRowModalOverlay) {
        bqRowModalOverlay.classList.add("hidden");
        bqRowModalOverlay.style.display = "none";
      }
    });
  }

  if (btnCopyBqJson && modalBqRawJson) {
    btnCopyBqJson.addEventListener("click", () => {
      navigator.clipboard.writeText(modalBqRawJson.textContent).then(() => {
        const orig = btnCopyBqJson.textContent;
        btnCopyBqJson.textContent = "✅ Copied!";
        setTimeout(() => {
          btnCopyBqJson.textContent = orig;
        }, 1500);
      });
    });
  }

  function loadBqExplorerView() {
    fetchBqStats();
    fetchBqLogs(0);
  }

  window.switchToSessionOverview = () => {
    switchTab("telemetry");
  };

  function fetchBqStats() {
    const seq = ++bqStatsSeq;
    fetch("/api/bq/stats")
      .then((res) => res.json())
      .then((stats) => {
        if (seq !== bqStatsSeq) return;
        const elTurns = document.getElementById("bq-stat-total-turns");
        const elCost = document.getElementById("bq-stat-total-cost");
        const elTokens = document.getElementById("bq-stat-total-tokens");
        const elSessions = document.getElementById("bq-stat-unique-sessions");

        if (stats.status === "dev_mode") {
          if (elTurns) elTurns.textContent = "Local Dev Mode";
          if (elCost) elCost.textContent = "Memory Sink";
          if (elTokens) elTokens.textContent = "JSON Traces";
          if (elSessions) elSessions.textContent = "Active in Tab 1";
          return;
        }

        if (stats.status === "error") {
          if (elTurns) elTurns.textContent = "Setup Needed";
          if (elCost) elCost.textContent = "$0.00000";
          if (elTokens) elTokens.textContent = "—";
          if (elSessions) elSessions.textContent = "—";
          return;
        }

        if (elTurns)
          elTurns.textContent = `${Number(stats.total_turns || 0).toLocaleString()} turns`;
        if (elCost)
          elCost.textContent = `$${Number(stats.total_cost || 0).toFixed(5)}`;
        if (elTokens) {
          const freshNum =
            stats.total_fresh_input !== undefined
              ? Number(stats.total_fresh_input || 0)
              : Math.max(
                  0,
                  Number(stats.total_input || 0) -
                    Number(stats.total_cached || 0),
                );
          const fresh = freshNum.toLocaleString();
          const cached = Number(stats.total_cached || 0).toLocaleString();
          elTokens.textContent = `${fresh} / ${cached}`;
        }
        if (elSessions)
          elSessions.textContent = `${Number(stats.unique_sessions || 0).toLocaleString()} sessions`;
      })
      .catch((err) => console.warn("Failed to fetch BQ stats:", err));
  }

  function fetchBqLogs(offset = 0) {
    const seq = ++bqLogsSeq;
    bqCurrentOffset = offset;
    if (bqFilterLimit) bqCurrentLimit = parseInt(bqFilterLimit.value, 10) || 50;

    const appFilter = bqFilterApp ? bqFilterApp.value : "all";
    const searchVal = bqSearchQuery ? bqSearchQuery.value.trim() : "";

    let url = `/api/bq/logs?limit=${bqCurrentLimit}&offset=${bqCurrentOffset}`;
    if (appFilter && appFilter !== "all")
      url += `&app_name=${encodeURIComponent(appFilter)}`;
    if (searchVal) url += `&search=${encodeURIComponent(searchVal)}`;

    if (bqTableCountBadge)
      bqTableCountBadge.textContent = "Checking Sink Status...";

    if (bqLogsAbortController) {
      try {
        bqLogsAbortController.abort();
      } catch (_) {}
    }
    let fetchOpts;
    if (typeof AbortController !== "undefined") {
      bqLogsAbortController = new AbortController();
      fetchOpts = { signal: bqLogsAbortController.signal };
    }

    fetch(url, fetchOpts)
      .then((res) => res.json())
      .then((data) => {
        if (seq !== bqLogsSeq) return;
        const targetLabelEl = document.getElementById(
          "bq-explorer-target-label",
        );
        if (targetLabelEl && data.target) {
          targetLabelEl.textContent = data.target;
        }
        if (data.status === "success") {
          bqTotalRows = data.total_rows || 0;
          bqCachedRows = data.rows || [];
          if (bqTableCountBadge) {
            bqTableCountBadge.className = "badge badge-primary";
          }
          renderBqLogsTable(bqCachedRows);
          updateBqPagination();
        } else if (data.status === "dev_mode") {
          bqTotalRows = 0;
          bqCachedRows = [];
          updateBqPagination();
          if (bqTableCountBadge) {
            bqTableCountBadge.className = "badge badge-success";
            bqTableCountBadge.textContent = "🟢 Local Dev Mode (JSON Traces)";
          }
          if (bqLogsTableBody) {
            const tracesPath = data.traces_path || "artifacts/traces/";
            const targetParts = String(data.target || "")
              .split(".")
              .filter(Boolean);
            const projId =
              data.project_id ||
              (targetParts.length >= 3 ? targetParts[0] : "<your-gcp-project>");
            const dsId =
              data.dataset_id ||
              (targetParts.length >= 3
                ? targetParts[1]
                : targetParts.length === 2
                  ? targetParts[0]
                  : "bq_adk_ds");
            bqLogsTableBody.innerHTML = `
              <tr>
                <td colspan="10" style="text-align:center; padding:2.5rem 1.5rem;">
                  <div style="max-width:660px; margin:0 auto; background:rgba(15,23,42,0.65); border:1px solid rgba(16,185,129,0.28); border-radius:12px; padding:1.5rem 1.75rem;">
                    <div style="font-size:1.05rem; color:#34d399; font-weight:700; margin-bottom:0.45rem; display:flex; align-items:center; justify-content:center; gap:0.45rem;">
                      <span>⚡</span> Local Development Mode Active (<code style="color:#a7f3d0;">TOKENOMICS_SINK=memory</code>)
                    </div>
                    <div style="font-size:0.84rem; color:#cbd5e1; line-height:1.55; margin-bottom:1.1rem;">
                      Telemetry is streaming directly to local trace files (<code style="color:#38bdf8;">${escapeHtml(tracesPath)}</code>) rather than BigQuery. All live turn-by-turn prompts, tool calls, and token breakdowns are available in real time in <strong>Session Overview</strong>.
                    </div>
                    <div style="margin-bottom:1.25rem;">
                      <button type="button" onclick="window.switchToSessionOverview()" class="btn btn-premium-primary" style="padding:0.5rem 1.15rem; font-size:0.82rem; font-weight:600; cursor:pointer;">
                        📊 &larr; View Live Turns in Session Overview
                      </button>
                    </div>
                    <div style="text-align:left; font-size:0.76rem; color:#94a3b8; background:rgba(2,6,23,0.65); padding:0.75rem 1rem; border-radius:8px; border:1px solid rgba(255,255,255,0.07);">
                      <div style="font-weight:600; color:#e2e8f0; margin-bottom:0.35rem;">Want to enable Production BigQuery Logging?</div>
                      <div>1. Set <code style="color:#a7f3d0;">TOKENOMICS_MODE=prod</code> and <code style="color:#a7f3d0;">TOKENOMICS_SINK=bigquery</code> in <code style="color:#38bdf8;">.env</code></div>
                      <div style="margin-top:0.2rem;">2. Provision dataset &amp; view via <code style="color:#fde68a;">TokenControlTower(project_id="${escapeHtml(projId)}", dataset_id="${escapeHtml(dsId)}").provision_cloud_infra()</code> (or attach ADK's <code style="color:#a7f3d0;">BigQueryAgentAnalyticsPlugin</code>)</div>
                    </div>
                  </div>
                </td>
              </tr>
            `;
          }
        } else {
          bqTotalRows = 0;
          bqCachedRows = [];
          updateBqPagination();
          if (bqTableCountBadge) {
            bqTableCountBadge.className = "badge badge-secondary";
            bqTableCountBadge.textContent = "BigQuery Setup Needed";
          }
          if (bqLogsTableBody) {
            const targetParts = String(data.target || "")
              .split(".")
              .filter(Boolean);
            const projFallback =
              targetParts.length >= 3 ? targetParts[0] : "<your-gcp-project>";
            const dsFallback =
              targetParts.length >= 3
                ? targetParts[1]
                : targetParts.length === 2
                  ? targetParts[0]
                  : "bq_adk_ds";
            const titleStr =
              data.friendly_title ||
              `BigQuery Sink Not Provisioned (${data.target || "bq_adk_ds.v_llm_response"})`;
            const summaryStr =
              data.friendly_summary ||
              "Enable the BigQuery API and provision the analytics dataset via TokenControlTower(...).provision_cloud_infra() or BigQueryAgentAnalyticsPlugin.";
            const cmdStr =
              data.remediation_cmd ||
              `python3 -c "from adk_tokenomics_plugin import TokenControlTower; TokenControlTower(project_id='${projFallback}', dataset_id='${dsFallback}').provision_cloud_infra()"`;
            bqLogsTableBody.innerHTML = `
              <tr>
                <td colspan="10" style="text-align:center; padding:2.25rem 1.5rem;">
                  <div style="max-width:660px; margin:0 auto; background:rgba(15,23,42,0.65); border:1px solid rgba(245,158,11,0.3); border-radius:12px; padding:1.4rem 1.6rem;">
                    <div style="color:#fbbf24; font-weight:700; font-size:0.98rem; margin-bottom:0.45rem;">
                      🛠️ ${escapeHtml(titleStr)}
                    </div>
                    <div style="font-size:0.83rem; color:#cbd5e1; margin:0 auto 0.9rem auto; line-height:1.5;">
                      ${escapeHtml(summaryStr)}
                    </div>
                    <div style="font-size:0.76rem; color:#fde68a; background:rgba(2,6,23,0.7); padding:0.6rem 0.9rem; border-radius:8px; border:1px solid rgba(255,255,255,0.08); font-family:var(--font-mono); word-break:break-all;">
                      ${escapeHtml(cmdStr)}
                    </div>
                  </div>
                </td>
              </tr>
            `;
          }
        }
      })
      .catch((err) => {
        if (err && err.name === "AbortError") return;
        if (seq !== bqLogsSeq) return;
        console.error("Failed to fetch BQ logs:", err);
        if (bqLogsTableBody) {
          bqLogsTableBody.innerHTML = `
            <tr>
              <td colspan="10" style="text-align:center; padding:2rem; color:#94a3b8;">
                Unable to reach local telemetry server.
              </td>
            </tr>
          `;
        }
        if (bqTableCountBadge) bqTableCountBadge.textContent = "Offline";
      });
  }

  function renderBqLogsTable(rows) {
    if (!bqLogsTableBody) return;

    if (!rows || rows.length === 0) {
      bqLogsTableBody.innerHTML = `
        <tr>
          <td colspan="10" style="text-align:center; padding:2rem; color:var(--color-text-muted);">
            No records matched the filter criteria in BigQuery.
          </td>
        </tr>
      `;
      if (bqTableCountBadge) bqTableCountBadge.textContent = "0 Records";
      return;
    }

    if (bqTableCountBadge) {
      bqTableCountBadge.textContent = `${bqTotalRows.toLocaleString()} Records Total`;
    }

    let html = "";
    rows.forEach((r, idx) => {
      const rowIdx = bqCurrentOffset + idx + 1;
      const timeFormatted = r.timestamp
        ? `${r.timestamp.replace("T", " ").substring(0, 19)} UTC`
        : "-";

      const appName = r.app_name || "unknown";
      const appBadgeClass = "badge-primary";
      const appDisplay = appName;
      const flowBadge = formatOrchestrationBadge(r.orchestration_flow);

      // Event Type Badge
      const evType = r.event_type || "LLM_RESPONSE";
      let evColor = "#a855f7";
      let evBg = "rgba(168,85,247,0.15)";
      if (evType === "TOOL_COMPLETED" || evType === "TOOL_STARTING") {
        evColor = "#f59e0b";
        evBg = "rgba(245,158,11,0.15)";
      } else if (evType === "AGENT_STARTING" || evType === "AGENT_COMPLETED") {
        evColor = "#06b6d4";
        evBg = "rgba(6,182,212,0.15)";
      } else if (evType === "USER_MESSAGE_RECEIVED") {
        evColor = "#3b82f6";
        evBg = "rgba(59,130,246,0.15)";
      } else if (evType.includes("ERROR")) {
        evColor = "#ef4444";
        evBg = "rgba(239,68,68,0.15)";
      }

      const querySnippet = r.user_query
        ? r.user_query.length > 55
          ? `${r.user_query.substring(0, 52)}...`
          : r.user_query
        : r.agent_response
          ? r.agent_response.length > 55
            ? `${r.agent_response.substring(0, 52)}...`
            : r.agent_response
          : "-";
      const modelName = r.model_name || "Gemini 3.8 Flash";
      const latencyStr = r.latency_ms
        ? `${Number(r.latency_ms).toLocaleString()} ms`
        : "-";

      const freshTokNum =
        r.input_tokens !== undefined
          ? Number(r.input_tokens || 0)
          : Math.max(
              0,
              Number(r.prompt_tokens || 0) - Number(r.cached_tokens || 0),
            );
      const freshTok = freshTokNum.toLocaleString();
      const cachedTok = Number(r.cached_tokens || 0).toLocaleString();
      const outTok = Number(r.output_tokens || 0).toLocaleString();
      const thinkTok = Number(r.thinking_tokens || 0).toLocaleString();
      const costStr = `$${Number(r.estimated_cost || 0).toFixed(5)}`;

      html += `
        <tr style="cursor:pointer;" onclick="window.inspectBqRow(${idx})">
          <td style="font-family:monospace; color:var(--color-text-muted);">${rowIdx}</td>
          <td style="font-family:monospace; font-size:0.75rem; color:#94a3b8; white-space:nowrap;">${escapeHtml(timeFormatted)}</td>
          <td>
            <span class="badge" style="background:${evBg}; color:${evColor}; border:1px solid ${evColor}44; font-size:0.7rem; font-weight:600;">
              ${escapeHtml(evType)}
            </span>
          </td>
          <td>
            <div style="display:flex; align-items:center; gap:0.35rem; flex-wrap:wrap;">
              <span class="badge ${appBadgeClass}" style="font-size:0.75rem;">${escapeHtml(appDisplay)}</span>
              ${flowBadge}
            </div>
          </td>
          <td style="font-size:0.8rem; color:#e2e8f0; max-width:130px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
            ${escapeHtml(modelName.replace("publishers/google/models/", ""))}
          </td>
          <td style="max-width:200px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(querySnippet)}">
            ${escapeHtml(querySnippet)}
          </td>
          <td style="font-family:monospace; font-size:0.75rem; white-space:nowrap;">
            <span style="color:#60a5fa;" title="Fresh Input">${freshTok}</span> / 
            <span style="color:#34d399;" title="Cached Discounted">${cachedTok}</span> / 
            <span style="color:#f59e0b;" title="Output">${outTok}</span> / 
            <span style="color:#c084fc;" title="Thinking">${thinkTok}</span>
          </td>
          <td style="font-family:monospace; font-size:0.75rem; color:#94a3b8; white-space:nowrap;">${escapeHtml(latencyStr)}</td>
          <td style="font-family:monospace; font-weight:600; color:var(--color-success);">${costStr}</td>
          <td style="text-align:center;">
            <button type="button" class="btn" style="padding:0.25rem 0.6rem; font-size:0.75rem; background:rgba(255,255,255,0.06); color:var(--color-primary-light); border:1px solid rgba(255,255,255,0.1); border-radius:6px; cursor:pointer;" onclick="event.stopPropagation(); window.inspectBqRow(${idx});">
              🔍 Inspect JSON
            </button>
          </td>
        </tr>
      `;
    });

    bqLogsTableBody.innerHTML = html;
  }

  function updateBqPagination() {
    const start = bqTotalRows === 0 ? 0 : bqCurrentOffset + 1;
    const end = Math.min(bqCurrentOffset + bqCurrentLimit, bqTotalRows);
    const currentPage = Math.floor(bqCurrentOffset / bqCurrentLimit) + 1;
    const totalPages = Math.ceil(bqTotalRows / bqCurrentLimit) || 1;

    if (bqPageRange) bqPageRange.textContent = `${start} - ${end}`;
    if (bqTotalCount) bqTotalCount.textContent = bqTotalRows.toLocaleString();
    if (bqPageCurrent)
      bqPageCurrent.textContent = `Page ${currentPage} of ${totalPages}`;

    if (btnBqPrevPage) btnBqPrevPage.disabled = bqCurrentOffset <= 0;
    if (btnBqNextPage) btnBqNextPage.disabled = end >= bqTotalRows;
  }

  window.inspectBqRow = (index) => {
    const row = bqCachedRows[index];
    if (!row || !bqRowModalOverlay) return;

    if (modalBqApp) modalBqApp.textContent = row.app_name || "-";
    if (modalBqModel)
      modalBqModel.textContent = row.model_name || "Gemini 3.8 Flash";
    if (modalBqCost)
      modalBqCost.textContent = `$${Number(row.estimated_cost || 0).toFixed(5)}`;
    const latMs = Number(row.latency_ms || 0);
    if (modalBqLatency)
      modalBqLatency.textContent =
        latMs > 0 ? `${latMs.toLocaleString()} ms` : "—";
    if (modalBqTime) modalBqTime.textContent = row.timestamp || "-";
    if (modalBqQuery)
      modalBqQuery.textContent = row.user_query || "(empty prompt)";
    if (modalBqResponse)
      modalBqResponse.textContent = row.agent_response || "(empty response)";
    if (modalBqRawJson)
      modalBqRawJson.textContent = JSON.stringify(row, null, 2);

    bqRowModalOverlay.classList.remove("hidden");
    bqRowModalOverlay.style.display = "flex";
  };

  if (btnBqApplyFilter) {
    btnBqApplyFilter.addEventListener("click", () => fetchBqLogs(0));
  }

  if (btnBqRefresh) {
    btnBqRefresh.addEventListener("click", () => {
      fetchBqStats();
      fetchBqLogs(bqCurrentOffset);
    });
  }

  if (btnBqPrevPage) {
    btnBqPrevPage.addEventListener("click", () => {
      if (bqCurrentOffset > 0) {
        fetchBqLogs(Math.max(0, bqCurrentOffset - bqCurrentLimit));
      }
    });
  }

  if (btnBqNextPage) {
    btnBqNextPage.addEventListener("click", () => {
      if (bqCurrentOffset + bqCurrentLimit < bqTotalRows) {
        fetchBqLogs(bqCurrentOffset + bqCurrentLimit);
      }
    });
  }

  if (btnBqExportJson) {
    btnBqExportJson.addEventListener("click", () => {
      if (!bqCachedRows || bqCachedRows.length === 0) {
        alert("No rows available to export.");
        return;
      }
      const dataStr = `data:text/json;charset=utf-8,${encodeURIComponent(JSON.stringify(bqCachedRows, null, 2))}`;
      const downloadAnchor = document.createElement("a");
      downloadAnchor.setAttribute("href", dataStr);
      downloadAnchor.setAttribute(
        "download",
        `bq_token_logs_${new Date().toISOString().substring(0, 10)}.json`,
      );
      document.body.appendChild(downloadAnchor);
      downloadAnchor.click();
      downloadAnchor.remove();
    });
  }

  function sanitizeCsvCell(val) {
    let str = val === null || val === undefined ? "" : String(val);
    if (/^\s*[=+\-@\t\r]/.test(str)) {
      str = `'${str}`;
    }
    return `"${str.replace(/"/g, '""')}"`;
  }

  window.sanitizeCsvCell = sanitizeCsvCell;

  if (btnBqExportCsv) {
    btnBqExportCsv.addEventListener("click", () => {
      if (!bqCachedRows || bqCachedRows.length === 0) {
        alert("No rows available to export.");
        return;
      }
      const headers = [
        "timestamp",
        "session_id",
        "app_name",
        "model_name",
        "prompt_tokens",
        "cached_tokens",
        "output_tokens",
        "thinking_tokens",
        "estimated_cost",
        "invoked_tools",
        "invoked_skills",
        "user_query",
        "agent_response",
      ];
      let csvBody = `${headers.join(",")}\n`;

      bqCachedRows.forEach((r) => {
        const rowData = [
          sanitizeCsvCell(r.timestamp || ""),
          sanitizeCsvCell(r.session_id || ""),
          sanitizeCsvCell(r.app_name || ""),
          sanitizeCsvCell(r.model_name || ""),
          Number(r.prompt_tokens || 0),
          Number(r.cached_tokens || 0),
          Number(r.output_tokens || 0),
          Number(r.thinking_tokens || 0),
          Number(r.estimated_cost || 0.0),
          sanitizeCsvCell(r.invoked_tools || ""),
          sanitizeCsvCell(r.invoked_skills || ""),
          sanitizeCsvCell(r.user_query || ""),
          sanitizeCsvCell(r.agent_response || ""),
        ];
        csvBody += `${rowData.join(",")}\n`;
      });

      const link = document.createElement("a");
      let downloadHref;
      let shouldRevokeBlob = false;
      if (
        typeof Blob !== "undefined" &&
        typeof URL !== "undefined" &&
        typeof URL.createObjectURL === "function"
      ) {
        const blob = new Blob([csvBody], { type: "text/csv;charset=utf-8;" });
        downloadHref = URL.createObjectURL(blob);
        shouldRevokeBlob = true;
      } else {
        downloadHref = `data:text/csv;charset=utf-8,${encodeURIComponent(csvBody)}`;
      }
      link.setAttribute("href", downloadHref);
      link.setAttribute(
        "download",
        `bq_token_logs_${new Date().toISOString().substring(0, 10)}.csv`,
      );
      document.body.appendChild(link);
      link.click();
      link.remove();
      if (shouldRevokeBlob && typeof URL.revokeObjectURL === "function") {
        setTimeout(() => URL.revokeObjectURL(downloadHref), 1000);
      }
    });
  }

  // ==========================================
  // READ-ONLY DATA SOURCE STATUS BADGE (.env / CLI)
  // ==========================================
  const headerSourceIcon = document.getElementById("header-source-icon");
  const headerBqDatasetBadge = document.getElementById(
    "header-bq-dataset-badge",
  );

  function fetchBqConfig() {
    fetch("/api/bq/config")
      .then((res) => res.json())
      .then((data) => {
        if (!data) return;
        const targetLabelEl = document.getElementById(
          "bq-explorer-target-label",
        );
        if (targetLabelEl && data.dataset_id) {
          const prefix = data.project_id ? `${data.project_id}.` : "";
          targetLabelEl.textContent = `${prefix}${data.dataset_id}.${data.view_id || data.table_id || "v_llm_response"}`;
        }
        if (data.project_id && data.dataset_id) {
          configuredBqBadgeText = `${data.project_id}.${data.dataset_id}`;
        } else if (data.dataset_id) {
          configuredBqBadgeText = `${data.dataset_id}.${data.view_id || "v_llm_response"}`;
        }
        if (data.project_id && !hasAutoSwitchedForLocalTrace) {
          applyPollBackoffForSource("bigquery");
        }
        if (!headerBqDatasetBadge) return;
        if (
          currentDataSource === "local_traces" &&
          hasAutoSwitchedForLocalTrace
        )
          return;
        if (data.project_id && data.dataset_id) {
          if (headerSourceIcon) headerSourceIcon.textContent = "🗄️ BigQuery:";
          headerBqDatasetBadge.textContent = configuredBqBadgeText;
        } else if (data.dataset_id) {
          headerBqDatasetBadge.textContent = configuredBqBadgeText;
        }
      })
      .catch((err) => console.warn("Failed to fetch data source config:", err));
  }

  fetchBqConfig();
});
