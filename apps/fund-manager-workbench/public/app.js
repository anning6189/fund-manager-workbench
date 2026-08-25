"use strict";

const token = document.querySelector('meta[name="workbench-token"]').content;
const root = document.getElementById("app");
const toastNode = document.getElementById("toast");

const state = {
  view: ["today", "ask", "library", "models", "reports", "data"].includes(location.hash.replace("#", "")) ? location.hash.replace("#", "") : "today",
  bootstrap: null,
  llmConfig: loadLlmConfig(),
  llmStatus: null,
  alerts: [],
  jobs: [],
  reports: [],
  dataStatus: null,
  opsStatus: null,
  activeReport: null,
  reportTab: "report",
};

const nav = [
  ["today", "首页"],
  ["ask", "AI研究员"],
  ["library", "研报库"],
  ["data", "数据来源"],
];

const pageTitles = Object.fromEntries(nav);
const categoryLabels = { industry: "行业研究", monitoring: "持续监控", company: "公司研究", event: "事件研究", model: "模型分析" };
const severityLabels = { critical: "紧急", important: "重要", watch: "关注", info: "提示" };
const statusLabels = {
  queued: "排队中", validating: "正在校验", running: "研究执行中",
  completed: "已完成", internal_research_ready: "内部研究可用", quality_checks_pending: "系统检查中",
  blocked: "受阻", failed: "失败", cancelled: "已取消", open: "待处理",
  acknowledged: "已确认", resolved: "已解决", fresh: "新鲜", stale: "过期", not_started: "未启动",
};

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[c]));
}

function formatDate(value, includeTime = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return escapeHtml(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  }).format(date).replaceAll("/", "-");
}

function toast(message, error = false) {
  toastNode.textContent = message;
  toastNode.className = `toast show${error ? " error" : ""}`;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { toastNode.className = "toast"; }, 3200);
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.body ? { "Content-Type": "application/json", "X-Workbench-Token": token } : {}), ...(options.headers || {}) };
  const response = await fetch(path, { ...options, headers });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || `请求失败（${response.status}）`);
  return payload;
}

function loadLlmConfig() {
  try {
    const value = JSON.parse(localStorage.getItem("llm-enhance-config-v1") || "null");
    return value && typeof value === "object" ? value : { enabled: false };
  } catch (e) {
    return { enabled: false };
  }
}

function saveLlmConfig(config) {
  state.llmConfig = { ...(config || {}), api_key: String(config?.api_key || "").trim() };
  try { localStorage.setItem("llm-enhance-config-v1", JSON.stringify(state.llmConfig)); } catch (e) { /* ignore */ }
}

function publicLlmConfig() {
  const cfg = state.llmConfig || {};
  if (!cfg.enabled || !cfg.base_url || !cfg.model || !cfg.api_key) return null;
  return {
    enabled: true,
    provider: cfg.provider || "openai-compatible",
    base_url: cfg.base_url,
    model: cfg.model,
    api_key: cfg.api_key,
  };
}

function maskSecret(value) {
  const text = String(value || "");
  if (!text) return "未填写";
  if (text.length <= 8) return "********";
  return `${text.slice(0, 4)}****${text.slice(-4)}`;
}

function status(value, kind) {
  const label = statusLabels[value] || value || "未知";
  const tone = kind || (value === "fresh" || value === "completed" || value === "internal_research_ready" ? "good" :
    value === "critical" || value === "failed" || value === "blocked" ? "bad" :
    value === "important" || value === "stale" ? "watch" : "info");
  return `<span class="status ${tone}">${escapeHtml(label)}</span>`;
}

function shell(content) {
  const b = state.bootstrap;
  return `
    <div class="shell">
      <header class="briefing-header">
        <button class="briefing-brand" data-nav="today" aria-label="返回今日简讯">
      <strong>消费行研agent</strong><span>消费行业研究 Agent</span>
        </button>
        <nav class="nav-list" aria-label="主导航">
          ${nav.map(([id, label]) => `<button class="nav-item ${state.view === id ? "active" : ""}" data-nav="${id}">${label}</button>`).join("")}
        </nav>
  <div class="briefing-tools">
    <button class="llm-settings-btn ${publicLlmConfig() ? "on" : ""}" id="llm-settings" title="模型增强设置" aria-label="模型增强设置">⚙ <span>${publicLlmConfig() ? "AI增强" : "设置"}</span></button>
    <div class="briefing-date"><strong>${escapeHtml(b.cutoff.date)}</strong><span>研究截止 · 08:00</span></div>
  </div>
      </header>
      <main class="workspace">
        <div class="content">${content}</div>
      </main>
    </div>`;
}

function bindShell() {
  document.querySelectorAll("[data-nav]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.nav)));
  document.getElementById("llm-settings")?.addEventListener("click", openLlmSettings);
}

async function openLlmSettings() {
  let serverStatus = state.llmStatus;
  try {
    serverStatus = await api("/api/llm/status");
    state.llmStatus = serverStatus;
  } catch (e) {
    serverStatus = { mode: "unknown", note: "暂时无法读取模型增强状态。" };
  }
  const cfg = state.llmConfig || { enabled: false };
  const drawer = document.createElement("div");
  drawer.className = "detail-backdrop";
  drawer.innerHTML = `<aside class="brief-drawer llm-settings-drawer" role="dialog" aria-modal="true">
    <div class="drawer-head"><div><span>用户自带 Key · 模式 B</span><h2>模型增强设置</h2></div><button class="drawer-close" aria-label="关闭设置">×</button></div>
    <div class="drawer-body">
      <section class="drawer-section llm-mode-card ${cfg.enabled ? "on" : ""}">
        <h3>当前模式</h3>
        <p><strong>${cfg.enabled ? "AI 增强版" : "规则基础版"}</strong>：不接入大模型时，今日主推、股票池分层、走势图、热力图、历史评级、后验审计仍然正常运行；接入后增强问答、解释、复盘和自校对表达。</p>
        <dl class="detail-grid">
          <div><dt>保存位置</dt><dd>当前浏览器</dd></div>
          <div><dt>服务器默认</dt><dd>${escapeHtml(serverStatus.base_mode || "rule_only")}</dd></div>
          <div><dt>模型名称</dt><dd>${escapeHtml(cfg.model || "未配置")}</dd></div>
          <div><dt>API Key</dt><dd>${escapeHtml(maskSecret(cfg.api_key))}</dd></div>
        </dl>
      </section>
      <section class="drawer-section">
        <h3>接入 OpenAI-compatible 模型</h3>
        <div class="llm-form">
          <label><span>启用 AI 增强</span><input id="llm-enabled" type="checkbox" ${cfg.enabled ? "checked" : ""}></label>
          <label><span>服务商标识</span><input class="input" id="llm-provider" value="${escapeHtml(cfg.provider || "openai-compatible")}" placeholder="openai / deepseek / kimi / qwen"></label>
          <label><span>API Base URL</span><input class="input" id="llm-base-url" value="${escapeHtml(cfg.base_url || "")}" placeholder="https://api.openai.com/v1"></label>
          <label><span>模型名称</span><input class="input" id="llm-model" value="${escapeHtml(cfg.model || "")}" placeholder="gpt-5.5 / deepseek-chat / qwen-plus"></label>
          <label class="full"><span>API Key</span><input class="input" id="llm-api-key" type="password" value="${escapeHtml(cfg.api_key || "")}" placeholder="只保存在当前浏览器，不写入 Git"></label>
        </div>
        <p class="llm-note">安全说明：该 Key 不写入项目数据库，不提交 GitHub；测试和问答时会临时发给本 Agent 后端转发到您填写的模型服务。</p>
        <p class="llm-test-result" id="llm-test-result"></p>
      </section>
      <section class="drawer-section"><h3>增强范围</h3><p>${escapeHtml(serverStatus.note || "填写用户自己的模型 Key 后，仅该浏览器启用 AI 增强。")}</p></section>
    </div>
    <div class="drawer-foot">
      <button class="btn danger" id="llm-clear">关闭并清除</button>
      <button class="btn" id="llm-test">测试连接</button>
      <button class="btn primary" id="llm-save">保存设置</button>
    </div>
  </aside>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  const readForm = () => ({
    enabled: Boolean(drawer.querySelector("#llm-enabled")?.checked),
    provider: drawer.querySelector("#llm-provider")?.value.trim() || "openai-compatible",
    base_url: drawer.querySelector("#llm-base-url")?.value.trim() || "",
    model: drawer.querySelector("#llm-model")?.value.trim() || "",
    api_key: drawer.querySelector("#llm-api-key")?.value.trim() || "",
  });
  const result = drawer.querySelector("#llm-test-result");
  drawer.querySelector(".drawer-close").addEventListener("click", close);
  drawer.addEventListener("click", ev => { if (ev.target === drawer) close(); });
  drawer.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
  drawer.querySelector("#llm-save")?.addEventListener("click", () => {
    const next = readForm();
    saveLlmConfig(next);
    toast(next.enabled ? "模型增强设置已保存" : "已保存：规则基础版");
    close();
    renderView();
  });
  drawer.querySelector("#llm-clear")?.addEventListener("click", () => {
    saveLlmConfig({ enabled: false });
    toast("已清除当前浏览器的模型 Key");
    close();
    renderView();
  });
  drawer.querySelector("#llm-test")?.addEventListener("click", async () => {
    const next = readForm();
    result.textContent = "正在测试连接…";
    result.className = "llm-test-result";
    try {
      const resp = await api("/api/llm/test", { method: "POST", body: JSON.stringify(next) });
      if (!resp.ok) throw new Error(resp.error || "连接失败");
      result.textContent = `连接成功：${resp.model}，耗时 ${(resp.elapsed_ms / 1000).toFixed(1)} 秒。`;
      result.className = "llm-test-result ok";
      saveLlmConfig({ ...next, enabled: true });
    } catch (e) {
      result.textContent = `连接失败：${e.message}`;
      result.className = "llm-test-result err";
    }
  });
  drawer.querySelector(".drawer-close").focus();
}

function navigate(view) {
  state.view = pageTitles[view] ? view : "today";
  location.hash = state.view;
  renderView();
}

function hero(eyebrow, title, description, actions = "") {
  return `<div class="hero"><div><div class="eyebrow">${eyebrow}</div><h1>${title}</h1><p>${description}</p></div>${actions ? `<div class="hero-actions">${actions}</div>` : ""}</div>`;
}

async function loadCore() {
  const [bootstrap, alerts, reports, content, brief, focus, heatmap, ops, calibration] = await Promise.all([api("/api/bootstrap"), api("/api/alerts?limit=80"), api("/api/reports"), api("/api/briefing-content"), api(`/api/morning-brief${state.briefDate ? `?date=${state.briefDate}` : ""}`), api(`/api/stock-focus${state.briefDate ? `?date=${state.briefDate}` : ""}`), api(`/api/sector-heatmap?period=${state.heatPeriod || "day"}`), api("/api/ops-status"), api("/api/self-calibration")]);
  state.bootstrap = bootstrap;
  state.alerts = alerts.alerts;
  state.reports = reports.reports;
  state.briefContent = content;
  state.morningBrief = brief;
  state.stockFocus = focus;
  state.sectorHeatmap = heatmap;
  state.opsStatus = ops;
  state.selfCalibration = calibration;
}

function briefingGroups(alerts) {
  const definitions = [
    { key: "external", match: a => a.rule_code === "CR.MON.EVENT.MATERIAL", area: a => a.sector_name || "消费行业", title: a => a.title, implication: a => a.detail?.summary || "该事件达到重要性阈值，需结合原始来源与后续数据验证其持续影响。" },
    { key: "macro", streams: ["macro"], area: "国内宏观", title: "宏观数据时点完整性需要关注", implication: "国家统计局、人民银行等宏观数据流尚未覆盖到研究截止时点，当前不宜据此形成方向性宏观判断。", healthyTitle: "宏观数据已同步至昨日截止", healthyImplication: "宏观数据流均已验证覆盖研究截止时点，可用于宏观研究；具体发布与数值见详情。" },
    { key: "policy", streams: ["official_policy_documents"], area: "消费政策", title: "消费政策文件监控存在更新缺口", implication: "政策催化与约束信息可能尚未完整进入研究底座，涉及政策敏感行业时应等待来源恢复。", healthyTitle: "政策文件监控正常", healthyImplication: "政策来源均在新鲜度要求内；进入研究库的政策事件见详情。" },
    { key: "industry", streams: ["official_industry_releases"], area: "行业数据", title: "官方消费行业数据发布尚未完整更新", implication: "文旅、商贸、海关与工业数据缺口会影响对消费总量、结构和景气变化的判断。", healthyTitle: "行业数据已同步", healthyImplication: "行业数据来源均在新鲜度要求内；进入研究库的行业数据事件见详情。" },
    { key: "news", streams: ["news_leads"], area: "资讯舆情", title: "实时资讯线索尚未达到新鲜度要求", implication: "今日突发事件识别可能不完整；系统不会用旧闻或缺失线索包装成当日结论。", healthyTitle: "资讯舆情已更新", healthyImplication: "资讯线索持续到达并已进入研究库，最新条目见详情。" },
    { key: "announcements", streams: ["announcements"], area: "公司公告", title: "交易所与法定公告流尚未完整更新", implication: "上市公司重大事项、业绩预告与风险提示可能存在遗漏，个股层面结论需谨慎。", healthyTitle: "公告流已同步", healthyImplication: "交易所与法定披露流均在新鲜度要求内。" },
    { key: "market", streams: ["market_daily"], area: "市场表现", title: "消费板块日行情数据尚未就绪", implication: "当前不能可靠比较板块涨跌、成交与估值变化，页面不展示推断性行情结论。", healthyTitle: "行情数据已就绪", healthyImplication: "日行情数据在新鲜度要求内。" },
    { key: "financials", streams: ["financials"], area: "财务跟踪", title: "财务数据存在过期或未启动数据流", implication: "跨公司经营质量与估值比较可能受旧数据影响，使用报告时需以其明确截止日为准。", healthyTitle: "财务数据流正常", healthyImplication: "财务数据流均在新鲜度要求内；库内文档与研究成果见详情。" },
    { key: "enterprise", streams: ["enterprise_risk"], area: "企业风险", title: "工商与企业风险数据流尚未就绪", implication: "诉讼、股权与工商变更等风险线索可能不完整，不应视为无风险。", healthyTitle: "企业风险数据流正常", healthyImplication: "工商与风险数据流均在新鲜度要求内。" },
  ];
  const used = new Set();
  const output = [];
  for (const definition of definitions) {
    if (definition.key === "external") {
      alerts.filter(definition.match).forEach(alert => output.push({ ...alert, groupKey: "external", area: definition.area(alert), displayTitle: definition.title(alert), implication: definition.implication(alert), sourceLabel: alert.detail?.source_name || alert.source?.name || "实时监控", sourceAlerts: [alert] }));
      continue;
    }
    const matches = alerts.filter(alert => definition.streams.includes(alert.detail?.stream_name));
    if (used.has(definition.key)) continue;
    used.add(definition.key);
    const gated = matches.length > 0;
    const sources = [...new Set(matches.map(alert => alert.detail?.name).filter(Boolean))];
    const licensedSupplementOnly = gated && definition.key === "macro" && matches.every(alert =>
      alert.source?.license_status === "contract_terms_pending_verification" ||
      alert.detail?.source_id === "CR.SRC.GILDATA.MACRO_INDUSTRY"
    );
    output.push({
      ...(matches[0] || { rule_code: "", state: "ok", detail: {} }),
      groupKey: definition.key,
      healthy: !gated,
      area: definition.area,
      displayTitle: gated ? (licensedSupplementOnly ? "宏观官方数据已同步，补充数据源处于授权隔离" : definition.title) : definition.healthyTitle,
      implication: gated ? (licensedSupplementOnly ? "国家统计局和人民银行数据已覆盖研究截止时点；Gildata 补充数据已采集但尚未获准进入正式研究库，不影响官方宏观数据的使用。" : definition.implication) : definition.healthyImplication,
      sourceLabel: gated ? (sources.slice(0, 2).join("、") || "数据监控") : "全部数据流正常",
      groupedCount: matches.length,
      sourceAlerts: matches,
    });
  }
  return output;
}

function renderToday() {
  renderBrief();
}

function renderOpsStatusCard() {
  const ops = state.opsStatus;
  if (!ops) return "";
  const dates = ops.dates || {};
  const checks = ops.checks || [];
  const statusText = ops.status === "ok" ? "今日自动更新正常" : ops.status === "warn" ? "今日数据需关注" : "自动更新异常";
  const statusClass = ops.status === "ok" ? "ok" : ops.status === "warn" ? "warn" : "err";
  return `
    <section class="ops-status-card ${statusClass}">
      <div class="ops-status-main">
        <span class="ops-dot"></span>
        <div><strong>${escapeHtml(statusText)}</strong><small>下次自动同步：${escapeHtml(ops.next_sync_at || "—")}</small></div>
      </div>
      <div class="ops-date-grid">
        <div><span>晨报日期</span><strong>${escapeHtml(dates.brief_date || "—")}</strong></div>
        <div><span>评级日期</span><strong>${escapeHtml(dates.rating_date || "—")}</strong></div>
        <div><span>行情日期</span><strong>${escapeHtml(dates.market_date || dates.quote_date || "—")}</strong></div>
        <div><span>看板数量</span><strong>${Number(dates.stock_rows || 0).toLocaleString("en-US")}</strong></div>
      </div>
      <div class="ops-checks">
        ${checks.map(c => `<span class="${c.ok ? "ok" : "warn"}">${c.ok ? "✓" : "!"} ${escapeHtml(c.label)}：${escapeHtml(c.detail || "")}</span>`).join("")}
      </div>
      ${ops.last_failure ? `<p class="ops-failure">最近异常：${escapeHtml(ops.last_failure)}</p>` : ""}
    </section>`;
}

const TONE_META = {
  bullish: { icon: "✅", label: "利好", cls: "t-bull" },
  bearish: { icon: "❌", label: "利空", cls: "t-bear" },
  neutral: { icon: "◐", label: "中性", cls: "t-neut" },
  risk: { icon: "⚠️", label: "风险", cls: "t-risk" },
};

function renderBrief() {
  const brief = state.morningBrief;
  if (!brief) { root.innerHTML = shell(`<div class="queue-item">晨报装配中……</div>`); bindShell(); return; }
  const b = state.bootstrap;
  const dailyReport = state.reports.find(r => r.status === "completed" && r.publication_status === "internal_research_ready" && String(r.cutoff_timestamp || "").slice(0, 10) === b.cutoff.date);
  const toneOf = t => TONE_META[t] || TONE_META.neutral;

  const takeawayHtml = brief.takeaway.map((t, i) => `
    <li class="tk-item ${toneOf(t.tone).cls} tk-clickable" data-tk-index="${i}" tabindex="0" role="button" aria-label="查看完整内容：${escapeHtml(t.label)}">
      <span class="tk-icon">${toneOf(t.tone).icon}</span>
      <div><strong>${escapeHtml(t.label)}</strong><p>${escapeHtml(t.text)}</p>
      <small>点击查看完整内容与信息来源 →</small></div>
    </li>`).join("");

  const macroRows = brief.macro_policy.data.filter(d => d.value).map(d => `
    <tr><td>${escapeHtml(d.name)}</td><td class="num"><strong>${escapeHtml(d.value)}</strong></td><td class="num">${escapeHtml(d.change)}</td><td>${escapeHtml(d.note)}</td></tr>`).join("");
  const macroDocs = brief.macro_policy.data.filter(d => !d.value).map(d => `
    <li><strong>${escapeHtml(d.name)}</strong><span class="doc-note">${escapeHtml(d.note)}${d.note.length >= 90 ? "…" : ""}</span><small>${escapeHtml(d.source || "")}</small></li>`).join("");
  const policyEvents = brief.macro_policy.events.map((e, i) => `
    <li class="mp-event tk-clickable" data-mp-index="${i}" tabindex="0" role="button" aria-label="查看完整内容：${escapeHtml(e.title.slice(0, 30))}">
      <div class="mp-event-head">${toneOf(e.tone).icon} <strong><span class="mp-date">${escapeHtml(String(e.available_at || "").slice(5, 10))}</span>${escapeHtml(e.title)}</strong><span class="mp-open">详情 →</span></div>
      <p class="doc-note">${escapeHtml(e.abstract || e.so_what || e.summary || "")}</p>
      <small>${escapeHtml(e.locator || "")}</small>
    </li>`).join("");

  const risksHtml = brief.risks.map(r => `<li class="tk-item t-risk"><span class="tk-icon">⚠️</span><div><p>${escapeHtml(r.text)}</p></div></li>`).join("");
  const picks = brief.macro_policy.daily_picks;
  const pickList = [];
  const pickBlock = (label, items) => (items || []).length ? `
    <div class="pick-block"><h3 class="pick-label">${label}</h3>
      ${items.map(it => { const i = pickList.push({ label, ...it }) - 1; return `<div class="ev-card pick-card tk-clickable" data-pick-i="${i}" tabindex="0" role="button" aria-label="查看详情：${escapeHtml(it.title.slice(0, 24))}"><strong>${escapeHtml(it.title)}</strong><p class="doc-note">${escapeHtml(it.text)}</p><small class="pick-open">点击查看详情与来源 →</small></div>`; }).join("")}
    </div>` : "";
  const dailyPicksHtml = picks ? `
    ${pickBlock("宏观政策", picks.macro_policies)}
    ${pickBlock("重点研报", picks.research_pick ? [picks.research_pick] : [])}
    ${pickBlock("行业大事件", picks.industry_events)}` : "";

  // 晨报历史档案：日期切换
  const briefDates = brief.available_dates || [];
  const curBriefDate = brief.brief_source_date || brief.date;
  const curIdx = briefDates.indexOf(curBriefDate);
  const prevBriefDate = curIdx >= 0 && curIdx < briefDates.length - 1 ? briefDates[curIdx + 1] : null;
  const nextBriefDate = curIdx > 0 ? briefDates[curIdx - 1] : null;
  const dateNav = briefDates.length ? `
    <div class="date-nav">
      <button class="btn small" id="brief-prev" ${prevBriefDate ? "" : "disabled"}>◀ 前一天</button>
      <select id="brief-date-select">${briefDates.map(d => `<option value="${d}" ${d === curBriefDate ? "selected" : ""}>${d}${d === briefDates[0] ? "（最新）" : ""}</option>`).join("")}</select>
      <button class="btn small" id="brief-next" ${nextBriefDate ? "" : "disabled"}>后一天 ▶</button>
      ${brief.is_history ? `<span class="history-badge">历史晨报 · ${escapeHtml(curBriefDate)}</span>` : `<span class="live-badge">最新晨报</span>`}
    </div>` : "";

  const focus = state.stockFocus || { counts: {}, tiers: {}, board: {}, main_push: [] };
  const BOARD_ORDER = ["核心候选", "重点跟踪", "长期好公司", "行业扫描"];
  const BOARD_LABEL = {
    "核心候选": "可考虑买入",
    "重点跟踪": "等待买点",
    "长期好公司": "长期观察",
    "行业扫描": "暂不推荐"
  };
  const BOARD_DESC = {
    "核心候选": "已达推荐基础",
    "重点跟踪": "还差买入信号",
    "长期好公司": "公司好但先观察",
    "行业扫描": "不推荐，仅监控"
  };
  const boardLabel = t => BOARD_LABEL[t] || t;
  const activeTier = state.focusTier && BOARD_ORDER.includes(state.focusTier) ? state.focusTier : "核心候选";
  const pillCls = { "核心候选": "p-focus", "重点跟踪": "p-watch", "长期好公司": "p-neutral", "行业扫描": "p-avoid" };
  const boardCounts = focus.board_counts || {};
  const pills = BOARD_ORDER.map(t => `<button class="pill ${pillCls[t]} ${t === activeTier ? "active" : ""}" data-tier="${t}">
    <span class="pill-text"><strong>${escapeHtml(boardLabel(t))}</strong><em>${escapeHtml(BOARD_DESC[t] || "")}</em></span>
    <span class="pill-count">${boardCounts[t] || 0}</span>
  </button>`).join("");
  // 板块筛选（Excel 风格多选）
  const allStocks = [...(focus.main_push || []), ...Object.values(focus.board || {}).flat()];
  const sectorNames = [...new Set(allStocks.map(s => s.sector_name || "未分类"))];
  // 三态：undefined=全部；空 Set=全不选；非空 Set=显式勾选
  const selectedSectors = state.focusSectors;
  const isAllSectors = selectedSectors === undefined;
  const tierRows = (focus.board || {})[activeTier] || [];
  const filteredRows = tierRows.filter(s => isAllSectors || selectedSectors.has(s.sector_name || "未分类"));
  const sectorPanel = state.sectorPanelOpen ? `
    <div class="sector-panel" id="sector-panel">
      <div class="sector-panel-head">
        <button class="link" id="sector-all">全选</button>
        <button class="link" id="sector-clear">清空</button>
      </div>
      ${sectorNames.map(name => `
        <label class="sector-option"><input type="checkbox" data-sector="${escapeHtml(name)}" ${isAllSectors || selectedSectors.has(name) ? "checked" : ""}><span>${escapeHtml(name)}</span></label>`).join("")}
    </div>` : "";
  const filterLabel = isAllSectors ? "全部板块" : selectedSectors.size === 0 ? "未选板块" : `已选 ${selectedSectors.size} 个板块`;
  const fmtFlags = flags => Array.isArray(flags) ? flags.slice(0, 2).join(" · ") : (flags || "—");
  const mainRows = (focus.main_push || []).filter(s => isAllSectors || selectedSectors.has(s.sector_name || "未分类")).map((s, i) => `
    <tr class="sf-stock-row main" data-stock-id="${escapeHtml(s.security_id || "")}" tabindex="0" role="button" aria-label="查看${escapeHtml(s.security_name)}走势">
      <td class="num rk">${i + 1}</td>
      <td><strong>${escapeHtml(s.security_name)}</strong><small class="sf-code">${escapeHtml(s.security_id || "")}${s.sector_name ? " · " + escapeHtml(s.sector_name) : ""} · 点击看走势</small></td>
      <td><span class="sf-label">${escapeHtml(s.holding_label || "自动主推")}</span></td>
      <td class="num"><strong>${s.model_score != null ? Number(s.model_score).toFixed(1) : s.invest_score != null ? Number(s.invest_score).toFixed(1) : s.total_score != null ? Number(s.total_score).toFixed(1) : "—"}</strong></td>
      <td class="num">${s.close_price != null ? Number(s.close_price).toFixed(2) : "—"}</td>
      <td class="num ${s.change_pct > 0 ? "up" : s.change_pct < 0 ? "down" : ""}">${s.change_pct != null ? (s.change_pct > 0 ? "+" : "") + Number(s.change_pct).toFixed(2) + "%" : "—"}</td>
      <td class="sf-why">${escapeHtml(s.core_logic || s.rationale || "")}</td>
      <td class="sf-why">${escapeHtml(s.downgrade_condition || "")}</td>
      <td class="sf-why">${escapeHtml(fmtFlags(s.data_quality_flags))}</td>
    </tr>`).join("");
  const focusRows = filteredRows.map((s, i) => `
    <tr class="sf-stock-row" data-stock-id="${escapeHtml(s.security_id || "")}" tabindex="0" role="button" aria-label="查看${escapeHtml(s.security_name)}走势">
      <td class="num rk">${i + 1}</td>
      <td><strong>${escapeHtml(s.security_name)}</strong><small class="sf-code">${escapeHtml(s.security_id || "")}${s.sector_name ? " · " + escapeHtml(s.sector_name) : ""} · 点击看走势</small></td>
      <td><span class="sf-state">${escapeHtml(boardLabel(s.board_status || activeTier))}</span></td>
      <td class="num"><strong>${s.timing_score != null ? Number(s.timing_score).toFixed(1) : s.total_score != null ? Number(s.total_score).toFixed(1) : "—"}</strong></td>
      <td class="num">${s.close_price != null ? Number(s.close_price).toFixed(2) : "—"}</td>
      <td class="num ${s.change_pct > 0 ? "up" : s.change_pct < 0 ? "down" : ""}">${s.change_pct != null ? (s.change_pct > 0 ? "+" : "") + Number(s.change_pct).toFixed(2) + "%" : "—"}</td>
      <td class="sf-why">${escapeHtml(s.not_main_reason || s.rationale || "")}</td>
      <td class="sf-why">${escapeHtml(s.decision_basis || "")}</td>
    </tr>`).join("");
  const focusSection = focus.date ? `
    <section class="mb-module stock-focus">
      <div class="sf-head"><h2><span class="mb-num">★</span>今日主推与股票池看板 <small class="sf-date">全自动 Agent 输出 · ${escapeHtml(focus.date)} 批次${focus.carryover ? "（沿用最近交易日）" : ""} · 行情截至 ${escapeHtml(focus.market_date || focus.date)}</small></h2><button class="btn small" id="sf-rules">规则与审计</button></div>
      <div class="auto-agent-strip">
        <strong>AutoInvest Agent</strong>
        <span>${escapeHtml(focus.automation?.rule_version || "自动荐股模型")}</span>
        <span>${escapeHtml(focus.automation?.source_level || "P2_local_rating")}</span>
        <span>${escapeHtml(focus.automation?.audit_note || "每日自动生成，保留审计依据")}</span>
      </div>
      <div class="sf-main-block">
        <div class="sf-subtitle"><strong>每日主推清单</strong><span>≤5只 · 只保留有明确买入动作的标的</span></div>
        <div class="table-scroll">
          <table class="data-table sf-table sf-main-table">
            <thead><tr><th>#</th><th>股票</th><th>正式标签</th><th class="num">投资分</th><th class="num">现价</th><th class="num">涨跌</th><th>核心逻辑</th><th>降级条件</th><th>数据质量</th></tr></thead>
            <tbody>${mainRows || `<tr><td colspan="9">今日无自动主推，等待数据质量或组合层 Gate 满足</td></tr>`}</tbody>
          </table>
        </div>
      </div>
      <div class="tier-pills-row">
        <div class="tier-pills">${pills}</div>
        <div class="sf-filter">
          <button class="btn small" id="sector-filter-btn">板块筛选：${filterLabel} ▾</button>
          ${sectorPanel}
        </div>
        <span class="sf-count">${filteredRows.length}/${tierRows.length} 只</span>
      </div>
      <div class="table-scroll">
      <table class="data-table sf-table">
        <thead><tr><th>#</th><th>股票</th><th>看板状态</th><th class="num">投资分</th><th class="num">现价</th><th class="num">涨跌</th><th>未进主推/看板原因</th><th>决策依据</th></tr></thead>
        <tbody>${focusRows || `<tr><td colspan="8">该层暂无标的</td></tr>`}</tbody>
      </table>
      </div>
      <p class="sf-note">${escapeHtml(focus.universe_note || "")}</p>
    </section>` : "";

  // 板块热力图（日/周/月，红涨绿跌，颜色深浅按板块涨跌幅相对强度）
  const heat = state.sectorHeatmap || { sectors: [] };
  const heatPeriod = state.heatPeriod || "day";
  const heatMaxAbs = Math.max(0.01, ...heat.sectors.map(s => Math.abs(s.avg_change)));
  const heatTiles = heat.sectors.length ? heat.sectors.map(s => {
    const v = s.avg_change;
    const cls = v > 0.05 ? "hm-up" : v < -0.05 ? "hm-down" : "hm-flat";
    const inten = Math.abs(v) / heatMaxAbs;
    const lvl = inten > 0.66 ? 3 : inten > 0.33 ? 2 : 1;
    return `
      <div class="hm-tile ${cls} hm-l${lvl}" data-hm-sector="${escapeHtml(s.sector_name)}" tabindex="0" role="button" aria-label="在今日股票关注中筛选：${escapeHtml(s.sector_name)}">
        <div class="hm-name"><span>${escapeHtml(s.sector_name)}</span><span class="hm-count">${s.stock_count}只</span></div>
        <div class="hm-val">${v > 0 ? "+" : ""}${v.toFixed(2)}%</div>
        <div class="hm-sub">涨 ${s.up_count} / 跌 ${s.down_count}</div>
        <div class="hm-leader">领涨 ${escapeHtml(s.leader_name)} ${s.leader_change > 0 ? "+" : ""}${s.leader_change.toFixed(1)}%</div>
      </div>`;
  }).join("") : `<div class="lib-empty" style="grid-column:1/-1">行情快照积累中——每个交易日同步后自动更新</div>`;
  const heatSection = `
    <section class="mb-module heatmap">
      <div class="sf-head"><h2><span class="mb-num">◆</span>板块热力图 <small class="sf-date">${heat.date ? `${escapeHtml(heat.date)} 收盘` : ""}${heat.anchor_date ? ` · 对比 ${escapeHtml(heat.anchor_date)}` : ""}${heat.sectors.length ? ` · 全池涨 ${heat.total_up} / 跌 ${heat.total_down}` : ""}</small></h2>
        <div class="hm-toggle">${[["day", "日"], ["week", "周"], ["month", "月"]].map(([p, label]) => `<button class="hm-btn ${p === heatPeriod ? "active" : ""}" data-hm-period="${p}">${label}</button>`).join("")}</div>
      </div>
      <div class="hm-grid">${heatTiles}</div>
      <p class="sf-note">等权平均 · 颜色深浅为相对强度 · 点击板块块可在上方“今日股票关注”中只看该板块</p>
    </section>`;

  const content = `
    <article class="morning-brief-v2">
      <div class="mb-head">
        <div><h1>消费行研agent</h1><p>买方视角 · 结论先行 · 全部内容可溯源</p></div>
        <div class="brief-asof">研究截止<br><strong>${escapeHtml(brief.date)} 08:00</strong>${brief.brief_source_date ? `<br><small>文案撰写于 ${escapeHtml(brief.brief_source_date)}</small>` : ""}</div>
      </div>

      ${dateNav}

      ${renderOpsStatusCard()}

      ${focusSection}

      ${heatSection}

      <section class="mb-module ai-enhance-panel">
        <div class="sf-head"><h2><span class="mb-num">AI</span>晨报增强解读 <small class="sf-date">接入大模型后可用 · 不改变底层规则结果</small></h2><button class="btn small" id="brief-ai-enhance">生成增强晨报</button></div>
        <div id="brief-ai-output" class="ai-enhance-box">当前展示为规则基础版晨报。接入模型后，可把今日主推、板块热力、核心观点和风险提示整合成更像晨会口径的文字。</div>
      </section>

      <section class="mb-module">
        <h2><span class="mb-num">1</span>今日核心观点</h2>
        <ul class="tk-list">${takeawayHtml}</ul>
      </section>

      <section class="mb-module">
        <h2><span class="mb-num">2</span>宏观与消费行业大事件</h2>
        <table class="data-table macro">${macroRows}</table>
        <p class="so-what">${escapeHtml(brief.macro_policy.read)}</p>
        ${dailyPicksHtml || `<ul class="doc-list">${macroDocs}</ul><ul class="doc-list">${policyEvents}</ul>`}
      </section>

      <section class="mb-module">
        <h2><span class="mb-num">3</span>风险提示</h2>
        <ul class="tk-list">${risksHtml}</ul>
      </section>

      ${dailyReport ? `<section class="brief-report-link"><button id="open-daily-report"><span>阅读全文</span><strong>今日消费行业突发与重点事件行研报告</strong><small>由消费行业研究 Agent 生成 · 截止 ${escapeHtml(b.cutoff.date)}</small></button></section>` : ""}
      <p class="brief-footnote">${escapeHtml(brief.boundary)}</p>
    </article>`;
  root.innerHTML = shell(content);
  bindShell();
  if (dailyReport) {
    document.getElementById("open-daily-report")?.addEventListener("click", () => { state.activeReport = dailyReport.run_id; state.reportTab = "report"; navigate("reports"); });
  }
  document.getElementById("brief-ai-enhance")?.addEventListener("click", ev => runLlmEnhancement("brief_enhance", {
    brief_date: brief.date,
    source_date: brief.brief_source_date,
    main_push: (focus.main_push || []).slice(0, 5),
    board_counts: focus.board_counts || {},
    heatmap: { date: heat.date, anchor_date: heat.anchor_date, sectors: (heat.sectors || []).slice(0, 12) },
    takeaway: brief.takeaway || [],
    macro_policy: brief.macro_policy || {},
    risks: brief.risks || [],
  }, "#brief-ai-output", ev.currentTarget));
  document.querySelectorAll("[data-tk-index]").forEach(el => {
    const open = () => openTakeawayDrawer(brief.takeaway[Number(el.dataset.tkIndex)], brief.takeaway_events || {});
    el.addEventListener("click", open);
    el.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
  });
  document.querySelectorAll("[data-mp-index]").forEach(el => {
    const open = () => {
      const e = brief.macro_policy.events[Number(el.dataset.mpIndex)];
      openTakeawayDrawer(
        { label: e.title, tone: e.tone, text: e.abstract || e.summary || "", refs: [e.monitor_event_id] },
        { [e.monitor_event_id]: e },
      );
    };
    el.addEventListener("click", open);
    el.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
  });
  document.querySelectorAll(".pill[data-tier]").forEach(btn => btn.addEventListener("click", () => {
    state.focusTier = btn.dataset.tier;
    renderBrief();
  }));
  document.getElementById("sector-filter-btn")?.addEventListener("click", ev => {
    ev.stopPropagation();
    state.sectorPanelOpen = !state.sectorPanelOpen;
    renderBrief();
  });
  document.getElementById("sector-panel")?.addEventListener("click", ev => ev.stopPropagation());
  document.querySelectorAll("[data-sector]").forEach(cb => cb.addEventListener("change", () => {
    const name = cb.dataset.sector;
    const current = state.focusSectors === undefined ? new Set(sectorNames) : new Set(state.focusSectors);
    if (cb.checked) current.add(name);
    else current.delete(name);
    state.focusSectors = current.size >= sectorNames.length ? undefined : current;
    renderBrief();
  }));
  document.getElementById("sector-all")?.addEventListener("click", () => {
    state.focusSectors = undefined;
    renderBrief();
  });
  document.getElementById("sector-clear")?.addEventListener("click", () => {
    state.focusSectors = new Set();
    renderBrief();
  });
  if (state.sectorPanelOpen) {
    setTimeout(() => document.addEventListener("click", () => {
      state.sectorPanelOpen = false;
      renderBrief();
    }, { once: true }), 0);
  }
  document.getElementById("sf-rules")?.addEventListener("click", openRulesDrawer);
  document.querySelectorAll("[data-stock-id]").forEach(row => {
    const open = () => row.dataset.stockId && openStockTrendDrawer(row.dataset.stockId);
    row.addEventListener("click", open);
    row.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
  });
  document.querySelectorAll("[data-hm-period]").forEach(btn => btn.addEventListener("click", async () => {
    state.heatPeriod = btn.dataset.hmPeriod;
    state.sectorHeatmap = await api(`/api/sector-heatmap?period=${state.heatPeriod}`);
    renderBrief();
  }));
  document.querySelectorAll("[data-hm-sector]").forEach(el => {
    const applyFilter = () => {
      state.focusSectors = new Set([el.dataset.hmSector]);
      renderBrief();
      document.querySelector(".stock-focus")?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    el.addEventListener("click", applyFilter);
    el.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); applyFilter(); } });
  });
  const goBriefDate = async d => {
    if (!d) return;
    state.briefDate = d;
    state.morningBrief = await api(`/api/morning-brief?date=${d}`);
    state.stockFocus = await api(`/api/stock-focus?date=${d}`);
    renderBrief();
  };
  document.getElementById("brief-prev")?.addEventListener("click", () => goBriefDate(prevBriefDate));
  document.getElementById("brief-next")?.addEventListener("click", () => goBriefDate(nextBriefDate));
  document.getElementById("brief-date-select")?.addEventListener("change", ev => goBriefDate(ev.target.value));
  document.querySelectorAll("[data-pick-i]").forEach(el => {
    const open = () => openPickDrawer(pickList[Number(el.dataset.pickI)]);
    el.addEventListener("click", open);
    el.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
  });
}

function openPickDrawer(pick) {
  const src = pick.source;
  const sourceHtml = src ? (src.type === "event"
    ? `<div class="ev-card">
         <div class="ev-head"><strong>${escapeHtml(src.title)}</strong><span>重要性 ${src.materiality_score ?? "—"}</span></div>
         <p class="doc-note">${escapeHtml(src.summary || "")}</p>
         <small class="ev-src">${escapeHtml(src.locator || "")}</small>
         <div class="ev-actions">${src.source_url ? `<a class="btn primary small" href="${escapeHtml(src.source_url)}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>` : `<span class="ev-no-link">该来源无公开网页入口，以库内记录为准</span>`}</div>
       </div>`
    : `<div class="ev-card">
         <div class="ev-head"><strong>${escapeHtml(src.title)}</strong><span>${escapeHtml(src.publisher || "")}</span></div>
         <div class="ev-actions">
           <a class="btn primary small" href="/api/documents/${encodeURIComponent(src.document_id)}/content" target="_blank" rel="noopener">查看库内原文 ↗</a>
           ${src.source_url ? `<a class="btn small" href="${escapeHtml(src.source_url)}" target="_blank" rel="noopener noreferrer">官网链接 ↗</a>` : ""}
         </div>
       </div>`
  ) : `<p class="doc-note">本条由研究底座数据直接得出，无单一来源文档。</p>`;
  const drawer = document.createElement("div");
  drawer.className = "detail-backdrop";
  drawer.innerHTML = `<aside class="brief-drawer" role="dialog" aria-modal="true">
    <div class="drawer-head"><div><span>${escapeHtml(pick.label)} · 宏观与消费行业大事件</span><h2>${escapeHtml(pick.title)}</h2></div><button class="drawer-close" aria-label="关闭详情">×</button></div>
    <div class="drawer-body">
      <section class="drawer-section"><h3>详细表述</h3><p style="line-height:1.85">${escapeHtml(pick.text)}</p></section>
      <section class="drawer-section"><h3>信息来源</h3>${sourceHtml}</section>
    </div>
    <div class="drawer-foot"><button class="btn drawer-dismiss">关闭</button></div>
  </aside>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  drawer.querySelector(".drawer-close").addEventListener("click", close);
  drawer.querySelector(".drawer-dismiss").addEventListener("click", close);
  drawer.addEventListener("click", ev => { if (ev.target === drawer) close(); });
  drawer.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
  drawer.querySelector(".drawer-close").focus();
}

function stockTrendChart(points) {
  const valid = (points || []).filter(p => p.close != null && Number(p.close) > 0);
  if (valid.length < 2) {
    return `<div class="trend-empty">历史行情快照不足，暂时无法绘制走势。</div>`;
  }
  const width = 720, height = 330, padX = 44, padY = 26;
  const closes = valid.map(p => Number(p.close));
  const min = Math.min(...closes);
  const max = Math.max(...closes);
  const span = Math.max(0.01, max - min);
  const x = i => padX + i * ((width - padX * 2) / Math.max(1, valid.length - 1));
  const y = v => height - padY - ((v - min) / span) * (height - padY * 2);
  const line = valid.map((p, i) => `${x(i).toFixed(1)},${y(Number(p.close)).toFixed(1)}`).join(" ");
  const first = valid[0], last = valid[valid.length - 1];
  const lastY = y(Number(last.close));
  const tone = Number(last.close) >= Number(first.close) ? "trend-up" : "trend-down";
  const mid = min + span / 2;
  const gridRows = [max, mid, min].map(v => `
    <g>
      <line x1="${padX}" y1="${y(v).toFixed(1)}" x2="${width - padX}" y2="${y(v).toFixed(1)}" class="trend-grid"></line>
      <text x="${padX - 8}" y="${(y(v) + 4).toFixed(1)}" text-anchor="end" class="trend-label">${v.toFixed(2)}</text>
    </g>`).join("");
  return `
    <div class="trend-chart-wrap">
      <svg class="trend-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="股票收盘价走势">
        ${gridRows}
        <polyline points="${line}" class="trend-line ${tone}"></polyline>
        <circle cx="${x(valid.length - 1).toFixed(1)}" cy="${lastY.toFixed(1)}" r="4" class="trend-dot ${tone}"></circle>
      </svg>
      <div class="trend-x"><span>${escapeHtml(first.date)}</span><span>${escapeHtml(last.date)}</span></div>
    </div>`;
}

function stockRatingHistory(history) {
  const items = Array.isArray(history) ? history : [];
  if (!items.length) {
    return `<div class="trend-empty">近一个月暂无评级轨迹。</div>`;
  }
  const labelMap = {
    "核心候选": "可考虑买入",
    "重点跟踪": "等待买点",
    "长期好公司": "长期观察",
    "行业扫描": "暂不推荐"
  };
  const clsMap = {
    "核心候选": "grade-buy",
    "重点跟踪": "grade-watch",
    "长期好公司": "grade-long",
    "行业扫描": "grade-no"
  };
  const statusOf = r => r.board_status || (
    r.tier === "重点关注" ? "核心候选" :
    r.tier === "增持观察" ? "重点跟踪" :
    r.tier === "中性" ? "长期好公司" :
    "行业扫描"
  );
  return `
    <div class="rating-timeline">
      ${items.map(r => {
        const status = statusOf(r);
        const label = labelMap[status] || status || "未评级";
        const cls = clsMap[status] || "grade-no";
        const score = r.invest_score != null ? Number(r.invest_score).toFixed(1) : r.total_score != null ? Number(r.total_score).toFixed(1) : "—";
        const stable = r.stability_score != null ? Number(r.stability_score).toFixed(1) : "—";
        return `<div class="rating-day ${cls}" title="${escapeHtml(r.rating_date || "")}：${escapeHtml(label)}，投资分 ${escapeHtml(score)}">
          <span class="rating-dot"></span>
          <strong>${escapeHtml(label)}</strong>
          <em>${escapeHtml(r.rating_date || "—")}</em>
          <small>投资分 ${escapeHtml(score)} · 稳定分 ${escapeHtml(stable)}</small>
        </div>`;
      }).join("")}
    </div>
    <p class="doc-note">说明：这里展示近一个月已有评级批次中，该股票每天/每个交易批次所处的评价等级，用来观察它是稳定留在同一层，还是频繁升降级。</p>`;
}

function stockEvidenceChain(evidence) {
  const ev = evidence || {};
  const flags = ev.data_quality || [];
  const risks = ev.risk_flags || [];
  const events = ev.recent_events || [];
  return `
    <div class="evidence-chain">
      <div class="evidence-main"><strong>模型判断</strong><p>${escapeHtml(ev.decision || "暂无明确模型说明")}</p></div>
      <div class="evidence-mini-grid">
        <div><strong>数据质量</strong>${flags.map(f => `<span>${escapeHtml(f)}</span>`).join("") || "<span>暂无标记</span>"}</div>
        <div><strong>风险扣分/复核点</strong>${risks.map(f => `<span class="risk">${escapeHtml(f)}</span>`).join("") || "<span>暂无显性风险扣分</span>"}</div>
      </div>
      <h4>近期相关事件</h4>
      ${events.length ? events.map(e => `<div class="evidence-event">
        <strong>${escapeHtml(e.title || "未命名事件")}</strong>
        <p>${escapeHtml(e.summary || "")}</p>
        <small>${escapeHtml((e.available_at || e.event_time || "").slice(0, 16))} · 重要性 ${escapeHtml(e.materiality_score ?? "—")} · ${escapeHtml(e.locator || "")}</small>
      </div>`).join("") : `<p class="doc-note">近期待入库事件中没有匹配到该股票名称；当前主要依据行情、估值和模型状态。</p>`}
    </div>`;
}

async function openStockTrendDrawer(securityId, initialPeriod = "1m") {
  const drawer = document.createElement("div");
  let activePeriod = initialPeriod;
  const PERIODS = [["1w", "1周"], ["1m", "1月"], ["3m", "3月"], ["6m", "半年"], ["1y", "1年"]];
  drawer.className = "detail-backdrop";
  drawer.innerHTML = `<aside class="brief-drawer stock-trend-drawer" role="dialog" aria-modal="true">
    <div class="drawer-head"><div><span>今日股票关注 · 走势</span><h2>加载中…</h2></div><button class="drawer-close" aria-label="关闭详情">×</button></div>
    <div class="drawer-body"><section class="drawer-section"><p>正在读取本地行情快照。</p></section></div>
    <div class="drawer-foot"><button class="btn drawer-dismiss">关闭</button></div>
  </aside>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  drawer.querySelector(".drawer-close").addEventListener("click", close);
  drawer.querySelector(".drawer-dismiss").addEventListener("click", close);
  drawer.addEventListener("click", ev => { if (ev.target === drawer) close(); });
  drawer.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
  drawer.querySelector(".drawer-close").focus();

  const renderTrend = async period => {
    activePeriod = period;
    drawer.querySelector(".drawer-body").innerHTML = `<section class="drawer-section"><p>正在读取${escapeHtml(PERIODS.find(p => p[0] === period)?.[1] || "走势")}行情快照。</p></section>`;
    const data = await api(`/api/stocks/${encodeURIComponent(securityId)}/trend?period=${encodeURIComponent(period)}`);
    const s = data.security || {};
    const evidence = data.evidence_chain || {};
    const sum = data.summary || {};
    const points = data.points || [];
    const ratingHistory = data.rating_history || [];
    const validPoints = points.filter(p => p.close != null && Number(p.close) > 0);
    const latest = validPoints[validPoints.length - 1] || {};
    const ret = sum.period_return_pct;
    drawer.querySelector(".drawer-head div").innerHTML = `<span>${escapeHtml(s.security_id || securityId)} · ${escapeHtml(s.sector_name || "未分类")}</span><h2>${escapeHtml(s.security_name || securityId)}</h2>`;
    drawer.querySelector(".drawer-body").innerHTML = `
      <section class="drawer-section trend-hero">
        <div class="trend-price ${ret > 0 ? "up" : ret < 0 ? "down" : ""}">
          <strong>${latest.close != null ? Number(latest.close).toFixed(2) : "—"}</strong>
          <span>${ret != null ? (ret > 0 ? "+" : "") + ret.toFixed(2) + "%" : "—"} · ${escapeHtml(sum.start_date || "—")} 至 ${escapeHtml(sum.end_date || "—")}</span>
        </div>
        <div class="trend-tabs">
          ${PERIODS.map(([key, label]) => `<button class="trend-tab ${key === activePeriod ? "active" : ""}" data-trend-period="${key}">${label}</button>`).join("")}
        </div>
      </section>
      <section class="drawer-section trend-panel">
        ${stockTrendChart(points)}
      </section>
      <section class="drawer-section trend-summary">
        <div><strong>${sum.high != null ? Number(sum.high).toFixed(2) : "—"}</strong><span>区间高点</span></div>
        <div><strong>${sum.low != null ? Number(sum.low).toFixed(2) : "—"}</strong><span>区间低点</span></div>
        <div><strong>${sum.point_count || 0}</strong><span>样本交易日</span></div>
        <div><strong>${escapeHtml(data.period || activePeriod)}</strong><span>当前周期</span></div>
      </section>
      <section class="drawer-section">
        <h3>当前评级</h3>
        <dl class="detail-grid">
          <div><dt>评级批次</dt><dd>${escapeHtml(s.rating_date || "—")}</dd></div>
          <div><dt>评价等级</dt><dd>${escapeHtml(({ "核心候选": "可考虑买入", "重点跟踪": "等待买点", "长期好公司": "长期观察", "行业扫描": "暂不推荐" })[s.board_status] || s.board_status || s.tier || "—")}</dd></div>
          <div><dt>投资分</dt><dd>${s.invest_score != null ? Number(s.invest_score).toFixed(1) : s.total_score != null ? Number(s.total_score).toFixed(1) : "—"}</dd></div>
          <div><dt>PE-TTM</dt><dd>${s.pe_ttm != null && s.pe_ttm > 0 ? Number(s.pe_ttm).toFixed(1) : "—"}</dd></div>
        </dl>
        <p style="margin-top:10px">${escapeHtml(s.state_reason || s.rationale || "")}</p>
      </section>
      <section class="drawer-section rating-history-section">
        <h3>近一个月评价等级轨迹</h3>
        ${stockRatingHistory(ratingHistory)}
      </section>
      <section class="drawer-section">
        <h3>推荐证据链与风险复核</h3>
        ${stockEvidenceChain(evidence)}
      </section>
      <section class="drawer-section ai-enhance-panel">
        <div class="sf-head"><h3>AI个股解释</h3><button class="btn small" id="stock-ai-explain">生成AI解释</button></div>
        <div id="stock-ai-output" class="ai-enhance-box">基础版已展示走势图、评级轨迹和证据链；接入模型后，可生成“为什么处于当前等级、主要风险、升级/降级条件”的自然语言解释。</div>
      </section>
      <section class="drawer-section"><h3>数据边界</h3><p>${escapeHtml(data.note || "")}</p></section>`;
    drawer.querySelectorAll("[data-trend-period]").forEach(btn => btn.addEventListener("click", () => {
      if (btn.dataset.trendPeriod !== activePeriod) renderTrend(btn.dataset.trendPeriod).catch(showError);
    }));
    drawer.querySelector("#stock-ai-explain")?.addEventListener("click", ev => runLlmEnhancement("stock_explain", {
      security: s,
      period: data.period || activePeriod,
      trend_summary: sum,
      latest_quote: latest,
      rating_history: ratingHistory,
      evidence_chain: evidence,
      note: data.note,
    }, drawer.querySelector("#stock-ai-output"), ev.currentTarget));
  };
  const showError = error => {
    drawer.querySelector(".drawer-head h2").textContent = "走势读取失败";
    drawer.querySelector(".drawer-body").innerHTML = `<section class="drawer-section"><p>${escapeHtml(error.message)}</p></section>`;
  };
  renderTrend(activePeriod).catch(showError);
}

function openRulesDrawer() {
  const focus = state.stockFocus || {};
  const calibration = state.selfCalibration || {};
  const activeRule = calibration.active_rule || {};
  const shadowRule = calibration.shadow_rule || {};
  const checks = calibration.checks || [];
  const fixes = calibration.auto_fixes || [];
  const events = calibration.events || [];
  const outcomes = calibration.outcomes || [];
  const recommendationGroups = new Set(["main_push", "buy_candidate"]);
  const validationGroups = new Set(["watch_signal", "long_quality", "sector_scan"]);
  const checkRows = checks.map(c => `<tr><td>${c.ok ? "✓" : "!"}</td><td>${escapeHtml(c.label || c.key || "")}</td><td>${escapeHtml(c.detail || (c.ok ? "通过" : "需关注"))}</td></tr>`).join("");
  const fixRows = fixes.map(f => `<tr><td>${escapeHtml(f.type || "auto_fix")}</td><td class="num">${escapeHtml(f.rows ?? "—")}</td><td>${escapeHtml(f.sample ? JSON.stringify(f.sample).slice(0, 80) : "已自动处理")}</td></tr>`).join("");
  const eventRows = events.map(e => `<tr><td>${escapeHtml(String(e.created_at || "").slice(0, 16))}</td><td>${escapeHtml(e.event_type || "")}</td><td>${escapeHtml(e.reason || "")}</td></tr>`).join("");
  const outcomeRow = o => `<tr><td>${escapeHtml(o.group_label || o.snapshot_group || "")}</td><td>${escapeHtml(o.horizon || "")}</td><td class="num">${escapeHtml(o.samples ?? 0)}</td><td class="num ${o.avg_return > 0 ? "up" : o.avg_return < 0 ? "down" : ""}">${o.avg_return != null ? (o.avg_return > 0 ? "+" : "") + Number(o.avg_return).toFixed(2) + "%" : "—"}</td><td class="num">${o.win_rate != null ? Number(o.win_rate).toFixed(1) + "%" : "—"}</td></tr>`;
  const recommendationOutcomeRows = outcomes.filter(o => recommendationGroups.has(o.snapshot_group)).map(outcomeRow).join("");
  const validationOutcomeRows = outcomes.filter(o => validationGroups.has(o.snapshot_group)).map(outcomeRow).join("");
  const drawer = document.createElement("div");
  drawer.className = "detail-backdrop";
  drawer.innerHTML = `<aside class="brief-drawer" role="dialog" aria-modal="true">
    <div class="drawer-head"><div><span>今日主推与股票池看板 · AutoInvest Agent</span><h2>规则与审计</h2></div><button class="drawer-close" aria-label="关闭详情">×</button></div>
    <div class="drawer-body">
      <section class="drawer-section"><h3>自动输出对象</h3><p>Agent 每日自动从全消费 A 股研究池生成两份结果：第一屏为“每日主推清单”（≤5 只，只放明确建仓/小仓位观察），第二屏为“消费股票池看板”（可考虑买入 25 只、等待买点、长期观察、暂不推荐）。</p></section>
      <section class="drawer-section"><h3>当前落地口径</h3>
        <ul class="doc-list">
          <li><strong>核心目标</strong>：围绕中期 / 中长期持有价值评价，不再使用旧的“当日动量40%/估值30%/事件30%”作为推荐模型。</li>
          <li><strong>主推生成</strong>：Agent 从“可考虑买入”中自动挑选 ≤5 只明确动作标的，并写入正式标签、建议动作、核心逻辑、降级条件；暂缓买入不进入主推。</li>
          <li><strong>看板状态</strong>：【核心·时机满足】/【跟踪·等信号】/【长期·好公司】/【暂不推荐·继续扫描】代表投资研究状态，不是每日热度排名。</li>
          <li><strong>审计责任</strong>：每条输出必须带决策依据、数据质量、未进主推原因或降级条件。</li>
        </ul>
      </section>
      <section class="drawer-section"><h3>新评判标准</h3>
        <table class="data-table">
          <tr><td><strong>投资价值分</strong></td><td class="num"><strong>核心</strong></td><td>以估值质量、跨日稳定性、催化质量和少量时点参考综合形成，代表中期/中长期是否值得纳入推荐体系。</td></tr>
          <tr><td><strong>稳定分</strong></td><td class="num"><strong>跨日</strong></td><td>参考最近评级日的持续表现和历史池位，防止长期好公司因为单日行情波动被频繁踢出。</td></tr>
          <tr><td><strong>时点参考</strong></td><td class="num"><strong>辅助</strong></td><td>涨跌幅、量比、换手只用于判断买入时点和短期拥挤度，不再主导股票是否值得推荐。</td></tr>
          <tr><td><strong>风险扣分</strong></td><td class="num"><strong>硬约束</strong></td><td>ST/退市风险、重大负面事件、治理风险会降低层级或进入暂不推荐/行业扫描，不进入主推。</td></tr>
        </table>
      </section>
      <section class="drawer-section"><h3>看板映射</h3>
        <ul class="doc-list">
          <li><strong>核心候选</strong>：投资分和稳定分较高，已满足中期/中长期推荐基础，但还需组合层 Gate 后才进入主推。</li>
          <li><strong>重点跟踪</strong>：投资价值较高，但催化、估值、数据质量或组合约束仍差一个信号。</li>
          <li><strong>长期好公司</strong>：长期逻辑保留，但当前不是买入时间点；该池具备跨日状态记忆。</li>
          <li><strong>暂不推荐/行业扫描</strong>：当前不推荐，只保留行业覆盖、风险观察和后续变化监控；若估值、基本面或催化改善，可再升级。</li>
        </ul>
      </section>
      <section class="drawer-section"><h3>自动降级与退出</h3>
        <ul class="doc-list">
          <li><strong>主推降级</strong>：核心指标或事件催化连续 2 周转弱，自动降为重点跟踪或长期好公司。</li>
          <li><strong>长期池退出</strong>：投资分持续恶化、进入回避/风险项、治理风险触发时，从长期好公司降至暂不推荐/行业扫描。</li>
          <li><strong>数据降级</strong>：关键字段缺失或来源为 P2 时，降低建议动作强度，并在数据质量中标注。</li>
        </ul>
      </section>
      <section class="drawer-section"><h3>数据来源与口径</h3><p>行情与估值：聚源 A 股实时行情（收盘价、涨跌幅、PE-TTM、换手率、量比）；事件：本机研究底座事件库（新闻、公告、研报线索）。评级批次为 ${escapeHtml(focus.date || "当日")}，所用行情实际交易日为 ${escapeHtml(focus.market_date || focus.date || "最近交易日")}。</p></section>
      <section class="drawer-section"><h3>全自动边界</h3><p>${escapeHtml(focus.automation?.audit_note || "Agent 自动生成输出并保留审计依据。当前版本为 P2 本地评分口径；接入 P0/P1 后自动提高数据质量等级。")}</p></section>
      <section class="drawer-section"><h3>Agent 自校准</h3>
        <p>本系统不需要人工批准：Agent 每日自动自检、自动修复低风险问题、保存推荐快照、生成后验结果，并以影子规则方式自动灰度；如质量恶化，自动回滚。</p>
        <div class="audit-kpis">
          <div><span>自检状态</span><strong>${escapeHtml(calibration.status || "unknown")}</strong><small>${escapeHtml(calibration.created_at || "尚未运行")}</small></div>
          <div><span>正式规则</span><strong>${escapeHtml(activeRule.rule_version || "—")}</strong><small>${escapeHtml(activeRule.status || "")}</small></div>
          <div><span>影子规则</span><strong>${escapeHtml(shadowRule.rule_version || "—")}</strong><small>${escapeHtml(shadowRule.status || "等待创建")}</small></div>
          <div><span>推荐快照</span><strong>${Number(calibration.snapshots?.rows || 0).toLocaleString("en-US")}</strong><small>${escapeHtml(calibration.snapshots?.latest_date || "")}</small></div>
        </div>
      </section>
      <section class="drawer-section"><h3>今日自检与自动修复</h3>
        <table class="data-table"><thead><tr><th>状态</th><th>检查项</th><th>说明</th></tr></thead><tbody>${checkRows || `<tr><td colspan="3">暂无自检记录</td></tr>`}</tbody></table>
        <h4>自动修复记录</h4>
        <table class="data-table"><thead><tr><th>修复类型</th><th class="num">数量</th><th>说明</th></tr></thead><tbody>${fixRows || `<tr><td colspan="3">今日无需自动修复</td></tr>`}</tbody></table>
      </section>
      <section class="drawer-section"><h3>规则自进化与后验表现</h3>
        <p>后验只把“每日主推清单”和“可以考虑买入”作为推荐表现；“等待买点 / 长期观察 / 暂不推荐”只用于校验分层是否合理。所有组内股票暂按等权统计，不代表真实仓位。</p>
        <h4>推荐后验表现</h4>
        <table class="data-table audit-outcome-table"><thead><tr><th>样本组</th><th>周期</th><th class="num">样本</th><th class="num">平均收益</th><th class="num">胜率</th></tr></thead><tbody>${recommendationOutcomeRows || `<tr><td colspan="5">推荐后验样本仍在积累</td></tr>`}</tbody></table>
        <h4>分层校验表现</h4>
        <table class="data-table audit-outcome-table"><thead><tr><th>样本组</th><th>周期</th><th class="num">样本</th><th class="num">平均收益</th><th class="num">胜率</th></tr></thead><tbody>${validationOutcomeRows || `<tr><td colspan="5">分层校验样本仍在积累</td></tr>`}</tbody></table>
        <h4>最近自动规则事件</h4>
        <table class="data-table"><thead><tr><th>时间</th><th>事件</th><th>原因</th></tr></thead><tbody>${eventRows || `<tr><td colspan="3">暂无规则事件</td></tr>`}</tbody></table>
      </section>
      <section class="drawer-section ai-enhance-panel">
        <div class="sf-head"><h3>AI复盘解释</h3><button class="btn small" id="audit-ai-review">生成AI复盘</button></div>
        <div id="audit-ai-output" class="ai-enhance-box">基础版已展示样本、平均收益、胜率和规则事件；接入模型后，可解释“主推/候选/观察池”的表现差异，并给出后续监控建议。</div>
      </section>
    </div>
    <div class="drawer-foot"><button class="btn drawer-dismiss">关闭</button></div>
  </aside>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  drawer.querySelector(".drawer-close").addEventListener("click", close);
  drawer.querySelector(".drawer-dismiss").addEventListener("click", close);
  drawer.addEventListener("click", ev => { if (ev.target === drawer) close(); });
  drawer.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
  drawer.querySelector("#audit-ai-review")?.addEventListener("click", ev => runLlmEnhancement("audit_review", {
    focus_date: focus.date,
    market_date: focus.market_date,
    board_counts: focus.board_counts || {},
    main_push: (focus.main_push || []).slice(0, 5),
    calibration: {
      status: calibration.status,
      checks,
      auto_fixes: fixes,
      events,
      outcomes,
      active_rule: activeRule,
      shadow_rule: shadowRule,
      guardrails: calibration.guardrails || [],
    },
  }, drawer.querySelector("#audit-ai-output"), ev.currentTarget));
  drawer.querySelector(".drawer-close").focus();
}

function openTakeawayDrawer(takeaway, eventsMap) {
  const toneOf = t => TONE_META[t] || TONE_META.neutral;
  const refs = (takeaway.refs || []).map(id => eventsMap[id]).filter(Boolean);
  const drawer = document.createElement("div");
  drawer.className = "detail-backdrop";
  drawer.innerHTML = `<aside class="brief-drawer" role="dialog" aria-modal="true">
    <div class="drawer-head"><div><span class="${toneOf(takeaway.tone).cls}" style="font-weight:700">${toneOf(takeaway.tone).icon} ${toneOf(takeaway.tone).label} · 今日核心观点</span><h2>${escapeHtml(takeaway.label)}</h2></div><button class="drawer-close" aria-label="关闭详情">×</button></div>
    <div class="drawer-body">
      <section class="drawer-section"><h3>观点摘要</h3><p style="line-height:1.85">${escapeHtml(takeaway.text)}</p></section>
      <section class="drawer-section"><h3>完整内容与信息来源（${refs.length} 条）</h3>
        ${refs.map(e => `<div class="ev-card">
          <div class="ev-head"><strong>${escapeHtml(e.title)}</strong><span>${toneOf(e.tone).icon} ${toneOf(e.tone).label} · ${e.materiality_score ?? "—"}</span></div>
          ${(e.data_rows || []).length ? `<table class="data-table">${e.data_rows.map(r => `<tr><td>${escapeHtml(r[0])}</td><td class="num"><strong>${escapeHtml(r[1])}</strong></td><td class="num">${escapeHtml(r[2])}</td><td>${escapeHtml(r[3])}</td></tr>`).join("")}</table>` : ""}
          ${e.summary ? `<p class="doc-note">${escapeHtml(e.summary)}</p>` : ""}
          ${e.so_what ? `<p class="so-what">${escapeHtml(e.so_what)}</p>` : ""}
          <small class="ev-src">${escapeHtml(e.locator || "")}${e.event_time ? " · 事件时间 " + formatDate(e.event_time) : ""}</small>
          <div class="ev-actions">${e.source_url ? `<a class="btn primary small" href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener noreferrer">查看报告原文 ↗</a>` : `<span class="ev-no-link">该条为授权数据/研报内容，无公开网页原文，以上库内记录为准</span>`}</div>
        </div>`).join("") || `<p class="doc-note">本条观点为综合判断，相关事件见晨报其他模块。</p>`}
      </section>
    </div>
    <div class="drawer-foot"><button class="btn drawer-dismiss">关闭</button></div>
  </aside>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  drawer.querySelector(".drawer-close").addEventListener("click", close);
  drawer.querySelector(".drawer-dismiss").addEventListener("click", close);
  drawer.addEventListener("click", ev => { if (ev.target === drawer) close(); });
  drawer.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
  drawer.querySelector(".drawer-close").focus();
}

function detailValue(value) {
  if (value === null || value === undefined || value === "") return "暂无";
  return escapeHtml(value);
}

function briefContentSections(item) {
  const content = state.briefContent || {};
  const groupKey = item.groupKey;
  const sections = [];
  const EVENT_GROUPS = { macro: ["macro_release"], policy: ["policy_release"], industry: ["industry_data_release"], news: ["news_lead"], announcements: ["announcement"], financials: ["earnings_release"], market: ["market_move"], enterprise: ["enterprise_risk"] };
  const EVENT_LABELS = { macro_release: "宏观发布", policy_release: "政策发布", industry_data_release: "行业数据", news_lead: "资讯线索", announcement: "公司公告", earnings_release: "财报", market_move: "行情异动", enterprise_risk: "企业风险" };
  const groupEvents = (content.events || []).filter(e => (EVENT_GROUPS[groupKey] || []).includes(e.event_type));
  if (groupEvents.length) {
    sections.push(`<section class="drawer-section"><h3>今日事件（已入研究库）</h3><div class="release-list">${groupEvents.map(e => `
      <div class="release-card">
        <div class="release-head"><strong>${escapeHtml(e.title)}</strong><span class="event-type-badge">${EVENT_LABELS[e.event_type] || escapeHtml(e.event_type)}</span></div>
        ${e.summary ? `<p class="release-figure">${escapeHtml(e.summary)}</p>` : ""}
        <div class="release-meta"><span>事件时间 ${formatDate(e.event_time)}</span><span>重要性 ${e.materiality_score ?? "—"}</span>${e.sector_name ? `<span>${escapeHtml(e.sector_name)}</span>` : ""}${e.locator ? `<span>${escapeHtml(e.locator)}</span>` : ""}${e.source_url ? `<a class="ext" href="${escapeHtml(e.source_url)}" target="_blank" rel="noopener noreferrer" title="对方网站可能拦截部分访问">打开原文 ↗</a>` : ""}</div>
      </div>`).join("")}</div></section>`);
  }
  if (groupKey === "macro" && (content.official_releases || []).length) {
    sections.push(`<section class="drawer-section"><h3>已入研究底座的官方发布</h3><div class="release-list">${content.official_releases.map(r => `
      <div class="release-card">
        <div class="release-head"><strong>${escapeHtml(r.title)}</strong><span>${escapeHtml(r.publisher || "")} · 发布于 ${formatDate(r.published_at)}</span></div>
        ${r.key_figure ? `<p class="release-figure">${escapeHtml(r.key_figure)}</p>` : ""}
        <div class="release-meta"><span>数据期 ${escapeHtml(r.as_of_date || "—")}</span><span>证据等级 ${escapeHtml(r.evidence_tier || "—")}</span><span>定位 ${escapeHtml(r.locator || "—")}</span><a href="/api/documents/${encodeURIComponent(r.document_id)}/content" target="_blank" rel="noopener">查看库内原文 ↗</a>${r.source_url ? `<a class="ext" href="${escapeHtml(r.source_url)}" target="_blank" rel="noopener noreferrer" title="对方网站可能拦截部分访问">官网链接 ↗</a>` : ""}</div>
      </div>`).join("")}</div></section>`);
  }
  if (groupKey === "news" && !groupEvents.length) {
    const leads = content.news_leads || [];
    if (leads.length) {
      sections.push(`<section class="drawer-section"><h3>原始线索（授权隔离 · 未核验，不得作为结论依据）</h3><div class="release-list">${leads.map(l => `
        <div class="release-card lead">
          <div class="release-head"><strong>${escapeHtml(l.title)}</strong><span class="lead-badge">授权隔离线索</span></div>
          <p class="release-figure">${escapeHtml(l.summary || "")}</p>
          <div class="release-meta"><span>事件时间 ${formatDate(l.event_time)}</span><span>重要性评分 ${l.materiality_score ?? "—"}</span><span>未进正式研究库 · 须回到原始发布核验</span></div>
      </div>`).join("")}</div></section>`);
    }
  }
  if (groupKey === "announcements" || groupKey === "financials") {
    const docs = content.library_documents || [];
    const readyReports = (state.reports || []).filter(r => r.status === "completed" && r.publication_status === "internal_research_ready");
    const blocks = [];
    if (docs.length) {
      blocks.push(`<div class="release-list">${docs.map(d => `
        <div class="release-card">
          <div class="release-head"><strong>${escapeHtml(d.title)}</strong><span>${escapeHtml(d.publisher || "")} · 披露于 ${formatDate(d.published_at)}</span></div>
          <div class="release-meta"><span>数据期 ${escapeHtml(d.as_of_date || "—")}</span><span>证据等级 ${escapeHtml(d.evidence_tier || "—")}</span><span>已入正式研究事实层，可被报告直接引用</span></div>
        </div>`).join("")}</div>`);
    }
    if (readyReports.length) {
      blocks.push(`<div class="release-list">${readyReports.map(r => `
        <div class="release-card report">
          <div class="release-head"><strong>${escapeHtml(r.title)}</strong><span>研究成果 · ${formatDate(r.completed_at || r.started_at)}</span></div>
          <div class="release-meta"><span>系统质量门已通过 · 内部研究可用</span><button class="btn small" data-open-report="${escapeHtml(r.run_id)}">打开报告</button></div>
        </div>`).join("")}</div>`);
    }
    if (blocks.length) {
      sections.push(`<section class="drawer-section"><h3>库内文档与研究成果</h3>${blocks.join("")}</section>`);
    }
  }
  return sections.join("");
}

function sourceRow(alert) {
  const source = alert.source || {};
  const detail = alert.detail || {};
  const sourceName = source.name || detail.name || detail.source_id || "未标识来源";
  const licenseGated = source.license_status === "contract_terms_pending_verification" || source.status === "live_connected_license_gate";
  const stateText = licenseGated ? "已采集，授权隔离" : detail.freshness_status === "stale" ? "数据已过期" : detail.cursor_status === "not_started" ? "尚未开始同步" : detail.cursor_status === "success" ? "最近同步成功" : "已授权 · 已连接";
  const entry = source.reachability_status === "network_blocked"
    ? `<span class="source-unavailable blocked">当前网络不可达：对方防护拦截本机访问</span>${source.alternate_entry ? `<a class="source-link" href="${escapeHtml(source.alternate_entry)}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(source.reachability_note || "")}">打开官方关联渠道 ↗</a>` : ""}`
    : source.source_url
    ? `<a class="source-link" href="${escapeHtml(source.source_url)}" target="_blank" rel="noopener noreferrer">打开来源主页 ↗</a>`
    : source.endpoint_type === "mcp" ? `<span class="source-unavailable">本机 MCP 数据源，无公开网页入口</span>` : `<span class="source-unavailable">暂无公开来源地址</span>`;
  return `<div class="source-detail-row">
    <div><strong>${escapeHtml(sourceName)}</strong><span>${escapeHtml(source.operator || source.source_family || "数据源")}</span></div>
    <dl><div><dt>当前状态</dt><dd>${escapeHtml(stateText)}</dd></div><div><dt>最后成功</dt><dd>${detailValue(detail.last_success_at ? formatDate(detail.last_success_at) : null)}</dd></div><div><dt>数据水位</dt><dd>${detailValue(detail.watermark_available_at ? formatDate(detail.watermark_available_at) : null)}</dd></div><div><dt>更新要求</dt><dd>${detailValue(detail.expected_max_lag_hours != null ? `${detail.expected_max_lag_hours} 小时内` : source.update_frequency)}</dd></div></dl>
    ${entry}
  </div>`;
}

function openBriefDetail(item) {
  const isEvent = item.rule_code === "CR.MON.EVENT.MATERIAL";
  const alerts = item.sourceAlerts || [];
  const event = item.event || {};
  const eventLink = event.source_url ? `<a class="btn primary" href="${escapeHtml(event.source_url)}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a><span class="ext-caveat">若官网拦截访问，内容以库内记录为准</span>` : "";
  const report = item.sector_name ? state.reports.find(r => r.status === "completed" && r.publication_status === "internal_research_ready" && String(r.title).includes(item.sector_name)) : null;
  const relatedReport = report ? `<button class="btn" id="detail-related-report">查看相关报告</button>` : "";
  const drawer = document.createElement("div");
  drawer.className = "detail-backdrop";
  drawer.innerHTML = `<aside class="brief-drawer" role="dialog" aria-modal="true" aria-labelledby="brief-detail-title">
    <div class="drawer-head"><div><span>${isEvent ? "重要事件" : item.healthy ? "已同步" : "数据门禁"} · ${escapeHtml(item.area)}</span><h2 id="brief-detail-title">${escapeHtml(item.displayTitle)}</h2></div><button class="drawer-close" aria-label="关闭详情">×</button></div>
    <div class="drawer-body">
      <section class="drawer-section"><h3>为什么重要</h3><p>${escapeHtml(item.implication)}</p></section>
      ${briefContentSections(item)}
      ${isEvent ? `<section class="drawer-section"><h3>事件摘要</h3><p>${escapeHtml(event.summary || item.detail?.summary || "当前事件记录没有更多摘要。")}</p><dl class="detail-grid"><div><dt>重要性评分</dt><dd>${detailValue(event.materiality_score)}</dd></div><div><dt>来源定位</dt><dd>${detailValue(event.locator)}</dd></div><div><dt>首次发现</dt><dd>${formatDate(item.first_detected_at)}</dd></div><div><dt>最近更新</dt><dd>${formatDate(item.last_detected_at)}</dd></div></dl></section>` : alerts.length ? `<section class="drawer-section"><h3>具体状态</h3><p>这不是一条新闻，而是影响研究可靠性的数据状态提醒。涉及 ${alerts.length} 个数据源；在生产同步、历史回填或授权门槛处理完成前，Agent 不会把缺失信息包装成行业结论。</p></section>` : `<section class="drawer-section"><h3>具体状态</h3><p>该类别数据流当前均在新鲜度要求内，没有未处理的数据门禁；上方为已进入研究库的内容。</p></section>`}
      ${alerts.length ? `<section class="drawer-section"><h3>${isEvent ? "来源" : "涉及的数据源"}</h3><div class="source-detail-list">${alerts.map(sourceRow).join("")}</div></section>` : ""}
      ${item.rule_code ? `<section class="drawer-section"><h3>记录信息</h3><dl class="detail-grid"><div><dt>监控规则</dt><dd>${escapeHtml(item.rule_code)}</dd></div><div><dt>状态</dt><dd>${escapeHtml(item.state_label || item.state)}</dd></div><div><dt>首次发现</dt><dd>${formatDate(item.first_detected_at)}</dd></div><div><dt>最近更新</dt><dd>${formatDate(item.last_detected_at)}</dd></div></dl></section>` : ""}
    </div>
    <div class="drawer-foot">${eventLink}${relatedReport}<button class="btn drawer-dismiss">关闭</button></div>
  </aside>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  drawer.querySelector(".drawer-close").addEventListener("click", close);
  drawer.querySelector(".drawer-dismiss").addEventListener("click", close);
  drawer.addEventListener("click", event => { if (event.target === drawer) close(); });
  drawer.addEventListener("keydown", event => { if (event.key === "Escape") close(); });
  drawer.querySelector("#detail-related-report")?.addEventListener("click", () => { state.activeReport = report.run_id; close(); navigate("reports"); });
  drawer.querySelectorAll("[data-open-report]").forEach(btn => btn.addEventListener("click", () => { state.activeReport = btn.dataset.openReport; close(); navigate("reports"); }));
  drawer.querySelector(".drawer-close").focus();
}

function renderSectors() {
  const content = `
    ${hero("Consumer sector map", "一张地图，覆盖完整消费行业", "11个研究领域使用同一套研究骨架，同时保留各自的周期驱动、产业链和专属指标。")}
    <div class="notice warning"><strong>覆盖口径提示</strong>“研究包已就绪”表示研究结构与任务模板可用，不代表行情、财务和宏观等全部数据已经完成回填。</div>
    <div class="sector-grid">
      ${state.sectors.map(s => `<article class="sector-card"><div class="sector-code">${escapeHtml(s.sector_code)}</div><h3>${escapeHtml(s.sector_name)}</h3><div class="sector-thesis">${escapeHtml(s.research_thesis)}</div><div class="sector-tags">${(s.cycle_drivers || []).slice(0, 4).map(t => `<span class="tag">${escapeHtml(t)}</span>`).join("")}</div><div class="sector-stats"><div class="sector-stat"><strong>${s.a_share_count || 0}</strong><span>A股公司</span></div><div class="sector-stat"><strong>${s.metric_count || 0}</strong><span>指标定义</span></div><div class="sector-stat"><strong>${s.open_alerts || 0}</strong><span>待处理项</span></div></div></article>`).join("")}
    </div>`;
  root.innerHTML = shell(content);
  bindShell();
}

async function loadTasks() {
  const p = new URLSearchParams();
  if (state.taskFilters.q) p.set("q", state.taskFilters.q);
  if (state.taskFilters.sector) p.set("sector", state.taskFilters.sector);
  if (state.taskFilters.category) p.set("category", state.taskFilters.category);
  if (state.taskFilters.favorites) p.set("favorites", "1");
  const result = await api(`/api/tasks?${p}`);
  state.tasks = result.results;
}

async function renderTasks() {
  root.innerHTML = shell(`<div class="loading">正在读取研究任务库…</div>`); bindShell();
  try { await loadTasks(); } catch (error) { return renderError(error); }
  const f = state.taskFilters;
  const content = `
    ${hero("Research task library", "把研究问题，交给标准工作流", "从110个研究产品中选择任务。每次提交都明确研究问题、截止日期、市场和具名提交人。")}
    <div class="filters">
      <input class="input search" id="task-q" placeholder="搜索任务名称或研究问题" value="${escapeHtml(f.q)}" />
      <select class="select" id="task-sector"><option value="">全部领域</option>${state.sectors.map(s => `<option value="${escapeHtml(s.sector_code)}" ${f.sector === s.sector_code ? "selected" : ""}>${escapeHtml(s.sector_name)}</option>`).join("")}</select>
      <select class="select" id="task-category"><option value="">全部类型</option>${Object.entries(categoryLabels).map(([k,v]) => `<option value="${k}" ${f.category === k ? "selected" : ""}>${v}</option>`).join("")}</select>
      <label><input type="checkbox" id="task-favorites" ${f.favorites ? "checked" : ""}/> 只看收藏</label>
      <span class="filter-spacer"></span><span class="section-meta">${state.tasks.length} 个结果</span>
    </div>
    ${state.tasks.length ? `<div class="task-grid">${state.tasks.map(t => `<article class="task-card"><div class="task-top"><div><div class="task-category">${escapeHtml(t.category_label || categoryLabels[t.category])}</div><h3>${escapeHtml(t.title)}</h3></div><button class="favorite ${t.is_favorite ? "active" : ""}" aria-label="${t.is_favorite ? "取消收藏" : "收藏"}" data-favorite="${escapeHtml(t.product_id)}" data-value="${t.is_favorite ? "0" : "1"}">☆</button></div><div class="task-meta">预计 ${t.expected_minutes} 分钟 · ${t.data_readiness === "ready_with_data_gaps" ? "存在数据缺口" : "数据就绪"}</div><div class="sector-tags">${(t.tags || []).slice(0, 4).map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div><div class="task-foot">${status(t.data_readiness === "ready_with_data_gaps" ? "数据缺口" : "fresh", t.data_readiness === "ready_with_data_gaps" ? "watch" : "good")}<button class="btn primary small" data-submit-task="${escapeHtml(t.product_id)}">发起任务</button></div></article>`).join("")}</div>` : `<div class="empty"><strong>没有匹配的研究任务</strong>调整搜索词或筛选条件后重试。</div>`}`;
  root.innerHTML = shell(content); bindShell();
  const refresh = () => {
    state.taskFilters = { q: document.getElementById("task-q").value.trim(), sector: document.getElementById("task-sector").value, category: document.getElementById("task-category").value, favorites: document.getElementById("task-favorites").checked };
    renderTasks();
  };
  document.getElementById("task-q")?.addEventListener("keydown", e => { if (e.key === "Enter") refresh(); });
  ["task-sector", "task-category", "task-favorites"].forEach(id => document.getElementById(id)?.addEventListener("change", refresh));
  document.querySelectorAll("[data-favorite]").forEach(btn => btn.addEventListener("click", () => setFavorite(btn.dataset.favorite, btn.dataset.value === "1")));
  document.querySelectorAll("[data-submit-task]").forEach(btn => btn.addEventListener("click", () => openTaskModal(btn.dataset.submitTask)));
}

async function setFavorite(productId, favorite) {
  try {
    await api("/api/favorites", { method: "POST", body: JSON.stringify({ product_id: productId, favorite }) });
    toast(favorite ? "已收藏研究任务" : "已取消收藏");
    renderTasks();
  } catch (error) { toast(error.message, true); }
}

async function openTaskModal(productId) {
  try {
    const { product } = await api(`/api/tasks/${encodeURIComponent(productId)}`);
    const needsEntities = product.entity_requirement && product.entity_requirement !== "none";
    const modal = document.createElement("div");
    modal.className = "modal-backdrop";
    modal.innerHTML = `<div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head"><div><h2 id="modal-title">${escapeHtml(product.title)}</h2><div class="modal-subtitle">预计 ${product.expected_minutes} 分钟 · ${escapeHtml(categoryLabels[product.category] || product.category)}</div></div><button class="close-btn" aria-label="关闭">×</button></div>
      <form id="task-form"><div class="modal-body"><div class="form-grid">
        <div class="field full"><label for="research-question">研究问题</label><textarea class="textarea" id="research-question" required minlength="5">${escapeHtml(product.research_question_template)}</textarea><div class="field-hint">系统将围绕这个问题组织证据、分析和报告。</div></div>
  <div class="field"><label for="cutoff-date">研究截止日期</label><input class="input" id="cutoff-date" type="date" max="${escapeHtml(state.bootstrap.cutoff.date)}" value="${escapeHtml(state.bootstrap.cutoff.date)}" required/><div class="field-hint">自动设为当日08:00:00（上海时间）</div></div>
        <div class="field"><label for="priority">优先级</label><select class="select" id="priority"><option value="normal">普通</option><option value="high">高</option><option value="urgent">紧急</option><option value="low">低</option></select></div>
        <div class="field full"><label>市场范围</label><div class="checkboxes"><label><input type="checkbox" id="market-a" checked/> A股</label><label><input type="checkbox" id="market-hk"/> 港股</label></div></div>
        ${needsEntities ? `<div class="field full"><label for="entity-search">研究对象</label><input class="input" id="entity-search" placeholder="输入公司名称或证券代码" autocomplete="off"/><div class="selected-entities" id="selected-entities"></div><div class="entity-results" id="entity-results" hidden></div><div class="field-hint">${product.entity_requirement === "two_or_more" ? "请选择至少两个研究对象" : "请选择至少一个研究对象"}</div></div>` : ""}
      </div><div class="notice warning" style="margin:18px 0 0"><strong>数据就绪度：${product.data_readiness === "ready_with_data_gaps" ? "存在缺口" : "正常"}</strong>提交后先执行时点、证据、授权和数据完整性检查；条件不足时任务会明确受阻原因。</div></div>
      <div class="modal-foot"><button type="button" class="btn cancel">取消</button><button type="submit" class="btn primary">提交并开始研究</button></div></form></div>`;
    document.body.appendChild(modal);
    const close = () => modal.remove();
    modal.querySelector(".close-btn").addEventListener("click", close);
    modal.querySelector(".cancel").addEventListener("click", close);
    modal.addEventListener("click", e => { if (e.target === modal) close(); });
    const selected = [];
    if (needsEntities) bindEntityPicker(modal, selected);
    modal.querySelector("#task-form").addEventListener("submit", async e => {
      e.preventDefault();
      const submit = modal.querySelector('button[type="submit"]'); submit.disabled = true; submit.textContent = "正在提交…";
      try {
        const markets = [modal.querySelector("#market-a").checked ? "A_SHARE" : null, modal.querySelector("#market-hk").checked ? "HK_SHARE" : null].filter(Boolean);
        if (!markets.length) throw new Error("至少选择一个市场");
        const minimum = product.entity_requirement === "two_or_more" ? 2 : product.entity_requirement === "one_or_more" ? 1 : 0;
        if (selected.length < minimum) throw new Error(`请选择至少${minimum}个研究对象`);
        await api("/api/jobs", { method: "POST", body: JSON.stringify({ product_id: productId, research_question: modal.querySelector("#research-question").value.trim(), cutoff_date: modal.querySelector("#cutoff-date").value, priority: modal.querySelector("#priority").value, markets, entities: selected, execute_now: true }) });
        close(); toast("研究任务已提交，正在执行前置校验"); navigate("jobs");
      } catch (error) { toast(error.message, true); submit.disabled = false; submit.textContent = "提交并开始研究"; }
    });
  } catch (error) { toast(error.message, true); }
}

function bindEntityPicker(modal, selected) {
  const input = modal.querySelector("#entity-search");
  const results = modal.querySelector("#entity-results");
  const selectedNode = modal.querySelector("#selected-entities");
  let timer;
  const paint = () => {
    selectedNode.innerHTML = selected.map((e, i) => `<span class="entity-chip">${escapeHtml(e.display_name)}<button type="button" data-remove-entity="${i}">×</button></span>`).join("");
    selectedNode.querySelectorAll("[data-remove-entity]").forEach(btn => btn.addEventListener("click", () => { selected.splice(Number(btn.dataset.removeEntity), 1); paint(); }));
  };
  input.addEventListener("input", () => {
    clearTimeout(timer); const q = input.value.trim(); if (!q) { results.hidden = true; return; }
    timer = setTimeout(async () => {
      try {
        const data = await api(`/api/entities?q=${encodeURIComponent(q)}`);
        results.innerHTML = data.entities.map(e => `<div class="entity-option"><div><strong>${escapeHtml(e.canonical_name)}</strong><div class="table-secondary">${escapeHtml(e.identifiers || e.entity_type)}</div></div><button type="button" class="btn small" data-add-entity="${escapeHtml(e.entity_id)}" data-name="${escapeHtml(e.canonical_name)}">选择</button></div>`).join("") || `<div class="entity-option">没有找到匹配对象</div>`;
        results.hidden = false;
        results.querySelectorAll("[data-add-entity]").forEach(btn => btn.addEventListener("click", () => {
          if (!selected.some(e => e.entity_id === btn.dataset.addEntity)) selected.push({ entity_id: btn.dataset.addEntity, display_name: btn.dataset.name });
          paint(); results.hidden = true; input.value = "";
        }));
      } catch (error) { toast(error.message, true); }
    }, 250);
  });
}

async function renderJobs() {
  root.innerHTML = shell(`<div class="loading">正在读取任务状态…</div>`); bindShell();
  try { state.jobs = (await api("/api/jobs")).jobs; } catch (error) { return renderError(error); }
  const content = `
    ${hero("Research operations", "每个任务，都知道进行到哪里", "任务状态、截止时间、数据就绪度和失败原因均持久保存在本机数据库。", `<button class="btn primary" data-nav="tasks">发起新任务</button><button class="btn" id="refresh-jobs">刷新</button>`)}
    ${state.jobs.length ? `<div class="table-wrap"><table><thead><tr><th>研究任务</th><th>提交人</th><th>研究截止</th><th>优先级</th><th>状态</th><th>更新时间</th><th>结果</th></tr></thead><tbody>${state.jobs.map(j => `<tr><td><div class="table-primary">${escapeHtml(j.title)}</div><div class="table-secondary mono">${escapeHtml(j.job_id)}</div>${j.error ? `<div class="table-secondary" style="color:var(--red)">${escapeHtml(Array.isArray(j.error) ? j.error.map(e => e.message).join("；") : j.error.message || JSON.stringify(j.error))}</div>` : ""}</td><td>${escapeHtml(j.submitted_by)}</td><td>${formatDate(j.cutoff_timestamp)}</td><td>${escapeHtml(j.priority)}</td><td>${status(j.status)}</td><td>${formatDate(j.updated_at)}</td><td>${j.workflow_run_id ? `<button class="btn small" data-open-report="${escapeHtml(j.workflow_run_id)}">查看报告</button>` : "—"}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty"><strong>还没有研究任务</strong>从任务库选择一个研究产品并提交。</div>`}`;
  root.innerHTML = shell(content); bindShell();
  document.getElementById("refresh-jobs")?.addEventListener("click", renderJobs);
  document.querySelectorAll("[data-open-report]").forEach(btn => btn.addEventListener("click", () => { state.activeReport = btn.dataset.openReport; navigate("reports"); }));
  if (state.jobs.some(j => ["queued", "validating", "running"].includes(j.status))) setTimeout(() => { if (state.view === "jobs") renderJobs(); }, 5000);
}

function renderAnswerText(text) {
  return escapeHtml(text || "")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\n/g, "<br>");
}

async function runLlmEnhancement(task, context, targetSelector, button) {
  const cfg = publicLlmConfig();
  const target = typeof targetSelector === "string" ? document.querySelector(targetSelector) : targetSelector;
  if (!target) return;
  if (!cfg) {
    target.innerHTML = `<div class="ai-enhance-empty">请先点击右上角“设置”，填写并启用自己的模型 Key；不接入大模型时，基础规则功能仍可正常使用。</div>`;
    toast("请先在右上角接入自己的模型 Key", true);
    return;
  }
  const oldText = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "AI生成中…"; }
  target.innerHTML = `<div class="ai-enhance-loading">正在调用您的模型生成增强解释……</div>`;
  try {
    const resp = await api("/api/llm/enhance", {
      method: "POST",
      body: JSON.stringify({ task, context, llm_config: cfg }),
    });
    if (!resp.ok) throw new Error(resp.error || "生成失败");
    target.innerHTML = `<div class="ai-enhance-answer">${renderAnswerText(resp.answer || "（空回答）")}<small>由 ${escapeHtml(resp.model || cfg.model)} 生成 · ${(Number(resp.elapsed_ms || 0) / 1000).toFixed(1)} 秒</small></div>`;
  } catch (e) {
    target.innerHTML = `<div class="ai-enhance-error">AI增强失败：${escapeHtml(e.message)}</div>`;
  } finally {
    if (button) { button.disabled = false; button.textContent = oldText; }
  }
}

function renderAsk() {
  if (!state.chat) {
    try { state.chat = JSON.parse(localStorage.getItem("ask-chat-v1") || "[]"); } catch (e) { state.chat = []; }
  }
  const chat = state.chat;
  const saveChat = () => { try { localStorage.setItem("ask-chat-v1", JSON.stringify(chat.slice(-30))); } catch (e) { /* ignore */ } };
  const messages = chat.map(m => m.role === "user"
    ? `<div class="chat-row user"><div class="chat-bubble user">${escapeHtml(m.content)}</div></div>`
    : `<div class="chat-row"><div class="chat-bubble agent ${m.error ? "err" : ""}">${renderAnswerText(m.content)}${m.elapsed ? `<small>生成耗时 ${(m.elapsed / 1000).toFixed(1)} 秒 · 基于本机研究底座</small>` : ""}</div></div>`
  ).join("");
  const pending = state.askPending
    ? `<div class="chat-row"><div class="chat-bubble agent pending">正在生成回答 <span id="ask-timer">0.0</span> 秒……</div></div>`
    : "";
  const llmCfg = publicLlmConfig();
  const modeHtml = llmCfg
    ? `<div class="llm-inline-status on"><strong>AI增强已启用</strong><span>${escapeHtml(llmCfg.provider)} · ${escapeHtml(llmCfg.model)} · ${escapeHtml(maskSecret(llmCfg.api_key))}</span><button class="btn small" id="ask-open-llm">模型设置</button></div>`
    : `<div class="llm-inline-status"><strong>当前为规则基础版</strong><span>问答默认尝试服务器本地模型代理；右上角可填写自己的模型 Key 开启 AI 增强。</span><button class="btn small" id="ask-open-llm">接入大模型</button></div>`;
  const content = `
    <section class="mb-module ask-page">
      <div class="sf-head"><h2><span class="mb-num">◈</span>AI研究员 <small class="sf-date">基于本机研究底座 · 快速生成</small></h2></div>
      ${modeHtml}
      <div class="chat-box" id="chat-box">
        ${messages || `<div class="chat-empty"><p>直接向研究员提问，例如：</p>
          <button class="ask-hint" data-q="今天白酒板块怎么看？">今天白酒板块怎么看？</button>
          <button class="ask-hint" data-q="今日重点关注股票的核心逻辑是什么？">今日重点关注股票的核心逻辑是什么？</button>
          <button class="ask-hint" data-q="当前最大的风险点是什么？">当前最大的风险点是什么？</button></div>`}
        ${pending}
      </div>
      <div class="chat-input-row">
        <textarea id="ask-input" rows="2" maxlength="500" placeholder="输入您的研究问题（500 字内，Ctrl+Enter 发送）…"></textarea>
        <button class="btn primary" id="ask-send" ${state.askPending ? "disabled" : ""}>发送</button>
      </div>
    </section>`;
  root.innerHTML = shell(content);
  bindShell();
  const input = document.getElementById("ask-input");
  const send = document.getElementById("ask-send");
  const box = document.getElementById("chat-box");
  if (box) box.scrollTop = box.scrollHeight;
  document.querySelectorAll(".ask-hint").forEach(btn => btn.addEventListener("click", () => {
    input.value = btn.dataset.q;
    input.focus();
  }));
  document.getElementById("ask-open-llm")?.addEventListener("click", openLlmSettings);
  const submit = async () => {
    const q = input.value.trim();
    if (!q || state.askPending) return;
    chat.push({ role: "user", content: q });
    saveChat();
    state.askPending = true;
    renderAsk();
    const t0 = Date.now();
    const timerEl = document.getElementById("ask-timer");
    state.askTimer = setInterval(() => {
      if (timerEl) timerEl.textContent = ((Date.now() - t0) / 1000).toFixed(1);
    }, 100);
    const finish = (text, meta) => {
      clearInterval(state.askTimer);
      state.askPending = false;
      chat.push({ role: "assistant", content: text, elapsed: meta?.elapsed_ms ?? (Date.now() - t0) });
      saveChat();
      renderAsk();
    };
    try {
      const resp = await fetch("/api/ask/stream", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Workbench-Token": token },
        body: JSON.stringify({ question: q, history: chat.slice(-7, -1), llm_config: publicLlmConfig() }),
      });
      if (!resp.ok || !resp.body) {
        finish(`生成失败：HTTP ${resp.status}`, null);
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      const pendingBubble = document.querySelector(".chat-bubble.agent.pending");
      let text = "", buffer = "", meta = null, started = false, thinkText = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const frames = buffer.split("\n\n");
        buffer = frames.pop() || "";
        for (const frame of frames) {
          const line = frame.replace(/^data:\s*/gm, "").trim();
          if (!line || line === "[DONE]") continue;
          try {
            const obj = JSON.parse(line);
            if (obj.done) { meta = obj; continue; }
            if (obj.error) { meta = { error: obj.error }; continue; }
            if (obj.kind === "think") {
              if (pendingBubble) {
                if (!started) { pendingBubble.classList.remove("pending"); started = true; }
                thinkText = (thinkText + obj.text).slice(-300);
                pendingBubble.innerHTML = `<span class="think-dim">正在思考…${escapeHtml(thinkText.slice(-160))}</span><span class="caret">▍</span>`;
              }
            } else if (obj.text) {
              text += obj.text;
              if (!started && pendingBubble) {
                pendingBubble.classList.remove("pending");
                started = true;
              }
              if (pendingBubble) pendingBubble.innerHTML = renderAnswerText(text) + '<span class="caret">▍</span>';
            }
          } catch (e) { /* 忽略半帧 */ }
        }
      }
      if (meta && meta.error) {
        finish(`生成失败：${meta.error}`, null);
      } else {
        finish(text || "（空回答）", meta);
      }
    } catch (e) {
      finish(`调用失败：${e.message}`, null);
    }
  };
  send?.addEventListener("click", submit);
  input?.addEventListener("keydown", ev => {
    if (ev.key === "Enter" && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); submit(); }
  });
  if (!chat.length) input?.focus();
}

const LIB_TYPE_CLS = { "研报": "lt-report", "新闻": "lt-news", "政策": "lt-policy", "行业事件": "lt-event" };

async function renderLibrary() {
  root.innerHTML = shell(`<div class="loading">正在读取研报库…</div>`);
  bindShell();
  const dateQ = state.libraryDate ? `?date=${state.libraryDate}` : "";
  state.library = await api(`/api/research-library${dateQ}`);
  paintLibrary();
}

function paintLibrary() {
  const lib = state.library;
  if (!lib) return;
  const items = lib.items || [];

  // 日期导航（30 天滚动档案）
  const dates = lib.available_dates || [];
  const cur = lib.date;
  const curIdx = dates.indexOf(cur);
  const prevDate = curIdx >= 0 && curIdx < dates.length - 1 ? dates[curIdx + 1] : null;
  const nextDate = curIdx > 0 ? dates[curIdx - 1] : null;
  const dateNav = dates.length ? `
    <div class="date-nav">
      <button class="btn small" id="lib-prev" ${prevDate ? "" : "disabled"}>◀ 前一天</button>
      <select id="lib-date-select">${dates.map(d => `<option value="${d}" ${d === cur ? "selected" : ""}>${d}${d === dates[0] ? "（最新）" : ""}</option>`).join("")}</select>
      <button class="btn small" id="lib-next" ${nextDate ? "" : "disabled"}>后一天 ▶</button>
      ${lib.is_history ? `<span class="history-badge">历史研报库 · ${escapeHtml(cur || "")}</span>` : `<span class="live-badge">今日研报库</span>`}
      <span class="lib-arch-note">档案保留最近 30 天，更早内容每日滚动清理</span>
    </div>` : "";

  // 板块筛选（三态：undefined=全部；空 Set=全不选；非空 Set=勾选）
  const sectorNames = [...new Set(items.map(i => i.sector_name || i.sector || "未分类").filter(Boolean))];
  const sel = state.libSectors;
  const isAll = sel === undefined;
  const match = i => isAll || sel.has(i.sector_name || i.sector || "未分类");
  const sectorPanel = state.libSectorPanelOpen ? `
    <div class="sector-panel" id="lib-sector-panel">
      <div class="sector-panel-head">
        <button class="link" id="lib-sector-all">全选</button>
        <button class="link" id="lib-sector-clear">清空</button>
      </div>
      ${sectorNames.map(name => `
        <label class="sector-option"><input type="checkbox" data-lib-sector="${escapeHtml(name)}" ${isAll || sel.has(name) ? "checked" : ""}><span>${escapeHtml(name)}</span></label>`).join("")}
    </div>` : "";
  const filterLabel = isAll ? "全部板块" : sel.size === 0 ? "未选板块" : `已选 ${sel.size} 个板块`;

  const libList = [];
  const card = item => {
    const i = libList.push(item) - 1;
    const typeCls = LIB_TYPE_CLS[item.item_type] || "lt-news";
    const preview = (item.points || [])[0] || "";
    return `
      <div class="ev-card lib-card tk-clickable" data-lib-i="${i}" tabindex="0" role="button" aria-label="查看摘要：${escapeHtml(String(item.title || "").slice(0, 24))}">
        <div class="lib-card-head"><span class="lib-type ${typeCls}">${escapeHtml(item.item_type || "新闻")}</span><span class="lib-sector">${escapeHtml(item.sector_name || item.sector || "未分类")}</span></div>
        <strong>${escapeHtml(item.title || "")}</strong>
        ${preview ? `<p class="doc-note">${escapeHtml(preview)}</p>` : ""}
        <small class="pick-open">点击查看分点摘要与来源 →</small>
      </div>`;
  };
  const urgent = items.filter(i => i.category === "重要且紧急" && match(i));
  const normal = items.filter(i => i.category !== "重要且紧急" && match(i));
  const col = (label, cls, list, note) => `
    <section class="lib-col">
      <h3 class="lib-col-head ${cls}">${label}<span class="lib-col-count">${list.length}</span></h3>
      <p class="lib-col-note">${note}</p>
      ${list.map(card).join("") || `<div class="lib-empty">今日此类暂无条目</div>`}
    </section>`;

  const content = `
    <article class="morning-brief-v2">
      <div class="mb-head">
        <div><h1>研报库</h1><p>今日应关注的研报 · 新闻 · 政策，按重要程度分类，全部可溯源</p></div>
        <div class="brief-asof">研究截止<br><strong>${escapeHtml(cur || "")} 08:00</strong></div>
      </div>
      ${dateNav}
      <div class="tier-pills-row">
        <div class="sf-filter">
          <button class="btn small" id="lib-sector-filter-btn">板块筛选：${filterLabel} ▾</button>
          ${sectorPanel}
        </div>
        <span class="sf-count">${urgent.length + normal.length}/${items.length} 条</span>
      </div>
      ${items.length ? `
      <div class="lib-cols">
        ${col("重要且紧急", "lib-urgent", urgent, "当日必须看：行情异动、当日政策、重大公告、评级调整")}
        ${col("重要不紧急", "lib-normal", normal, "值得读但可安排：深度研报、趋势分析、一般行业动态")}
      </div>` : `
      <div class="queue-item">${lib.is_history ? "该日研报库内容未生成（研报库自 2026-08-17 起每日生成）。" : "研报库装配中——当日内容在每日同步后生成，可用上方日期导航查看历史。"}</div>`}
      <p class="brief-footnote">研报库每日更新；条目摘要由消费行研agent基于研究底座撰写，来源可跳转原文。内容为研究参考，不构成投资建议。</p>
    </article>`;
  root.innerHTML = shell(content);
  bindShell();

  const goLibDate = async d => {
    if (!d) return;
    state.libraryDate = d;
    state.library = await api(`/api/research-library?date=${d}`);
    paintLibrary();
  };
  document.getElementById("lib-prev")?.addEventListener("click", () => goLibDate(prevDate));
  document.getElementById("lib-next")?.addEventListener("click", () => goLibDate(nextDate));
  document.getElementById("lib-date-select")?.addEventListener("change", ev => goLibDate(ev.target.value));

  document.getElementById("lib-sector-filter-btn")?.addEventListener("click", ev => {
    ev.stopPropagation();
    state.libSectorPanelOpen = !state.libSectorPanelOpen;
    paintLibrary();
  });
  document.getElementById("lib-sector-panel")?.addEventListener("click", ev => ev.stopPropagation());
  document.querySelectorAll("[data-lib-sector]").forEach(cb => cb.addEventListener("change", () => {
    const name = cb.dataset.libSector;
    const current = state.libSectors === undefined ? new Set(sectorNames) : new Set(state.libSectors);
    if (cb.checked) current.add(name);
    else current.delete(name);
    state.libSectors = current.size >= sectorNames.length ? undefined : current;
    paintLibrary();
  }));
  document.getElementById("lib-sector-all")?.addEventListener("click", () => { state.libSectors = undefined; paintLibrary(); });
  document.getElementById("lib-sector-clear")?.addEventListener("click", () => { state.libSectors = new Set(); paintLibrary(); });
  if (state.libSectorPanelOpen) {
    setTimeout(() => document.addEventListener("click", () => {
      state.libSectorPanelOpen = false;
      paintLibrary();
    }, { once: true }), 0);
  }

  document.querySelectorAll("[data-lib-i]").forEach(el => {
    const open = () => openLibraryDrawer(libList[Number(el.dataset.libI)]);
    el.addEventListener("click", open);
    el.addEventListener("keydown", ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); open(); } });
  });
}

function openLibraryDrawer(item) {
  const src = item.source;
  const sourceHtml = src ? (src.type === "event"
    ? `<div class="ev-card">
         <div class="ev-head"><strong>${escapeHtml(src.title)}</strong><span>重要性 ${src.materiality_score ?? "—"}</span></div>
         <p class="doc-note">${escapeHtml(src.summary || "")}</p>
         <small class="ev-src">${escapeHtml(src.locator || "")}</small>
         <div class="ev-actions">${src.source_url ? `<a class="btn primary small" href="${escapeHtml(src.source_url)}" target="_blank" rel="noopener noreferrer">打开原文 ↗</a>` : `<span class="ev-no-link">该来源无公开网页入口，以库内记录为准</span>`}</div>
       </div>`
    : `<div class="ev-card">
         <div class="ev-head"><strong>${escapeHtml(src.title)}</strong><span>${escapeHtml(src.publisher || "")}</span></div>
         <div class="ev-actions">
           <a class="btn primary small" href="/api/documents/${encodeURIComponent(src.document_id)}/content" target="_blank" rel="noopener">查看库内原文 ↗</a>
           ${src.source_url ? `<a class="btn small" href="${escapeHtml(src.source_url)}" target="_blank" rel="noopener noreferrer">官网链接 ↗</a>` : ""}
         </div>
       </div>`
  ) : `<p class="doc-note">本条由研究底座数据直接得出，无单一来源文档。</p>`;
  const pointsHtml = (item.points || []).map(p => `<li>${escapeHtml(p)}</li>`).join("");
  const typeCls = LIB_TYPE_CLS[item.item_type] || "lt-news";
  const drawer = document.createElement("div");
  drawer.className = "detail-backdrop";
  drawer.innerHTML = `<aside class="brief-drawer" role="dialog" aria-modal="true">
    <div class="drawer-head"><div><span>${escapeHtml(item.category || "")} · <span class="lib-type ${typeCls}">${escapeHtml(item.item_type || "新闻")}</span> · ${escapeHtml(item.sector_name || item.sector || "未分类")}</span><h2>${escapeHtml(item.title || "")}</h2></div><button class="drawer-close" aria-label="关闭详情">×</button></div>
    <div class="drawer-body">
      <section class="drawer-section"><h3>分点摘要</h3><ul class="lib-points">${pointsHtml}</ul></section>
      <section class="drawer-section"><h3>信息来源</h3>${sourceHtml}</section>
    </div>
    <div class="drawer-foot"><button class="btn drawer-dismiss">关闭</button></div>
  </aside>`;
  document.body.appendChild(drawer);
  const close = () => drawer.remove();
  drawer.querySelector(".drawer-close").addEventListener("click", close);
  drawer.querySelector(".drawer-dismiss").addEventListener("click", close);
  drawer.addEventListener("click", ev => { if (ev.target === drawer) close(); });
  drawer.addEventListener("keydown", ev => { if (ev.key === "Escape") close(); });
  drawer.querySelector(".drawer-close").focus();
}

async function renderReports() {
  root.innerHTML = shell(`<div class="loading">正在读取研究报告…</div>`); bindShell();
  try { state.reports = (await api("/api/reports")).reports; } catch (error) { return renderError(error); }
  if (!state.activeReport && state.reports.length) state.activeReport = state.reports[0].run_id;
  let detail = null;
  if (state.activeReport) {
    try { detail = await api(`/api/reports/${encodeURIComponent(state.activeReport)}`); } catch (error) { toast(error.message, true); }
  }
  const list = `<div class="card report-list"><div class="card-head">报告历史 <span>${state.reports.length}</span></div>${state.reports.map(r => `<button class="report-item ${r.run_id === state.activeReport ? "active" : ""}" data-report-id="${escapeHtml(r.run_id)}"><div class="report-title">${escapeHtml(r.title)}</div><div class="report-meta"><span>${formatDate(r.started_at, false)}</span><span>${escapeHtml(r.status_label)}</span><span>${r.claim_count} 条结论</span></div></button>`).join("") || `<div class="queue-item">暂无报告</div>`}</div>`;
  const reportDocument = detail ? renderReportDetail(detail) : `<div class="empty"><strong>选择一份报告</strong>在左侧打开报告内容和证据链。</div>`;
  const content = `${hero("Report workbench", "生成完成，直接展示", "Agent 完成证据、时点、反证和合规检查后，报告立即显示；可逐条查看结论、证据并留下批注。")}<div class="report-layout">${list}<div>${reportDocument}</div></div>`;
  root.innerHTML = shell(content); bindShell();
  document.querySelectorAll("[data-report-id]").forEach(btn => btn.addEventListener("click", () => { state.activeReport = btn.dataset.reportId; state.reportTab = "report"; renderReports(); }));
  document.querySelectorAll("[data-report-tab]").forEach(btn => btn.addEventListener("click", () => { state.reportTab = btn.dataset.reportTab; renderReports(); }));
  document.getElementById("annotation-form")?.addEventListener("submit", submitAnnotation);
}

function renderReportDetail(detail) {
  const r = detail.run;
  const tabs = `<div class="tabs"><button class="tab ${state.reportTab === "report" ? "active" : ""}" data-report-tab="report">报告正文</button><button class="tab ${state.reportTab === "claims" ? "active" : ""}" data-report-tab="claims">结论与证据 ${detail.claims.length}</button><button class="tab ${state.reportTab === "annotations" ? "active" : ""}" data-report-tab="annotations">批注 ${detail.annotations.length}</button></div>`;
  let body = "";
  if (state.reportTab === "claims") body = renderClaims(detail.claims);
  else if (state.reportTab === "annotations") body = renderAnnotations(detail);
  else body = detail.report_markdown ? markdown(detail.report_markdown) : `<div class="notice warning"><strong>报告正文文件不可用</strong>结论和证据记录仍可在“结论与证据”中审阅。</div>`;
  return `<article class="report-document"><div class="eyebrow">${escapeHtml(r.template_id)} · ${escapeHtml(r.run_id)}</div><h1>${escapeHtml(detail.title)}</h1><div class="report-metadata">${status(r.status)}<span class="status info">截止 ${formatDate(r.cutoff_timestamp)}</span><span class="status ${r.publication_status === "internal_research_ready" ? "good" : "watch"}">${escapeHtml(r.publication_status)}</span></div>${tabs}${body}</article>`;
}

function markdown(source) {
  const lines = String(source).replace(/\r/g, "").split("\n");
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { if (inList) { html += "</ul>"; inList = false; } continue; }
    if (line.startsWith("### ")) html += `<h3>${inlineMarkdown(line.slice(4))}</h3>`;
    else if (line.startsWith("## ")) html += `<h2>${inlineMarkdown(line.slice(3))}</h2>`;
    else if (line.startsWith("# ")) html += `<h2>${inlineMarkdown(line.slice(2))}</h2>`;
    else if (/^[-*] /.test(line)) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inlineMarkdown(line.slice(2))}</li>`; }
    else html += `<p>${inlineMarkdown(line)}</p>`;
  }
  if (inList) html += "</ul>";
  return html;
}

function inlineMarkdown(value) {
  return escapeHtml(value).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/`(.+?)`/g, "<code>$1</code>");
}

function renderClaims(claims) {
  if (!claims.length) return `<div class="empty"><strong>没有结构化结论</strong>此报告未生成结论图谱。</div>`;
  return claims.map(c => `<section class="claim"><div class="claim-head"><span class="tag">${escapeHtml(c.content_label)}</span><span class="tag">${escapeHtml(c.importance)}</span><span class="confidence">置信度 ${Math.round(c.confidence * 100)}%</span></div><div class="claim-text">${escapeHtml(c.text)}</div>${c.formula ? `<div class="formula">${escapeHtml(c.formula)}</div>` : ""}<div class="evidence-list">${c.evidence.map(e => `<div class="evidence-item ${e.relation_type === "counter" ? "counter" : ""}"><div class="evidence-source">${e.relation_type === "counter" ? "反证" : "支持证据"} · ${escapeHtml(e.document_title)}</div><div class="evidence-locator">${escapeHtml(e.publisher)} · ${escapeHtml(e.locator)} · 可得时间 ${formatDate(e.available_at)}</div></div>`).join("") || `<div class="notice warning"><strong>证据未绑定</strong>该结论当前不能作为正式发布依据。</div>`}</div><div style="margin-top:8px"><button class="btn ghost small" data-annotate-claim="${escapeHtml(c.claim_id)}">批注此结论</button></div></section>`).join("");
}

function renderAnnotations(detail) {
  return `<form id="annotation-form"><div class="field"><label for="annotation-note">新增报告批注</label><textarea class="textarea" id="annotation-note" required minlength="2" placeholder="记录需要补证、修改或讨论的问题"></textarea></div><div style="margin:9px 0 20px"><button class="btn primary small">保存具名批注</button></div></form>${detail.annotations.map(a => `<div class="annotation"><div class="table-primary">${escapeHtml(a.author)} · ${status(a.status, a.status === "resolved" ? "good" : "watch")}</div><div>${escapeHtml(a.note)}</div><div class="table-secondary">${formatDate(a.created_at)} ${a.claim_id ? `· 结论 ${escapeHtml(a.claim_id)}` : "· 报告整体"}</div></div>`).join("") || `<div class="empty"><strong>暂无批注</strong>用具名身份记录需要补充或修改的问题。</div>`}`;
}

async function submitAnnotation(event) {
  event.preventDefault();
  try {
    await api(`/api/reports/${encodeURIComponent(state.activeReport)}/annotations`, { method: "POST", body: JSON.stringify({ note: document.getElementById("annotation-note").value.trim(), section_name: "report" }) });
    toast("批注已保存"); renderReports();
  } catch (error) { toast(error.message, true); }
}

async function renderData() {
  root.innerHTML = shell(`<div class="loading">正在汇总数据状态…</div>`); bindShell();
  try { state.dataStatus = await api("/api/data-status"); } catch (error) { return renderError(error); }
  const d = state.dataStatus;
  const freshCounts = d.freshness.reduce((a, x) => (a[x.status] = (a[x.status] || 0) + 1, a), {});
  const pendingLicenses = d.licenses.filter(x => !x.decision || x.decision === "pending");
  const gaps = d.coverage.filter(x => x.populated_stream_count < x.required_stream_count);
  const content = `
    ${hero("Data truth center", "让每条结论，带着数据现实出现", "查看新鲜度、覆盖缺口、商业授权和生产快照。系统明确区分“没有变化”与“没有数据”。")}
    <div class="notice warning"><strong>研究真实性边界</strong>${escapeHtml(d.truth_boundary)}</div>
    <div class="data-summary"><div class="summary-tile"><strong>${freshCounts.fresh || 0}</strong><span>新鲜数据流</span></div><div class="summary-tile"><strong>${freshCounts.stale || 0}</strong><span>过期数据流</span></div><div class="summary-tile"><strong>${pendingLicenses.length}</strong><span>待确认授权源</span></div><div class="summary-tile"><strong>${gaps.length}</strong><span>存在数据缺口的覆盖单元</span></div></div>
    <section class="section"><div class="section-head"><h2 class="section-title">数据新鲜度</h2><span class="section-meta">按来源与数据流</span></div>${d.freshness.length ? `<div class="table-wrap"><table><thead><tr><th>来源</th><th>数据流</th><th>状态</th><th>最近可得时间</th><th>检查时间</th><th>滞后</th></tr></thead><tbody>${d.freshness.map(f => `<tr><td><div class="table-primary">${escapeHtml(f.source_name || f.source_id)}</div><div class="table-secondary mono">${escapeHtml(f.source_id)}</div></td><td>${escapeHtml(f.stream_name)}</td><td>${status(f.status)}</td><td>${formatDate(f.latest_available_at)}</td><td>${formatDate(f.checked_at)}</td><td>${f.lag_hours == null ? "—" : `${Math.round(f.lag_hours)} 小时`}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty"><strong>尚无已运行的新鲜度记录</strong>这不表示数据正常，而是检查尚未形成记录。</div>`}</section>
    <section class="section"><div class="section-head"><h2 class="section-title">数据授权与访问状态</h2><span class="section-meta">商业授权未确认时禁止正式入库与发布</span></div><div class="table-wrap"><table><thead><tr><th>数据来源</th><th>来源类型</th><th>许可状态</th><th>正式决策</th><th>再分发</th><th>说明</th></tr></thead><tbody>${d.licenses.map(l => `<tr><td><div class="table-primary">${escapeHtml(l.name)}</div><div class="table-secondary mono">${escapeHtml(l.source_id)}</div></td><td>${escapeHtml(l.source_family)}</td><td>${status(l.license_status, l.license_status.includes("pending") ? "watch" : "info")}</td><td>${status(l.decision || "pending", l.decision === "approved" || l.decision === "public_official" ? "good" : "watch")}</td><td>${l.redistribution_allowed ? "允许" : "不允许"}</td><td><div class="license-note">${escapeHtml(l.notes || l.cache_policy || "尚无正式授权说明")}</div></td></tr>`).join("")}</tbody></table></div></section>
    <section class="section"><div class="section-head"><h2 class="section-title">行业与市场覆盖</h2><span class="section-meta">11个领域 × A/H市场</span></div><div class="table-wrap"><table><thead><tr><th>领域</th><th>市场</th><th>证券数</th><th>指标定义</th><th>已填充数据流</th><th>证券池</th><th>研究包</th></tr></thead><tbody>${d.coverage.map(c => `<tr><td>${escapeHtml(c.sector_name)}</td><td>${escapeHtml(c.market)}</td><td>${c.security_count}</td><td>${c.metric_definition_count}</td><td>${c.populated_stream_count}/${c.required_stream_count}</td><td>${status(c.universe_status, c.universe_status === "populated" ? "good" : "watch")}</td><td>${status(c.research_pack_status, c.research_pack_status === "ready" ? "good" : "watch")}</td></tr>`).join("")}</tbody></table></div></section>
    <div class="notice danger"><strong>固定边界</strong>本系统不接入基金持仓、不推断仓位、不自动交易、不自动对外发布。</div>`;
  root.innerHTML = shell(content); bindShell();
}

async function acknowledgeAlert(alertId) {
  try { await api(`/api/alerts/${encodeURIComponent(alertId)}/acknowledge`, { method: "POST", body: "{}" }); toast("已标记为查看"); await loadCore(); renderToday(); }
  catch (error) { toast(error.message, true); }
}

function renderError(error) {
  root.innerHTML = state.bootstrap ? shell(`<div class="error-panel"><div class="eyebrow">Service error</div><h2>页面暂时无法读取研究数据</h2><p>${escapeHtml(error.message)}</p><button class="btn primary" onclick="location.reload()">重新连接</button></div>`) : `<div class="error-panel"><div class="brand-mark">CR</div><h2>无法连接本机研究服务</h2><p>${escapeHtml(error.message)}</p><p>请确认“消费行业研究工作台”正在运行，然后刷新页面。</p><button class="btn primary" onclick="location.reload()">重新连接</button></div>`;
  if (state.bootstrap) bindShell();
}

async function renderView() {
  if (!state.bootstrap) return;
  if (state.view === "today") renderToday();
  else if (state.view === "jobs") await renderJobs();
  else if (state.view === "reports") await renderReports();
  else if (state.view === "ask") renderAsk();
  else if (state.view === "library") await renderLibrary();
  else if (state.view === "data") await renderData();
}

window.addEventListener("hashchange", () => {
  const view = location.hash.replace("#", "");
  if (!pageTitles[view]) {
    state.view = "today";
    history.replaceState(null, "", "#today");
    renderView();
  } else if (view !== state.view) { state.view = view; renderView(); }
});

document.addEventListener("click", event => {
  const navButton = event.target.closest("[data-nav]");
  if (navButton) navigate(navButton.dataset.nav);
  const claimButton = event.target.closest("[data-annotate-claim]");
  if (claimButton) { state.reportTab = "annotations"; renderReports().then(() => { const note = document.getElementById("annotation-note"); if (note) { note.value = `关于结论 ${claimButton.dataset.annotateClaim}：`; note.focus(); } }); }
});

(async function start() {
  try {
    const requestedView = location.hash.replace("#", "");
    if (requestedView && !pageTitles[requestedView]) history.replaceState(null, "", "#today");
    await loadCore();
    await renderView();
  }
  catch (error) { renderError(error); }
})();
