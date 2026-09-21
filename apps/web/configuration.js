(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const form = $("configuration-form");
  let original = null;
  let reading = false;
  let resumeDraft = null;
  let parsingResume = false;
  let workingProfile = null;
  let modelConnections = [];
  let activeModelConnectionId = "";
  let assistantModeEdited = false;
  const CAPABILITY_FIELDS = ["vision_enabled", "mail_sync_on_startup"];
  const split = (value) => value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const MODEL_PRESETS = {
    deepseek: {label: "DeepSeek", api_style: "anthropic", base_url: "https://api.deepseek.com", model: "deepseek-flash"},
    "openai-compatible": {label: "OpenAI 兼容接口", api_style: "openai", base_url: "", model: ""},
  };
  const degreeOption = (value) => {
    const text = String(value || "");
    if (text.includes("博")) return "博士";
    if (text.includes("硕") || text.includes("研究生")) return "硕士";
    return text.includes("本") ? "本科" : "";
  };
  async function post(path, payload = {}) {
    const response = await fetch(`/api/local-ui/configuration/${path}`, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-RecruitOps-Local-UI": "1"},
      body: JSON.stringify(payload), signal: AbortSignal.timeout(path === "resume/parse" ? 60000 : 30000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "输入格式有误");
    return result;
  }
  function message(text, failed = false) {
    $("configuration-message").textContent = text;
    $("configuration-message").className = failed ? "configuration-error" : "configuration-message";
  }
  let keywordField = null;
  function requireKeywords(control) {
    if (split(control.value).length) return true;
    keywordField = control;
    control.setAttribute("aria-invalid", "true");
    message("岗位筛选关键词不能为空，请至少填写一个关键词。", true);
    $("configuration-keywords-dialog").showModal();
    return false;
  }
  $("configuration-keywords-dismiss").addEventListener("click", () => $("configuration-keywords-dialog").close());
  $("configuration-keywords-dialog").addEventListener("close", () => keywordField?.focus());
  for (const control of [form.elements.title_keywords, $("resume-title-keywords")]) {
    control.addEventListener("input", () => control.removeAttribute("aria-invalid"));
  }
  function mailboxConfigured() {
    return ["mail_imap_host", "mail_imap_username"].every((key) => form.elements.namedItem(key).value.trim())
      && Boolean(form.elements.mail_imap_password.value.trim() || original?.secrets?.mail_imap_password);
  }
  function syncCapabilityDependencies() {
    const modelEnabled = $("configuration-assistant-mode").value !== "disabled";
    for (const key of ["vision_enabled"]) {
      const control = form.elements.namedItem(key);
      control.disabled = !modelEnabled;
      if (control.disabled) control.checked = false;
    }
    const startup = form.elements.mail_sync_on_startup;
    startup.disabled = !mailboxConfigured();
    if (startup.disabled) startup.checked = false;
  }
  function renderReadiness(payload) {
    const onboarding = payload?.onboarding;
    $("configuration-readiness").textContent = onboarding?.ready === true ? "基础配置已就绪" : onboarding ? "首次配置尚未完成" : "配置能力状态暂不可用";
    const list = $("configuration-missing"); list.replaceChildren();
    const messages = Object.values(onboarding?.messages || {});
    if (!onboarding) messages.push("后端未返回配置检查结果，请重新读取或更新后端。");
    for (const key of ["job_scoring", "scheduled_tasks", "mail"]) {
      const capability = payload?.module_readiness?.[key];
      if (capability && capability.ready !== true) messages.push(capability.message);
    }
    for (const message of messages) {
      const item = document.createElement("li"); item.textContent = message; list.append(item);
    }
    $("onboarding-notice").hidden = onboarding?.ready === true;
    $("onboarding-notice-text").textContent = onboarding ? "首次配置待完成：模型连接、简历事实、岗位关键词与行业范围。" : "配置状态不可用，智能功能可能尚未就绪。";
  }
  function input(label, field, type = "text") {
    const wrapper = document.createElement("label"), caption = document.createElement("span");
    caption.textContent = label;
    const control = document.createElement("input");
    control.type = type; control.dataset.modelField = field;
    if (type === "password") control.autocomplete = "new-password";
    wrapper.append(caption, control);
    return {wrapper, control};
  }
  function collectModelConnections() {
    return [...$("model-connection-list").querySelectorAll(".model-connection")].map((row) => ({
      id: row.dataset.connectionId,
      name: row.querySelector('[data-model-field="name"]').value.trim(),
      provider: row.querySelector('[data-model-field="provider"]').value,
      api_style: MODEL_PRESETS[row.querySelector('[data-model-field="provider"]').value].api_style,
      base_url: row.querySelector('[data-model-field="base_url"]').value.trim(),
      model: row.querySelector('[data-model-field="model"]').value.trim(),
      api_key: row.querySelector('[data-model-field="api_key"]').value.trim(),
    }));
  }
  function renderModelConnections() {
    const region = $("model-connection-list"); region.replaceChildren();
    for (const connection of modelConnections) {
      const row = document.createElement("section");
      row.className = "model-connection"; row.dataset.connectionId = connection.id;
      const heading = document.createElement("div"); heading.className = "model-connection-heading";
      const primaryLabel = document.createElement("label"); primaryLabel.className = "model-primary-choice";
      const primary = document.createElement("input"); primary.type = "radio"; primary.name = "active-model-connection";
      primary.checked = connection.id === activeModelConnectionId;
      primary.addEventListener("change", () => { activeModelConnectionId = connection.id; });
      primaryLabel.append(primary, document.createTextNode("主连接"));
      const state = document.createElement("span"); state.className = "model-connection-state";
      state.textContent = connection.key_configured ? "密钥已保存" : "待配置";
      heading.append(primaryLabel, state);
      const grid = document.createElement("div"); grid.className = "model-connection-grid";
      const name = input("连接名称", "name"); name.control.value = connection.name || "";
      const providerLabel = document.createElement("label"), providerCaption = document.createElement("span");
      providerCaption.textContent = "接口类型";
      const provider = document.createElement("select"); provider.dataset.modelField = "provider";
      for (const [value, preset] of Object.entries(MODEL_PRESETS)) {
        const option = document.createElement("option"); option.value = value; option.textContent = preset.label;
        provider.append(option);
      }
      provider.value = connection.provider || "deepseek";
      providerLabel.append(providerCaption, provider);
      const base = input("API 服务地址", "base_url", "url"); base.control.value = connection.base_url || "";
      const model = input("模型名称", "model"); model.control.value = connection.model || "";
      const key = input("API 密钥", "api_key", "password");
      key.control.value = connection.api_key || "";
      key.control.placeholder = connection.key_configured ? "已保存，留空不修改" : "请输入 API 密钥";
      provider.addEventListener("change", () => {
        const preset = MODEL_PRESETS[provider.value];
        if (provider.value === "deepseek") {
          base.control.value = preset.base_url; model.control.value = preset.model;
        }
      });
      const actions = document.createElement("div"); actions.className = "model-connection-actions";
      const test = document.createElement("button"); test.type = "button"; test.className = "button button--secondary"; test.textContent = "测试连接";
      const remove = document.createElement("button"); remove.type = "button"; remove.className = "button button--ghost"; remove.textContent = "删除";
      const result = document.createElement("span"); result.className = "model-test-result"; result.setAttribute("role", "status");
      test.addEventListener("click", async () => {
        test.disabled = true; result.textContent = "正在测试…";
        try {
          const payload = collectModelConnections().find((item) => item.id === connection.id);
          const response = await post("model/test", {
            id: payload.id,
            provider: payload.provider,
            api_style: payload.api_style,
            base_url: payload.base_url,
            model: payload.model,
            api_key: payload.api_key,
          });
          result.textContent = `连接成功 · ${response.model} · ${response.latency_ms} ms`;
          result.className = "model-test-result is-success";
        } catch (error) { result.textContent = error.message; result.className = "model-test-result is-error"; }
        finally { test.disabled = false; }
      });
      remove.addEventListener("click", () => {
        if (modelConnections.length === 1) return message("至少保留一个模型连接", true);
        modelConnections = collectModelConnections()
          .filter((item) => item.id !== connection.id)
          .map((item) => ({...item, key_configured: modelConnections.find((saved) => saved.id === item.id)?.key_configured || false}));
        if (activeModelConnectionId === connection.id) activeModelConnectionId = modelConnections[0].id;
        renderModelConnections();
      });
      actions.append(test, remove, result);
      grid.append(name.wrapper, providerLabel, base.wrapper, model.wrapper, key.wrapper);
      row.append(heading, grid, actions); region.append(row);
    }
  }
  function renderIndustryGroups(options, selected) {
    const region = $("configuration-industry-groups"); region.replaceChildren();
    const chosen = new Set(selected || []);
    for (const option of options || []) {
      const label = document.createElement("label");
      const checkbox = document.createElement("input"); checkbox.type = "checkbox";
      checkbox.value = option.code; checkbox.checked = chosen.has(option.code);
      label.append(checkbox, document.createTextNode(option.label)); region.append(label);
    }
  }
  function renderMailProviders(options, settings) {
    const select = $("mail-provider"); select.replaceChildren();
    for (const provider of options || []) {
      const option = document.createElement("option"); option.value = provider.id; option.textContent = provider.label;
      option.dataset.host = provider.host; option.dataset.port = String(provider.port); select.append(option);
    }
    const match = [...select.options].find((option) => option.dataset.host === settings.mail_imap_host);
    select.value = match ? match.value : "custom";
  }
  async function load() {
    if (reading || parsingResume) return;
    resumeDraft = null;
    $("resume-preview").hidden = true;
    reading = true;
    try {
      const payload = await post("read");
      if (!Array.isArray(payload.model_connections) || !payload.options?.industry_groups || !payload.options?.mail_providers) {
        original = null;
        throw new Error("配置 API 尚未支持多模型连接、行业和邮箱选项，请先更新后端。未修改现有配置。");
      }
      original = payload;
      renderReadiness(payload);
      workingProfile = structuredClone(payload.profile);
      modelConnections = payload.model_connections.map((item) => ({...item}));
      activeModelConnectionId = payload.active_model_connection_id;
      renderModelConnections();
      for (const [key, value] of Object.entries(payload.settings)) {
        const control = form.elements.namedItem(key);
        if (!control) continue;
        if (control.type === "checkbox") control.checked = payload.configured_capabilities?.[key] ?? value; else control.value = value ?? "";
      }
      for (const key of CAPABILITY_FIELDS) {
        const control = form.elements.namedItem(key);
        if (control) control.checked = (payload.configured_capabilities?.[key] ?? payload.settings[key]) === true;
      }
      $("configuration-assistant-mode").value = payload.module_readiness?.assistant?.status === "disabled" ? "disabled" : "auto";
      assistantModeEdited = false;
      const profile = payload.profile;
      form.elements.degree.value = degreeOption(profile.degree);
      form.elements.title_keywords.value = (profile.matching.title_keywords || []).join("\n");
      renderIndustryGroups(payload.options.industry_groups, (profile.scope || {}).industry_groups);
      renderMailProviders(payload.options.mail_providers, payload.settings);
      form.elements.mail_imap_password.value = "";
      syncCapabilityDependencies();
      $("mail-secret-state").textContent = payload.secrets.mail_imap_password ? "已配置，留空保留" : "未配置";
      const active = modelConnections.find((item) => item.id === activeModelConnectionId);
      const diagnostic = payload.module_readiness?.assistant || {status: !active?.key_configured ? "missing_model" : "unknown"};
      $("configuration-model-status").textContent = ({missing_model: "待配置模型", disabled: "已主动关闭", restart_required: "待重启", configured: "模型已配置"})[diagnostic.status] || "状态待检查";
      document.dispatchEvent(new CustomEvent("recruitops:assistant-configuration", {detail: {status: diagnostic.status}}));
      const scoringReady = payload.module_readiness?.job_scoring?.ready === true;
      const mailReady = payload.module_readiness?.mail?.ready === true;
      const scheduledReady = payload.module_readiness?.scheduled_tasks?.ready === true;
      $("configuration-analysis-status").textContent = scoringReady ? "已开启" : "未就绪";
      $("configuration-mail-status").textContent = mailReady ? "已配置" : "未就绪";
      $("configuration-runtime-status").textContent = [
        ["定时任务", scheduledReady ? "已开启" : "未就绪"],
        ["视觉辅助", payload.settings.llm_enabled && payload.settings.vision_enabled],
        ["启动同步", mailReady && payload.settings.mail_sync_on_startup],
      ].map(([label, state]) => `${label}：${typeof state === "boolean" ? state ? "已启用" : "未启用" : state}`).join(" · ");
      message(payload.restart_required ? "配置已保存，重启桌面后生效；当前能力状态仍以本次启动为准。" : "");
    } catch (error) {
      original = null;
      renderReadiness(null);
      for (const id of ["configuration-model-status", "configuration-analysis-status", "configuration-mail-status"]) $(id).textContent = "状态不可用";
      $("configuration-runtime-status").textContent = "运行能力状态不可用";
      document.dispatchEvent(new CustomEvent("recruitops:assistant-configuration", {detail: {status: "unknown"}}));
      message(`读取失败：${error.message}`, true);
    }
    finally { reading = false; }
  }
  document.querySelector('[data-view="configuration"]').addEventListener("click", () => { if (!original) load(); });
  $("onboarding-open").addEventListener("click", () => document.querySelector('[data-view="configuration"]').click());
  $("configuration-reload").addEventListener("click", load);
  document.addEventListener("recruitops:configuration-reload", load);
  form.addEventListener("input", syncCapabilityDependencies);
  form.addEventListener("change", syncCapabilityDependencies);
  $("configuration-assistant-mode").addEventListener("change", () => { assistantModeEdited = true; });
  function assistantOverrides() {
    if (!assistantModeEdited) return {};
    const enabled = $("configuration-assistant-mode").value !== "disabled";
    return {llm_enabled: enabled, codex_runtime_enabled: enabled};
  }
  $("model-connection-save").addEventListener("click", async () => {
    if (!original || reading || parsingResume) return message("请先成功读取配置并等待当前操作完成", true);
    const button = $("model-connection-save"); button.disabled = true;
    try {
      const connections = collectModelConnections();
      const active = connections.find((item) => item.id === activeModelConnectionId);
      if (!active?.base_url || !active?.model || !(active.api_key || modelConnections.find((item) => item.id === activeModelConnectionId)?.key_configured)) {
        return message("请填写主模型的服务地址、模型名称和 API 密钥。", true);
      }
      const result = await post("save", {settings: assistantOverrides(), model_connections: connections,
        active_model_connection_id: activeModelConnectionId});
      await load(); message(result.message);
    } catch (error) { message(`保存失败：${error.message}`, true); }
    finally { button.disabled = false; }
  });
  void load();
  $("model-connection-add").addEventListener("click", () => {
    if (!original) return message("请先成功读取配置", true);
    if (modelConnections.length >= 8) return message("最多保留 8 个模型连接", true);
    modelConnections = collectModelConnections().map((item) => ({
      ...item,
      key_configured: modelConnections.find((saved) => saved.id === item.id)?.key_configured || false,
    }));
    const id = `model-${crypto.randomUUID().replaceAll("-", "").slice(0, 16)}`;
    modelConnections.push({id, name: "备用模型", provider: "openai-compatible", api_style: "openai", base_url: "", model: "", key_configured: false});
    renderModelConnections();
  });
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (parsingResume || resumeDraft) return message("请先确认或取消简历解析结果", true);
    if (!original) return message("请先读取配置", true);
    if (!requireKeywords(form.elements.title_keywords)) return;
    const button = $("configuration-save"); button.disabled = true;
    const completeButton = $("configuration-complete"); completeButton.disabled = true;
    try {
      syncCapabilityDependencies();
      const settings = assistantOverrides();
      for (const key of [...CAPABILITY_FIELDS, "mail_imap_host", "mail_imap_port", "mail_imap_username", "mail_imap_password", "mail_imap_mailbox"]) {
        const control = form.elements.namedItem(key);
        if (!control) continue;
        settings[key] = control.type === "checkbox" ? control.checked : control.type === "number" ? Number(control.value) : control.value.trim();
      }
      if (settings.mail_sync_on_startup && !mailboxConfigured()) return message("启动同步前，请填写 IMAP 服务器、邮箱账号和授权码。", true);
      const profile = structuredClone(workingProfile);
      profile.degree = form.elements.degree.value || null;
      profile.job_type = "校招";
      profile.matching.title_keywords = split(form.elements.title_keywords.value);
      const industryGroups = [...$("configuration-industry-groups").querySelectorAll('input[type="checkbox"]:checked')].map((item) => item.value);
      if (!industryGroups.length) return message("请至少选择一个行业方向。", true);
      profile.scope = {...(profile.scope || {}), recruit_types: ["秋招"], industry_groups: industryGroups};
      profile.exclusions = {...(profile.exclusions || {}), internships: "exclude", social: true};
      const connections = collectModelConnections();
      if (!connections.some((item) => item.id === activeModelConnectionId)) return message("请选择主模型连接", true);
      const active = connections.find((item) => item.id === activeModelConnectionId);
      if (settings.vision_enabled && (!active.base_url || !active.model || !(active.api_key || modelConnections.find((item) => item.id === active.id)?.key_configured))) {
        return message("启用视觉辅助前，请完成主模型连接配置。", true);
      }
      const payload = {settings, profile, model_connections: connections, active_model_connection_id: activeModelConnectionId};
      if (event.submitter === completeButton) payload.complete_onboarding = true;
      const result = await post("save", payload);
      await load(); message(result.message);
    } catch (error) { message(`保存失败：${error.message}`, true); }
    finally { button.disabled = false; completeButton.disabled = false; }
  });
  async function upload(file) {
    if (!file || file.size > 10000000) throw new Error("请选择不超过 10 MB 的文件");
    const content = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(",")[1]);
      reader.onerror = () => reject(new Error("文件读取失败")); reader.readAsDataURL(file);
    });
    return {filename: file.name, content_base64: content};
  }
  $("resume-upload").addEventListener("change", (event) => {
    $("resume-file-state").textContent = event.target.files[0]?.name || "支持文字版 PDF、TXT、Markdown，最大 10 MB";
  });
  $("resume-analyze").addEventListener("click", async () => {
    if (!original || reading || parsingResume) return message("请先成功读取配置并等待当前操作完成", true);
    const file = $("resume-upload").files[0];
    if (!file) return message("请先选择简历文件", true);
    parsingResume = true;
    resumeDraft = null;
    $("resume-preview").hidden = true;
    $("resume-upload").disabled = true;
    $("resume-analyze").disabled = true;
    $("configuration-save").disabled = true;
    try {
      message("正在解析简历，原配置保持不变…");
      const result = await post("resume", await upload(file));
      const parsed = await post("resume/parse", {text: result.text});
      resumeDraft = parsed;
      const draft = parsed.draft;
      $("resume-degree").value = degreeOption(draft.degree?.value || form.elements.degree.value);
      $("resume-skills").value = draft.skills.map((fact) => fact.value).join("\n");
      $("resume-stack").value = draft.supporting_skills.map((fact) => fact.value).join("\n");
      $("resume-projects").value = draft.projects.join("\n\n");
      $("resume-title-keywords").value = (draft.title_keywords || []).map((fact) => fact.value).join("\n");
      $("resume-evidence").textContent = [
        draft.degree, ...draft.skills, ...draft.supporting_skills,
        ...(draft.title_keywords || []), ...(draft.directions || []),
      ].filter(Boolean).map((fact) => `${fact.value || fact.name}：${fact.evidence}`).join("\n\n");
      $("resume-preview").hidden = false;
      message("简历解析完成。请检查岗位筛选关键词，然后应用分析结果。");
    } catch (error) { message(error.message, true); }
    finally { parsingResume = false; $("resume-upload").disabled = false; $("resume-analyze").disabled = false; $("configuration-save").disabled = false; }
  });
  $("resume-confirm").addEventListener("click", () => {
    if (!resumeDraft) return;
    if (!requireKeywords($("resume-title-keywords"))) return;
    form.elements.degree.value = degreeOption($("resume-degree").value);
    workingProfile.degree = form.elements.degree.value || null;
    workingProfile.skills = split($("resume-skills").value);
    workingProfile.matching.supporting_skills = split($("resume-stack").value);
    workingProfile.matching.project_evidence = [resumeDraft.text];
    workingProfile.matching.learning_targets = [];
    workingProfile.matching.unverified_skills = [];
    workingProfile.matching.title_keywords = split($("resume-title-keywords").value);
    workingProfile.matching.directions = (resumeDraft.draft.directions || []).map((item) => ({name: item.name, keywords: item.keywords || [], exclude_keywords: []}));
    workingProfile.matching.primary_directions = workingProfile.matching.directions.map((item) => item.name);
    workingProfile.matching.secondary_directions = [];
    workingProfile.direction = workingProfile.matching.primary_directions[0] || null;
    form.elements.title_keywords.value = workingProfile.matching.title_keywords.join("\n");
    resumeDraft = null;
    $("resume-preview").hidden = true;
    message("简历分析结果已应用到表单，保存配置后用于岗位筛选和匹配度评分。");
  });
  $("resume-cancel").addEventListener("click", () => {
    resumeDraft = null;
    $("resume-preview").hidden = true;
    $("resume-upload").value = "";
    $("resume-file-state").textContent = "支持文字版 PDF、TXT、Markdown，最大 10 MB";
    message("已取消，原资料未修改。");
  });
  $("mail-provider").addEventListener("change", () => {
    const option = $("mail-provider").selectedOptions[0];
    if (!option || option.value === "custom") return;
    form.elements.mail_imap_host.value = option.dataset.host;
    form.elements.mail_imap_port.value = option.dataset.port;
  });
  $("mail-connection-test").addEventListener("click", async () => {
    const button = $("mail-connection-test"), result = $("mail-test-result"); button.disabled = true;
    result.textContent = "正在测试…";
    try {
      const response = await post("mail/test", {
        host: form.elements.mail_imap_host.value.trim(), port: Number(form.elements.mail_imap_port.value),
        username: form.elements.mail_imap_username.value.trim(), password: form.elements.mail_imap_password.value,
        mailbox: form.elements.mail_imap_mailbox.value.trim() || "INBOX",
      });
      result.textContent = `连接成功 · 只读访问 · ${response.latency_ms} ms`; result.className = "is-success";
    } catch (error) { result.textContent = error.message; result.className = "is-error"; }
    finally { button.disabled = false; }
  });
  $("application-import-template").addEventListener("click", () => {
    const blob = new Blob(["\ufeff公司,岗位,阶段,投递进度网址\r\n示例公司,软件工程师,已投递,https://example.com/my/applications\r\n"], {type: "text/csv;charset=utf-8"});
    const url = URL.createObjectURL(blob), link = document.createElement("a");
    link.href = url; link.download = "投递记录模板.csv"; link.click(); URL.revokeObjectURL(url);
  });
  $("application-import-submit").addEventListener("click", async () => {
    const button = $("application-import-submit"); button.disabled = true;
    try {
      const result = await post("applications/import", await upload($("application-import-file").files[0]));
      $("application-import-result").textContent = `导入 ${result.inserted} 条，跳过已有记录 ${result.skipped} 条。已有阶段未修改。`;
    } catch (error) { $("application-import-result").textContent = error.message; }
    finally { button.disabled = false; }
  });
  const labels = {
    companies_seen: "来源公司", new_companies: "新发现公司", new_entries: "新增入口", excluded_entries: "排除入口（微信 / 表单等）",
    company_total: "本轮目标公司", complete_companies: "抓取完整", partial_companies: "部分抓取成功", failed_companies: "抓取失败",
    skipped_companies: "跳过公司", list_jobs: "列表岗位", filtered_jobs: "标题排除", new_jobs: "新增入库岗位",
    reused_jobs: "复用已有岗位", detail_success: "详情抓取成功", detail_failed: "详情抓取失败",
    scored_jobs: "评分成功", scoring_failed: "评分失败", unscored_jobs: "未评分",
  };
  async function latest() {
    try {
      const {report} = await post("latest-crawl");
      if (!report) return;
      const region = $("latest-crawl-report"); region.hidden = false; region.replaceChildren();
      const heading = document.createElement("h3");
      const status = {succeeded: "执行完成", partial: "执行完成，存在未完成项", failed: "执行失败"}[report.status] || "未知";
      heading.textContent = `最近一次抓取 · ${status}`; region.append(heading);
      const date = document.createElement("p"); date.textContent = report.finished_at ? new Date(report.finished_at).toLocaleString() : ""; region.append(date);
      const grid = document.createElement("dl"); grid.className = "crawl-report-grid";
      for (const [key, label] of Object.entries(labels)) {
        const item = document.createElement("div"), term = document.createElement("dt"), value = document.createElement("dd");
        term.textContent = label; value.textContent = report[key] ?? "未记录";
        item.append(term, value); grid.append(item);
      }
      region.append(grid);
      const reasonLabels = {article: "微信公众号 / 文章", form: "问卷 / 表单", login_page: "登录入口",
        third_party_listing: "第三方聚合页", application_record: "个人投递页", success_page: "投递成功页", missing_entry: "缺少入口"};
      if (Object.keys(report.excluded_reasons || {}).length) {
        const reasons = document.createElement("p");
        reasons.textContent = "入口排除：" + Object.entries(report.excluded_reasons)
          .map(([key, count]) => `${reasonLabels[key] || "其他不可用入口"} ${count}`).join("；");
        region.append(reasons);
      }
      if (report.error) { const error = document.createElement("p"); error.textContent = report.error; region.append(error); }
      const key = "latest-crawl-notification";
      if (localStorage.getItem(key) !== report.execution_id) {
        const notice = document.createElement("div"); notice.className = "toast";
        notice.textContent = `定时抓取${status}，新增 ${report.new_jobs ?? "未记录"} 个岗位。详情见定时任务。`;
        $("toast-region").append(notice); setTimeout(() => notice.remove(), 15000);
        localStorage.setItem(key, report.execution_id);
      }
    } catch (_) { /* Keep the last visible report during a temporary disconnect. */ }
  }
  $("automations-refresh-button").addEventListener("click", latest);
  latest(); setInterval(latest, 30000);
  async function notifyOtherTasks() {
    try {
      const response = await fetch("/api/automations", {signal: AbortSignal.timeout(10000)});
      if (!response.ok) return;
      for (const task of (await response.json()).items || []) {
        if (task.task_id === "daily_recruitment_intelligence") continue;
        const execution = task.latest_execution;
        if (!execution || execution.status !== "succeeded") continue;
        const key = `automation-notification:${task.id}`;
        if (!localStorage.getItem(key)) { localStorage.setItem(key, execution.id); continue; }
        if (localStorage.getItem(key) === execution.id) continue;
        localStorage.setItem(key, execution.id);
        const notice = document.createElement("div"); notice.className = "toast";
        notice.textContent = `定时任务已完成：${task.task_label}`;
        $("toast-region").append(notice); setTimeout(() => notice.remove(), 10000);
      }
    } catch (_) { /* Retry on the next poll. */ }
  }
  notifyOtherTasks(); setInterval(notifyOtherTasks, 30000);
})();
