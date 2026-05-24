const state = {
  config: null,
  scanItems: [],
  analysisTasks: [],
  organizeTasks: [],
  organizeQueue: [],
  results: [],
  runs: [],
  selectedRunIds: new Set(),
  selectedResultId: "",
  runtimeStatus: null,
  pipelineLogState: new Map(),
  analysisRefreshTimer: null,
  analysisRefreshInFlight: false,
  analysisResultsDirty: false,
  pipelineRefreshTimer: null,
  pipelineRefreshInFlight: false,
};

const ANALYSIS_PENDING_STATUSES = new Set(["queued", "running"]);
const ANALYSIS_TERMINAL_STATUSES = new Set(["succeeded", "failed", "interrupted"]);

const viewMeta = {
  organizeView: ["资源整理", "扫描收件箱，分析后进入待整理队列。"],
  pipelineView: ["整理流水线", "只显示会写入 OpenList 的整理执行任务。"],
  runsView: ["运行记录", "查询 data/runs 下的分析、重映射和整理记录。"],
  settingsView: ["设置", "维护 OpenList、LLM、TMDB、媒体库和扫描参数。"],
};

document.addEventListener("DOMContentLoaded", async () => {
  bindTabs();
  bindFoldSections();
  bindActions();
  await loadConfig();
  await loadRuntimeStatus();
  await health();
  await refreshAll();
});

function bindTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("is-active"));
      document.querySelectorAll(".view").forEach((item) => item.classList.remove("is-active"));
      tab.classList.add("is-active");
      byId(tab.dataset.view).classList.add("is-active");
      const [title, subtitle] = viewMeta[tab.dataset.view];
      byId("viewTitle").textContent = title;
      byId("viewSubtitle").textContent = subtitle;
    });
  });
}

function bindFoldSections() {
  document.querySelectorAll("[data-fold-section]").forEach((section) => {
    const head = section.querySelector(".fold-head");
    if (!head) return;
    head.addEventListener("click", () => {
      const collapsed = section.classList.toggle("is-collapsed");
      head.setAttribute("aria-expanded", String(!collapsed));
    });
  });
}

function bindActions() {
  byId("refreshAllBtn")?.addEventListener("click", refreshAll);
  byId("scanBtn").addEventListener("click", scanSource);
  byId("selectAllBtn").addEventListener("click", () => setAllScanChecks(true));
  byId("clearSelectionBtn").addEventListener("click", () => setAllScanChecks(false));
  byId("batchAnalyzeBtn").addEventListener("click", submitBatchAnalysis);
  byId("refreshResultsBtn").addEventListener("click", refreshResults);
  byId("batchOrganizeBtn").addEventListener("click", submitBatchOrganize);
  byId("startOrganizeQueueBtn").addEventListener("click", submitOrganizeQueue);
  byId("clearOrganizeQueueBtn").addEventListener("click", clearOrganizeQueue);
  byId("refreshTasksBtn").addEventListener("click", refreshOrganizeTasks);
  byId("retryFailedBtn").addEventListener("click", retryFailedOrganize);
  byId("runAnalysisStatusFilter").addEventListener("change", refreshRuns);
  byId("runOrganizeStatusFilter").addEventListener("change", refreshRuns);
  byId("runSourceFilter").addEventListener("change", refreshRuns);
  byId("deleteSelectedRunsBtn").addEventListener("click", deleteSelectedRuns);
  byId("testOpenlistBtn").addEventListener("click", testOpenlist);
  byId("loadModelsBtn").addEventListener("click", loadModels);
  byId("saveConfigBtn").addEventListener("click", saveConfig);
  byId("modelSelect").addEventListener("change", (event) => {
    if (event.target.value) byId("llmModel").value = event.target.value;
  });
}

async function refreshAll() {
  await Promise.allSettled([
    refreshAnalysisTasks({ refreshResultsOnChange: false }),
    refreshOrganizeTasks(),
    refreshResults(),
    refreshRuns(),
  ]);
}

async function health() {
  try {
    await api("/api/health");
    renderRuntimeStatus();
  } catch {
    const status = byId("backendStatus");
    if (status) status.textContent = "后端离线";
  }
}

async function loadConfig() {
  state.config = await api("/api/config");
  fillConfigForm(state.config);
}

async function loadRuntimeStatus() {
  state.runtimeStatus = await api("/api/runtime/config-status");
  renderRuntimeStatus();
}

function renderRuntimeStatus() {
  const statusEl = byId("backendStatus");
  if (!statusEl) return;
  const status = state.runtimeStatus;
  if (!status) {
    statusEl.textContent = "后端在线";
    return;
  }
  const active = status.active || {};
  const pieces = [
    active.openlist_configured ? "OpenList" : "OpenList 未生效",
    active.llm_configured ? "LLM" : "LLM 未生效",
    active.tmdb_configured ? "TMDB" : "TMDB 未生效",
  ];
  const needsRestart = Boolean(status.restart_required);
  statusEl.textContent = needsRestart ? "配置已保存，需重启" : pieces.join(" / ");
  statusEl.classList.toggle("needs-attention", needsRestart);
}

async function scanSource() {
  setBusy("scanBtn", true);
  try {
    const data = await api("/api/scans", { method: "POST" });
    state.scanItems = data.items || [];
    renderScanItems();
  } catch (error) {
    renderInlineError(byId("scanList"), error);
  } finally {
    setBusy("scanBtn", false);
  }
}

function renderScanItems() {
  const list = byId("scanList");
  byId("scanCount").textContent = `${state.scanItems.length} 个条目`;
  if (!state.scanItems.length) {
    list.className = "list empty";
    list.textContent = "没有发现可扫描文件夹";
    return;
  }
  list.className = "list";
  list.innerHTML = "";
  state.scanItems.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "scan-item";
    row.innerHTML = `
      <input type="checkbox" class="scan-check" data-index="${index}">
      <div class="scan-main">
        <div class="item-title">${escapeHtml(item.name)}</div>
      </div>
      <div class="scan-actions">
        <button data-action="analyze" data-index="${index}">分析</button>
      </div>
      <div class="assist-panel">
        <div class="assist-title">辅助信息输入</div>
        <div class="form-grid two">
          <label>TMDB TV ID
            <input data-tv-tmdb-index="${index}" placeholder="例如 35753；填写后跳过标题识别和候选选择">
          </label>
          <label>TMDB Movie IDs
            <input data-movie-tmdb-index="${index}" placeholder="多个电影 ID 用逗号分隔">
          </label>
        </div>
        <label>LLM 额外提示词
          <textarea data-prompt-index="${index}" placeholder="可留空；复杂季度、OVA、剧场版关系可以写在这里"></textarea>
        </label>
      </div>
    `;
    row.querySelector("[data-action='analyze']").addEventListener("click", () => submitAnalysis([index]));
    list.appendChild(row);
  });
}

function setAllScanChecks(checked) {
  document.querySelectorAll(".scan-check").forEach((item) => {
    item.checked = checked;
  });
}

async function submitBatchAnalysis() {
  const indexes = [...document.querySelectorAll(".scan-check:checked")].map((item) => Number(item.dataset.index));
  await submitAnalysis(indexes);
}

async function submitAnalysis(indexes) {
  if (!indexes.length) return;
  const items = indexes.map((index) => {
    const item = state.scanItems[index];
    return {
      ...item,
      prompt: promptValue(index),
      tv_tmdb_id: tvTmdbValue(index),
      movie_tmdb_ids: movieTmdbValues(index),
    };
  });
  await api("/api/analysis", {
    method: "POST",
    body: JSON.stringify({ items }),
  });
  await refreshAnalysisTasks({ refreshResultsOnChange: false });
}

function promptValue(index) {
  const input = document.querySelector(`[data-prompt-index="${index}"]`);
  return input ? input.value : "";
}

function tvTmdbValue(index) {
  const input = document.querySelector(`[data-tv-tmdb-index="${index}"]`);
  return input ? input.value.trim() : "";
}

function movieTmdbValues(index) {
  const input = document.querySelector(`[data-movie-tmdb-index="${index}"]`);
  if (!input) return [];
  return input.value.split(",").map((item) => item.trim()).filter(Boolean);
}

async function refreshAnalysisTasks(options = {}) {
  const { refreshResultsOnChange = false } = options;
  if (state.analysisRefreshInFlight) return;
  state.analysisRefreshInFlight = true;
  const previousTerminalCount = countAnalysisTasksByStatus(ANALYSIS_TERMINAL_STATUSES, state.analysisTasks);
  try {
    const data = await api("/api/analysis-tasks");
    state.analysisTasks = data.tasks || [];
    renderAnalysisTasks();
    const currentTerminalCount = countAnalysisTasksByStatus(
      ANALYSIS_TERMINAL_STATUSES,
      state.analysisTasks,
    );
    if (refreshResultsOnChange && currentTerminalCount > previousTerminalCount) {
      state.analysisResultsDirty = true;
    }
    if (state.analysisResultsDirty) {
      try {
        await refreshResults();
        state.analysisResultsDirty = false;
      } catch (error) {
        console.warn("Background result refresh failed", error);
      }
    }
  } finally {
    state.analysisRefreshInFlight = false;
    scheduleAnalysisAutoRefresh();
  }
}

function renderAnalysisTasks() {
  const list = byId("analysisTaskList");
  const activeTasks = state.analysisTasks.filter((task) => task.status !== "succeeded");
  byId("analysisTaskCount").textContent = `${activeTasks.length} 个当前任务`;
  if (!activeTasks.length) {
    list.className = "list compact-list empty";
    list.textContent = "暂无当前任务";
    return;
  }
  list.className = "list compact-list";
  list.innerHTML = "";
  activeTasks.forEach((task) => {
    const row = document.createElement("div");
    row.className = "task-item";
    row.innerHTML = taskTemplate(task, { compact: true });
    list.appendChild(row);
  });
}

async function refreshResults() {
  const data = await api("/api/results?latest_only=false&limit=80");
  state.results = data.results || [];
  renderResults();
  renderInspector(selectedResult());
  state.analysisResultsDirty = false;
}

function renderResults() {
  const list = byId("resultList");
  byId("resultCount").textContent = `${state.results.length} 个结果`;
  if (!state.results.length) {
    list.className = "list result-list empty";
    list.textContent = "等待分析完成";
    return;
  }
  list.className = "list result-list";
  list.innerHTML = "";
  if (
    (!state.selectedResultId || !state.results.some((result) => resultKey(result) === state.selectedResultId))
    && state.results[0]
  ) {
    state.selectedResultId = resultKey(state.results[0]);
  }
  state.results.forEach((result) => {
    const plan = result.work_plan;
    const key = resultKey(result);
    const row = document.createElement("div");
    row.className = `result-item ${key === state.selectedResultId ? "is-selected" : ""}`;
    row.innerHTML = `
      <div class="result-head">
        <div>
          <div class="item-title">${escapeHtml(displayOriginTitle(result))}</div>
          <div class="meta-line">${escapeHtml(result.source_name || "-")}</div>
        </div>
        ${analysisStatusBadge(result)}
      </div>
      <div class="result-metrics">
        <span class="metric">${countOf(plan?.validated_mappings)} mapped</span>
        <span class="metric">${countOf(plan?.missing_tmdb_episodes)} missing eps</span>
        <span class="metric">${countOf(plan?.rejected_mappings)} rejected</span>
        <span class="metric">${countOf(plan?.unmapped_files)} unmapped</span>
      </div>
    `;
    row.addEventListener("click", () => {
      state.selectedResultId = key;
      renderResults();
      renderInspector(result);
    });
    list.appendChild(row);
  });
}

function renderInspector(result) {
  const panel = byId("resultInspector");
  if (!result) {
    panel.className = "inspector empty";
    panel.textContent = "选择一个分析结果查看详情";
    return;
  }
  const plan = result.work_plan;
  const runId = result.run_id || runForAnalysis(result.id)?.run_id || "";
  const tmdbLink = tmdbPageLink(result);
  panel.className = "inspector";
  panel.innerHTML = `
    ${renderOrganizeControls(result, runId)}
    <div class="inspector-card identity-card">
      <div class="result-head">
        <div>
          <div class="item-title">${escapeHtml(displayOriginTitle(result))}</div>
        </div>
        <div class="result-actions">
          <button data-tmdb-url="${escapeHtml(tmdbLink.url)}" ${tmdbLink.url ? "" : "disabled"}>访问 TMDB 页面</button>
        </div>
      </div>
      <div class="detail-stack">
        ${renderTextRow("源文件夹", result.source_name || "-")}
        ${renderTextRow("媒体库目标", libraryTargetText(result))}
        ${renderTextRow("归档目标", result.archive_target_path || "-")}
        ${renderTextRow("记录文件夹", runId || "-")}
      </div>
    </div>
    ${renderResultDiagnostics(result)}
    ${renderPlanDetails(plan, runId)}
    <div class="report-block">
      <div class="pane-head">
        <div>
          <h3>文本报告书</h3>
        </div>
      </div>
      <pre class="full-report-tree">${escapeHtml(normalizeReportTree(result.report_tree || ""))}</pre>
    </div>
  `;
  const organizeBtn = panel.querySelector("[data-organize-run]");
  if (organizeBtn) {
    organizeBtn.addEventListener("click", () => addSelectedResultToOrganizeQueue(result, runId));
  }
  const confirmInput = panel.querySelector("#allowFailed");
  if (confirmInput && organizeBtn) {
    confirmInput.addEventListener("change", () => syncOrganizeButtonState(result, organizeBtn));
    syncOrganizeButtonState(result, organizeBtn);
  }
  const tmdbBtn = panel.querySelector("[data-tmdb-url]");
  if (tmdbBtn && tmdbLink.url) {
    tmdbBtn.addEventListener("click", () => window.open(tmdbLink.url, "_blank", "noopener"));
  }
  panel.querySelectorAll("[data-manual-map-run]").forEach((button) => {
    button.addEventListener("click", () => saveManualEpisodeMapping(button));
  });
}

function scheduleAnalysisAutoRefresh() {
  if (state.analysisRefreshTimer) {
    clearTimeout(state.analysisRefreshTimer);
    state.analysisRefreshTimer = null;
  }
  if (!hasPendingAnalysisTasks() && !state.analysisResultsDirty) return;
  state.analysisRefreshTimer = window.setTimeout(() => {
    state.analysisRefreshTimer = null;
    void refreshAnalysisTasks({ refreshResultsOnChange: true }).catch((error) => {
      console.warn("Analysis auto refresh failed", error);
    });
  }, analysisRefreshIntervalMs());
}

function analysisRefreshIntervalMs() {
  const seconds = Number(state.config?.ui?.pipeline_refresh_interval_seconds || 3);
  return Math.max(1, Number.isFinite(seconds) ? seconds : 3) * 1000;
}

function hasPendingAnalysisTasks(tasks = state.analysisTasks) {
  return tasks.some((task) => ANALYSIS_PENDING_STATUSES.has(task.status || ""));
}

function countAnalysisTasksByStatus(statuses, tasks = state.analysisTasks) {
  return tasks.filter((task) => statuses.has(task.status || "")).length;
}

function tmdbPageLink(result) {
  const plan = result.work_plan || {};
  const selectedTvId = plan.selected_tv_series?.tmdb_id || "";
  const tvTargetId = (plan.library_targets || []).find((target) => target.media_type === "tv")?.tmdb_id || "";
  const resultTvId = result.media_type === "tv" ? result.tmdb_id || "" : "";
  const tvId = selectedTvId || tvTargetId || resultTvId;
  if (tvId) {
    return {
      label: "访问 TMDB 页面",
      url: `https://www.themoviedb.org/tv/${encodeURIComponent(tvId)}/seasons`,
    };
  }
  const firstMovieId =
    (plan.selected_movies || [])[0]?.tmdb_id ||
    (plan.library_targets || []).find((target) => target.media_type === "movie")?.tmdb_id ||
    (result.media_type === "movie" ? result.tmdb_id || "" : "");
  if (firstMovieId) {
    return {
      label: "访问 TMDB 页面",
      url: `https://www.themoviedb.org/movie/${encodeURIComponent(firstMovieId)}`,
    };
  }
  return { label: "访问 TMDB 页面", url: "" };
}

function renderOrganizeControls(result, runId) {
  const canOrganize = result.status === "succeeded" || result.status === "needs_review";
  const needsConfirm = result.status === "needs_review";
  const confirmOption = needsConfirm
    ? `<label class="option-pill"><input type="checkbox" id="allowFailed"> 确认整理</label>`
    : "";
  return `
    <div class="inspector-card organize-card">
      <h3>整理参数</h3>
      <div class="option-row">
        ${confirmOption}
        <label class="option-pill"><input type="checkbox" id="deleteTarget"> 整理前删除同名文件</label>
        <label class="option-pill"><input type="checkbox" id="overwriteArchive"> 归档前删除目标文件</label>
        <label class="option-pill"><input type="checkbox" id="deleteSource"> 整理后删源</label>
      </div>
      <div class="inspector-actions">
        <button class="success-action" data-organize-run="${escapeHtml(runId)}" ${runId && canOrganize ? "" : "disabled"}>加入待整理队列</button>
      </div>
    </div>
  `;
}

function syncOrganizeButtonState(result, button) {
  const needsConfirm = result.status === "needs_review";
  const canOrganize = result.status === "succeeded" || result.status === "needs_review";
  button.disabled = !button.dataset.organizeRun || !canOrganize || (needsConfirm && !byId("allowFailed")?.checked);
}

function readOrganizeOptions(includeAllowFailed = true) {
  return {
    allow_failed_analysis: includeAllowFailed && Boolean(byId("allowFailed")?.checked),
    delete_target_before: Boolean(byId("deleteTarget")?.checked),
    overwrite_archive_target_before: Boolean(byId("overwriteArchive")?.checked),
    delete_source_after: Boolean(byId("deleteSource")?.checked),
  };
}

function addSelectedResultToOrganizeQueue(result, runId) {
  if (!runId) return;
  const options = readOrganizeOptions();
  const existingIndex = state.organizeQueue.findIndex((item) => item.run_id === runId);
  const queueItem = {
    run_id: runId,
    title: displayOriginTitle(result),
    origin_title: displayOriginTitle(result),
    source_name: result.source_name,
    status: result.status,
    media_target_path: libraryTargetText(result),
    archive_target_path: result.archive_target_path || "",
    options,
  };
  if (existingIndex >= 0) {
    state.organizeQueue[existingIndex] = queueItem;
  } else {
    state.organizeQueue.push(queueItem);
  }
  renderOrganizeQueue();
}

function renderOrganizeQueue() {
  const list = byId("organizeQueueList");
  byId("organizeQueueCount").textContent = `${state.organizeQueue.length} 个待整理`;
  if (!state.organizeQueue.length) {
    list.className = "list compact-list empty";
    list.textContent = "暂无待整理项目";
    return;
  }
  list.className = "list compact-list";
  list.innerHTML = "";
  state.organizeQueue.forEach((item) => {
    const row = document.createElement("div");
    row.className = "queue-item";
    row.innerHTML = `
      <div class="queue-head queue-topline">
        <div class="item-title">${escapeHtml(item.origin_title || item.title || "-")}</div>
        <div class="queue-actions">
          <button data-remove-queue="${escapeHtml(item.run_id)}">移除</button>
          ${analysisStatusBadge(item)}
        </div>
      </div>
      <div class="queue-field-grid">
        ${renderQueueField("源文件夹", item.source_name || "-")}
        ${renderQueueField("记录文件夹", item.run_id || "-")}
        <div class="queue-field queue-field-wide">
          <span>参数</span>
          <div class="queue-options">${renderQueueOptionPills(item.options)}</div>
        </div>
      </div>
    `;
    row.querySelector("[data-remove-queue]").addEventListener("click", () => {
      removeOrganizeQueueItem(item.run_id);
    });
    list.appendChild(row);
  });
}

function renderQueueOptionPills(options) {
  const labels = [
    ["allow_failed_analysis", "确认整理"],
    ["delete_target_before", "删除同名文件"],
    ["overwrite_archive_target_before", "删除归档目标文件"],
    ["delete_source_after", "整理后删源"],
  ];
  return labels
    .filter(([key]) => options[key])
    .map(([, label]) => `<span class="metric">${escapeHtml(label)}</span>`)
    .join("") || `<span class="metric">默认参数</span>`;
}

function removeOrganizeQueueItem(runId) {
  state.organizeQueue = state.organizeQueue.filter((item) => item.run_id !== runId);
  renderOrganizeQueue();
}

function clearOrganizeQueue() {
  state.organizeQueue = [];
  renderOrganizeQueue();
}

async function submitOrganizeQueue() {
  if (!state.organizeQueue.length) return;
  const groups = new Map();
  state.organizeQueue.forEach((item) => {
    const key = JSON.stringify(item.options);
    const existing = groups.get(key) || { options: item.options, run_ids: [] };
    existing.run_ids.push(item.run_id);
    groups.set(key, existing);
  });
  for (const group of groups.values()) {
    await api("/api/organize/batch", {
      method: "POST",
      body: JSON.stringify({ run_ids: group.run_ids, options: group.options }),
    });
  }
  clearOrganizeQueue();
  await refreshOrganizeTasks();
}

async function submitOrganizeRun(runId, options) {
  if (!runId) return;
  await api("/api/organize", {
    method: "POST",
    body: JSON.stringify({ run_id: runId, options }),
  });
  await refreshOrganizeTasks();
}

async function submitBatchOrganize() {
  state.results.filter((item) => item.status === "succeeded").forEach((result) => {
    const runId = result.run_id || runForAnalysis(result.id)?.run_id || "";
    if (!runId) return;
    const existingIndex = state.organizeQueue.findIndex((item) => item.run_id === runId);
    const queueItem = {
      run_id: runId,
      title: displayOriginTitle(result),
      origin_title: displayOriginTitle(result),
      source_name: result.source_name,
      status: result.status,
      media_target_path: libraryTargetText(result),
      archive_target_path: result.archive_target_path || "",
      options: readOrganizeOptions(false),
    };
    if (existingIndex >= 0) {
      state.organizeQueue[existingIndex] = queueItem;
    } else {
      state.organizeQueue.push(queueItem);
    }
  });
  renderOrganizeQueue();
}

async function refreshOrganizeTasks(options = {}) {
  const scope = options.scope || "all";
  if (state.pipelineRefreshInFlight) return;
  state.pipelineRefreshInFlight = true;
  let shouldRefreshAll = false;
  try {
    const previousActiveIds = new Set(
      state.organizeTasks.filter((task) => !isTerminalPipelineTask(task)).map((task) => task.id),
    );
    const data = await api(`/api/tasks?scope=${encodeURIComponent(scope)}`);
    const incomingTasks = data.tasks || [];
    state.organizeTasks =
      scope === "all" ? incomingTasks : mergeOrganizeTasksByScope(state.organizeTasks, incomingTasks, scope);
    const viewport = captureViewportScroll();
    renderOrganizeTasks({
      renderActive: scope !== "completed",
      renderCompleted: scope !== "active",
    });
    restoreViewportScroll(viewport);
    const activeIds = new Set(
      state.organizeTasks.filter((task) => !isTerminalPipelineTask(task)).map((task) => task.id),
    );
    const activeDisappeared = [...previousActiveIds].some((taskId) => !activeIds.has(taskId));
    shouldRefreshAll = scope === "active" && activeDisappeared;
  } catch (error) {
    console.warn("Pipeline refresh failed", error);
  } finally {
    state.pipelineRefreshInFlight = false;
    schedulePipelineAutoRefresh();
  }
  if (shouldRefreshAll) {
    void refreshOrganizeTasks({ scope: "all" }).catch((error) => {
      console.warn("Pipeline refresh failed", error);
    });
  }
}

function mergeOrganizeTasksByScope(existingTasks, incomingTasks, scope) {
  if (scope === "active") {
    return [
      ...incomingTasks,
      ...existingTasks.filter((task) => isTerminalPipelineTask(task)),
    ];
  }
  if (scope === "completed") {
    return [
      ...existingTasks.filter((task) => !isTerminalPipelineTask(task)),
      ...incomingTasks,
    ];
  }
  return incomingTasks;
}

function renderOrganizeTasks(options = {}) {
  const { renderActive = true, renderCompleted = true } = options;
  const activeList = byId("activeTaskList");
  const completedList = byId("completedTaskList");
  if (renderActive && activeList) capturePipelineLogState(activeList);
  if (renderCompleted && completedList) capturePipelineLogState(completedList);
  const activeCount = byId("activePipelineCount");
  const completedCount = byId("completedPipelineCount");
  const activeTasks = state.organizeTasks.filter((task) => !isTerminalPipelineTask(task));
  const completedTasks = state.organizeTasks.filter((task) => isTerminalPipelineTask(task));
  if (activeCount) activeCount.textContent = `${activeTasks.length} 个进行中`;
  if (completedCount) completedCount.textContent = `${completedTasks.length} 个已完成`;
  if (renderActive) renderPipelineTaskList(activeList, activeTasks, "暂无进行中的整理任务");
  if (renderCompleted) renderPipelineTaskList(completedList, completedTasks, "暂无已完成的整理任务");
}

function schedulePipelineAutoRefresh() {
  if (state.pipelineRefreshTimer) {
    clearTimeout(state.pipelineRefreshTimer);
    state.pipelineRefreshTimer = null;
  }
  if (!hasActivePipelineTasks()) return;
  state.pipelineRefreshTimer = window.setTimeout(() => {
    state.pipelineRefreshTimer = null;
    void refreshOrganizeTasks({ scope: "active" }).catch((error) => {
      console.warn("Pipeline auto refresh failed", error);
    });
  }, pipelineRefreshIntervalMs());
}

function renderPipelineTaskList(list, tasks, emptyText) {
  if (!list) return;
  if (!tasks.length) {
    list.className = "pipeline-board empty";
    list.textContent = emptyText;
    return;
  }
  list.className = "pipeline-board";
  list.innerHTML = "";
  tasks.forEach((task) => {
    const row = document.createElement("div");
    row.className = "pipeline-run";
    row.dataset.pipelineTaskId = task.id;
    const previousLog = state.pipelineLogState.get(task.id);
    row.innerHTML = pipelineTaskTemplate(task, { includeLogText: Boolean(previousLog?.open) });
    const logs = row.querySelector("[data-pipeline-log]");
    const logCount = (task.logs || []).length;
    if (logs) logs.dataset.logCount = String(logCount);
    if (logs && previousLog?.open) {
      logs.open = true;
      bindPipelineLogState(task.id, logs, task);
    }
    if (logs && !previousLog?.open) bindPipelineLogState(task.id, logs, task);
    const retry = row.querySelector("[data-retry]");
    if (retry) retry.addEventListener("click", () => retryTask(task.id));
    list.appendChild(row);
    if (logs && previousLog) restorePipelineLogState(task.id, logs, previousLog, task);
  });
}

function isTerminalPipelineTask(task) {
  return ["succeeded", "failed", "interrupted"].includes(task.status);
}

function hasActivePipelineTasks(tasks = state.organizeTasks) {
  return tasks.some((task) => !isTerminalPipelineTask(task));
}

function pipelineRefreshIntervalMs() {
  const seconds = Number(state.config?.ui?.pipeline_refresh_interval_seconds || 3);
  return Math.max(1, Number.isFinite(seconds) ? seconds : 3) * 1000;
}

function capturePipelineLogState(root) {
  root.querySelectorAll("[data-pipeline-log]").forEach((details) => {
    const taskId = details.closest("[data-pipeline-task-id]")?.dataset.pipelineTaskId;
    if (!taskId) return;
    updatePipelineLogState(taskId, details);
  });
}

function bindPipelineLogState(taskId, details, task) {
  details.addEventListener("toggle", () => {
    ensurePipelineLogContent(details, task);
    if (details.open) scrollPipelineLogToBottom(details);
    updatePipelineLogState(taskId, details);
  });
  const pre = details.querySelector("pre");
  if (pre) {
    pre.addEventListener("scroll", () => updatePipelineLogState(taskId, details), { passive: true });
  }
}

function updatePipelineLogState(taskId, details) {
  const pre = details.querySelector("pre");
  state.pipelineLogState.set(taskId, {
    open: details.open,
    scrollTop: pre?.scrollTop || 0,
    logCount: Number(details.dataset.logCount || 0),
  });
}

function restorePipelineLogState(taskId, details, previousLog, task) {
  details.open = previousLog.open;
  ensurePipelineLogContent(details, task);
  const pre = details.querySelector("pre");
  if (!pre) return;
  const currentLogCount = Number(details.dataset.logCount || 0);
  const previousLogCount = Number(previousLog.logCount || 0);
  const shouldScrollToBottom = details.open && currentLogCount > previousLogCount;
  const scrollTop = Number(previousLog.scrollTop || 0);
  const restore = () => {
    pre.scrollTop = shouldScrollToBottom ? pre.scrollHeight : scrollTop;
    updatePipelineLogState(taskId, details);
  };
  restore();
  requestAnimationFrame(restore);
  setTimeout(restore, 0);
}

function ensurePipelineLogContent(details, task) {
  const pre = details.querySelector("pre");
  if (!pre) return;
  const renderedCount = Number(pre.dataset.renderedLogCount || 0);
  const logCount = Number(details.dataset.logCount || 0);
  if (renderedCount === logCount) return;
  pre.textContent = pipelineLogText(task);
  pre.dataset.renderedLogCount = String(logCount);
}

function scrollPipelineLogToBottom(details) {
  const pre = details.querySelector("pre");
  if (!pre) return;
  const scroll = () => {
    pre.scrollTop = pre.scrollHeight;
  };
  scroll();
  requestAnimationFrame(scroll);
  setTimeout(scroll, 0);
}

function captureViewportScroll() {
  const element = document.scrollingElement || document.documentElement;
  return {
    left: element.scrollLeft,
    top: element.scrollTop,
  };
}

function restoreViewportScroll(position) {
  const restore = () => window.scrollTo(position.left, position.top);
  restore();
  requestAnimationFrame(restore);
  setTimeout(restore, 0);
}

function pipelineLogText(task) {
  const logs = task.logs || [];
  return logs.map((log) => `${formatDate(log.at)} ${organizeStageLabel(log.stage) || log.stage} · ${log.message}`).join("\n");
}

function pipelineTaskTemplate(task, options = {}) {
  const logs = task.logs || [];
  const lastLog = logs[logs.length - 1] || null;
  const failure = task.error || (task.status === "failed" ? lastLog?.message || "" : "");
  const logText = options.includeLogText ? escapeHtml(pipelineLogText(task)) : "";
  return `
    <div class="pipeline-run-head">
      <div>
        <div class="item-title">${escapeHtml(task.source_name || task.run_id)}</div>
      </div>
      <div class="pipeline-run-actions">
        ${["failed", "interrupted"].includes(task.status) && task.kind === "organize" ? `<button data-retry="${task.id}">${task.status === "interrupted" ? "继续整理" : "重试整理"}</button>` : ""}
        ${taskStatusBadge(task)}
      </div>
    </div>
    <div class="pipeline-stages" aria-label="整理阶段">
      ${organizePipelineStages(task).map((stage) => pipelineStageTemplate(stage)).join("")}
    </div>
    <div class="detail-stack">
      ${renderTextRow("源文件夹", task.source_name || "-")}
      ${renderTextRow("媒体库目标", task.media_target_path || "-")}
      ${renderTextRow("归档目标", task.archive_target_path || "-")}
      ${renderTextRow("记录文件夹", task.run_id || "-")}
    </div>
    ${failure ? `<div class="pipeline-reason"><strong>原因</strong><span>${escapeHtml(failure)}</span></div>` : ""}
    <details class="pipeline-logs" data-pipeline-log>
      <summary>执行日志 · ${logs.length} 条</summary>
      <pre data-rendered-log-count="${options.includeLogText ? logs.length : 0}">${logText}</pre>
    </details>
  `;
}

function pipelineStageTemplate(stage) {
  const detail = stage.detail ? `<span>${escapeHtml(stage.detail)}</span>` : "";
  return `
    <div class="pipeline-stage ${escapeHtml(stage.state)}">
      <div class="stage-dot"></div>
      <strong>${escapeHtml(stage.label)}</strong>
      ${detail}
    </div>
  `;
}

function pipelineStageRequested(task, key) {
  const options = task.options || {};
  if (key === "cleanup") {
    return Boolean(options.delete_target_before || options.overwrite_archive_target_before);
  }
  if (key === "cleanup_source") {
    return Boolean(options.delete_source_after);
  }
  return true;
}

function taskTemplate(task, options = {}) {
  const logs = task.logs || [];
  const pathSection = options.compact
    ? ""
    : `
      <div class="path-grid">
        <div class="path-box"><div class="meta-line">媒体库</div><div class="item-path">${escapeHtml(task.media_target_path || "-")}</div></div>
        <div class="path-box"><div class="meta-line">归档</div><div class="item-path">${escapeHtml(task.archive_target_path || "-")}</div></div>
      </div>
    `;
  const statusLine = options.compact
    ? ""
    : `<div class="meta-line">${escapeHtml(task.stage)} · ${escapeHtml(task.source_path || task.run_id || "")}</div>`;
  const progressBar = options.compact
    ? ""
    : `<div class="progress"><span style="width:${Number(task.progress || 0)}%"></span></div>`;
  return `
    <div class="task-head">
      <div>
        <div class="item-title">${escapeHtml(task.source_name || task.run_id)}</div>
        ${statusLine}
      </div>
      ${taskStatusBadge(task)}
    </div>
    ${progressBar}
    ${pathSection}
    ${task.error ? `<div class="diagnosis"><strong>失败原因</strong><span>${escapeHtml(task.error)}</span></div>` : ""}
    <pre>${escapeHtml(logs.map((log) => `${formatDate(log.at)} ${log.message}`).join("\n"))}</pre>
    ${
      task.status === "failed"
        ? `<button data-retry="${task.id}">${task.kind === "organize" ? "重试整理" : "重试分析"}</button>`
        : ""
    }
  `;
}

async function retryTask(id) {
  await api(`/api/tasks/${id}/retry`, { method: "POST" });
  await refreshOrganizeTasks();
}

async function retryFailedOrganize() {
  await api("/api/tasks/retry-failed", { method: "POST" });
  await refreshOrganizeTasks();
}

async function refreshRuns() {
  const params = new URLSearchParams({
    latest_only: "false",
    limit: "100",
    status: byId("runAnalysisStatusFilter")?.value || "",
    organize_status: byId("runOrganizeStatusFilter")?.value || "",
    source: byId("runSourceFilter")?.value || "",
  });
  const data = await api(`/api/runs?${params.toString()}`);
  state.runs = data.runs || [];
  const available = new Set(state.runs.map((run) => run.run_id));
  state.selectedRunIds = new Set(
    [...state.selectedRunIds].filter((runId) => available.has(runId)),
  );
  renderRuns();
}

function renderRuns() {
  const list = byId("runList");
  if (!state.runs.length) {
    list.className = "run-table empty";
    list.textContent = "暂无运行记录";
    updateRunDeleteButton();
    return;
  }
  list.className = "run-table";
  list.innerHTML = `
    <div class="run-row header">
      <label class="run-select-all">
        <input type="checkbox" data-run-select-all ${state.selectedRunIds.size === state.runs.length ? "checked" : ""} />
      </label>
      <span>创建日期</span>
      <span>源文件夹</span>
      <span>分析状态</span>
      <span>流水线执行状态</span>
    </div>
  `;
  state.runs.forEach((run) => {
    const row = document.createElement("div");
    row.className = "run-row";
    row.dataset.runId = run.run_id;
    row.innerHTML = `
      <label class="run-check" aria-label="选择 ${escapeHtml(run.run_id)}">
        <input type="checkbox" data-run-select="${escapeHtml(run.run_id)}" ${state.selectedRunIds.has(run.run_id) ? "checked" : ""} />
      </label>
      <span class="meta-line">${escapeHtml(formatDate(run.created_at))}</span>
      <span class="item-path">
        <strong>${escapeHtml(run.source_name || "-")}</strong>
        <em>${escapeHtml(run.run_id)}</em>
      </span>
      <span>${analysisStatusBadge(run)}</span>
      <span>${organizeStatusBadge(run.organize_status)}</span>
    `;
    list.appendChild(row);
  });
  list.querySelector("[data-run-select-all]")?.addEventListener("change", (event) => {
    setAllRunChecks(event.target.checked);
  });
  list.querySelectorAll("[data-run-select]").forEach((input) => {
    input.addEventListener("change", (event) => {
      const runId = event.target.dataset.runSelect;
      if (!runId) return;
      if (event.target.checked) {
        state.selectedRunIds.add(runId);
      } else {
        state.selectedRunIds.delete(runId);
      }
      updateRunDeleteButton();
      syncRunSelectAll();
    });
  });
  syncRunSelectAll();
  updateRunDeleteButton();
}

function setAllRunChecks(checked) {
  state.selectedRunIds = checked ? new Set(state.runs.map((run) => run.run_id)) : new Set();
  byId("runList").querySelectorAll("[data-run-select]").forEach((input) => {
    input.checked = checked;
  });
  syncRunSelectAll();
  updateRunDeleteButton();
}

function syncRunSelectAll() {
  const input = byId("runList").querySelector("[data-run-select-all]");
  if (!input) return;
  input.checked = Boolean(state.runs.length && state.selectedRunIds.size === state.runs.length);
  input.indeterminate = Boolean(
    state.selectedRunIds.size && state.selectedRunIds.size < state.runs.length,
  );
}

function updateRunDeleteButton() {
  const button = byId("deleteSelectedRunsBtn");
  if (!button) return;
  const count = state.selectedRunIds.size;
  button.disabled = count === 0;
  button.textContent = count ? `删除选中记录 (${count})` : "删除选中记录";
}

async function deleteSelectedRuns() {
  const runIds = [...state.selectedRunIds];
  if (!runIds.length) return;
  const confirmed = window.confirm(`确认删除 ${runIds.length} 条运行记录？此操作会删除对应 run 文件夹。`);
  if (!confirmed) return;
  const button = byId("deleteSelectedRunsBtn");
  setBusy(button, true);
  try {
    const result = await api("/api/runs", {
      method: "DELETE",
      body: JSON.stringify({ run_ids: runIds }),
    });
    for (const runId of result.deleted || []) {
      state.selectedRunIds.delete(runId);
    }
    await Promise.allSettled([refreshRuns(), refreshResults(), refreshOrganizeTasks()]);
    if (result.failed?.length) {
      throw new Error(
        result.failed.map((item) => `${item.run_id}: ${item.error}`).join("\n"),
      );
    }
  } catch (error) {
    window.alert(`删除失败：${error.message}`);
  } finally {
    setBusy(button, false);
    updateRunDeleteButton();
  }
}

function renderResultDiagnostics(result) {
  const plan = result.work_plan;
  const issues = [];
  if (result.status === "failed" && result.summary) {
    issues.push({
      title: "分析失败原因",
      detail: result.summary,
    });
  }
  if (plan?.missing_tmdb_episodes?.length) {
    issues.push({
      title: "TMDB 剧集缺失",
      detail: `${plan.missing_tmdb_episodes.length} 个 TMDB episode 没有映射到视频文件。`,
    });
  }
  if (plan?.missing_movies?.length) {
    issues.push({
      title: "TMDB 电影缺失",
      detail: `${plan.missing_movies.length} 个选中的 TMDB movie 没有映射到视频文件。`,
    });
  }
  if (plan?.rejected_mappings?.length) {
    issues.push({
      title: "映射被拒绝",
      detail: `${plan.rejected_mappings.length} 条 LLM 映射没有通过程序校验。`,
    });
  }
  if (!plan?.validated_mappings?.length && result.status !== "succeeded" && result.status !== "failed") {
    issues.push({
      title: "没有有效映射",
      detail: "程序没有校验通过任何可整理的视频映射。",
    });
  }
  if (!issues.length && result.status === "succeeded") {
    return `<div class="diagnosis ok"><strong>分析通过</strong><span>${escapeHtml(result.summary)}</span></div>`;
  }
  if (!issues.length) return "";
  return `
    <div class="diagnosis">
      <strong>${result.status === "failed" ? "分析失败" : "需要处理"}</strong>
      <div class="issue-grid">${issues.map(renderIssue).join("")}</div>
    </div>
  `;
}

function renderIssue(issue) {
  return `
    <div class="issue-card">
      <strong>${escapeHtml(issue.title)}</strong>
      <span>${escapeHtml(issue.detail)}</span>
    </div>
  `;
}

function renderPlanDetails(plan, runId = "") {
  if (!plan) return "";
  return [
    renderMappingBlock(
      "缺失 TMDB Episodes",
      plan.missing_tmdb_episodes || [],
      (item) => renderMissingEpisode(item, plan, runId),
      Number.POSITIVE_INFINITY,
    ),
    renderMappingBlock("缺失 Movies", plan.missing_movies || [], renderMissingMovie),
    renderMappingBlock("Rejected 映射", plan.rejected_mappings || [], renderRejectedMapping, 18),
    renderMappingBlock("Unmapped 文件", plan.unmapped_files || [], renderUnmappedFile, 18),
  ].filter(Boolean).join("");
}

function renderMappingBlock(title, items, renderer, limit = 12) {
  if (!items.length) return "";
  const visible = items.slice(0, limit);
  const more = items.length > visible.length ? `<div class="more-line">还有 ${items.length - visible.length} 条未显示</div>` : "";
  return `
    <details class="detail-block" ${title.includes("缺失") ? "open" : ""}>
      <summary>${title} <span>${items.length}</span></summary>
      <div class="mapping-list">${visible.map(renderer).join("")}${more}</div>
    </details>
  `;
}

function renderMissingEpisode(item, plan, runId) {
  const candidates = (plan.unmapped_files || []).filter((file) => file.file_kind === "video");
  const key = `${item.season_number}-${item.episode_number}`;
  const options = candidates.map((file) => {
    const label = displaySourcePath(file.source_path, plan.source_path);
    return `<option value="${escapeHtml(file.source_path)}">${escapeHtml(label)}</option>`;
  }).join("");
  const disabled = candidates.length ? "" : "disabled";
  return `
    <details class="episode-missing-row">
      <summary>
        <span>S${pad2(item.season_number)}E${pad2(item.episode_number)}</span>
        <strong>${escapeHtml(item.episode_name || "未命名")}</strong>
      </summary>
      <div class="manual-map-editor">
        <label>映射源视频
          <select data-manual-map-select="${escapeHtml(key)}" ${disabled}>
            <option value="">不映射这个剧集</option>
            ${options}
          </select>
        </label>
        <button
          data-manual-map-run="${escapeHtml(runId)}"
          data-season-number="${item.season_number}"
          data-episode-number="${item.episode_number}"
          data-select-key="${escapeHtml(key)}"
          ${disabled || (runId ? "" : "disabled")}
        >保存映射</button>
        <span class="manual-map-status" data-manual-map-status="${escapeHtml(key)}"></span>
      </div>
    </details>
  `;
}

function renderMissingMovie(item) {
  return `
    <div class="mapping-row">
      <strong>${escapeHtml(item.title || item.tmdb_movie_id)} (${escapeHtml(item.year || "0000")})</strong>
      <span>${escapeHtml(item.reason)}</span>
    </div>
  `;
}

async function saveManualEpisodeMapping(button) {
  const runId = button.dataset.manualMapRun || "";
  const key = button.dataset.selectKey || "";
  const select = document.querySelector(`[data-manual-map-select="${cssEscape(key)}"]`);
  const status = document.querySelector(`[data-manual-map-status="${cssEscape(key)}"]`);
  const sourcePath = select?.value || "";
  if (!runId || !sourcePath) {
    if (status) status.textContent = "未选择文件";
    return;
  }
  button.disabled = true;
  if (status) status.textContent = "保存中";
  try {
    await api(`/api/runs/${encodeURIComponent(runId)}/manual-episode-mappings`, {
      method: "POST",
      body: JSON.stringify({
        source_path: sourcePath,
        season_number: Number(button.dataset.seasonNumber),
        episode_number: Number(button.dataset.episodeNumber),
      }),
    });
    if (status) status.textContent = "已写入 manual_episode_mappings.json";
    button.textContent = "已保存";
  } catch (error) {
    if (status) status.textContent = error.message;
    button.disabled = false;
  }
}

function renderRejectedMapping(item) {
  return `
    <div class="mapping-row">
      <strong>${escapeHtml(reasonLabel(item.reason))}</strong>
      <span>${escapeHtml(item.source_path)}</span>
      ${item.details ? `<em>${escapeHtml(item.details)}</em>` : ""}
    </div>
  `;
}

function renderUnmappedFile(item) {
  return `
    <div class="mapping-row">
      <strong>${escapeHtml(item.file_kind)} · ${escapeHtml(reasonLabel(item.reason))}</strong>
      <span>${escapeHtml(item.source_path)}</span>
    </div>
  `;
}

async function testOpenlist() {
  setBusy("testOpenlistBtn", true);
  try {
    const result = await api("/api/config/test-openlist", {
      method: "POST",
      body: JSON.stringify(readConfigForm().openlist),
    });
    byId("settingsMessage").textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    byId("settingsMessage").textContent = error.message;
  } finally {
    setBusy("testOpenlistBtn", false);
  }
}

async function loadModels() {
  setBusy("loadModelsBtn", true);
  try {
    const result = await api("/api/llm/models", {
      method: "POST",
      body: JSON.stringify(readConfigForm().llm),
    });
    byId("modelSelect").innerHTML = (result.models || [])
      .map((model) => `<option value="${escapeHtml(model)}">${escapeHtml(model)}</option>`)
      .join("");
  } catch (error) {
    byId("settingsMessage").textContent = error.message;
  } finally {
    setBusy("loadModelsBtn", false);
  }
}

async function saveConfig() {
  const config = readConfigForm();
  const saved = await api("/api/config", {
    method: "PUT",
    body: JSON.stringify(config),
  });
  state.config = saved;
  await loadRuntimeStatus();
  byId("settingsMessage").textContent =
    "已保存到 data/config.json。当前运行中的任务仍使用启动时配置；重启程序后新配置生效。";
}

function fillConfigForm(config) {
  byId("openlistBaseUrl").value = config.openlist.base_url;
  byId("openlistUsername").value = config.openlist.username;
  byId("openlistPassword").value = config.openlist.password;
  byId("openlistTimeout").value = config.openlist.request_timeout_seconds;
  byId("openlistInterval").value = config.openlist.operation_interval_seconds;
  byId("openlistRetries").value = config.openlist.retry_count;
  byId("openlistRefreshAll").checked = config.openlist.refresh_all_on_full_scan;
  byId("llmBaseUrl").value = config.llm.base_url;
  byId("llmApiKey").value = config.llm.api_key;
  byId("llmModel").value = config.llm.model;
  byId("llmTimeout").value = config.llm.request_timeout_seconds;
  byId("tmdbBaseUrl").value = config.tmdb.base_url;
  byId("tmdbApiKey").value = config.tmdb.api_key;
  byId("tmdbLanguage").value = config.tmdb.language;
  byId("tmdbLanguages").value = config.tmdb.allowed_languages.join(", ");
  byId("tmdbTimeout").value = config.tmdb.request_timeout_seconds;
  byId("sourcePath").value = config.media_library.source_path;
  byId("tvMediaPath").value = config.media_library.tv_media_library_path;
  byId("movieMediaPath").value = config.media_library.movie_media_library_path;
  byId("archivePath").value = config.media_library.archive_path;
  byId("archiveTemplate").value = config.media_library.archive_path_template;
  byId("tvMediaTemplate").value = config.media_library.tv_media_library_path_template;
  byId("movieMediaTemplate").value = config.media_library.movie_media_library_path_template;
  byId("includeEpisodeTitle").checked = config.media_library.include_episode_title_in_filename;
  byId("treeMaxDepth").value = config.scan.tree_max_depth;
  byId("treeMaxNodes").value = config.scan.tree_max_nodes;
  byId("videoExtensions").value = config.scan.video_extensions.join(", ");
  byId("subtitleExtensions").value = config.scan.subtitle_extensions.join(", ");
  byId("ignoredFolderNames").value = config.scan.ignored_folder_names.join(", ");
}

function readConfigForm() {
  return {
    openlist: {
      base_url: byId("openlistBaseUrl").value,
      username: byId("openlistUsername").value,
      password: byId("openlistPassword").value,
      request_timeout_seconds: numberValue("openlistTimeout"),
      operation_interval_seconds: numberValue("openlistInterval"),
      retry_count: numberValue("openlistRetries"),
      refresh_all_on_full_scan: byId("openlistRefreshAll").checked,
    },
    llm: {
      base_url: byId("llmBaseUrl").value,
      api_key: byId("llmApiKey").value,
      model: byId("llmModel").value,
      request_timeout_seconds: numberValue("llmTimeout"),
    },
    tmdb: {
      base_url: byId("tmdbBaseUrl").value,
      api_key: byId("tmdbApiKey").value,
      language: byId("tmdbLanguage").value,
      allowed_languages: splitCsv("tmdbLanguages"),
      request_timeout_seconds: numberValue("tmdbTimeout"),
    },
    media_library: {
      source_path: byId("sourcePath").value,
      tv_media_library_path: byId("tvMediaPath").value,
      movie_media_library_path: byId("movieMediaPath").value,
      archive_path: byId("archivePath").value,
      archive_path_template: byId("archiveTemplate").value,
      tv_media_library_path_template: byId("tvMediaTemplate").value,
      movie_media_library_path_template: byId("movieMediaTemplate").value,
      include_episode_title_in_filename: byId("includeEpisodeTitle").checked,
    },
    scan: {
      tree_max_depth: numberValue("treeMaxDepth"),
      tree_max_nodes: numberValue("treeMaxNodes"),
      video_extensions: splitCsv("videoExtensions"),
      subtitle_extensions: splitCsv("subtitleExtensions"),
      ignored_folder_names: splitCsv("ignoredFolderNames"),
    },
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    let detail = await response.text();
    try {
      detail = JSON.stringify(JSON.parse(detail).detail);
    } catch {
      // Keep raw text.
    }
    throw new Error(detail);
  }
  return response.json();
}

function selectedResult() {
  return state.results.find((item) => resultKey(item) === state.selectedResultId) || null;
}

function runForAnalysis(analysisId) {
  return state.runs.find((run) => {
    const result = state.results.find((item) => item.id === analysisId);
    return result && run.source_path === result.source_path && run.title === result.title;
  });
}

function resultKey(result) {
  return result.run_id || result.id || "";
}

function libraryTargetText(result) {
  const targets = result.work_plan?.library_targets || [];
  if (targets.length) return targets.map((target) => target.target_path).join("\n");
  return result.media_target_path || "-";
}

function displayOriginTitle(result) {
  return result?.original_title || result?.title || "-";
}

function normalizeReportTree(tree) {
  return String(tree || "").replace(/^dry-run\s*\r?\n?/i, "");
}

function renderTextRow(label, value) {
  const safeValue = escapeHtml(value || "-").replaceAll("\r\n", "<br>").replaceAll("\n", "<br>");
  return `
    <div class="detail-row">
      <span>${escapeHtml(label)}</span>
      <strong>${safeValue}</strong>
    </div>
  `;
}

function renderQueueField(label, value) {
  return `
    <div class="queue-field">
      <span>${escapeHtml(label)}</span>
      <strong>${escapeHtml(value || "-")}</strong>
    </div>
  `;
}

function analysisStatusBadge(item) {
  const status = item.status || item.analysis_status || "";
  const plan = item.work_plan || {};
  const missingEpisodes = countOf(plan.missing_tmdb_episodes) || Number(item.missing_tmdb_episodes || 0);
  const missingMovies = countOf(plan.missing_movies) || Number(item.missing_movies || 0);
  const rejected = countOf(plan.rejected_mappings) || Number(item.rejected || 0);
  const validated = countOf(plan.validated_mappings) || Number(item.validated || 0);
  const label = analysisStatusLabel(status, {
    missingEpisodes,
    missingMovies,
    rejected,
    validated,
    reviewReason: item.review_reason || "",
  });
  return statusBadge(status, label);
}

function analysisStatusLabel(status, context = {}) {
  if (status === "succeeded") {
    return context.validated ? `分析通过 · ${context.validated} 项` : "分析通过";
  }
  if (status === "needs_review") {
    if (context.missingEpisodes) return `需确认 · 缺 ${context.missingEpisodes} 集`;
    if (context.missingMovies) return `需确认 · 缺 ${context.missingMovies} 部电影`;
    if (context.rejected) return `需确认 · ${context.rejected} 条拒绝`;
    if (context.reviewReason) return "需人工确认";
    return "需人工确认";
  }
  if (status === "failed") return "分析失败";
  if (status === "unknown") return "状态未知";
  return status || "未分析";
}

function taskStatusBadge(task) {
  const label = taskStatusLabel(task);
  return statusBadge(task.status, label);
}

function taskStatusLabel(task) {
  const stage = task.stage || "";
  if (task.status === "queued") return task.kind === "organize" ? "整理排队中" : "分析排队中";
  if (task.status === "running") {
    const label = task.kind === "organize" ? organizeStageLabel(stage) : analysisStageLabel(stage);
    return label || (task.kind === "organize" ? "整理中" : "分析中");
  }
  if (task.status === "succeeded") {
    if (task.kind === "analysis" && task.analysis_status) {
      return analysisStatusLabel(task.analysis_status);
    }
    return task.kind === "organize" ? "整理完成" : "分析完成";
  }
  if (task.status === "failed") return task.kind === "organize" ? "整理失败" : "分析失败";
  if (task.status === "interrupted") return task.kind === "organize" ? "整理已中断" : "任务已中断";
  return task.status || "状态未知";
}

function analysisStageLabel(stage) {
  const labels = {
    queued: "分析排队中",
    prepare: "准备运行目录",
    manual_tmdb: "使用指定 TMDB",
    identify_work: "识别作品",
    tmdb_search: "查询 TMDB",
    scan: "扫描视频文件",
    select_candidates: "选择 TMDB 候选",
    tmdb_details: "读取 TMDB 剧集",
    decide_mappings: "生成映射",
    validate: "校验结果",
    complete: "分析完成",
    failed: "分析失败",
  };
  return labels[stage] || "";
}

function organizeStageLabel(stage) {
  const labels = {
    queued: "整理排队中",
    start: "开始整理",
    preflight: "检查目标路径",
    cleanup: "清理目标",
    archive_skip: "跳过归档复制",
    archive_reuse: "复用归档",
    archive_copy: "复制归档",
    library_prepare: "准备媒体库",
    library_skip: "跳过已存在文件",
    library_copy: "复制媒体文件",
    library_rename: "重命名媒体文件",
    verify: "校验整理结果",
    cleanup_source: "删除源文件夹",
    complete: "整理完成",
    failed: "整理失败",
  };
  return labels[stage] || "";
}

function organizePipelineStages(task) {
  const stageDefs = [
    { key: "preflight", label: "检查" },
    { key: "cleanup", label: "清理" },
    { key: "archive", label: "归档" },
    { key: "library", label: "媒体库" },
    { key: "verify", label: "校验" },
    { key: "cleanup_source", label: "删源" },
    { key: "complete", label: "完成" },
  ];
  const logs = task.logs || [];
  const progress = Number(task.progress || 0);
  const effectiveStage = ["failed", "interrupted"].includes(task.status)
    ? failedPipelineStage(task)
    : task.stage;
  const currentKey = pipelineKeyForStage(effectiveStage);
  const currentIndex = Math.max(0, stageDefs.findIndex((stage) => stage.key === currentKey));
  const failedKey = task.status === "failed" ? currentKey : "";
  const interruptedKey = task.status === "interrupted" ? currentKey : "";
  return stageDefs.map((stage, index) => {
    const relatedLogs = logs.filter((log) => pipelineKeyForStage(log.stage) === stage.key);
    const latest = relatedLogs[relatedLogs.length - 1] || null;
    let stateName = "pending";
    if (task.status === "queued") {
      stateName = "pending";
    } else if (failedKey && stage.key === failedKey) {
      stateName = "failed";
    } else if (interruptedKey && stage.key === interruptedKey) {
      stateName = "interrupted";
    } else if (task.status === "succeeded" || index < currentIndex) {
      stateName = "succeeded";
    } else if (task.status === "running" && stage.key === currentKey) {
      stateName = "running";
    }
    if (["cleanup", "cleanup_source"].includes(stage.key) && !pipelineStageRequested(task, stage.key)) {
      if (task.status === "succeeded" || index < currentIndex) {
        stateName = "skipped";
      }
    }
    return {
      ...stage,
      state: stateName,
      detail: pipelineStageDetail({ stage, stateName, task, latest, progress }),
    };
  });
}

function failedPipelineStage(task) {
  const logs = task.logs || [];
  for (let index = logs.length - 1; index >= 0; index -= 1) {
    const stage = logs[index]?.stage;
    if (stage && stage !== "failed") return stage;
  }
  return task.stage || "preflight";
}

function pipelineKeyForStage(stage) {
  if (["queued", "start"].includes(stage)) return "preflight";
  if (["cleanup"].includes(stage)) return "cleanup";
  if (["archive_skip", "archive_reuse", "archive_copy"].includes(stage)) return "archive";
  if (["library_prepare", "library_skip", "library_copy", "library_rename"].includes(stage)) return "library";
  if (["verify"].includes(stage)) return "verify";
  if (["cleanup_source"].includes(stage)) return "cleanup_source";
  if (["complete"].includes(stage)) return "complete";
  if (["failed"].includes(stage)) return "complete";
  return stage || "preflight";
}

function pipelineStageDetail({ stage, stateName, task, latest, progress }) {
  if (stateName === "failed") return "失败";
  if (stateName === "interrupted") return "中断";
  if (stateName === "running") return `${Math.max(0, Math.min(100, progress))}%`;
  if (stateName === "succeeded") {
    if (latest?.at) return formatTime(latest.at);
    return "完成";
  }
  if (stateName === "skipped") return "跳过";
  if (task.status === "queued" && stage.key === "preflight") return "等待";
  return "";
}

function organizeStatusBadge(status) {
  const labels = {
    not_started: "未整理",
    running: "整理中",
    succeeded: "整理完成",
    failed: "整理失败",
    interrupted: "整理中断",
  };
  return statusBadge(status, labels[status] || status || "未整理");
}

function statusBadge(status, label) {
  const className = statusClass(status);
  return `<span class="tag ${escapeHtml(className)}">${escapeHtml(label)}</span>`;
}

function statusClass(status) {
  return String(status || "unknown").replaceAll("_", "-");
}

function displaySourcePath(sourcePath, sourceRoot = "") {
  const prefix = sourceRoot ? `${sourceRoot.replace(/\/$/, "")}/` : "";
  return prefix && sourcePath.startsWith(prefix) ? sourcePath.slice(prefix.length) : sourcePath;
}

function countOf(value) {
  return Array.isArray(value) ? value.length : 0;
}

function numberValue(id) {
  return Number(byId(id).value);
}

function splitCsv(id) {
  return byId(id).value.split(",").map((item) => item.trim()).filter(Boolean);
}

function setBusy(id, busy) {
  const element = typeof id === "string" ? byId(id) : id;
  if (element) element.disabled = busy;
}

function byId(id) {
  return document.getElementById(id);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function cssEscape(value) {
  if (window.CSS?.escape) return CSS.escape(value);
  return String(value).replaceAll('"', '\\"');
}

function formatDate(value) {
  if (!value) return "";
  return new Date(value).toLocaleString();
}

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleTimeString();
}

function pad2(value) {
  return String(value ?? 0).padStart(2, "0");
}

function reasonLabel(reason) {
  const labels = {
    tmdb_episode_missing: "缺少明确 TMDB episode",
    tmdb_movie_id_missing: "缺少明确 TMDB movie",
    source_not_in_candidate_list: "LLM 引用了候选列表外的源文件",
    source_is_not_video: "源文件不是视频",
    duplicate_source_mapping: "同一源文件被重复映射",
    duplicate_target_path: "目标路径重复",
    not_selected_for_media_library: "未进入媒体库，仅归档",
  };
  return labels[reason] || reason || "-";
}

function renderInlineError(container, error) {
  container.className = "list";
  container.innerHTML = `<div class="diagnosis"><strong>操作失败</strong><span>${escapeHtml(error.message)}</span></div>`;
}
