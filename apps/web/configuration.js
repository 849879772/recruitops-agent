(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const form = $("configuration-form");
  let original = null;
  let reading = false;
  let resumeDraft = null;
  let parsingResume = false;
  const matchingFields = ["title_keywords", "excluded_title_keywords", "primary_directions", "secondary_directions", "supporting_skills", "learning_targets", "unverified_skills", "project_evidence"];
  const split = (value) => value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
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
  async function load() {
    if (reading || parsingResume) return;
    resumeDraft = null;
    $("resume-preview").hidden = true;
    reading = true;
    try {
      const payload = await post("read");
      original = payload;
      for (const [key, value] of Object.entries(payload.settings)) {
        const control = form.elements.namedItem(key);
        if (!control) continue;
        if (control.type === "checkbox") control.checked = value; else control.value = value ?? "";
      }
      const profile = payload.profile;
      for (const key of ["degree", "job_type", "direction"]) form.elements[key].value = profile[key] || "";
      form.elements.skills.value = (profile.skills || []).join("\n");
      for (const key of matchingFields)
        form.elements[key].value = (profile.matching[key] || []).join("\n");
      for (const [key, id] of [["llm_api_key", "api-secret-state"], ["mail_imap_password", "mail-secret-state"]]) {
        form.elements[key].value = "";
        $(id).textContent = payload.secrets[key] ? "已配置，留空保留" : "未配置";
      }
      message("");
    } catch (error) { message(`读取失败：${error.message}`, true); }
    finally { reading = false; }
  }
  document.querySelector('[data-view="configuration"]').addEventListener("click", () => { if (!original) load(); });
  $("configuration-reload").addEventListener("click", load);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (parsingResume || resumeDraft) return message("请先确认或取消简历解析结果", true);
    if (!original) return message("请先读取配置", true);
    const button = $("configuration-save"); button.disabled = true;
    try {
      const settings = {};
      for (const key of [...Object.keys(original.settings), "llm_api_key", "mail_imap_password"]) {
        const control = form.elements.namedItem(key);
        if (!control) continue;
        settings[key] = control.type === "checkbox" ? control.checked : control.type === "number" ? Number(control.value) : control.value.trim();
      }
      const profile = structuredClone(original.profile);
      for (const key of ["degree", "job_type", "direction"]) profile[key] = form.elements[key].value.trim();
      profile.skills = split(form.elements.skills.value);
      for (const key of matchingFields)
        profile.matching[key] = split(form.elements[key].value);
      const result = await post("save", {settings, profile});
      await load(); message(result.message);
    } catch (error) { message(`保存失败：${error.message}`, true); }
    finally { button.disabled = false; }
  });
  async function upload(file) {
    if (!file || file.size > 10000000) throw new Error("请选择不超过 10 MB 的文件");
    const content = await new Promise((resolve, reject) => {
      const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(",")[1]);
      reader.onerror = () => reject(new Error("文件读取失败")); reader.readAsDataURL(file);
    });
    return {filename: file.name, content_base64: content};
  }
  $("resume-upload").addEventListener("change", async (event) => {
    if (!event.target.files[0]) return;
    parsingResume = true;
    resumeDraft = null;
    $("resume-preview").hidden = true;
    event.target.disabled = true;
    $("configuration-save").disabled = true;
    try {
      message("正在解析简历，原配置保持不变…");
      const result = await post("resume", await upload(event.target.files[0]));
      const parsed = await post("resume/parse", {text: result.text});
      resumeDraft = parsed;
      const draft = parsed.draft;
      $("resume-degree").value = draft.degree?.value || "";
      $("resume-skills").value = draft.skills.map((fact) => fact.value).join("\n");
      $("resume-stack").value = draft.supporting_skills.map((fact) => fact.value).join("\n");
      $("resume-projects").value = draft.projects.join("\n\n");
      $("resume-evidence").textContent = [draft.degree, ...draft.skills, ...draft.supporting_skills]
        .filter(Boolean).map((fact) => `${fact.value}：${fact.evidence}`).join("\n\n");
      $("resume-preview").hidden = false;
      message("解析完成。确认后替换旧学历、技能、工程栈和简历正文，并清空旧学习目标及未经验证技能；求职意向保留。最后保存配置生效。");
    } catch (error) { message(error.message, true); }
    finally { parsingResume = false; event.target.disabled = false; $("configuration-save").disabled = false; }
  });
  $("resume-confirm").addEventListener("click", () => {
    if (!resumeDraft) return;
    form.elements.degree.value = $("resume-degree").value.trim();
    form.elements.skills.value = $("resume-skills").value;
    form.elements.supporting_skills.value = $("resume-stack").value;
    form.elements.project_evidence.value = resumeDraft.text;
    form.elements.learning_targets.value = "";
    form.elements.unverified_skills.value = "";
    resumeDraft = null;
    $("resume-preview").hidden = true;
    message("新简历资料已替换到表单，保存配置后生效。");
  });
  $("resume-cancel").addEventListener("click", () => {
    resumeDraft = null;
    $("resume-preview").hidden = true;
    $("resume-upload").value = "";
    message("已取消，原资料未修改。");
  });
  form.elements.direction.addEventListener("change", () => {
    form.elements.primary_directions.value = "";
    form.elements.secondary_directions.value = "";
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
