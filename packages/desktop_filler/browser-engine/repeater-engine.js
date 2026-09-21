(function initResumeRepeaterEngine(root) {
  "use strict";

  const ACTION = "ensure-repeat-count";
  const SECTION_RULES = [
    { key: "education", aliases: ["教育经历", "教育背景", "教育信息", "学历经历", "学历信息", "学习经历", "education", "academic"] },
    { key: "career", aliases: ["工作经历", "工作经验", "职业经历", "全职经历", "career", "work experience", "employment"] },
    { key: "internship", aliases: ["实习经历", "实习经验", "实习实践", "internship"] },
    { key: "project", aliases: ["项目经历", "项目经验", "课题项目", "科研项目", "project", "research experience"] },
    { key: "award", aliases: ["获奖经历", "获奖情况", "获奖信息", "获奖记录", "荣誉奖励", "奖项荣誉", "竞赛获奖", "award", "honor"] },
    { key: "paper", aliases: ["论文专著", "论文/专著", "论文成果", "学术论文", "paper", "publication"] },
    { key: "patent", aliases: ["专利经历", "专利成果", "专利", "patent"] },
    { key: "practice", aliases: ["在校实践", "校园实践", "社会实践", "practice", "campus experience"] },
    { key: "language", aliases: ["语言能力", "外语能力", "语言技能", "language"] },
    { key: "certificate", aliases: ["证书", "资格证书", "技能证书", "certificate"] },
    { key: "skill", aliases: ["IT技能", "专业技能", "技能特长", "计算机能力", "skill"] }
  ];
  const RULE_MAP = new Map(SECTION_RULES.map((rule) => [rule.key, rule]));
  const BLOCKED_ACTION = /删除|移除|清空|重置|提交|投递|申请|保存|完成|确认|取消|返回|导入|上传|编辑|修改|delete|remove|submit|save|confirm|cancel|upload|edit/i;
  const ADD_ACTION = /添加|新增|增加|继续添加|再加|新建|创建|补充|add|new|create|plus/i;

  function normalize(value) {
    return String(value || "").toLowerCase().replace(/[\s\u00a0:_：*＊()（）\[\]【】/\\.\-|]+/g, "").trim();
  }

  function desiredCounts(resume) {
    const publications = Array.isArray(resume?.publications) ? resume.publications : [];
    const patents = publications.filter((item) => /专利|patent/i.test(`${item?.type || ""}${item?.name || ""}`));
    const papers = publications.filter((item) => !/专利|patent/i.test(`${item?.type || ""}${item?.name || ""}`));
    const englishCount = resume?.basic?.englishLevel || resume?.basic?.englishScore
      || (resume?.certificates || []).some((item) => /英语|外语|四级|六级|cet/i.test(`${item?.type || ""}${item?.name || ""}`)) ? 1 : 0;
    return {
      education: resume?.education?.length || 0,
      career: resume?.careers?.length || 0,
      internship: resume?.internships?.length || 0,
      project: resume?.projects?.length || 0,
      award: resume?.awards?.length || 0,
      paper: papers.length,
      patent: patents.length,
      practice: resume?.practices?.length || 0,
      language: englishCount,
      certificate: resume?.certificates?.length || 0,
      skill: resume?.structuredSkills?.length || 0
    };
  }

  function sectionScore(text, sectionKey) {
    const rule = RULE_MAP.get(sectionKey);
    const target = normalize(text);
    if (!rule || !target) return 0;
    let best = 0;
    for (const alias of rule.aliases) {
      const normalizedAlias = normalize(alias);
      if (!normalizedAlias) continue;
      if (target === normalizedAlias) best = Math.max(best, 100);
      else if (target.startsWith(normalizedAlias) || target.endsWith(normalizedAlias)) best = Math.max(best, 92);
      else if (target.includes(normalizedAlias)) best = Math.max(best, 84);
      else if (normalizedAlias.includes(target) && target.length >= 3) best = Math.max(best, 72);
    }
    return best;
  }

  function inferSection(text) {
    return SECTION_RULES.map((rule) => ({ key: rule.key, score: sectionScore(text, rule.key) }))
      .sort((a, b) => b.score - a.score)[0] || { key: "", score: 0 };
  }

  function controlText(control) {
    return [control?.text, control?.ariaLabel, control?.title, control?.name, control?.id, control?.className]
      .filter(Boolean).join(" ");
  }

  function addControlScore(control, sectionKey) {
    const text = controlText(control);
    const normalized = normalize(text);
    if (!normalized || BLOCKED_ACTION.test(text)) return 0;
    const isSymbol = /^(?:\+|＋)$/.test(String(control?.text || "").trim());
    if (!ADD_ACTION.test(text) && !isSymbol && !/(?:^|[-_])(add|plus|create|new)(?:$|[-_])/i.test(text)) return 0;
    let score = isSymbol ? 48 : 58;
    const semanticScore = sectionScore(text, sectionKey);
    score += Math.round(semanticScore * 0.34);
    if (control?.ariaLabel || control?.title) score += 6;
    if (control?.disabled) return 0;
    return Math.min(100, score);
  }

  function selectControl(discovery) {
    const controls = (discovery?.controls || []).map((control) => ({
      ...control,
      score: Number.isFinite(control.score) ? control.score : addControlScore(control, discovery.sectionKey)
    })).filter((control) => control.score > 0).sort((a, b) => b.score - a.score);
    if (!controls.length) return { control: null, ambiguous: false };
    const top = controls[0];
    const second = controls[1];
    const ambiguous = Boolean(second && top.score - second.score < 12);
    return { control: top, ambiguous };
  }

  function buildPlans(discoveries, resume) {
    const desired = desiredCounts(resume);
    const plans = [];
    const unresolved = [];
    for (const discovery of discoveries || []) {
      const desiredCount = desired[discovery.sectionKey] || 0;
      const currentCount = Math.max(0, Number(discovery.currentCount) || 0);
      if (!desiredCount || currentCount >= desiredCount) continue;
      const { control, ambiguous } = selectControl(discovery);
      const sectionConfidence = Number(discovery.sectionConfidence) || 0;
      const locked = Boolean(discovery.controls?.length && discovery.controls.every((item) => item.disabled));
      if (!control || ambiguous || sectionConfidence < 78 || control.score < 72) {
        unresolved.push({ ...discovery, desiredCount, locked, reasonCode:
          locked ? "locked" : ambiguous ? "ambiguous" : !control ? "missing-control" : "low-confidence" });
        continue;
      }
      plans.push({
        action: ACTION,
        sectionId: discovery.sectionId,
        sectionKey: discovery.sectionKey,
        controlId: control.controlId,
        currentCount,
        desiredCount,
        confidence: Math.min(0.99, Math.min(sectionConfidence, control.score) / 100),
        source: "structure"
      });
    }
    return { plans, unresolved };
  }

  function validatePlans(rawPlan, discoveries, resume) {
    const parsed = typeof rawPlan === "string" ? JSON.parse(rawPlan) : rawPlan;
    const desired = desiredCounts(resume);
    const discoveryMap = new Map((discoveries || []).map((item) => [item.sectionId, item]));
    const used = new Set();
    const plans = [];
    for (const item of Array.isArray(parsed?.plans) ? parsed.plans : []) {
      const discovery = discoveryMap.get(String(item?.sectionId || ""));
      const control = discovery?.controls?.find((candidate) => candidate.controlId === item?.controlId);
      const desiredCount = desired[discovery?.sectionKey] || 0;
      const confidence = Number(item?.confidence);
      if (!discovery || !control || used.has(discovery.sectionId)) continue;
      if (item?.action !== ACTION || !Number.isFinite(confidence) || confidence < 0.78 || confidence > 1) continue;
      if (!desiredCount || desiredCount <= (Number(discovery.currentCount) || 0)) continue;
      if (!addControlScore(control, discovery.sectionKey)) continue;
      used.add(discovery.sectionId);
      plans.push({
        action: ACTION,
        sectionId: discovery.sectionId,
        sectionKey: discovery.sectionKey,
        controlId: control.controlId,
        ...(Number.isInteger(discovery.frameId) ? { frameId: discovery.frameId } : {}),
        ...(discovery.rawSectionId ? { rawSectionId: discovery.rawSectionId } : {}),
        ...(control.rawControlId ? { rawControlId: control.rawControlId } : {}),
        currentCount: Math.max(0, Number(discovery.currentCount) || 0),
        desiredCount,
        confidence,
        source: "validated"
      });
    }
    return plans;
  }

  const api = {
    ACTION,
    SECTION_RULES,
    addControlScore,
    buildPlans,
    desiredCounts,
    inferSection,
    normalize,
    sectionScore,
    validatePlans
  };
  root.ResumeRepeaterEngine = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
