(function initResumeFillerContent() {
  "use strict";

  if (globalThis.__LOCAL_RESUME_FILLER_HANDLER__) {
    chrome.runtime.onMessage.removeListener(globalThis.__LOCAL_RESUME_FILLER_HANDLER__);
  }

  const originalValues = new Map();
  const markerAttribute = "data-local-resume-field-id";
  const formItemSelector = [
    ".form-item", ".ant-form-item", ".ant4-form-item", ".el-form-item", ".ivu-form-item", ".arco-form-item", ".brick-field",
    ".semi-form-field", ".ud-formily-item", ".atsx-form-item", ".info_box",
    "[class*='apply-field-']"
  ].join(",");
  const genericCalendarRootSelector = [
    ".ant-calendar-picker", ".ant4-calendar-picker",
    "[class*='calendar-picker']", "[class*='date-picker']", "[class*='datepicker']",
    "[data-picker='date']", "[data-type='date-picker']"
  ].join(",");
  const genericCalendarDiscoverySelector = [
    genericCalendarRootSelector,
    "[class^='ant'][class*='-picker']", "[class*=' ant'][class*='-picker']"
  ].join(",");
  const repeatSections = new Set(["教育经历", "工作经历", "项目经历", "课题项目经验", "获奖", "获奖情况", "竞赛获奖", "论文", "论文/专著", "专利", "论文/专著/专利", "在校实践", "实习经历", "技能", "IT技能", "技能证书", "语言能力", "作品", "作品信息"]);
  let nextFieldId = Array.from(document.querySelectorAll(`[${markerAttribute}]`)).reduce((maximum, element) => {
    const number = Number((element.getAttribute(markerAttribute) || "").match(/resume-field-(\d+)/)?.[1]);
    return Number.isFinite(number) ? Math.max(maximum, number + 1) : maximum;
  }, 1);
  const assignedFieldIds = new Map();

  function delay(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function waitForCondition(check, timeoutMs, stableMs = 0) {
    const deadline = Date.now() + timeoutMs;
    let since = null;
    while (true) {
      const value = check();
      if (value) {
        since ??= Date.now();
        if (Date.now() - since >= stableMs) return value;
      } else since = null;
      const remaining = deadline - Date.now();
      if (remaining <= 0) return null;
      await delay(Math.min(30, remaining));
    }
  }

  function isVisible(element) {
    if (!(element instanceof Element)) return false;
    const style = getComputedStyle(element);
    const rect = element.getBoundingClientRect();
    return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
  }

  function textOf(element) {
    return String(element?.innerText || element?.textContent || "").replace(/\s+/g, " ").trim();
  }

  function detectPlatform() {
    const host = location.hostname;
    if (host === "app.mokahr.com" || (document.querySelector("[class*='sd-Select-container']") && document.querySelector("[class*='apply-field-'], [data-nav-id]"))) return "moka-sd";
    if (host === "join.qq.com") return "tencent-element";
    if (host === "talent.baidu.com") return "baidu";
    if (host === "campus.kuaishou.cn") return "kuaishou-ant";
    if (host === "jobs.bilibili.com") return "bilibili-ant";
    if (host === "careers.cvte.com") return "cvte-ant4";
    if (host.endsWith("jobs.feishu.cn")) {
      if (document.querySelector(".ud-formily-item, .ud__select, .ud__picker")) return "feishu-ud";
      return "feishu-atsx";
    }
    if (document.querySelector(".phoenix-select__input, .phoenix-radio-group")) return "beisen-phoenix";
    return "generic";
  }

  function clickLikeUser(element) {
    if (!element) return;
    const preventSubmit = (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    document.addEventListener("submit", preventSubmit, true);
    const common = { bubbles: true, cancelable: true, composed: true, view: window };
    try {
      if (typeof PointerEvent === "function") {
        element.dispatchEvent(new PointerEvent("pointerover", { ...common, pointerId: 1, pointerType: "mouse", isPrimary: true }));
        element.dispatchEvent(new PointerEvent("pointerdown", { ...common, pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons: 1 }));
      }
      element.dispatchEvent(new MouseEvent("mousedown", { ...common, button: 0, buttons: 1 }));
      if (typeof PointerEvent === "function") {
        element.dispatchEvent(new PointerEvent("pointerup", { ...common, pointerId: 1, pointerType: "mouse", isPrimary: true, button: 0, buttons: 0 }));
      }
      element.dispatchEvent(new MouseEvent("mouseup", { ...common, button: 0, buttons: 0 }));
      if (typeof element.click === "function") element.click();
      else element.dispatchEvent(new MouseEvent("click", { ...common, button: 0, buttons: 0 }));
    } finally {
      document.removeEventListener("submit", preventSubmit, true);
    }
  }

  function hoverLikeUser(element) {
    if (!element) return;
    const common = { bubbles: true, cancelable: true, composed: true, view: window };
    if (typeof PointerEvent === "function") {
      element.dispatchEvent(new PointerEvent("pointerover", { ...common, pointerId: 1, pointerType: "mouse", isPrimary: true }));
      element.dispatchEvent(new PointerEvent("pointerenter", { ...common, pointerId: 1, pointerType: "mouse", isPrimary: true, bubbles: false }));
    }
    element.dispatchEvent(new MouseEvent("mouseover", common));
    element.dispatchEvent(new MouseEvent("mouseenter", { ...common, bubbles: false }));
  }

  function getFormItem(element) {
    return element?.closest?.(formItemSelector) || null;
  }

  function textBeforeControl(formItem, element) {
    if (!formItem || !element) return "";
    let directChild = element;
    while (directChild?.parentElement && directChild.parentElement !== formItem) directChild = directChild.parentElement;
    const parts = [];
    for (const node of formItem.childNodes) {
      if (node === directChild) break;
      if (node.nodeType === Node.TEXT_NODE) parts.push(node.textContent || "");
      else if (node instanceof Element && !node.matches("button, [role='button']")) parts.push(textOf(node));
    }
    return parts.join(" ").replace(/[＊*]\s*/g, "").replace(/\s+/g, " ").trim();
  }

  function frameworkDateRole(element, formItem, frameworkText) {
    if (!formItem || detectPlatform() !== "moka-sd" || !element.matches?.("[class*='sd-Select-container']")) return frameworkText;
    const input = element.querySelector("input");
    const allControls = Array.from(formItem.querySelectorAll("[class*='sd-Select-container']"));
    const allIndex = allControls.indexOf(element);
    const storedDateControl = formItem.matches?.("[class*='date_info']") && allIndex >= 0 && allIndex % 2 === 0;
    if (!storedDateControl && !/年|year/i.test(`${input?.placeholder || ""} ${input?.getAttribute("aria-label") || ""}`)) return frameworkText;
    const yearControls = formItem.matches?.("[class*='date_info']")
      ? allControls.filter((_control, index) => index % 2 === 0)
      : allControls.filter((control) => /年|year/i.test(`${control.querySelector("input")?.placeholder || ""} ${control.querySelector("input")?.getAttribute("aria-label") || ""}`));
    if (yearControls.length >= 2) {
      const index = yearControls.indexOf(element);
      if (index === 0) return "开始时间";
      if (index === 1) return "结束时间";
    }
    return frameworkText;
  }

  function getLabel(element) {
    const formItem = getFormItem(element);
    if (element instanceof HTMLInputElement && element.type === "file") {
      const sectionTitle = textOf(element.closest?.(".createFormSection-mutiple")?.querySelector(".createFormSection-text"));
      if (sectionTitle) return sectionTitle.slice(0, 160);
    }
    if (element.matches?.("input[type='checkbox']")
      && !["moka-sd", "tencent-element"].includes(detectPlatform())) {
      const optionLabel = textOf(element.closest("label"));
      if (optionLabel) return optionLabel.slice(0, 160);
    }
    const tencentTitles = detectPlatform() === "tencent-element"
      ? Array.from(formItem?.querySelectorAll(":scope > .subtitle, :scope > .info_name") || [])
      : [];
    const frameworkTitle = tencentTitles.at(-1) || formItem?.querySelector(
      ".form-item__text, .form-item__label, .ant-form-item-label, .ant4-form-item-label, .el-form-item__label, .ivu-form-item-label, .arco-form-item-label, .semi-form-field-label, .ud-formily-item-label, .atsx-form-item-label, .brick-field-label, .info_name, [class*='field-label']"
    );
    let frameworkText = textOf(frameworkTitle);
    if (!frameworkText && ["moka-sd", "tencent-element", "bilibili-ant"].includes(detectPlatform())) {
      frameworkText = textBeforeControl(formItem, element);
    }
    frameworkText = frameworkDateRole(element, formItem, frameworkText);
    if (detectPlatform() === "moka-sd" && /证件号码|身份证号/.test(frameworkText)
      && element.matches?.("[class*='sd-Select-container']")) frameworkText = "证件类型";
    if (element.matches?.("input[type='checkbox']") && formItem?.matches?.("[class*='date_info']")) {
      const optionText = textOf(element.closest("label"));
      if (/至今/.test(optionText)) frameworkText = "至今";
    }
    if (/外语考试|语言等级/.test(frameworkText)) {
      if (element.matches?.(".el-dropdown")) frameworkText = "英语等级";
      else if (element instanceof HTMLInputElement && /分数|成绩/.test(element.placeholder || "")) frameworkText = "英语成绩";
    }
    if (/个人证件/.test(frameworkText)) {
      if (element.matches?.(".el-dropdown")) frameworkText = "证件类型";
      else if (element instanceof HTMLInputElement && /证件号码/.test(element.placeholder || "")) frameworkText = "证件号码";
    }
    if (/起止时间/.test(frameworkText) && element.matches?.(".atsx-date-picker-period-month-label")) {
      const labels = Array.from(element.parentElement?.querySelectorAll(".atsx-date-picker-period-month-label") || []);
      const index = labels.indexOf(element);
      if (index >= 0) frameworkText = index === 0 ? "开始时间" : "结束时间";
    }
    if (/起止时间/.test(frameworkText) && element instanceof HTMLInputElement) {
      const dateInputs = Array.from(formItem.querySelectorAll("input"))
        .filter((input) => getFormItem(input) === formItem && input.type !== "checkbox");
      const index = dateInputs.indexOf(element);
      if (dateInputs.length === 2 && index >= 0) frameworkText = index === 0 ? "开始时间" : "结束时间";
    }
    if (/起止时间/.test(frameworkText) && element.matches?.(".el-date-editor")) {
      const dateControls = Array.from(formItem.querySelectorAll(":scope .el-date-editor"));
      const index = dateControls.indexOf(element);
      if (index >= 0) frameworkText = index === 0 ? "开始时间" : "结束时间";
    }
    if (frameworkText) return frameworkText.slice(0, 160);

    const explicit = element.labels ? Array.from(element.labels).map(textOf).filter(Boolean).join(" ") : "";
    if (explicit) return explicit.slice(0, 160);
    const wrappingLabel = element.closest?.("label");
    return wrappingLabel ? textOf(wrappingLabel).slice(0, 160) : "";
  }

  function getSection(element) {
    const knownSections = [
      "投递意向", "个人信息", "求职意向", "教育经历", "项目经历", "课题项目经验",
      "工作经历", "实习经历", "获奖", "获奖情况", "竞赛获奖", "其他荣誉", "在校实践",
      "论文", "论文/专著", "专利", "论文/专著/专利", "证书", "技能", "IT技能", "技能证书", "语言能力", "作品", "作品信息",
      "自我评价", "基本信息", "简历附件", "附件", "家庭成员", "家庭情况", "亲属信息"
    ];
    const mokaSection = element.closest?.("[data-nav-id]");
    if (mokaSection) {
      const navId = mokaSection.getAttribute("data-nav-id") || "";
      const mapping = [
        [/education/i, "教育经历"], [/project/i, "项目经历"], [/intern|practice/i, "实习经历"],
        [/award|honor/i, "获奖情况"], [/work|career/i, "工作经历"], [/language/i, "语言能力"],
        [/family|relative/i, "家庭成员"], [/basic|personal/i, "个人信息"], [/apply|intention/i, "求职意向"]
      ];
      const mapped = mapping.find(([pattern]) => pattern.test(navId))?.[1];
      if (mapped) return mapped;
      const navText = textOf(mokaSection);
      const textMapping = [
        [/^赛事/, "竞赛获奖"], [/^作品/, "作品信息"], [/^论文|^期刊/, "论文"], [/^语言/, "语言能力"],
        [/^教育/, "教育经历"], [/^实习/, "实习经历"], [/^项目/, "项目经历"], [/^获奖|^荣誉/, "获奖情况"]
      ];
      const mappedByText = textMapping.find(([pattern]) => pattern.test(navText))?.[1];
      if (mappedByText) return mappedByText;
      const heading = textOf(mokaSection.querySelector("h1, h2, h3, [class*='title']"));
      const known = knownSections.find((section) => heading === section || heading.startsWith(`${section} `));
      if (known) return known;
    }
    const tencentSection = element.closest?.(".send_box[id^='page-resume-sections']");
    if (tencentSection) {
      const mapping = {
        "page-resume-sections2": "个人信息",
        "page-resume-sections3": "求职意向",
        "page-resume-sections4": "教育经历",
        "page-resume-sections5": "实习经历",
        "page-resume-sections6": "项目经历",
        "page-resume-sections8": "获奖情况",
        "page-resume-sections9": "AI应用技能",
        "page-resume-sections10": "语言能力",
        "page-resume-sections11": "作品信息",
        "page-resume-sections12": "资料证明人",
        "page-resume-sections13": "其他信息"
      };
      if (mapping[tencentSection.id]) return mapping[tencentSection.id];
      const heading = textOf(tencentSection.querySelector("h1, h2, h3, h4, [class*='title']"));
      const known = knownSections.find((section) => heading === section || heading.startsWith(`${section} `));
      if (known) return known;
    }
    const kuaishouSection = element.closest?.(".edit-resume-form-item");
    const kuaishouTitle = textOf(kuaishouSection?.querySelector(".edit-resume-form-item-title"));
    if (kuaishouTitle) return kuaishouTitle;
    const baiduSection = element.closest?.("[class*='resume-item__']");
    if (detectPlatform() === "baidu" && baiduSection) {
      const sectionText = textOf(baiduSection);
      const mapping = [
        [/^基础信息/, "基本信息"], [/^教育经历/, "教育经历"], [/^工作经历/, "工作经历"],
        [/^项目经验/, "项目经历"], [/^作品集/, "作品信息"]
      ];
      const mapped = mapping.find(([pattern]) => pattern.test(sectionText))?.[1];
      if (mapped) return mapped;
    }
    const cvteSection = element.closest?.(
      ".educations-section, .work-experience-section, .project-experience-section, .award-experience-section, .language-experience-section, .other-info-section, .terms-section"
    );
    if (cvteSection) {
      const mapping = [
        ["educations-section", "教育经历"], ["work-experience-section", "工作经历"],
        ["project-experience-section", "项目经历"], ["award-experience-section", "获奖情况"],
        ["language-experience-section", "语言能力"], ["other-info-section", "其他信息"],
        ["terms-section", "其他信息"]
      ];
      const mapped = mapping.find(([className]) => cvteSection.classList.contains(className))?.[1];
      if (mapped) return mapped;
    }
    const bilibiliCard = element.closest?.(".bili-resume-card");
    if (bilibiliCard) {
      const cardText = `${bilibiliCard.id || ""} ${textOf(bilibiliCard.querySelector("h1, h2, h3, h4, [class*='title']"))}`;
      const mapping = [
        [/教育/, "教育经历"], [/项目/, "项目经历"], [/实习/, "实习经历"], [/工作/, "工作经历"],
        [/获奖|荣誉/, "获奖情况"], [/语言/, "语言能力"], [/作品/, "作品信息"], [/技能/, "技能"]
      ];
      const mapped = mapping.find(([pattern]) => pattern.test(cardText))?.[1];
      if (mapped) return mapped;
    }
    const formilyModule = element.closest?.("[class*='applyFormModuleWrapper']");
    const formilyText = textOf(formilyModule);
    for (const section of knownSections) {
      if (formilyText === section || formilyText.startsWith(`${section} `)) return section;
    }
    const feishuGroup = element.closest?.(".register-form-group-wrapper");
    const feishuText = textOf(feishuGroup);
    if (/学校名称/.test(feishuText) && /学历|专业/.test(feishuText)) return "教育经历";
    if (/公司名称/.test(feishuText) && /职位名称|描述/.test(feishuText)) return "实习经历";
    const atsxSection = element.closest?.(".createFormSection-mutiple");
    const atsxSectionTitle = textOf(atsxSection?.querySelector(".createFormSection-text"));
    if (atsxSectionTitle) return atsxSectionTitle;

    let node = getFormItem(element) || element;
    for (let depth = 0; node && depth < 28; depth += 1, node = node.parentElement) {
      let sibling = node.previousElementSibling;
      for (let count = 0; sibling && count < 4; count += 1, sibling = sibling.previousElementSibling) {
        const previousText = textOf(sibling);
        const sectionAlias = [
          [/^英语能力/, "语言能力"], [/^其他外语能力/, "语言能力"], [/^计算机能力/, "技能"],
          [/^公司内部亲属关系/, "个人信息"]
        ].find(([pattern]) => pattern.test(previousText))?.[1];
        if (sectionAlias) return sectionAlias;
        for (const section of knownSections) {
          if (previousText === section || previousText.startsWith(`${section} `)) return section;
        }
      }
      const heading = node.querySelector?.(":scope > legend, :scope > h1, :scope > h2, :scope > h3, :scope > [class*='title']");
      const headingText = textOf(heading);
      for (const section of knownSections) {
        if (headingText === section || headingText.startsWith(`${section} `)) return section;
      }
    }
    return "";
  }

  function getContext(element) {
    const formItem = getFormItem(element);
    if (formItem) return textOf(formItem).slice(0, 240);
    return textOf(element.parentElement).slice(0, 180);
  }

  function getRecordIndex(element, section) {
    if (!repeatSections.has(section)) return undefined;
    const atsxIdentity = [
      element.id,
      element.getAttribute?.("data-cy"),
      element.closest?.("[data-cy]")?.getAttribute("data-cy")
    ].filter(Boolean).join(" ");
    const atsxIndex = /(?:education|career|internship|project|work|award|language|sns)\[(\d+)\]/.exec(atsxIdentity);
    if (atsxIndex) return Number(atsxIndex[1]);
    if (detectPlatform() === "baidu") {
      const baiduIndex = /field-\d+-(?:school|edudate|academic|major|subjectName|position|subjectDate|subjectDesc|positionDesc)(\d+)/.exec(String(getFormItem(element)?.className || ""));
      if (baiduIndex) return Number(baiduIndex[1]);
    }
    const indexedIdentity = [element.id, element.getAttribute?.("name")].filter(Boolean).join(" ");
    const indexedMatch = /(?:educations|projects|internships|awards|languages|publications|skills)[_-](\d+)[_-]/i.exec(indexedIdentity);
    if (indexedMatch) return Number(indexedMatch[1]);
    const atsxRecord = element.closest?.(".resumeEditForm-item");
    const atsxSection = atsxRecord?.closest(".createFormSection-mutiple");
    if (atsxSection) {
      const records = Array.from(atsxSection.querySelectorAll(".resumeEditForm-item"))
        .filter((item) => item.closest(".createFormSection-mutiple") === atsxSection
          && !item.parentElement.closest(".resumeEditForm-item"));
      const index = records.findIndex((item) => item.contains(element));
      if (index >= 0) return index;
    }
    const tencentList = element.closest?.(".info_list");
    const tencentListParent = tencentList?.parentElement;
    if (tencentList && tencentListParent) {
      const lists = Array.from(tencentListParent.children).filter((child) => child.classList?.contains("info_list"));
      if (lists.length >= 2) {
        const index = lists.indexOf(tencentList);
        if (index >= 0) return index;
      }
    }
    const tencentRecord = element.closest?.(".experience_box");
    const tencentSection = tencentRecord?.closest?.(".send_box");
    if (tencentRecord && tencentSection) {
      const records = Array.from(tencentSection.querySelectorAll(":scope .experience_box"));
      const index = records.indexOf(tencentRecord);
      if (index >= 0) return index;
    }
    const mokaSection = element.closest?.("[data-nav-id]");
    const mokaField = getFormItem(element);
    if (mokaSection && mokaField) {
      let record = mokaField.parentElement;
      while (record && record.parentElement && mokaSection.contains(record)) {
        const siblings = Array.from(record.parentElement.children).filter((child) =>
          child.querySelectorAll?.("[class*='apply-field-']").length >= 2
        );
        if (siblings.length >= 2) {
          const index = siblings.indexOf(record);
          if (index >= 0) return index;
        }
        if (record.parentElement === mokaSection) break;
        record = record.parentElement;
      }
    }
    const feishuCard = element.closest?.("[class*='apply-form-array-card__']");
    if (feishuCard?.parentElement) {
      const records = Array.from(feishuCard.parentElement.children)
        .filter((child) => String(child.className).includes("apply-form-array-card__"));
      const index = records.indexOf(feishuCard);
      if (index >= 0) return index;
    }
    const kuaishouRecord = element.closest?.(".eduction-container, [class*='education-container'], [class*='project-container'], [class*='internship-container']");
    if (kuaishouRecord?.parentElement) {
      const classToken = Array.from(kuaishouRecord.classList).find((name) => /container/.test(name));
      const records = Array.from(kuaishouRecord.parentElement.children)
        .filter((child) => classToken ? child.classList?.contains(classToken) : false);
      const index = records.indexOf(kuaishouRecord);
      if (index >= 0) return index;
    }
    const kuaishouGenericRecord = element.closest?.(".edit-resume-form-item-container");
    if (kuaishouGenericRecord?.parentElement) {
      const records = Array.from(kuaishouGenericRecord.parentElement.children)
        .filter((child) => child.classList?.contains("edit-resume-form-item-container"));
      const index = records.indexOf(kuaishouGenericRecord);
      if (index >= 0) return index;
    }
    const bilibiliRecord = element.closest?.(".bili-form-multiple");
    if (bilibiliRecord?.parentElement) {
      const records = Array.from(bilibiliRecord.parentElement.children)
        .filter((child) => child.classList?.contains("bili-form-multiple"));
      const index = records.indexOf(bilibiliRecord);
      if (index >= 0) return index;
    }
    const cvteRecord = element.closest?.(".education-item, .work-experience-item, .project-experience-item, .award-experience-item, .language-experience-item");
    if (cvteRecord?.parentElement) {
      const baseClass = Array.from(cvteRecord.classList).find((name) => /(?:education|experience)-item/.test(name));
      const records = Array.from(cvteRecord.parentElement.children)
        .filter((child) => baseClass && child.classList?.contains(baseClass));
      const index = records.indexOf(cvteRecord);
      if (index >= 0) return index;
    }
    if (detectPlatform() === "beisen-phoenix") {
      const record = element.closest?.(".ux-standard-form");
      const formId = record?.querySelector(":scope > .form[id]")?.id || record?.querySelector(".form[id]")?.id || "";
      if (record && formId) {
        const records = Array.from(document.querySelectorAll(".ux-standard-form"))
          .filter((item) => (item.querySelector(":scope > .form[id]")?.id || item.querySelector(".form[id]")?.id || "") === formId);
        const index = records.indexOf(record);
        if (index >= 0) return index;
      }
    }
    const part = element.closest?.(".form-part");
    if (!part) return 0;
    let record = part;
    for (let depth = 0; record?.parentElement && depth < 10; depth += 1, record = record.parentElement) {
      const records = Array.from(record.parentElement.children).filter((child) => child.querySelector?.(".form-part"));
      if (records.length < 2) continue;
      const index = records.findIndex((child) => child.contains(part));
      if (index >= 0) return index;
    }
    return 0;
  }

  function isRequired(element) {
    const formItem = getFormItem(element);
    return Boolean(formItem?.querySelector(
      ".form-item__required, .atsx-form-item-required, .subtitle.must, [class*='required'], [aria-required='true']"
    ) || element.getAttribute?.("required") !== null);
  }

  function ensureFieldId(element) {
    const existing = element.getAttribute(markerAttribute);
    if (existing && (!assignedFieldIds.has(existing) || assignedFieldIds.get(existing) === element)) {
      assignedFieldIds.set(existing, element);
      return existing;
    }
    let fieldId;
    do fieldId = `resume-field-${nextFieldId++}`;
    while (assignedFieldIds.has(fieldId));
    element.setAttribute(markerAttribute, fieldId);
    assignedFieldIds.set(fieldId, element);
    return fieldId;
  }

  function genericCalendarRoot(element) {
    if (!(element instanceof Element)) return null;
    let node = element;
    for (let depth = 0; node && depth < 7; depth += 1, node = node.parentElement) {
      if (globalThis.ResumeFillerCore.isAntPickerRootClass(node.className)) return node;
    }
    const root = element.matches?.(genericCalendarRootSelector)
      ? element
      : element.closest?.(genericCalendarRootSelector);
    if (root && !root.matches?.(".ant-picker, .ud__picker, .el-date-editor")) return root;
    if (element instanceof HTMLInputElement
      && element.type !== "date"
      && element.getAttribute("aria-haspopup") === "dialog"
      && /日期|时间|年月|date|calendar/i.test(`${getLabel(element)} ${element.placeholder || ""} ${element.className || ""}`)) {
      return element;
    }
    return null;
  }

  function primaryControls(formItem) {
    const platform = detectPlatform();
    if (platform === "baidu") {
      const radioGroup = formItem.querySelector(".brick-radio-group");
      if (radioGroup) return [radioGroup];
      const brickSelect = formItem.querySelector(".brick-select");
      if (brickSelect) return [brickSelect];
      const antSelect = formItem.querySelector(".ant-select");
      if (antSelect) return [antSelect];
      const controls = Array.from(formItem.querySelectorAll(
        "textarea, input:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button']):not([type='reset']):not([type='image'])"
      )).filter((element) => getFormItem(element) === formItem && !element.closest(".brick-select, .ant-select"));
      const textarea = controls.find((element) => element instanceof HTMLTextAreaElement && element.placeholder)
        || controls.find((element) => element instanceof HTMLTextAreaElement);
      return controls.filter((element) => !(element instanceof HTMLTextAreaElement) || element === textarea);
    }
    if (platform === "moka-sd") {
      const results = [];
      const phoneInput = formItem.querySelector("input[placeholder*='手机号'], input[type='tel']");
      const allSelectRoots = Array.from(formItem.querySelectorAll("[class*='sd-Select-container']"));
      const selectRoots = allSelectRoots
        .filter((element) => getFormItem(element) === formItem)
        .filter((element) => {
          if (phoneInput) return false;
          const dateIndex = allSelectRoots.indexOf(element);
          if (formItem.matches?.("[class*='date_info']")) return dateIndex % 2 === 0;
          const input = element.querySelector("input");
          const hint = `${input?.placeholder || ""} ${input?.getAttribute("aria-label") || ""}`;
          return !/月|month/i.test(hint);
        });
      results.push(...selectRoots);
      results.push(...Array.from(formItem.querySelectorAll("textarea, input:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button']):not([type='reset']):not([type='image'])"))
        .filter((element) => getFormItem(element) === formItem && !element.closest("[class*='sd-Select-container']"))
        .filter((element, index, inputs) => element.type !== "radio" || inputs.findIndex((item) => item.type === "radio" && (item.name || "__group") === (element.name || "__group")) === index));
      return results;
    }
    if (platform === "tencent-element") {
      const results = Array.from(formItem.querySelectorAll(
        ".el-radio-group, .el-select, .el-dropdown, .el-cascader, .el-date-editor, textarea, input:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button']):not([type='reset']):not([type='image'])"
      )).filter((element) => {
        if (getFormItem(element) !== formItem) return false;
        if (element instanceof HTMLInputElement && element.closest(".el-radio-group, .el-select, .el-dropdown, .el-cascader, .el-date-editor")) return false;
        const phoneInput = formItem.querySelector("input[placeholder*='手机号码'], input[type='tel']");
        if (phoneInput && element.matches?.(".el-select")) return false;
        return true;
      });
      return results.filter((element, index) => !results.some((parent, parentIndex) => parentIndex < index && parent.contains(element)));
    }
    const selector = [
      ".phoenix-radio-group", ".ud__radio-group", ".atsx-radio-group",
      ".ant-select", ".ant4-select", ".ud__select", ".atsx-select", ".ant-picker", ".ud__picker",
      genericCalendarDiscoverySelector,
      "[role='combobox']:not(input)",
      ".atsx-date-picker-period-month-label",
      "input.phoenix-select__input", "select", "textarea",
      "input:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button']):not([type='reset']):not([type='image'])",
      "[contenteditable='true']"
    ].join(",");
    const controls = Array.from(formItem.querySelectorAll(selector)).filter((element) => {
      if (getFormItem(element) !== formItem) return false;
      // 飞书起止年月是一个复合容器；保留内部开始/结束标签作为两个独立字段。
      if (globalThis.ResumeFillerCore.isFeishuPeriodRootClass(element.className)) return false;
      const antPhoneInput = Array.from(formItem.querySelectorAll("input:not([type='hidden'])"))
        .find((input) => !input.closest(".ant-select, .ant4-select") && /手机/.test(textOf(formItem)));
      if (antPhoneInput && element.closest?.(".ant-select, .ant4-select")) {
        // Ant 系招聘页常把国家码和手机号放在同一个表单项中；国家码不是独立简历字段。
        return false;
      }
      if (element.matches?.(".ant-picker, .ud__picker")) {
        const dateInputs = element.querySelectorAll("input:not([type='hidden'])");
        if (dateInputs.length > 1) return false;
      }
      if (element.matches("input[type='radio']") && element.closest(".phoenix-radio-group, .ud__radio-group, .atsx-radio-group")) return false;
      if (element.matches("input[type='radio']")) {
        const radios = Array.from(formItem.querySelectorAll("input[type='radio']"));
        const group = element.name || "__group";
        if (radios.findIndex((radio) => (radio.name || "__group") === group) !== radios.indexOf(element)) return false;
      }
      if (element instanceof HTMLInputElement) {
        if (element.closest(".ant-select, .ant4-select, .ud__select, .atsx-select")) return false;
        const calendar = genericCalendarRoot(element);
        if (calendar && calendar !== element) return false;
        const picker = element.closest(".ant-picker, .ud__picker");
        if (picker && picker.querySelectorAll("input:not([type='hidden'])").length <= 1) return false;
        const parentCombobox = element.closest("[role='combobox']");
        if (parentCombobox && parentCombobox !== element) return false;
      }
      return true;
    });
    return controls.filter((element) => !controls.some((parent) => parent !== element && parent.contains(element)));
  }

  function controlKind(element) {
    if (element.matches?.("input[class*='sd-Input-tag-input']") && getFormItem(element)?.matches?.("[class*='location_info']")) return "moka-location";
    if (element.matches?.(".brick-radio-group")) return "brick-radio";
    if (element.matches?.(".brick-select")) return "brick-select";
    if (element.matches?.("[class*='sd-Select-container']")) {
      const input = element.querySelector("input");
      const formItem = getFormItem(element);
      const dateControls = Array.from(formItem?.querySelectorAll("[class*='sd-Select-container']") || []);
      const dateIndex = dateControls.indexOf(element);
      const isDateYear = formItem?.matches?.("[class*='date_info']") && dateIndex >= 0 && dateIndex % 2 === 0;
      return isDateYear || /年|year/i.test(`${input?.placeholder || ""} ${input?.getAttribute("aria-label") || ""}`)
        ? "moka-month" : "moka-select";
    }
    if (element.matches?.(".el-radio-group")) return "element-radio";
    if (element.matches?.(".el-dropdown")) return "element-dropdown";
    if (element.matches?.(".el-cascader")) return "element-cascader";
    if (element.matches?.(".el-date-editor")) return "element-date";
    if (element.matches?.(".el-select")) return "element-select";
    if (element.matches?.(".phoenix-radio-group")) return "phoenix-radio";
    if (element.matches?.(".ud__radio-group")) return "ud-radio";
    if (element.matches?.(".atsx-radio-group")) return "atsx-radio";
    if (element.matches?.(".atsx-date-picker-period-month-label")) return "feishu-month";
    if (element.matches?.(".atsx-date-picker") && element.querySelector?.("input[placeholder='YYYY']")) return "feishu-year";
    if (element.matches?.("input.phoenix-select__input")) return "phoenix-select";
    if (element.matches?.(".ant-picker") || element.closest?.(".ant-picker")) return "ant-date";
    if (element.matches?.(".ud__picker") || element.closest?.(".ud__picker")) return "ud-date";
    if (genericCalendarRoot(element)) return "calendar-date";
    if (element.matches?.(".ant-select, .ant4-select") || element.closest?.(".ant-select, .ant4-select")) {
      return element.closest?.(".ant-select-multiple, .ant4-select-multiple") || element.matches?.(".ant-select-multiple, .ant4-select-multiple") ? "multi-select" : "ant-select";
    }
    if (element.matches?.(".ud__select") || element.closest?.(".ud__select")) {
      return element.closest?.(".ud__select__selector-multiple") || element.querySelector?.(".ud__select__selector-multiple") ? "multi-select" : "ud-select";
    }
    if (element.matches?.(".atsx-select") || element.closest?.(".atsx-select")) return "atsx-select";
    if (element.getAttribute?.("role") === "combobox") return "combobox";
    if (element instanceof HTMLSelectElement) return "select";
    if (element instanceof HTMLTextAreaElement) return "textarea";
    if (element instanceof HTMLInputElement && element.type === "file") return "file";
    if (element instanceof HTMLInputElement && element.type === "radio") return "radio";
    if (element instanceof HTMLInputElement && element.type === "checkbox") return "checkbox";
    if (element instanceof HTMLInputElement) return "input";
    if (element.isContentEditable) return "contenteditable";
    return "unknown";
  }

  function collectCandidates(root = document) {
    const candidates = [];
    const seen = new Set();
    root.querySelectorAll(formItemSelector).forEach((item) => {
      if (!isVisible(item)) return;
      for (const control of primaryControls(item)) {
        if (seen.has(control) || isDisabledControl(control) || (!isVisible(control) && !(control instanceof HTMLInputElement && control.type === "file"))) continue;
        candidates.push(control);
        seen.add(control);
      }
    });

    const selector = [
      genericCalendarDiscoverySelector,
      "input:not([type='hidden']):not([type='password']):not([type='submit']):not([type='button']):not([type='reset']):not([type='image'])",
      "textarea", "select", "[role='combobox']:not(input)", "[contenteditable='true']"
    ].join(",");
    root.querySelectorAll(selector).forEach((element) => {
      if (seen.has(element) || getFormItem(element) || element.disabled || element.readOnly || !isVisible(element)) return;
      if (element.closest(".ant-select, .ant4-select, .ud__select, .atsx-select, .ant-picker, .ud__picker")) return;
      const calendar = genericCalendarRoot(element);
      if (calendar && calendar !== element) return;
      if (!getLabel(element)
        && !element.getAttribute("aria-label")
        && !element.getAttribute("placeholder")
        && !element.getAttribute("name")
        && !element.id) return;
      candidates.push(element);
      seen.add(element);
    });
    root.querySelectorAll("input[type='file']").forEach((element) => {
      if (seen.has(element) || element.disabled) return;
      candidates.push(element);
      seen.add(element);
    });
    return candidates.filter((element) => !isDisabledControl(element)
      && globalThis.ResumeFillerCore.fieldDisposition({
        label: getLabel(element), placeholder: element.getAttribute("placeholder"), className: element.className
      }) !== "ignore");
  }

  function isDisabledControl(element) {
    return Boolean(element.disabled || element.getAttribute?.("aria-disabled") === "true"
      || element.closest?.("[class*='sd-Select-containerDisabled'], [aria-disabled='true']"));
  }

  function selectedPhoenixRadio(element) {
    const selected = Array.from(element.querySelectorAll(".phoenix-radio")).find((option) =>
      option.getAttribute("aria-checked") === "true" || /checked|selected|active/.test(option.className)
    );
    return selected ? textOf(selected) : "";
  }

  function selectedUdRadio(element) {
    const checked = element.querySelector("input[type='radio']:checked");
    if (checked) return textOf(checked.closest("label"));
    const wrapper = element.querySelector(".ud__radio__wrapper--checked");
    return textOf(wrapper);
  }

  function selectedAtsxRadio(element) {
    const checked = element.querySelector("input[type='radio']:checked");
    return checked ? textOf(checked.closest("label") || checked.parentElement) : "";
  }

  function selectedBrickRadio(element) {
    const checked = element.querySelector("input[type='radio']:checked");
    if (checked) return textOf(checked.closest("label") || checked.parentElement);
    const selected = element.querySelector(".brick-radio-checked, [class*='radio-checked']");
    return textOf(selected);
  }

  function selectedBrickValue(element) {
    const selected = element.querySelector(".brick-select-selection-selected[title]")
      || element.querySelector(".brick-select-selection-selected");
    const value = selected?.getAttribute("title") || textOf(selected);
    return value && !/请选择|请输入/.test(value) ? value : "";
  }

  function selectedFeishuMonth(element) {
    const year = textOf(element.querySelector("[data-cy='year']"));
    const month = textOf(element.querySelector("[data-cy='month']"));
    return /^\d{4}$/.test(year) && /^\d{1,2}$/.test(month)
      ? `${year}-${month.padStart(2, "0")}`
      : "";
  }

  function selectedComboboxValue(element) {
    const scope = element.matches?.(".ant-select, .ant4-select, .ud__select, .atsx-select")
      ? element
      : element.closest?.(".ant-select, .ant4-select, .ud__select, .atsx-select") || element;
    const selectedTexts = Array.from(scope.querySelectorAll?.(
      ".ant-select-selection-item, .ant4-select-selection-item, .ud__select__selector__selectItem, [class*='selection-item'], [class*='selection__choice'], [class*='selected-value'], [class*='selector__selection']"
    ) || []).map(textOf).filter((text) => text && !/请选择|请输入/.test(text));
    if (selectedTexts.length) return selectedTexts.join("、");
    const input = scope.querySelector?.(
      "input.atsx-select-search__field, input.ant-select-selection-search-input, input.ant4-select-selection-search-input, input.ud__select__selector__search__input, input.ud__native-input"
    );
    if (scope.dataset?.localResumeCommittedValue && input?.value) return input.value;
    const hasOldPluginMarker = scope.matches?.(
      ".local-resume-match-high, .local-resume-match-medium, .local-resume-match-low, .local-resume-filled"
    );
    if (scope.matches?.(".ud__select") && input?.value && !hasOldPluginMarker) return input.value;
    const displayed = textOf(scope);
    return displayed && !/请选择|请输入/.test(displayed) ? displayed : "";
  }

  function selectedMokaValue(element) {
    const display = textOf(element.querySelector("[class*='display-value']"));
    if (display && !/请选择/.test(display)) return display;
    const values = Array.from(element.querySelectorAll("input"))
      .map((input) => input.value.trim())
      .filter((value) => value && !/请选择/.test(value));
    if (values.length) return values.join(" ");
    const selected = textOf(element.querySelector("[class*='selected'], [class*='value']"));
    return selected && !/请选择/.test(selected) ? selected : "";
  }

  function selectedMokaMonth(element) {
    const formItem = getFormItem(element);
    const selects = Array.from(formItem?.querySelectorAll("[class*='sd-Select-container']") || []);
    const index = selects.indexOf(element);
    const displayValue = (control) => control?.querySelector("input")?.value
      || textOf(control?.querySelector("[class*='display-value']"))
      || textOf(control);
    const year = displayValue(element);
    const monthControl = selects.slice(index + 1).find((control) =>
      /月|month/i.test(`${control.querySelector("input")?.placeholder || ""} ${control.querySelector("input")?.getAttribute("aria-label") || ""}`)
    ) || selects[index + 1];
    const month = displayValue(monthControl);
    const yearNumber = year.match(/\d{4}/)?.[0];
    const monthNumber = month.match(/\d{1,2}/)?.[0];
    return yearNumber && monthNumber
      ? `${yearNumber}-${monthNumber.padStart(2, "0")}`
      : element.dataset?.localResumeCommittedValue || "";
  }

  function selectedElementValue(element) {
    const inputs = Array.from(element.querySelectorAll("input:not([type='hidden']), textarea"));
    const values = inputs.map((input) => input.value.trim()).filter(Boolean);
    if (values.length) return values.join(" - ");
    const selected = textOf(element.querySelector(
      ".el-select__tags, .el-radio.is-checked, .is-checked, .place-select, .el-dropdown-link"
    ));
    return selected && !/请选择|请输入/.test(selected) ? selected : "";
  }

  function selectedPhoenixValue(element) {
    const formItem = getFormItem(element);
    if (formItem?.querySelector(".phoenix-select__placeHolder--show")) return "";
    const display = formItem?.querySelector(
      ".phoenix-select__singleLabel, .phoenix-select__multiLabel, .phoenix-select__calcEle, .phoenix-select__content"
    );
    const value = textOf(display);
    return value === "请选择" ? "" : value;
  }

  function currentValue(element) {
    const kind = controlKind(element);
    if (kind === "moka-location") return selectedMokaLocation(element);
    if (kind === "moka-month") return selectedMokaMonth(element);
    if (kind === "moka-select") return selectedMokaValue(element);
    if (["element-select", "element-dropdown", "element-cascader", "element-date", "element-radio"].includes(kind)) return selectedElementValue(element);
    if (kind === "phoenix-radio") return selectedPhoenixRadio(element);
    if (kind === "brick-radio") return selectedBrickRadio(element);
    if (kind === "brick-select") return selectedBrickValue(element);
    if (kind === "ud-radio") return selectedUdRadio(element);
    if (kind === "atsx-radio") return selectedAtsxRadio(element);
    if (kind === "feishu-month") return selectedFeishuMonth(element);
    if (kind === "phoenix-select") return selectedPhoenixValue(element) || String(element.value || "");
    if (["combobox", "ant-select", "ud-select", "atsx-select", "multi-select"].includes(kind)) return selectedComboboxValue(element) || String(element.value || "");
    if (["feishu-year", "ant-date", "ud-date", "calendar-date"].includes(kind)) return String(element.querySelector?.("input")?.value || element.value || "");
    if (kind === "radio") {
      const radios = element.name
        ? Array.from(document.querySelectorAll(`input[type='radio'][name="${CSS.escape(element.name)}"]`))
        : Array.from(getFormItem(element)?.querySelectorAll("input[type='radio']") || [element]);
      const selected = radios.find((radio) => radio.checked);
      return selected ? (selected.value || textOf(selected.closest("label") || selected.parentElement)) : "";
    }
    if (kind === "checkbox") return element.checked ? (element.value || "true") : "";
    if (kind === "file") return Array.from(element.files || []).map((file) => file.name).join(", ");
    if (element.isContentEditable) return element.textContent || "";
    return "value" in element ? String(element.value || "") : "";
  }

  function toMeta(element) {
    const section = getSection(element);
    const value = currentValue(element);
    const kind = controlKind(element);
    return {
      fieldId: ensureFieldId(element),
      tag: element.tagName.toLowerCase(),
      type: element instanceof HTMLInputElement ? element.type : element.getAttribute("role") || "",
      controlKind: kind,
      controlCategory: globalThis.ResumeFillerCore.controlCategory(kind),
      label: getLabel(element),
      section,
      recordIndex: getRecordIndex(element, section),
      required: isRequired(element),
      ariaLabel: element.getAttribute("aria-label") || "",
      placeholder: element.getAttribute("placeholder") || "",
      name: element.getAttribute("name") || "",
      id: element.id || "",
      context: getContext(element),
      platform: detectPlatform(),
      frameUrl: location.href,
      controlFingerprint: [
        element.tagName.toLowerCase(),
        String(element.className || "").split(/\s+/).slice(0, 4).join("."),
        getFormItem(element)?.className || ""
      ].filter(Boolean).join(" | ").slice(0, 260),
      currentValue: value,
      hasValue: Boolean(value.trim())
    };
  }

  function clearHighlights() {
    document.querySelectorAll(`[${markerAttribute}]`).forEach((element) => {
      element.classList.remove("local-resume-match-high", "local-resume-match-medium", "local-resume-match-low", "local-resume-filled");
    });
  }

  function ensureHighlightStyle() {
    if (document.getElementById("local-resume-filler-style")) return;
    const style = document.createElement("style");
    style.id = "local-resume-filler-style";
    style.textContent = `
      .local-resume-match-high { outline: 2px solid #17846f !important; outline-offset: 2px !important; }
      .local-resume-match-medium { outline: 2px solid #d99525 !important; outline-offset: 2px !important; }
      .local-resume-match-low { outline: 2px dashed #c55f52 !important; outline-offset: 2px !important; }
      .local-resume-filled { outline: 2px solid #2563eb !important; outline-offset: 2px !important; }
      .local-resume-filling { outline: 3px solid #f59e0b !important; outline-offset: 3px !important; transition: outline-color 120ms ease; }
    `;
    document.documentElement.appendChild(style);
  }

  function highlight(matches) {
    ensureHighlightStyle();
    clearHighlights();
    for (const match of matches) {
      const element = document.querySelector(`[${markerAttribute}="${CSS.escape(match.fieldId)}"]`);
      if (element) element.classList.add(`local-resume-match-${match.confidence}`);
    }
  }

  function dispatchInputEvents(element) {
    element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    element.dispatchEvent(new Event("blur", { bubbles: true, composed: true }));
  }

  async function followFillProgress(element) {
    const target = getFormItem(element) || element;
    target.classList.add("local-resume-filling");
    const rect = target.getBoundingClientRect();
    let inView = rect.top >= 0 && rect.bottom <= window.innerHeight
      && rect.left >= 0 && rect.right <= window.innerWidth;
    for (let parent = target.parentElement; inView && parent && parent !== document.body; parent = parent.parentElement) {
      const style = getComputedStyle(parent);
      if (!/auto|scroll|hidden|clip/.test(`${style.overflow} ${style.overflowY} ${style.overflowX}`)) continue;
      const bounds = parent.getBoundingClientRect();
      inView = rect.top >= bounds.top && rect.bottom <= bounds.bottom && rect.left >= bounds.left && rect.right <= bounds.right;
    }
    if (!inView) {
      target.scrollIntoView({ behavior: "instant", block: "center", inline: "nearest" });
      await delay(16);
    }
    return target;
  }

  function validationErrorFor(element) {
    const formItem = getFormItem(element);
    if (!formItem) return "";
    const error = formItem.querySelector(
      ".ant-form-item-explain-error, .ud-formily-item-feedback-error, .atsx-form-item-explain-error, .el-form-item__error, [role='alert']"
    );
    const errorText = textOf(error);
    if (errorText) return errorText;
    return /has-error|status-error|is-error/.test(String(formItem.className)) ? "页面校验未通过" : "";
  }

  function setNativeValue(element, value) {
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const descriptor = Object.getOwnPropertyDescriptor(prototype, "value");
    if (descriptor?.set) descriptor.set.call(element, value);
    else element.value = value;
  }

  function rememberOriginal(element, fieldId) {
    if (originalValues.has(fieldId)) return;
    originalValues.set(fieldId, { kind: controlKind(element), value: currentValue(element), checked: element.checked });
  }

  function normalizedIncludes(actual, expected) {
    const normalize = globalThis.ResumeFillerCore.normalize;
    const a = normalize(actual);
    const b = normalize(expected);
    return Boolean(a && b && (a === b || a.includes(b) || b.includes(a)));
  }

  function visibleElements(selector) {
    return Array.from(document.querySelectorAll(selector)).filter(isVisible);
  }

  async function dismissPhoenixCalendar(element, calendar) {
    let activeCalendar = visibleElements(".phoenix-calendar").at(-1) || calendar;
    if (activeCalendar && isVisible(activeCalendar)) {
      const label = getFormItem(element)?.querySelector(".form-item__text, .form-item__label");
      clickLikeUser(label);
      await delay(100);
    }
    activeCalendar = visibleElements(".phoenix-calendar").at(-1);
    if (activeCalendar && isVisible(activeCalendar)) {
      const eventInit = { key: "Escape", code: "Escape", bubbles: true };
      activeCalendar.dispatchEvent(new KeyboardEvent("keydown", eventInit));
      element.dispatchEvent(new KeyboardEvent("keydown", eventInit));
      document.dispatchEvent(new KeyboardEvent("keydown", eventInit));
      await delay(60);
    }
    element.blur();
  }

  function findPhoenixPickerAction(actionText) {
    const search = visibleElements("input[placeholder='搜索']").at(-1);
    let scope = search?.parentElement;
    while (scope && scope !== document.body) {
      const controls = Array.from(scope.querySelectorAll("button, [role='button'], span, div")).filter(isVisible);
      const hasConfirm = controls.some((item) => textOf(item) === "确定");
      const action = controls
        .filter((item) => textOf(item) === actionText)
        .sort((a, b) => a.children.length - b.children.length)[0];
      if (hasConfirm && action) return action;
      scope = scope.parentElement;
    }
    return null;
  }

  async function dismissPhoenixPicker(element) {
    const cancel = findPhoenixPickerAction("取消");
    if (cancel) {
      cancel.click();
      await delay(150);
      return;
    }
    const eventInit = { key: "Escape", code: "Escape", bubbles: true };
    element.dispatchEvent(new KeyboardEvent("keydown", eventInit));
    document.dispatchEvent(new KeyboardEvent("keydown", eventInit));
    element.blur();
    await delay(60);
  }

  function optionScore(text, value) {
    const normalize = globalThis.ResumeFillerCore.normalize;
    const option = normalize(text);
    const target = normalize(value);
    if (!option || !target) return 0;
    if (option === target) return 100;
    if (option.includes(target) || target.includes(option)) return 80;
    return 0;
  }

  function assignmentOptionScore(text, value, assignment) {
    return globalThis.ResumeFillerCore.choiceOptionScore(text, value, assignment?.key, assignment?.label);
  }

  function isRegionAssignment(assignment) {
    return ["nativePlace", "currentResidence", "preferredCity", "educationLocation"].includes(assignment?.key)
      || /籍贯|家乡|生源地|现居住地|现居地|当前居住地|所在地点|目前就读地|学校所在地|期望工作地点|意向城市/.test(assignment?.label || "");
  }

  function isSalaryAssignment(assignment) {
    return assignment?.key === "desiredSalary" || /期望月薪|期望薪资|期望薪酬/.test(assignment?.label || "");
  }

  async function fillPhoenixRadio(element, value, assignment) {
    const options = Array.from(element.querySelectorAll(".phoenix-radio"));
    const target = options.map((option) => ({ option, score: assignmentOptionScore(textOf(option), value, assignment) }))
      .sort((a, b) => b.score - a.score)[0];
    if (!target?.score) return { ok: false, reason: "找不到对应单选项" };
    const chosenText = textOf(target.option);
    target.option.click();
    await delay(80);
    const actual = selectedPhoenixRadio(element);
    return { ok: normalizedIncludes(actual, chosenText), actual, reason: actual ? "单选项未保持选中" : "未检测到选中状态" };
  }

  async function fillNativeRadio(element, value, assignment) {
    const radios = element.name
      ? Array.from(document.querySelectorAll(`input[type='radio'][name="${CSS.escape(element.name)}"]`))
      : Array.from(getFormItem(element)?.querySelectorAll("input[type='radio']") || [element]);
    const target = radios.map((radio) => ({
      radio,
      score: assignmentOptionScore(`${radio.value || ""} ${textOf(radio.closest("label") || radio.parentElement)}`, value, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) return { ok: false, reason: "找不到对应单选项" };
    target.radio.click();
    dispatchInputEvents(target.radio);
    await delay(40);
    return { ok: target.radio.checked, actual: target.radio.value, reason: "单选项未保持选中" };
  }

  async function fillBrickRadio(element, value, assignment) {
    const options = Array.from(element.querySelectorAll("label.brick-radio, label"));
    const target = options.map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), value, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) return { ok: false, reason: "百度单选组中没有对应选项" };
    clickLikeUser(target.option);
    await delay(100);
    const actual = selectedBrickRadio(element);
    return { ok: normalizedIncludes(actual, textOf(target.option)), actual, reason: "百度单选项未保持选中" };
  }

  async function fillBrickSelect(element, value, assignment) {
    clickLikeUser(element.querySelector(".brick-select-selection") || element);
    await delay(180);
    const options = visibleElements(".brick-select-option, .brick-menu-item");
    const target = options.map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), value, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) {
      await dismissFrameworkPicker(element);
      return { ok: false, reason: "百度下拉中没有对应选项" };
    }
    const chosenText = textOf(target.option);
    clickLikeUser(target.option);
    await delay(260);
    const actual = selectedBrickValue(element);
    return { ok: normalizedIncludes(actual, chosenText), actual, reason: "百度下拉选项未保持选中" };
  }

  async function fillUdRadio(element, value, assignment) {
    const options = Array.from(element.querySelectorAll("label, .ud__radio__wrapper"));
    const target = options.map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), value, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) return { ok: false, reason: "找不到对应单选项" };
    clickLikeUser(target.option);
    await delay(80);
    const actual = selectedUdRadio(element);
    return { ok: normalizedIncludes(actual, textOf(target.option)), actual, reason: "单选项未保持选中" };
  }

  async function fillAtsxRadio(element, value, assignment) {
    const options = Array.from(element.querySelectorAll("label, .atsx-radio-wrapper"));
    const target = options.map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), value, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) return { ok: false, reason: "找不到对应单选项" };
    clickLikeUser(target.option);
    await delay(80);
    const actual = selectedAtsxRadio(element);
    return { ok: normalizedIncludes(actual, textOf(target.option)), actual, reason: "飞书单选项未保持选中" };
  }

  async function fillFeishuMonth(element, value) {
    const match = /^(\d{4})[-/.](\d{1,2})$/.exec(String(value).trim());
    if (!match) return { ok: false, reason: "飞书年月字段需要 YYYY-MM 格式" };
    clickLikeUser(element);
    await delay(120);
    let panel = visibleElements(".atsx-date-picker-period-month-panel").at(-1);
    let lists = Array.from(panel?.querySelectorAll(".atsx-date-picker-period-month-panel-list") || []);
    if (!panel || lists.length < 2) return { ok: false, reason: "未打开飞书年月选择器" };
    const year = match[1];
    const month = String(Number(match[2])).padStart(2, "0");
    const yearItem = Array.from(lists[0].querySelectorAll(".atsx-date-picker-period-month-panel-list-item"))
      .find((item) => item.getAttribute("data-cy") === year || textOf(item) === year);
    if (!yearItem) {
      element.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      return { ok: false, reason: `飞书年月选择器没有目标年份 ${year}` };
    }
    clickLikeUser(yearItem);
    await delay(120);

    // 选择年份会让飞书重新渲染整个弹层，必须重新取得月份节点。
    panel = visibleElements(".atsx-date-picker-period-month-panel").at(-1);
    lists = Array.from(panel?.querySelectorAll(".atsx-date-picker-period-month-panel-list") || []);
    const monthItem = Array.from(lists[1]?.querySelectorAll(".atsx-date-picker-period-month-panel-list-item") || [])
      .find((item) => item.getAttribute("data-cy") === month || textOf(item) === month);
    if (!panel || lists.length < 2 || !monthItem) {
      element.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      return { ok: false, reason: `飞书年月选择器没有目标月份 ${month}` };
    }
    clickLikeUser(monthItem);
    await delay(150);
    const actual = selectedFeishuMonth(element);
    return { ok: normalizedIncludes(actual, `${year}-${month}`), actual, reason: "飞书年月选择后未保持" };
  }

  async function fillFeishuYear(element, value) {
    const input = element.querySelector?.("input[placeholder='YYYY']");
    if (!(input instanceof HTMLInputElement)) return { ok: false, reason: "飞书年份组件没有年份输入框" };
    const year = globalThis.ResumeFillerCore.formatDateForControl(value, {
      controlKind: "feishu-year",
      placeholder: "YYYY"
    });
    if (!/^\d{4}$/.test(year)) return { ok: false, reason: "飞书年份资料格式无效" };

    clickLikeUser(input);
    await delay(120);
    let panel = visibleElements(".atsx-date-picker-panel").at(-1);
    if (!panel) return { ok: false, reason: "未打开飞书年份选择器" };

    let yearItem = null;
    for (let attempt = 0; attempt < 30; attempt += 1) {
      panel = visibleElements(".atsx-date-picker-panel").at(-1) || panel;
      yearItem = Array.from(panel.querySelectorAll(".atsx-date-picker-panel-body-cell-content"))
        .find((item) => item.getAttribute("data-cy") === year || textOf(item) === year);
      if (yearItem) break;
      const range = textOf(panel.querySelector(".atsx-date-picker-panel-header-title")).match(/(\d{4})\D+(\d{4})/);
      if (!range) break;
      const direction = Number(year) < Number(range[1]) ? "prev" : "next";
      const navigation = panel.querySelector(`[data-cy='${direction}']`);
      if (!navigation) break;
      clickLikeUser(navigation);
      await delay(80);
    }
    if (!yearItem) {
      await dismissFrameworkPicker(element);
      return { ok: false, reason: `飞书年份选择器没有 ${year}` };
    }

    clickLikeUser(yearItem);
    await delay(160);
    const actual = input.value;
    return { ok: actual === year, actual, reason: "飞书年份选择后未保持" };
  }

  async function fillPhoenixDate(element, value) {
    clickLikeUser(element);
    for (let attempt = 0; attempt < 8
      && !visibleElements(".phoenix-calendar, .phoenix-selectList__listItem").length; attempt += 1) await delay(100);
    let openCalendar = visibleElements(".phoenix-calendar").at(-1);
    if (!openCalendar) {
      const choices = visibleElements(".phoenix-selectList__listItem");
      const year = String(value).match(/^\d{4}/)?.[0];
      const target = choices.find((item) => textOf(item).replace(/年$/, "") === year);
      if (target && choices.every((item) => /^\d{4}年?$/.test(textOf(item)))) {
        clickLikeUser(target);
        await delay(160);
        const actual = currentValue(element);
        return { ok: actual.replace(/年$/, "") === year, actual, formatted: year, strategy: "phoenix-year-option", reason: "年份下拉未保持选中" };
      }
    }
    const monthMatch = /^(\d{4})[-/.](\d{1,2})(?:[-/.]\d{1,2})?$/.exec(String(value).trim());
    const monthCalendar = visibleElements(".phoenix-calendar-month-calendar").at(-1);
    if (monthCalendar && monthMatch) value = `${monthMatch[1]}-${monthMatch[2].padStart(2, "0")}`;
    if (monthMatch && !/\d{4}[-/.]\d{1,2}[-/.]\d{1,2}/.test(String(value)) && openCalendar && !monthCalendar) {
      value = `${monthMatch[1]}-${String(Number(monthMatch[2])).padStart(2, "0")}-01`;
    }
    if (monthMatch && monthCalendar) {
      const targetYear = Number(monthMatch[1]);
      const targetMonth = Number(monthMatch[2]);
      let attempts = 0;
      while (attempts < 50) {
        const visibleYear = Number(textOf(monthCalendar.querySelector(".phoenix-calendar-month-panel-year-select-content")));
        if (!Number.isFinite(visibleYear)) break;
        if (visibleYear === targetYear) {
          const monthCell = Array.from(monthCalendar.querySelectorAll(".phoenix-calendar-month-panel-cell"))
            .find((cell) => textOf(cell) === `${targetMonth}月`);
          if (!monthCell) break;
          (monthCell.querySelector("a") || monthCell).click();
          await delay(120);
          const actual = currentValue(element);
          return { ok: normalizedIncludes(actual, value), actual, reason: "年月组件未接受选择" };
        }
        const direction = visibleYear > targetYear
          ? ".phoenix-calendar-month-panel-prev-year-btn"
          : ".phoenix-calendar-month-panel-next-year-btn";
        const yearButton = monthCalendar.querySelector(direction);
        if (!yearButton) break;
        yearButton.click();
        attempts += 1;
        await delay(35);
      }
    }
    const dayMatch = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/.exec(String(value).trim());
    if (openCalendar && dayMatch) {
      const targetMonthIndex = Number(dayMatch[1]) * 12 + Number(dayMatch[2]) - 1;
      let attempts = 0;
      while (attempts < 240) {
        const calendar = visibleElements(".phoenix-calendar").at(-1);
        if (!calendar) break;
        const visibleYear = Number(textOf(calendar.querySelector(".phoenix-calendar-year-select")).replace(/\D/g, ""));
        const visibleMonth = Number(textOf(calendar.querySelector(".phoenix-calendar-month-select")).replace(/\D/g, ""));
        if (!Number.isFinite(visibleYear) || !Number.isFinite(visibleMonth)) break;
        const visibleMonthIndex = visibleYear * 12 + visibleMonth - 1;
        if (visibleMonthIndex === targetMonthIndex) {
          const day = String(Number(dayMatch[3]));
          const selectedDay = Array.from(calendar.querySelectorAll("[role='gridcell'], td")).find((cell) =>
            textOf(cell) === day && !/last-month|prev-month|next-month|disabled/i.test(cell.className)
          );
          if (!selectedDay) break;
          clickLikeUser(selectedDay.querySelector("a, .phoenix-calendar-date") || selectedDay);
          await delay(150);
          await dismissPhoenixCalendar(element, calendar);
          const actual = currentValue(element);
          return { ok: normalizedIncludes(actual, value), actual, reason: "日期组件未接受选择" };
        }
        const direction = visibleMonthIndex > targetMonthIndex
          ? ".phoenix-calendar-prev-month-btn"
          : ".phoenix-calendar-next-month-btn";
        const monthButton = calendar.querySelector(direction);
        if (!monthButton) break;
        monthButton.click();
        attempts += 1;
        await delay(35);
      }
    }
    const input = visibleElements("input.phoenix-calendar-input").at(-1);
    if (!input) {
      await dismissPhoenixCalendar(element, visibleElements(".phoenix-calendar").at(-1));
      return { ok: false, reason: "日期组件没有可用的日期输入方式" };
    }
    setNativeValue(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", bubbles: true }));
    await delay(80);
    const calendar = visibleElements(".phoenix-calendar").at(-1);
    if (calendar && dayMatch) {
      const day = String(Number(dayMatch[3]));
      const dayCells = Array.from(calendar.querySelectorAll("[role='gridcell'], td"));
      const selectedDay = dayCells.find((cell) => textOf(cell) === day && /selected|current/.test(cell.className))
        || dayCells.find((cell) => textOf(cell) === day && !/disabled|prev|next|last/i.test(cell.className));
      (selectedDay?.querySelector("a, .phoenix-calendar-date") || selectedDay)?.click();
      await delay(380);
    }
    await dismissPhoenixCalendar(element, calendar);
    const actual = currentValue(element);
    return { ok: normalizedIncludes(actual, value), actual, reason: "日期组件未接受输入" };
  }

  function findRegionLabel(value) {
    const labels = visibleElements(".area-text-label");
    const exact = labels.find((item) => globalThis.ResumeFillerCore.normalize(textOf(item)) === globalThis.ResumeFillerCore.normalize(value));
    if (exact) return exact;
    const target = labels.map((item) => ({ item, score: optionScore(textOf(item), value) }))
      .sort((a, b) => b.score - a.score)[0];
    return target?.score ? target.item : null;
  }

  function enabledRegionChoice(container) {
    return container?.querySelector(
      "[class*='RadioUnchecked']:not([class*='disabled']), [class*='RadioChecked']:not([class*='disabled'])"
    ) || null;
  }

  function regionDrillControl(container) {
    return container?.querySelector("[class*='area-icon-right'], [class*='area-icon-Right']") || null;
  }

  async function setRegionSearch(search, value) {
    setNativeValue(search, value);
    search.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    search.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    await delay(200);
  }

  async function fillPhoenixRegion(element, value, attempt = 0) {
    element.click();
    await delay(160);
    if (!visibleElements(".area-text-label").length) {
      element.click();
      await delay(160);
    }
    if (!visibleElements(".area-text-label").length) return null;

    const parts = globalThis.ResumeFillerCore.splitRegionPath(value);
    const matchedParts = [];
    let selectedText = "";
    let missingPart = "";

    for (let index = 0; index < parts.length; index += 1) {
      const part = parts[index];
      let label = findRegionLabel(part);
      if (!label) {
        const search = visibleElements("input[placeholder='搜索']").at(-1);
        if (search) {
          await setRegionSearch(search, part);
          label = findRegionLabel(part);
          if (!label) await setRegionSearch(search, "");
        }
      }
      if (!label) {
        missingPart = part;
        break;
      }

      const labelText = textOf(label);
      const container = label.closest(".area-item-container");
      const choice = enabledRegionChoice(container);
      const drill = regionDrillControl(container);
      matchedParts.push(labelText);

      if (choice) {
        let checked = container.querySelector("[class*='RadioChecked']:not([class*='disabled'])");
        if (!checked) {
          clickLikeUser(choice);
          await delay(100);
          checked = container.querySelector("[class*='RadioChecked']:not([class*='disabled'])");
        }
        if (!checked) {
          clickLikeUser(choice.closest(".icon-container") || choice);
          await delay(100);
          checked = container.querySelector("[class*='RadioChecked']:not([class*='disabled'])");
        }
        if (!checked) {
          clickLikeUser(label);
          await delay(160);
          checked = container.querySelector("[class*='RadioChecked']:not([class*='disabled'])");
        }
        if (checked) selectedText = labelText;
        else missingPart = part;
        await delay(100);
      }

      if (index < parts.length - 1 && drill) {
        clickLikeUser(label);
        await delay(160);
        continue;
      }
      if (index < parts.length - 1) missingPart = parts[index + 1];
      else if (!choice) missingPart = part;
      break;
    }

    if (!selectedText) {
      await dismissPhoenixPicker(element);
      if (attempt < 1) return fillPhoenixRegion(element, value, attempt + 1);
      const matched = matchedParts.length ? `已找到“${matchedParts.join("")}”，但该层级不可直接选择；` : "";
      return {
        ok: false,
        strategy: "region-cascade",
        reason: `${matched}网站没有可选的“${missingPart || value}”地区选项`
      };
    }

    const confirm = findPhoenixPickerAction("确定");
    if (!confirm) {
      await dismissPhoenixPicker(element);
      return { ok: false, strategy: "region-cascade", reason: "地区选择器没有确认按钮" };
    }
    clickLikeUser(confirm.closest("button, [role='button']") || confirm);
    await delay(250);
    const actual = currentValue(element);
    const ok = Boolean(actual) && normalizedIncludes(actual, selectedText);
    if (!ok && attempt < 1) {
      if (visibleElements("input[placeholder='搜索']").length) await dismissPhoenixPicker(element);
      await delay(120);
      return fillPhoenixRegion(element, value, attempt + 1);
    }
    const warning = ok && missingPart
      ? `网站未提供“${missingPart}”，已填写可选的最深层级“${selectedText}”`
      : "";
    return {
      ok,
      actual,
      strategy: "region-cascade",
      warning,
      reason: ok ? "" : "地区选择后未保持选中"
    };
  }

  async function fillPhoenixSelect(element, value, assignment) {
    if (/Date$/.test(assignment.key || "") || /日期|时间/.test(assignment.label || "")) {
      return fillPhoenixDate(element, value);
    }

    const isRegionField = isRegionAssignment(assignment);

    if (isRegionField) {
      const regionResult = await fillPhoenixRegion(element, value);
      if (regionResult) return regionResult;
    }

    clickLikeUser(element);
    const popupSelector = ".area-text-label, .item-text-label, .phoenix-selectList__listItem";
    for (let attempt = 0; attempt < 8 && !visibleElements(popupSelector).length; attempt += 1) await delay(100);
    const pickerLabels = visibleElements(".item-text-label");
    if (pickerLabels.length) {
      const parts = String(value).split(/[\s,/，-]+/).filter(Boolean);
      let selectedText = "";
      for (const part of parts) {
        const label = visibleElements(".item-text-label").find((item) => normalizedIncludes(textOf(item), part));
        if (!label) continue;
        const item = label.closest(".list-item-container") || label.parentElement;
        const selectIcon = item?.querySelector(".icon-container");
        if (selectIcon) clickLikeUser(selectIcon.querySelector("svg") || selectIcon);
        else clickLikeUser(item);
        selectedText = textOf(label);
        await delay(100);
      }
      if (selectedText) {
        const confirm = findPhoenixPickerAction("确定");
        clickLikeUser(confirm);
        await delay(250);
        const actual = currentValue(element);
        return { ok: Boolean(actual) && normalizedIncludes(actual, selectedText), actual, reason: "地区选择后未保持选中" };
      }
    }
    let items = visibleElements(".phoenix-selectList__listItem");
    let target = items.map((item) => ({ item, score: assignmentOptionScore(textOf(item), value, assignment) })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) {
      const search = visibleElements("input[placeholder='搜索']").at(-1);
      if (search) {
        setNativeValue(search, value);
        dispatchInputEvents(search);
        await delay(120);
        items = visibleElements(".phoenix-selectList__listItem");
        target = items.map((item) => ({ item, score: assignmentOptionScore(textOf(item), value, assignment) })).sort((a, b) => b.score - a.score)[0];
      }
    }
    if (!target?.score) {
      const label = getFormItem(element)?.querySelector(".form-item__text, .form-item__label");
      label?.click();
      await delay(60);
      element.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      return { ok: false, reason: "下拉列表中没有对应选项" };
    }
    const chosenText = textOf(target.item);
    clickLikeUser(target.item);
    await delay(200);
    let actual = currentValue(element);
    if (!normalizedIncludes(actual, chosenText) && isVisible(target.item)) {
      clickLikeUser(target.item);
      await delay(200);
      actual = currentValue(element);
    }
    const ok = normalizedIncludes(actual, chosenText);
    let warning = "";
    if (ok && isRegionField && globalThis.ResumeFillerCore.normalize(chosenText) !== globalThis.ResumeFillerCore.normalize(value)) {
      warning = `网站只提供“${chosenText}”，已填写该最深可选层级`;
    } else if (ok && isSalaryAssignment(assignment)
      && globalThis.ResumeFillerCore.normalize(chosenText) !== globalThis.ResumeFillerCore.normalize(value)) {
      warning = `期望月薪“${value}”已映射到网站可选档位“${chosenText}”`;
    }
    return { ok, actual, warning, reason: "下拉选项点击后未保持选中" };
  }

  function fillNativeSelect(element, value, assignment) {
    const target = Array.from(element.options).map((option) => ({
      option,
      score: assignmentOptionScore(`${option.text} ${option.value}`, value, assignment)
    }))
      .sort((a, b) => b.score - a.score)[0];
    if (!target?.score) return { ok: false, reason: "原生下拉没有对应选项" };
    element.value = target.option.value;
    dispatchInputEvents(element);
    const actual = element.options[element.selectedIndex]?.text || "";
    const ok = normalizedIncludes(actual, target.option.text);
    let warning = "";
    if (ok && isRegionAssignment(assignment)
      && globalThis.ResumeFillerCore.normalize(actual) !== globalThis.ResumeFillerCore.normalize(value)) {
      warning = `网站只提供“${actual}”，已填写该最深可选层级`;
    } else if (ok && isSalaryAssignment(assignment)
      && globalThis.ResumeFillerCore.normalize(actual) !== globalThis.ResumeFillerCore.normalize(value)) {
      warning = `期望月薪“${value}”已映射到网站可选档位“${actual}”`;
    }
    // The delayed verifier reads select.value, which may differ from its label.
    return { ok, actual: element.value, warning, reason: "原生下拉未更新" };
  }

  function comboboxSearchCandidates(value, assignment) {
    if (!isRegionAssignment(assignment)) return [value];
    const parts = [...globalThis.ResumeFillerCore.splitRegionPath(value)].reverse();
    const candidates = [];
    for (const part of [...parts, value]) {
      if (!part) continue;
      candidates.push(part);
      const withoutSuffix = part.replace(/(?:特别行政区|自治区|自治州|地区|新区|省|市|区|县|旗)$/u, "");
      if (withoutSuffix && withoutSuffix !== part) candidates.push(withoutSuffix);
    }
    return [...new Set(candidates)];
  }

  function setSearchInputValue(search, value) {
    search.focus();
    const previous = search.value;
    if (previous && previous !== value) {
      setNativeValue(search, "");
      search.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        composed: true,
        data: null,
        inputType: "deleteContentBackward"
      }));
    }
    search.dispatchEvent(new KeyboardEvent("keydown", {
      key: value.slice(-1),
      bubbles: true,
      composed: true
    }));
    search.dispatchEvent(new InputEvent("beforeinput", {
      bubbles: true,
      composed: true,
      cancelable: true,
      data: value,
      inputType: "insertText"
    }));
    setNativeValue(search, value);
    search.dispatchEvent(new InputEvent("input", {
      bubbles: true,
      composed: true,
      data: value,
      inputType: "insertText"
    }));
    search.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    search.dispatchEvent(new KeyboardEvent("keyup", { key: value.slice(-1), bubbles: true, composed: true }));
  }

  async function fillCombobox(element, value, assignment) {
    const scope = element.matches?.(".ant-select, .ant4-select, .ud__select, .atsx-select")
      ? element
      : element.closest?.(".ant-select, .ant4-select, .ud__select, .atsx-select") || element;
    const opener = scope.querySelector?.(".ant-select-selector, .ant4-select-selector, .ud__select__selector, .atsx-select-selector") || scope;
    clickLikeUser(opener);
    const optionSelector = [
      "[role='option']", ".ant-select-item-option", ".ant4-select-item-option", ".ud__select-dropdown-option",
      ".ud__select__list__item", ".ud__tree__node", ".atsx-select-dropdown-menu-item",
      "[class*='select-dropdown'] li", "[class*='select-option']"
    ].join(",");
    const findTarget = (candidate) => visibleElements(optionSelector).map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), candidate, assignment),
      depth: option.matches?.(".ud__tree__node") ? option.querySelectorAll(":scope > .ud__tree__node__indent").length : 0
    })).sort((a, b) => b.score - a.score || (isRegionAssignment(assignment) ? b.depth - a.depth : 0))[0];
    let target = await waitForCondition(() => {
      const candidate = findTarget(value);
      return candidate?.score ? candidate : null;
    }, 160);
    const search = scope.querySelector?.(
      "input.atsx-select-search__field, input.ant-select-selection-search-input, input.ant4-select-selection-search-input, input.ud__select__selector__search__input, input.ud__native-input"
    ) || (element instanceof HTMLInputElement ? element : null);
    if (!target?.score && search && !search.readOnly) {
      const searchCandidates = comboboxSearchCandidates(value, assignment);
      for (const candidate of searchCandidates) {
        if (target?.score) break;
        setSearchInputValue(search, candidate);
        for (let attempt = 0; attempt < 8 && !target?.score; attempt += 1) {
          await delay(150);
          target = findTarget(candidate);
        }
      }
    }
    if (!target?.score) {
      if (search && !search.readOnly) {
        setSearchInputValue(search, "");
      }
      scope.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }));
      return { ok: false, reason: "组合下拉中没有对应选项" };
    }
    const chosenText = textOf(target.option);
    const optionTarget = target.option.closest?.(
      "[role='option'], .ant-select-item-option, .ant4-select-item-option, .ud__select-dropdown-option, .ud__select__list__item, .ud__tree__node, .atsx-select-dropdown-menu-item"
    ) || target.option;
    const clickableOption = optionTarget.querySelector?.(
      ".ud__tree__node__label, .ud__select__list__item__content, .ant-select-item-option-content, .ant4-select-item-option-content"
    ) || optionTarget;
    clickLikeUser(clickableOption);
    await waitForCondition(() => normalizedIncludes(selectedComboboxValue(scope), chosenText), scope.matches?.(".ud__select") ? 520 : 320, 60);
    const searchValue = scope.querySelector?.(
      "input.atsx-select-search__field, input.ant-select-selection-search-input, input.ant4-select-selection-search-input, input.ud__select__selector__search__input, input.ud__native-input"
    )?.value || "";
    const displayed = selectedComboboxValue(scope);
    const actual = displayed || searchValue;
    const optionCommitted = !isVisible(optionTarget)
      || /selected|checked/.test(String(optionTarget.className))
      || Boolean(optionTarget.querySelector?.("[class*='check'] svg, [aria-checked='true'], [aria-selected='true']"));
    const remoteSchoolAccepted = scope.matches?.(".ud__select")
      && search?.getAttribute?.("data-form-field-id") === "school"
      && normalizedIncludes(searchValue, chosenText)
      && !validationErrorFor(scope);
    // 飞书学校控件在点击远程候选后仍保留为普通输入框，不生成 selected 节点；
    // 输入值保持且必填校验消失，才视为候选已被页面接受。
    const ok = (optionCommitted || remoteSchoolAccepted) && normalizedIncludes(actual, chosenText);
    if (ok) scope.dataset.localResumeCommittedValue = chosenText;
    const warning = ok && isRegionAssignment(assignment) && !normalizedIncludes(actual, value)
      ? `网站只提供“${actual}”，已填写该最深可选层级`
      : "";
    if (!ok) await dismissFrameworkPicker(scope);
    return { ok, actual, warning, verified: ok, reason: "组合下拉选项未保持选中" };
  }

  function selectedMokaLocation(element) {
    const scope = getFormItem(element);
    return Array.from(scope?.querySelectorAll("[class*='sd-Tag-content'], [class*='sd-Tag-label'], [class*='sd-Tag-text'], [class*='sd-Tag-container']") || [])
      .filter((node) => !node.querySelector("[class*='sd-Tag-content'], [class*='sd-Tag-label'], [class*='sd-Tag-text']"))
      .map(textOf).filter(Boolean).join(" / ");
  }

  async function fillMokaLocation(element, value, assignment) {
    const live = () => resolveAssignmentElement(assignment) || element;
    const options = () => visibleElements("[class*='sd-Select-common-item'], [class*='sd-Menu-content-item'], [role='option']")
      .filter((node) => !isDisabledControl(node));
    for (const candidate of comboboxSearchCandidates(value, assignment)) {
      const input = live();
      clickLikeUser(input);
      setSearchInputValue(input, candidate);
      let target;
      for (let attempt = 0; attempt < 8; attempt += 1) {
        await delay(120);
        target = options().map((option) => ({ option, score: assignmentOptionScore(textOf(option), candidate, assignment) }))
          .sort((a, b) => b.score - a.score)[0];
        if (target?.score) break;
      }
      if (!target?.score) continue;
      const chosenText = textOf(target.option);
      clickLikeUser(target.option);
      await delay(240);
      const actual = selectedMokaLocation(live());
      if (actual && normalizedIncludes(actual, chosenText)) {
        await dismissFrameworkPicker(live());
        return { ok: true, actual, verified: true,
          warning: !normalizedIncludes(actual, value) ? `网站已选择“${actual}”，更深层级请复核` : "" };
      }
    }
    setSearchInputValue(live(), "");
    await dismissFrameworkPicker(live());
    return { ok: false, reason: "地区搜索后未确认生成已选地区标签，请手动选择后重扫" };
  }

  async function fillMokaSelect(element, value, assignment, resolveLive = () => element) {
    const input = element.querySelector("input");
    clickLikeUser(input || element);
    // Menu containers contain many options; they must never be selected as one option.
    const optionSelector = "[class*='sd-Select-common-item'], [class*='sd-Menu-content-item']";
    const findTarget = (candidate) => visibleElements(optionSelector).map((option) => ({
      option,
      score: /Date$/.test(assignment?.key || "") && /^\d+$/.test(candidate)
        ? (Number(textOf(option).replace(/[年月]/g, "")) === Number(candidate) ? 100 : 0)
        : assignmentOptionScore(textOf(option), candidate, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    let target = await waitForCondition(() => {
      const candidate = findTarget(value);
      return candidate?.score ? candidate : null;
    }, 180);
    if (!target?.score && input && !input.readOnly) {
      for (const candidate of comboboxSearchCandidates(value, assignment)) {
        setSearchInputValue(input, candidate);
        for (let attempt = 0; attempt < 8 && !target?.score; attempt += 1) {
          await delay(150);
          target = findTarget(candidate);
        }
        if (target?.score) break;
      }
    }
    if (!target?.score) {
      await dismissFrameworkPicker(element);
      return { ok: false, reason: "Moka 下拉中没有对应选项" };
    }
    const chosenText = textOf(target.option);
    const clickable = target.option.querySelector("[class*='sd-Menu-content-item'], [class*='option-label']")
      || target.option.closest("[class*='sd-Menu-content-item']")
      || target.option;
    clickLikeUser(clickable);
    await waitForCondition(() => normalizedIncludes(selectedMokaValue(resolveLive() || element), chosenText), 260, 60);
    const actual = selectedMokaValue(resolveLive() || element);
    const ok = normalizedIncludes(actual, chosenText);
    const warning = ok && isRegionAssignment(assignment) && !normalizedIncludes(actual, value)
      ? `网站只提供“${actual}”，已填写该最深可选层级` : "";
    if (!ok) await dismissFrameworkPicker(element);
    return { ok, actual, warning, verified: ok, reason: "Moka 下拉选项未保持选中" };
  }

  async function fillMokaMonth(element, value, assignment) {
    const parts = dateParts(value);
    if (!parts) return { ok: false, reason: "Moka 月份资料格式无效" };
    const liveYear = () => resolveAssignmentElement(assignment) || (element.isConnected ? element : null);
    const liveMonth = () => {
      const year = liveYear();
      const controls = Array.from(getFormItem(year)?.querySelectorAll("[class*='sd-Select-container']") || []);
      const index = controls.indexOf(year);
      return index >= 0 ? controls[index + 1] : null;
    };
    if (!liveMonth()) return { ok: false, reason: "Moka 日期缺少月份选择器" };
    const yearResult = await fillMokaSelect(element, String(parts.year), assignment, liveYear);
    if (!yearResult.ok) return { ...yearResult, reason: `年份：${yearResult.reason}` };
    const monthControl = liveMonth();
    if (!monthControl) return { ok: false, reason: "年份选择后月份控件不可用，请重新扫描" };
    const monthResult = await fillMokaSelect(monthControl, String(parts.month), assignment, liveMonth);
    if (!monthResult.ok) return { ...monthResult, reason: `月份：${monthResult.reason}` };
    const liveElement = resolveAssignmentElement(assignment) || element;
    const actual = selectedMokaMonth(liveElement);
    if (actual) {
      element.dataset.localResumeCommittedValue = actual;
      liveElement.dataset.localResumeCommittedValue = actual;
    }
    return {
      ok: dateMatches(actual, `${parts.year}-${parts.month}`),
      actual,
      formatted: `${parts.year}-${String(parts.month).padStart(2, "0")}`,
      reason: "Moka 年月选择后未保持"
    };
  }

  async function fillElementSelect(element, value, assignment) {
    const existing = selectedElementValue(element);
    if (existing && assignmentOptionScore(existing, value, assignment) > 0) {
      return { ok: true, actual: existing, reason: "" };
    }
    await dismissFrameworkPicker(element);
    await delay(60);
    const dropdownState = new Map(Array.from(document.querySelectorAll(".el-select-dropdown")).map((dropdown) => [
      dropdown,
      `${dropdown.style.display}|${dropdown.style.zIndex}|${dropdown.getAttribute("style") || ""}`
    ]));
    const opener = element.querySelector("input, .el-select__wrapper, .el-input") || element;
    clickLikeUser(opener);
    await delay(180);
    const activeDropdowns = () => Array.from(document.querySelectorAll(".el-select-dropdown")).filter((dropdown) => {
      if (isVisible(dropdown)) return true;
      const previous = dropdownState.get(dropdown);
      const current = `${dropdown.style.display}|${dropdown.style.zIndex}|${dropdown.getAttribute("style") || ""}`;
      return previous !== undefined && previous !== current;
    });
    if (!activeDropdowns().length) {
      clickLikeUser(opener);
      await delay(140);
    }
    const optionSelector = ".el-select-dropdown__item, [role='option']";
    const availableOptions = () => {
      const dropdowns = activeDropdowns();
      return dropdowns.length
        ? dropdowns.flatMap((dropdown) => Array.from(dropdown.querySelectorAll(optionSelector)))
        : visibleElements(optionSelector);
    };
    const findTarget = (candidate) => availableOptions().map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), candidate, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    let target = findTarget(value);
    const search = element.querySelector("input:not([readonly])");
    if (!target?.score && search) {
      for (const candidate of comboboxSearchCandidates(value, assignment)) {
        setSearchInputValue(search, candidate);
        for (let attempt = 0; attempt < 8 && !target?.score; attempt += 1) {
          await delay(120);
          target = findTarget(candidate);
        }
        if (target?.score) break;
      }
    }
    if (!target?.score) {
      const normalizedLabel = globalThis.ResumeFillerCore.normalize(getLabel(element));
      const peers = Array.from(document.querySelectorAll(".el-select"))
        .filter((select) => globalThis.ResumeFillerCore.normalize(getLabel(select)) === normalizedLabel);
      const occurrence = Math.max(0, peers.indexOf(element));
      const dropdownCandidates = Array.from(document.querySelectorAll(".el-select-dropdown")).map((dropdown) => {
        const best = Array.from(dropdown.querySelectorAll(".el-select-dropdown__item, [role='option']")).map((option) => ({
          option,
          score: assignmentOptionScore(textOf(option), value, assignment)
        })).sort((a, b) => b.score - a.score)[0];
        return best?.score ? { ...best, dropdown } : null;
      }).filter(Boolean).sort((a, b) => b.score - a.score);
      const bestScore = dropdownCandidates[0]?.score || 0;
      const sameQuality = dropdownCandidates.filter((item) => item.score === bestScore);
      target = sameQuality[occurrence] || sameQuality[0] || dropdownCandidates[0];
    }
    if (!target?.score) {
      await dismissFrameworkPicker(element);
      return { ok: false, reason: "Element 下拉中没有对应选项" };
    }
    const chosenText = textOf(target.option);
    target.option.click();
    await delay(240);
    const actual = selectedElementValue(element);
    const ok = normalizedIncludes(actual, chosenText);
    return { ok, actual, reason: "Element 下拉选项未保持选中" };
  }

  async function fillElementDropdown(element, value, assignment) {
    const existing = selectedElementValue(element);
    if (existing && assignmentOptionScore(existing, value, assignment) > 0) {
      return { ok: true, actual: existing, reason: "" };
    }
    const link = element.querySelector(".el-dropdown-link, [role='button']") || element;
    hoverLikeUser(element);
    hoverLikeUser(link);
    await delay(180);
    if (!visibleElements(".el-dropdown-menu__item").length) clickLikeUser(link);
    await delay(180);
    let target = visibleElements(".el-dropdown-menu__item").map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), value, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) {
      target = Array.from(document.querySelectorAll(".el-dropdown-menu")).flatMap((menu) =>
        Array.from(menu.querySelectorAll(".el-dropdown-menu__item")).map((option) => ({
          option,
          score: assignmentOptionScore(textOf(option), value, assignment)
        }))
      ).sort((a, b) => b.score - a.score)[0];
    }
    if (!target?.score) {
      await dismissFrameworkPicker(element);
      return { ok: false, reason: "Element 复合下拉中没有对应选项" };
    }
    const chosenText = textOf(target.option);
    target.option.click();
    await delay(180);
    const actual = selectedElementValue(element);
    return { ok: normalizedIncludes(actual, chosenText), actual, reason: "Element 复合下拉选项未保持选中" };
  }

  async function fillElementRadio(element, value, assignment) {
    const options = Array.from(element.querySelectorAll(".el-radio, label, [role='radio']"));
    const target = options.map((option) => ({
      option,
      score: assignmentOptionScore(textOf(option), value, assignment)
    })).sort((a, b) => b.score - a.score)[0];
    if (!target?.score) return { ok: false, reason: "Element 单选组没有对应选项" };
    target.option.click();
    await delay(100);
    const actual = selectedElementValue(element);
    return { ok: normalizedIncludes(actual, textOf(target.option)), actual, reason: "Element 单选项未保持选中" };
  }

  async function fillElementCascader(element, value, assignment) {
    const existing = selectedElementValue(element);
    if (existing && normalizedIncludes(existing, value)) return { ok: true, actual: existing, reason: "" };
    const panelsBefore = new Map(Array.from(document.querySelectorAll(".el-cascader-panel")).map((panel) => [
      panel,
      panel.closest(".el-popper")?.getAttribute("style") || ""
    ]));
    const opener = element.querySelector("input, .el-input") || element;
    clickLikeUser(opener);
    await delay(180);
    let requested = isRegionAssignment(assignment)
      ? ["中国大陆", ...globalThis.ResumeFillerCore.splitRegionPath(value)]
      : String(value).split(/[\s,/，-]+/).filter(Boolean);
    if (/^(北京|天津|上海|重庆)市$/.test(requested[1] || "")) {
      requested = [requested[0], requested[1].replace(/市$/, ""), ...requested.slice(1)];
    }
    const occurrence = Math.max(0, Array.from(document.querySelectorAll(".el-cascader")).indexOf(element));
    const activePanels = () => Array.from(document.querySelectorAll(".el-cascader-panel")).filter((candidate) =>
      isVisible(candidate)
      || (panelsBefore.has(candidate) && panelsBefore.get(candidate) !== (candidate.closest(".el-popper")?.getAttribute("style") || ""))
    );
    if (!activePanels().length) {
      clickLikeUser(opener);
      await delay(140);
    }
    let panel = activePanels().at(-1)
      || Array.from(document.querySelectorAll(".el-cascader-panel"))[occurrence]
      || document.querySelector(".el-cascader-panel");
    let deepest = "";
    let missing = "";
    for (const part of requested) {
      panel = activePanels().at(-1) || panel;
      let target = null;
      for (let attempt = 0; attempt < 6 && !target?.score; attempt += 1) {
        const visibleNodes = Array.from(panel?.querySelectorAll(".el-cascader-node") || []).filter(isVisible);
        const nodes = visibleNodes.length ? visibleNodes : Array.from(panel?.querySelectorAll(".el-cascader-node") || []);
        target = nodes.map((node) => ({ node, score: assignmentOptionScore(textOf(node), part, assignment) }))
          .sort((a, b) => b.score - a.score)[0];
        if (!target?.score) await delay(100);
      }
      if (!target?.score) {
        if (part === "中国大陆") continue;
        missing = part;
        break;
      }
      deepest = textOf(target.node.querySelector(".el-cascader-node__label") || target.node);
      clickLikeUser(target.node.querySelector(".el-cascader-node__label") || target.node);
      await delay(180);
    }
    if (!deepest) {
      await dismissFrameworkPicker(element);
      return { ok: false, reason: "Element 级联选择器没有对应路径" };
    }
    await dismissFrameworkPicker(element);
    const actual = selectedElementValue(element);
    const ok = Boolean(actual) && normalizedIncludes(actual, deepest);
    return {
      ok,
      actual,
      warning: ok && missing ? `网站未提供“${missing}”，已填写最深可选层级“${deepest}”` : "",
      reason: ok ? "" : "Element 级联选择后未保持"
    };
  }

  async function fillElementDate(element, value, assignment) {
    const input = element.querySelector("input:not([type='hidden'])");
    if (!(input instanceof HTMLInputElement)) return { ok: false, reason: "Element 日期组件没有可用输入框" };
    let formatted = globalThis.ResumeFillerCore.formatDateForControl(value, {
      controlKind: "element-date",
      placeholder: input.placeholder || assignment?.placeholder || ""
    });
    if (/日|DD/i.test(input.placeholder || "") && /^\d{4}-\d{2}$/.test(formatted)) formatted += "-01";
    clickLikeUser(input);
    await delay(100);
    setNativeValue(input, formatted);
    input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, composed: true }));
    input.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", bubbles: true, composed: true }));
    await delay(180);
    await dismissFrameworkPicker(element);
    const actual = input.value;
    return { ok: dateMatches(actual, formatted), actual, formatted, reason: "Element 日期组件未接受目标日期" };
  }

  async function dismissFrameworkPicker(element) {
    const eventInit = { key: "Escape", code: "Escape", bubbles: true, composed: true };
    element.dispatchEvent(new KeyboardEvent("keydown", eventInit));
    document.dispatchEvent(new KeyboardEvent("keydown", eventInit));
    element.querySelector?.("input")?.blur();
    element.blur?.();
    await delay(100);
  }

  function dateParts(value) {
    const match = /^(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?$/.exec(String(value || "").trim());
    if (!match) return null;
    return {
      year: Number(match[1]),
      month: Number(match[2]),
      day: match[3] ? Number(match[3]) : null
    };
  }

  function dateMatches(actual, expected) {
    const actualNumbers = String(actual || "").match(/\d+/g)?.map(Number) || [];
    const expectedNumbers = String(expected || "").match(/\d+/g)?.map(Number) || [];
    return expectedNumbers.length > 0
      && expectedNumbers.every((number, index) => actualNumbers[index] === number);
  }

  function visibleGenericCalendarPanel() {
    const selectors = [
      ".ant-calendar-picker-container", ".ant4-calendar-picker-container", ".ant-picker-dropdown",
      "[class*='calendar'][class*='dropdown']", "[class*='calendar'][class*='panel']",
      "[class*='date-picker'][class*='dropdown']", "[class*='picker-dropdown']", "[role='dialog']"
    ].join(",");
    return visibleElements(selectors).filter((panel) => panel.querySelector(
      "[role='gridcell'], td, [class*='calendar-cell'], [class*='picker-cell']"
    )).at(-1) || null;
  }

  function calendarMonthFromPanel(panel) {
    const header = panel.querySelector(
      "[class*='calendar-header'], [class*='picker-header'], [class*='calendar-my-select'], [class*='header-view']"
    );
    const text = textOf(header || panel);
    let match = /(\d{4})\s*年?\s*[-/. ]?\s*(\d{1,2})\s*月/.exec(text);
    if (!match) {
      match = /(\d{1,2})\s*月\s*[-/. ]?\s*(\d{4})\s*年?/.exec(text);
      if (match) return { year: Number(match[2]), month: Number(match[1]) };
    }
    return match ? { year: Number(match[1]), month: Number(match[2]) } : null;
  }

  function calendarCellDate(cell) {
    const metadata = [
      cell.getAttribute("title"), cell.getAttribute("data-date"), cell.getAttribute("data-value"),
      cell.getAttribute("aria-label"), cell.querySelector?.("[title], [data-date], [data-value], [aria-label]")?.getAttribute("title"),
      cell.querySelector?.("[data-date]")?.getAttribute("data-date"),
      cell.querySelector?.("[data-value]")?.getAttribute("data-value"),
      cell.querySelector?.("[aria-label]")?.getAttribute("aria-label")
    ].filter(Boolean);
    for (const value of metadata) {
      const match = /(\d{4})\D+(\d{1,2})\D+(\d{1,2})/.exec(String(value));
      if (match) return { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
    }
    return null;
  }

  function usableCalendarCell(cell) {
    return !cell.disabled
      && cell.getAttribute("aria-disabled") !== "true"
      && !/disabled|outside|other[-_ ]?month/i.test(String(cell.className));
  }

  function findCalendarDayCell(panel, target) {
    const cells = Array.from(panel.querySelectorAll(
      "[role='gridcell'], td, [class*='calendar-cell'], [class*='picker-cell']"
    )).filter((cell, index, all) => usableCalendarCell(cell)
      && !all.some((parent, parentIndex) => parentIndex < index && parent.contains(cell) && calendarCellDate(parent)));
    const exact = cells.find((cell) => {
      const value = calendarCellDate(cell);
      return value?.year === target.year && value.month === target.month && value.day === target.day;
    });
    if (exact) return exact;
    const visibleMonth = calendarMonthFromPanel(panel);
    if (visibleMonth?.year !== target.year || visibleMonth.month !== target.month) return null;
    return cells.find((cell) => textOf(cell) === String(target.day));
  }

  function findCalendarNavigation(panel, navigation) {
    const controls = Array.from(panel.querySelectorAll("button, a, [role='button']"))
      .filter((control) => !control.disabled && control.getAttribute("aria-disabled") !== "true");
    const descriptor = (control) => `${control.className || ""} ${control.title || ""} ${control.getAttribute("aria-label") || ""} ${textOf(control)}`;
    const patterns = navigation.direction === "previous"
      ? {
          year: /super[-_ ]?prev|prev[-_ ]?year|year[-_ ]?prev|上一年|前一年/i,
          month: /prev[-_ ]?month|month[-_ ]?prev|picker-header-(?!super-)prev-btn|上个月|上一月/i
        }
      : {
          year: /super[-_ ]?next|next[-_ ]?year|year[-_ ]?next|下一年|后一年/i,
          month: /next[-_ ]?month|month[-_ ]?next|picker-header-(?!super-)next-btn|下个月|下一月/i
        };
    const preferred = navigation.preferYear ? patterns.year : patterns.month;
    return controls.find((control) => preferred.test(descriptor(control)))
      || controls.find((control) => patterns.month.test(descriptor(control)))
      || null;
  }

  async function fillGenericCalendarDate(element, value) {
    const root = genericCalendarRoot(element) || element;
    const input = root instanceof HTMLInputElement ? root : root.querySelector?.("input:not([type='hidden'])");
    if (!(input instanceof HTMLInputElement)) return { ok: false, reason: "日历组件没有可用输入框" };
    const target = globalThis.ResumeFillerCore.calendarTarget(value);
    if (!target) return { ok: false, reason: "日期资料格式无效" };

    clickLikeUser(input);
    await delay(160);
    let panel = visibleGenericCalendarPanel();
    if (!panel) {
      clickLikeUser(root);
      await delay(160);
      panel = visibleGenericCalendarPanel();
    }
    if (!panel) return { ok: false, formatted: target.formatted, reason: "未打开网页日历；已停止文本写入，避免出现假成功" };

    let cell = null;
    for (let attempt = 0; attempt < 180; attempt += 1) {
      panel = visibleGenericCalendarPanel() || panel;
      cell = findCalendarDayCell(panel, target);
      if (cell) break;
      const visibleMonth = calendarMonthFromPanel(panel);
      if (!visibleMonth) break;
      const navigation = globalThis.ResumeFillerCore.calendarNavigation(visibleMonth.year, visibleMonth.month, target);
      if (!navigation || navigation.direction === "none") break;
      const button = findCalendarNavigation(panel, navigation);
      if (!button) break;
      clickLikeUser(button);
      await delay(70);
    }
    if (!cell) {
      await dismissFrameworkPicker(root);
      return { ok: false, formatted: target.formatted, reason: `网页日历中无法选择 ${target.formatted}` };
    }

    const clickable = cell.querySelector("button, a, [class*='cell-inner'], [class*='calendar-date']") || cell;
    clickLikeUser(clickable);
    await delay(320);
    await dismissFrameworkPicker(root);
    const actual = input.value || currentValue(root);
    const ok = dateMatches(actual, target.formatted);
    return {
      ok,
      actual,
      formatted: target.formatted,
      warning: ok && !target.sourceHasDay ? `网站要求具体日期，已按资料月份填写 ${target.formatted}` : "",
      reason: "已点击日期选项，但网页组件没有保存该日期"
    };
  }

  async function fillAntDate(element, value) {
    const input = element instanceof HTMLInputElement ? element : element.querySelector?.("input");
    if (!(input instanceof HTMLInputElement)) return { ok: false, reason: "Ant 日期组件没有可用输入框" };
    const parts = dateParts(value);
    if (!parts) return { ok: false, reason: "日期资料格式无效" };
    const sourceHasDay = parts.day !== null;
    const day = parts.day || 1;
    const formatted = `${parts.year}-${String(parts.month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;

    clickLikeUser(input);
    await delay(140);
    let panel = visibleElements(".ant-picker-dropdown").at(-1);
    if (!panel) {
      clickLikeUser(element.closest?.(".ant-picker") || element);
      await delay(140);
      panel = visibleElements(".ant-picker-dropdown").at(-1);
    }
    if (!panel) return { ok: false, reason: "未打开 Ant 日期选择器" };

    const targetMonthIndex = parts.year * 12 + parts.month - 1;
    let cell = null;
    for (let attempt = 0; attempt < 180; attempt += 1) {
      panel = visibleElements(".ant-picker-dropdown").at(-1) || panel;
      cell = Array.from(panel.querySelectorAll(".ant-picker-cell"))
        .find((item) => item.getAttribute("title") === formatted && !/disabled/.test(item.className));
      if (cell) break;
      const header = textOf(panel.querySelector(".ant-picker-header-view"));
      const headerMatch = /(\d{4})\D+(\d{1,2})/.exec(header);
      if (!headerMatch) break;
      const visibleMonthIndex = Number(headerMatch[1]) * 12 + Number(headerMatch[2]) - 1;
      const difference = targetMonthIndex - visibleMonthIndex;
      if (!difference) break;
      const selector = difference < 0
        ? (Math.abs(difference) >= 12 ? ".ant-picker-header-super-prev-btn" : ".ant-picker-header-prev-btn")
        : (Math.abs(difference) >= 12 ? ".ant-picker-header-super-next-btn" : ".ant-picker-header-next-btn");
      const navigation = panel.querySelector(selector);
      if (!navigation || navigation.disabled) break;
      clickLikeUser(navigation);
      await delay(55);
    }
    if (!cell) {
      await dismissFrameworkPicker(element);
      return { ok: false, formatted, reason: `日期选择器中没有 ${formatted}` };
    }

    clickLikeUser(cell.querySelector(".ant-picker-cell-inner") || cell);
    await delay(320);
    await dismissFrameworkPicker(element);
    const actual = input.value;
    const ok = dateMatches(actual, formatted);
    return {
      ok,
      actual,
      formatted,
      warning: ok && !sourceHasDay ? `网站要求具体日期，已按资料月份填写 ${formatted}` : "",
      reason: "Ant 日期选择后未保持"
    };
  }

  async function fillUdPicker(element, value, assignment) {
    const input = element instanceof HTMLInputElement ? element : element.querySelector?.("input");
    if (!(input instanceof HTMLInputElement)) return { ok: false, reason: "飞书日期组件没有可用输入框" };
    const formatted = globalThis.ResumeFillerCore.formatDateForControl(value, {
      controlKind: "ud-date",
      placeholder: input.placeholder || assignment?.placeholder || ""
    });
    const parts = dateParts(formatted) || (/^\d{4}$/.test(formatted) ? { year: Number(formatted), month: null, day: null } : null);
    if (!parts) return { ok: false, reason: "飞书日期资料格式无效" };

    clickLikeUser(input);
    await delay(140);
    let panel = visibleElements(".ud__picker-dropdown").at(-1);
    if (!panel) {
      clickLikeUser(element.closest?.(".ud__picker") || element);
      await delay(140);
      panel = visibleElements(".ud__picker-dropdown").at(-1);
    }
    if (!panel) return { ok: false, reason: "未打开飞书日期选择器" };

    if (input.placeholder === "YYYY") {
      let yearCell = null;
      for (let attempt = 0; attempt < 30; attempt += 1) {
        panel = visibleElements(".ud__picker-dropdown").at(-1) || panel;
        yearCell = Array.from(panel.querySelectorAll(".ud__picker-year-panel-cell"))
          .find((item) => textOf(item) === String(parts.year) && !/disabled/.test(item.className));
        if (yearCell) break;
        const header = textOf(panel.querySelector(".ud__picker-panel-header-btn"));
        const range = header.match(/(\d{4})\D+(\d{4})/);
        if (!range) break;
        const icons = Array.from(panel.querySelectorAll(".ud__picker-panel-header-icon"))
          .filter((item) => !item.classList.contains("ud__picker-panel-header-collapse"));
        const navigation = parts.year < Number(range[1]) ? icons[0] : icons.at(-1);
        if (!navigation) break;
        clickLikeUser(navigation);
        await delay(70);
      }
      if (!yearCell) {
        await dismissFrameworkPicker(element);
        return { ok: false, formatted, reason: `年份选择器中没有 ${parts.year}` };
      }
      clickLikeUser(yearCell.querySelector(".ud__picker__cell-interactive-area") || yearCell);
    } else {
      for (let attempt = 0; attempt < 150; attempt += 1) {
        panel = visibleElements(".ud__picker-dropdown").at(-1) || panel;
        const header = textOf(panel.querySelector(".ud__picker-panel-header-btn"));
        const visibleYear = Number(header.match(/\d{4}/)?.[0]);
        if (!Number.isFinite(visibleYear)) break;
        if (visibleYear === parts.year) break;
        const icons = Array.from(panel.querySelectorAll(".ud__picker-panel-header-icon"))
          .filter((item) => !item.classList.contains("ud__picker-panel-header-collapse"));
        const navigation = parts.year < visibleYear ? icons[0] : icons.at(-1);
        if (!navigation) break;
        clickLikeUser(navigation);
        await delay(55);
      }
      panel = visibleElements(".ud__picker-dropdown").at(-1) || panel;
      const monthText = `${String(parts.month).padStart(2, "0")}月`;
      const monthCell = Array.from(panel.querySelectorAll(".ud__picker-month-panel-cell"))
        .find((item) => textOf(item) === monthText && !/disabled/.test(item.className));
      if (!monthCell) {
        await dismissFrameworkPicker(element);
        return { ok: false, formatted, reason: `月份选择器中没有 ${monthText}` };
      }
      clickLikeUser(monthCell.querySelector(".ud__picker__cell-interactive-area") || monthCell);
    }

    await delay(320);
    await dismissFrameworkPicker(element);
    const actual = input.value;
    return {
      ok: dateMatches(actual, formatted),
      actual,
      formatted,
      reason: "飞书日期选择后未保持"
    };
  }

  async function fillFrameworkDate(element, value, assignment) {
    const kind = controlKind(element);
    if (kind === "ant-date") return fillAntDate(element, value, assignment);
    if (kind === "ud-date") return fillUdPicker(element, value, assignment);
    if (kind === "calendar-date") return fillGenericCalendarDate(element, value, assignment);
    const input = element instanceof HTMLInputElement ? element : element.querySelector?.("input");
    if (!(input instanceof HTMLInputElement)) return { ok: false, reason: "日期组件没有可用输入框" };
    const formatted = globalThis.ResumeFillerCore.formatDateForControl(value, {
      controlKind: controlKind(element),
      placeholder: input.placeholder || assignment?.placeholder || ""
    });
    clickLikeUser(input);
    await delay(80);
    setNativeValue(input, formatted);
    input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
    input.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true, composed: true }));
    input.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", bubbles: true, composed: true }));
    await delay(160);
    await dismissFrameworkPicker(element);
    const actual = input.value || currentValue(element);
    return {
      ok: normalizedIncludes(actual, formatted),
      actual,
      formatted,
      reason: "日期组件未接受目标日期"
    };
  }

  async function commitAutocomplete(element, value) {
    await delay(100);
    const options = visibleElements(
      ".phoenix-selectList__listItem, [role='option'], .autocomplete-item, .ant-select-item-option, .el-select-dropdown__item"
    );
    const target = options.map((item) => ({ item, score: optionScore(textOf(item), value) })).sort((a, b) => b.score - a.score)[0];
    if (target?.score) {
      target.item.click();
      await delay(60);
    }
  }

  function needsAutocomplete(element, assignment) {
    if (!(element instanceof HTMLInputElement)) return false;
    const autocomplete = element.getAttribute("aria-autocomplete");
    if (element.hasAttribute("list") || (autocomplete && autocomplete !== "none")
      || /^(listbox|tree|dialog|true)$/.test(element.getAttribute("aria-haspopup") || "")) return true;
    if (element.closest("[role='combobox'], [class*='autocomplete'], [class*='suggest'], .phoenix-select, .ant-select, .ud__select")) return true;
    return /school|major|company|organization|city|location|address|residence|nativePlace/i.test(assignment?.key || "")
      || /学校|院校|专业|公司|单位|城市|地区|地址|籍贯|搜索|联想/.test(`${getLabel(element)} ${element.placeholder || ""}`);
  }

  async function fillChoiceControl(element, kind, value, assignment) {
    if (/^(至今|present|current)$/i.test(value) && /EndDate$/.test(assignment.key || "")) {
      const scope = getFormItem(element);
      const ongoing = Array.from(scope?.querySelectorAll("input[type='checkbox']") || [])
        .find((input) => /至今|present/i.test(textOf(input.closest("label") || input.parentElement)));
      if (ongoing) {
        if (!ongoing.checked) clickLikeUser(ongoing);
        await delay(120);
        const live = scope.isConnected ? scope : getFormItem(resolveAssignmentElement(assignment));
        const checked = Array.from(live?.querySelectorAll("input[type='checkbox']") || []).some((input) => input.checked && /至今|present/i.test(textOf(input.closest("label") || input.parentElement)));
        return { ok: checked, actual: checked ? "至今" : "", ongoing: true, strategy: "ongoing-checkbox", reason: "至今勾选未保持" };
      }
      return { ok: false, strategy: "ongoing-manual", reason: "项目仍在进行，页面没有可确认的至今选项，请手动选择；不会编造结束日期" };
    }
    if (kind === "moka-location") return { ...await fillMokaLocation(element, value, assignment), strategy: "moka-location-option" };
    if (kind === "brick-select") {
      return { ...await fillBrickSelect(element, value, assignment), strategy: "baidu-brick-select-option" };
    }
    if (kind === "brick-radio") {
      return { ...await fillBrickRadio(element, value, assignment), strategy: "baidu-brick-radio-option" };
    }
    if (kind === "moka-select") {
      return { ...await fillMokaSelect(element, value, assignment), strategy: "moka-select-option" };
    }
    if (kind === "moka-month") {
      return { ...await fillMokaMonth(element, value, assignment), strategy: "moka-year-month-option" };
    }
    if (kind === "element-select") {
      return { ...await fillElementSelect(element, value, assignment), strategy: "element-select-option" };
    }
    if (kind === "element-dropdown") {
      return { ...await fillElementDropdown(element, value, assignment), strategy: "element-dropdown-option" };
    }
    if (kind === "element-cascader") {
      return { ...await fillElementCascader(element, value, assignment), strategy: "element-cascader-path" };
    }
    if (kind === "element-date") {
      return { ...await fillElementDate(element, value, assignment), strategy: "element-date-input" };
    }
    if (kind === "element-radio") {
      return { ...await fillElementRadio(element, value, assignment), strategy: "element-radio-option" };
    }
    if (kind === "phoenix-radio") {
      return { ...await fillPhoenixRadio(element, value, assignment), strategy: "phoenix-radio-option" };
    }
    if (kind === "radio") {
      return { ...await fillNativeRadio(element, value, assignment), strategy: "native-radio-option" };
    }
    if (kind === "ud-radio") {
      return { ...await fillUdRadio(element, value, assignment), strategy: "feishu-radio-option" };
    }
    if (kind === "atsx-radio") {
      return { ...await fillAtsxRadio(element, value, assignment), strategy: "feishu-atsx-radio-option" };
    }
    if (kind === "feishu-year") {
      return { ...await fillFeishuYear(element, value), strategy: "feishu-year-option" };
    }
    if (kind === "feishu-month") {
      return { ...await fillFeishuMonth(element, value), strategy: "feishu-month-option" };
    }
    if (kind === "ud-date" || kind === "ant-date" || kind === "calendar-date") {
      return { ...await fillFrameworkDate(element, value, assignment), strategy: `${kind}-picker` };
    }
    if (kind === "phoenix-select") {
      const result = await fillPhoenixSelect(element, value, assignment);
      const isDate = /Date$/.test(assignment.key || "") || /日期|时间/.test(assignment.label || "");
      return { ...result, strategy: result.strategy || (isDate ? "phoenix-date" : "phoenix-select-option") };
    }
    if (kind === "select") {
      return { ...fillNativeSelect(element, value, assignment), strategy: "native-select-option" };
    }
    if (["combobox", "ant-select", "ud-select", "atsx-select", "multi-select"].includes(kind)) {
      return { ...await fillCombobox(element, value, assignment), strategy: "combobox-option" };
    }
    if (kind === "checkbox") {
      const shouldCheck = /^(true|1|是|有|至今)$/i.test(value);
      if (element.checked !== shouldCheck) element.click();
      dispatchInputEvents(element);
      return {
        ok: element.checked === shouldCheck,
        actual: String(element.checked),
        reason: "勾选状态未更新",
        strategy: "checkbox-toggle"
      };
    }
    return { ok: false, reason: "暂不支持此选择控件", strategy: "unsupported-choice" };
  }

  async function ensurePhonePrefix(element, assignment) {
    const platform = detectPlatform();
    const antComposite = ["kuaishou-ant", "bilibili-ant"].includes(platform);
    if (assignment?.key !== "phone" || !(antComposite || platform === "tencent-element")) return { ok: true };
    const formItem = getFormItem(element);
    const prefix = formItem?.querySelector(antComposite ? ".ant-select, .ant4-select" : ".el-select");
    const platformName = platform === "kuaishou-ant" ? "快手" : platform === "bilibili-ant" ? "B站" : "腾讯";
    if (!prefix) return { ok: false, reason: `${platformName}手机号缺少国家码选择器` };
    let actual = antComposite
      ? textOf(prefix.querySelector(".ant-select-selection-item, .ant4-select-selection-item"))
      : selectedElementValue(prefix);
    if (/\+?86/.test(actual)) return { ok: true, actual };
    if (platform === "tencent-element") {
      const result = await fillElementSelect(prefix, "+86", { ...assignment, key: "phoneCountryCode", label: "国家码" });
      actual = selectedElementValue(prefix);
      return { ok: /\+?86/.test(actual), actual, reason: "腾讯手机号国家码未保持为 +86", warning: result.ok ? "" : result.reason };
    }
    clickLikeUser(prefix.querySelector(".ant-select-selector, .ant4-select-selector") || prefix);
    await delay(180);
    const target = visibleElements(".ant-select-item-option, .ant4-select-item-option, [role='option']").find((option) => /\+?86|中国大陆/.test(textOf(option)));
    if (!target) {
      await dismissFrameworkPicker(prefix);
      return { ok: false, actual, reason: `${platformName}国家码下拉中没有找到 +86` };
    }
    clickLikeUser(target.querySelector(".ant-select-item-option-content, .ant4-select-item-option-content") || target);
    await delay(220);
    actual = textOf(prefix.querySelector(".ant-select-selection-item, .ant4-select-selection-item"));
    return { ok: /\+?86/.test(actual), actual, reason: `${platformName}手机号国家码未保持为 +86` };
  }

  async function fillCustomControl(element, kind, value, assignment) {
    if (kind === "input" || kind === "textarea") {
      const prefixResult = await ensurePhonePrefix(element, assignment);
      if (!prefixResult.ok) return { ...prefixResult, strategy: `${detectPlatform()}-phone-prefix` };
      let inputValue = /Date$/.test(element.dataset?.resumeKey || "")
        ? globalThis.ResumeFillerCore.formatDateForControl(value, { controlKind: kind, placeholder: element.placeholder })
        : value;
      if (element.type === "date" && /^\d{4}-\d{2}$/.test(value)) inputValue = `${value}-01`;
      if (element.placeholder === "YYYY" && /^\d{4}/.test(value)) inputValue = value.slice(0, 4);
      const autocomplete = kind === "input" && needsAutocomplete(element, assignment);
      const unchanged = !autocomplete && element.value === inputValue;
      if (!unchanged) {
        setNativeValue(element, inputValue);
        dispatchInputEvents(element);
      }
      if (autocomplete) await commitAutocomplete(element, inputValue);
      element = resolveAssignmentElement(assignment) || element;
      return {
        ok: normalizedIncludes(element.value, inputValue),
        actual: element.value,
        unchanged,
        reason: "文本值被页面组件拒绝",
        strategy: assignment?.key === "phone" && ["kuaishou-ant", "bilibili-ant", "tencent-element"].includes(detectPlatform())
          ? `${detectPlatform()}-phone-composite`
          : kind === "textarea" ? "textarea-input" : "text-input"
      };
    }
    if (kind === "contenteditable") {
      element.textContent = value;
      dispatchInputEvents(element);
      return {
        ok: normalizedIncludes(element.textContent, value),
        actual: element.textContent,
        reason: "富文本值未更新",
        strategy: "contenteditable-input"
      };
    }
    return { ok: false, reason: "暂不支持此自定义输入控件", strategy: "unsupported-custom" };
  }

  async function fillElement(element, assignment) {
    rememberOriginal(element, assignment.fieldId);
    const value = String(assignment.selectionValue ?? assignment.value ?? "");
    const kind = controlKind(element);
    const category = globalThis.ResumeFillerCore.controlCategory(kind);
    let result;

    if (category === "choice") result = await fillChoiceControl(element, kind, value, assignment);
    else if (category === "custom") result = await fillCustomControl(element, kind, value, assignment);
    else result = {
      ok: false,
      reason: category === "upload" ? "文件需要手动上传" : "暂不支持此控件",
      strategy: category === "upload" ? "manual-upload" : "unsupported"
    };

    element = resolveAssignmentElement(assignment) || element;

    if (result.ok) {
      await delay(60);
      const liveElement = resolveAssignmentElement(assignment) || element;
      const observed = result.ongoing ? result.actual : currentValue(liveElement);
      const kindsWithDirectState = new Set(["radio", "checkbox", "phoenix-radio", "ud-radio", "atsx-radio"]);
      const verificationTarget = result.actual || result.formatted || value;
      const verified = kindsWithDirectState.has(kind)
        ? Boolean(result.ok)
        : Boolean(observed) && normalizedIncludes(observed, verificationTarget);
      if (!verified) {
        result = { ...result, ok: false, actual: observed, reason: "填写后复核发现页面没有保存该值" };
      } else {
        result = { ...result, actual: observed || result.actual, verified: true };
      }
    }

    if (result.ok) {
      await delay(140);
      element = resolveAssignmentElement(assignment) || element;
      const observed = result.ongoing ? result.actual : currentValue(element);
      const directState = ["radio", "checkbox", "phoenix-radio", "ud-radio", "atsx-radio"].includes(kind);
      if (!directState && !result.ongoing && (!element.isConnected || !normalizedIncludes(observed, result.actual || result.formatted || value))) {
        result = { ...result, ok: false, actual: observed, reason: "延迟复核发现页面没有保持该值" };
      }
      const validationError = validationErrorFor(element);
      if (validationError) {
        result = { ...result, ok: false, reason: `网页校验失败：${validationError}` };
      }
    }

    if (result.ok) {
      element.classList.remove("local-resume-match-high", "local-resume-match-medium", "local-resume-match-low");
      element.classList.add("local-resume-filled");
    }
    return {
      ...result,
      controlKind: kind,
      controlCategory: category,
      expected: value,
      platform: detectPlatform(),
      frameUrl: location.href,
      controlFingerprint: toMeta(element).controlFingerprint,
      recordIndex: assignment.recordIndex
    };
  }

  function decodeBase64(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
    return bytes;
  }

  async function uploadResumeFile(fieldIds, attachment) {
    const results = [];
    if (!attachment?.name || !attachment?.base64) return { uploaded: 0, failed: fieldIds, results };
    const bytes = decodeBase64(attachment.base64);
    let uploaded = 0;
    const failed = [];
    for (const fieldId of fieldIds) {
      const element = document.querySelector(`[${markerAttribute}="${CSS.escape(fieldId)}"]`);
      if (!(element instanceof HTMLInputElement) || element.type !== "file") {
        failed.push(fieldId);
        results.push({ fieldId, ok: false, reason: "简历上传框已经失效，请重新扫描" });
        continue;
      }
      try {
        const file = new File([bytes], attachment.name, {
          type: attachment.type || "application/octet-stream",
          lastModified: attachment.lastModified || Date.now()
        });
        const transfer = new DataTransfer();
        transfer.items.add(file);
        element.files = transfer.files;
        element.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
        element.dispatchEvent(new Event("change", { bubbles: true, composed: true }));
        await delay(180);
        const accepted = Array.from(element.files || []).some((item) => item.name === attachment.name)
          || !document.contains(element);
        if (accepted) uploaded += 1;
        else failed.push(fieldId);
        results.push({ fieldId, ok: accepted, actual: attachment.name, reason: accepted ? "" : "网页未接受简历文件" });
      } catch (error) {
        failed.push(fieldId);
        results.push({ fieldId, ok: false, reason: error instanceof Error ? error.message : String(error) });
      }
    }
    return { uploaded, failed, results };
  }

  function scan(resume) {
    const elements = collectCandidates();
    const metas = elements.map(toMeta);
    const matches = globalThis.ResumeFillerCore.matchFields(metas, resume);
    const matchedIds = new Set(matches.map((match) => match.fieldId));
    const missingFields = metas.filter((meta) => !meta.hasValue && !matchedIds.has(meta.fieldId) && globalThis.ResumeFillerCore.fieldHasIdentity(meta) && meta.controlKind !== "file");
    const learnableFields = metas.filter((meta) => meta.hasValue && !matchedIds.has(meta.fieldId) && globalThis.ResumeFillerCore.fieldHasIdentity(meta) && meta.controlKind !== "file");
    const manualFields = metas.filter((meta) => !meta.hasValue && globalThis.ResumeFillerCore.fieldHasIdentity(meta) && meta.controlKind === "file");
    highlight(matches);
    return {
      totalFields: metas.length,
      emptyFields: metas.filter((meta) => !meta.hasValue).length,
      matches,
      missingFields,
      learnableFields,
      manualFields
    };
  }

  function resolveAssignmentElement(assignment) {
    const cached = assignedFieldIds.get(assignment.fieldId);
    if (cached?.isConnected && cached.getAttribute(markerAttribute) === assignment.fieldId) return cached;
    const direct = document.querySelector(`[${markerAttribute}="${CSS.escape(assignment.fieldId)}"]`);
    if (direct) {
      assignedFieldIds.set(assignment.fieldId, direct);
      return direct;
    }
    const normalize = globalThis.ResumeFillerCore.normalize;
    const label = normalize(assignment.label || "");
    const section = normalize(assignment.section || "");
    const expectedCategory = globalThis.ResumeFillerCore.controlCategory(assignment.controlKind);
    if (!label && !section) return null;
    const candidates = collectCandidates().map((element) => {
      const title = getSection(element);
      return {
        element,
        label: normalize(getLabel(element)),
        section: normalize(title),
        recordIndex: getRecordIndex(element, title),
        category: globalThis.ResumeFillerCore.controlCategory(controlKind(element))
      };
    }).filter((candidate) =>
      (!label || candidate.label === label)
      && (!section || candidate.section === section)
      && (!Number.isInteger(assignment.recordIndex) || candidate.recordIndex === assignment.recordIndex)
      && (expectedCategory === "unsupported" || candidate.category === expectedCategory)
    );
    if (candidates.length !== 1) return null;
    const element = candidates[0].element;
    const previousId = element.getAttribute(markerAttribute);
    if (previousId && assignedFieldIds.get(previousId) === element) assignedFieldIds.delete(previousId);
    element.setAttribute(markerAttribute, assignment.fieldId);
    assignedFieldIds.set(assignment.fieldId, element);
    return element;
  }

  async function fill(assignments) {
    let filled = 0;
    const failed = [];
    const results = [];
    const preventSubmit = (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    document.addEventListener("submit", preventSubmit, true);
    try {
      for (const assignment of assignments) {
        const element = resolveAssignmentElement(assignment);
        if (!element) {
          failed.push(assignment.fieldId);
          results.push({
            fieldId: assignment.fieldId,
            key: assignment.key,
            label: assignment.label,
            section: assignment.section,
            controlKind: assignment.controlKind,
            controlCategory: globalThis.ResumeFillerCore.controlCategory(assignment.controlKind),
            expected: String(assignment.value ?? ""),
            ok: false,
            reason: "页面结构已经变化，请重新扫描"
          });
          continue;
        }
        let progressTarget = null;
        try {
          progressTarget = await followFillProgress(element);
          const result = await fillElement(element, assignment);
          results.push({
            fieldId: assignment.fieldId,
            key: assignment.key,
            label: assignment.label,
            section: assignment.section,
            ...result
          });
          if (result.ok) filled += 1;
          else failed.push(assignment.fieldId);
        } catch (error) {
          failed.push(assignment.fieldId);
          results.push({
            fieldId: assignment.fieldId,
            key: assignment.key,
            label: assignment.label,
            section: assignment.section,
            controlKind: controlKind(element),
            controlCategory: globalThis.ResumeFillerCore.controlCategory(controlKind(element)),
            expected: String(assignment.value ?? ""),
            ok: false,
            reason: `${assignment.label || "当前字段"}填写异常：${error instanceof Error ? error.message : String(error)}`
          });
        } finally {
          progressTarget?.classList.remove("local-resume-filling");
        }
      }
    } finally {
      document.removeEventListener("submit", preventSubmit, true);
    }
    return { filled, failed, results };
  }

  function findExactText(text) {
    return Array.from(document.querySelectorAll("button, [role='button'], span, div"))
      .filter((element) => isVisible(element) && textOf(element) === text)
      .sort((a, b) => a.children.length - b.children.length)[0];
  }

  function desiredRepeaters(resume) {
    const publications = Array.isArray(resume?.publications) ? resume.publications : [];
    const patentCount = publications.filter((item) => /专利|patent/i.test(`${item.type || ""}${item.name || ""}`)).length;
    const paperCount = publications.length - patentCount;
    return new Map([
      ["教育经历", resume?.education?.length || 0],
      ["工作经历", resume?.careers?.length || 0],
      ["实习经历", resume?.internships?.length || 0],
      ["项目经历", resume?.projects?.length || 0],
      ["课题项目经验", resume?.projects?.length || 0],
      ["获奖", resume?.awards?.length || 0],
      ["获奖情况", resume?.awards?.length || 0],
      ["竞赛获奖", resume?.awards?.length || 0],
      ["论文", paperCount],
      ["论文/专著", paperCount],
      ["专利", patentCount],
      ["论文/专著/专利", publications.length],
      ["语言能力", resume?.basic?.englishLevel || resume?.certificates?.length ? 1 : 0],
      ["技能", resume?.structuredSkills?.length || 0],
      ["IT技能", resume?.structuredSkills?.length || 0],
      ["技能证书", resume?.certificates?.length || 0],
      ["在校实践", resume?.practices?.length || 0],
      ["作品", resume?.works?.length || 0],
      ["作品信息", resume?.works?.length || 0],
      ["自我评价", resume?.selfIntroduction ? 1 : 0]
    ]);
  }

  function indexedRecordCount(container, sectionTitle) {
    const prefixes = sectionTitle === "教育经历" ? ["educations"]
      : /项目/.test(sectionTitle) ? ["projects"]
        : /实习/.test(sectionTitle) ? ["internships"]
          : /获奖/.test(sectionTitle) ? ["awards"]
            : /论文|专利/.test(sectionTitle) ? ["publications"]
              : /语言/.test(sectionTitle) ? ["languages"] : [];
    const indexes = new Set();
    container.querySelectorAll("[id],[name]").forEach((element) => {
      const identity = `${element.id || ""} ${element.getAttribute("name") || ""}`;
      for (const prefix of prefixes) {
        const match = new RegExp(`${prefix}[_-](\\d+)[_-]`, "i").exec(identity);
        if (match) indexes.add(Number(match[1]));
      }
    });
    if (indexes.size) return indexes.size;
    if (sectionTitle === "教育经历") return container.querySelectorAll(".eduction-container, [class*='education-container']").length;
    return container.querySelectorAll("[class*='item-container'], [class*='record-container']").length;
  }

  async function prepareFormilyRepeaters(resume) {
    const desired = desiredRepeaters(resume);
    let added = 0;
    const modules = Array.from(document.querySelectorAll("[class*='applyFormModuleWrapper']"));
    for (const module of modules) {
      const title = Array.from(desired.keys()).find((name) => {
        const text = textOf(module);
        return text === name || text.startsWith(`${name} `);
      });
      const targetCount = desired.get(title) || 0;
      if (!title || !targetCount) continue;
      let current = module.querySelectorAll("[class*='apply-form-array-card__']").length;
      let attempts = 0;
      while (current < targetCount && attempts < targetCount + 1) {
        const addButton = Array.from(module.querySelectorAll("button, [role='button']"))
          .find((button) => textOf(button) === "添加" && isVisible(button));
        if (!addButton) break;
        clickLikeUser(addButton);
        attempts += 1;
        await delay(260);
        const next = module.querySelectorAll("[class*='apply-form-array-card__']").length;
        if (next <= current && !module.className.includes("empty")) break;
        added += Math.max(1, next - current);
        current = Math.max(next, current + 1);
      }
    }
    return added;
  }

  async function prepareKuaishouRepeaters(resume) {
    const desired = desiredRepeaters(resume);
    const patentNames = new Set((resume?.publications || [])
      .filter((item) => /专利|patent/i.test(`${item.type || ""}${item.name || ""}`))
      .map((item) => globalThis.ResumeFillerCore.normalize(item.name))
      .filter(Boolean));
    let added = 0;
    const sections = Array.from(document.querySelectorAll(".edit-resume-form-item"));
    for (const section of sections) {
      const title = textOf(section.querySelector(".edit-resume-form-item-title"));
      const targetCount = desired.get(title) || 0;
      if (title === "IT技能" && targetCount === 0) {
        for (let attempt = 0; attempt < 20; attempt += 1) {
          const records = Array.from(section.querySelectorAll(".edit-resume-form-item-container"));
          const legacyRecord = records.reverse().find((record) => {
            const categoryItem = Array.from(record.querySelectorAll(".ant-form-item"))
              .find((item) => /技能类别/.test(textOf(item.querySelector(".ant-form-item-label"))));
            return categoryItem && !textOf(categoryItem.querySelector(".ant-select-selection-item"));
          });
          const removeButton = Array.from(legacyRecord?.querySelectorAll("button") || [])
            .find((button) => textOf(button) === "删除" && isVisible(button));
          if (!removeButton) break;
          const before = records.length;
          clickLikeUser(removeButton);
          await delay(180);
          if (section.querySelectorAll(".edit-resume-form-item-container").length >= before) break;
        }
        continue;
      }
      if (title === "论文" && targetCount === 0 && patentNames.size) {
        for (let attempt = 0; attempt < 10; attempt += 1) {
          const records = Array.from(section.querySelectorAll(".edit-resume-form-item-container"));
          const legacyRecord = records.find((record) => {
            const nameItem = Array.from(record.querySelectorAll(".ant-form-item"))
              .find((item) => /论文名称/.test(textOf(item.querySelector(".ant-form-item-label"))));
            const inputValue = nameItem?.querySelector("input, textarea")?.value || "";
            return patentNames.has(globalThis.ResumeFillerCore.normalize(inputValue));
          });
          const removeButton = Array.from(legacyRecord?.querySelectorAll("button") || [])
            .find((button) => textOf(button) === "删除" && isVisible(button));
          if (!removeButton) break;
          const before = records.length;
          clickLikeUser(removeButton);
          await delay(180);
          if (section.querySelectorAll(".edit-resume-form-item-container").length >= before) break;
        }
        continue;
      }
      if (!targetCount) continue;
      let current = indexedRecordCount(section, title);
      let attempts = 0;
      while (current < targetCount && attempts < targetCount + 1) {
        const addButton = Array.from(section.querySelectorAll(".action-button button, .edit-resume-content > button"))
          .find((button) => textOf(button) === "添加" && isVisible(button));
        if (!addButton) break;
        clickLikeUser(addButton);
        attempts += 1;
        await delay(260);
        const next = indexedRecordCount(section, title);
        if (next <= current) break;
        added += next - current;
        current = next;
      }
    }
    return added;
  }

  function detectedRecordCount(sectionElement, sectionTitle) {
    const indexes = [];
    for (const item of sectionElement.querySelectorAll(formItemSelector)) {
      for (const control of primaryControls(item)) {
        const index = getRecordIndex(control, sectionTitle);
        if (Number.isInteger(index)) indexes.push(index);
      }
    }
    return indexes.length ? Math.max(...indexes) + 1 : 0;
  }

  async function prepareMokaRepeaters(resume) {
    if (detectPlatform() !== "moka-sd") return 0;
    const configs = [
      [/education/i, "教育经历", resume?.education?.length || 0],
      [/project/i, "项目经历", resume?.projects?.length || 0],
      [/intern|practice/i, "实习经历", resume?.internships?.length || 0],
      [/award|honor/i, "获奖情况", resume?.awards?.length || 0]
    ];
    let added = 0;
    for (const section of document.querySelectorAll("[data-nav-id]")) {
      const navId = section.getAttribute("data-nav-id") || "";
      const config = configs.find(([pattern]) => pattern.test(navId));
      if (!config) continue;
      const [, title, desired] = config;
      if (!desired) continue;
      const findSection = () => {
        const sections = Array.from(document.querySelectorAll("[data-nav-id]"))
          .filter((item) => item.getAttribute("data-nav-id") === navId);
        return sections.length === 1 ? sections[0] : null;
      };
      const sectionKey = globalThis.ResumeRepeaterEngine.inferSection(title).key;
      let current = detectedRecordCount(section, title) || (section.querySelector(formItemSelector) ? 1 : 0);
      for (let attempt = 0; current < desired && attempt < desired + 1; attempt += 1) {
        const button = Array.from(findSection()?.querySelectorAll("button, [role='button']") || [])
          .find((item) => textOf(item) === "添加" && isVisible(item) && !isDisabledControl(item));
        if (!button) break;
        const beforeEditors = visibleRepeaterEditors().length;
        clickLikeUser(button);
        const change = await waitForRepeaterChange(findSection, sectionKey, current, beforeEditors);
        if (change.type !== "inline") break;
        const next = change.count;
        added += next - current;
        current = next;
      }
    }
    return added;
  }

  async function prepareTencentRepeaters(resume) {
    if (detectPlatform() !== "tencent-element") return 0;
    const configs = [
      ["page-resume-sections4", "教育经历", resume?.education?.length || 0, /添加学历|增加学历|添加教育/],
      ["page-resume-sections6", "项目经历", resume?.projects?.length || 0, /添加项目/],
      ["page-resume-sections8", "获奖情况", resume?.awards?.length || 0, /添加获奖|添加奖项/]
    ];
    let added = 0;
    for (const [id, title, desired, buttonPattern] of configs) {
      const section = document.getElementById(id);
      if (!section || !desired) continue;
      let current = section.querySelectorAll(".experience_box > .info_list").length || detectedRecordCount(section, title) || 1;
      for (let attempt = 0; current < desired && attempt < desired + 1; attempt += 1) {
        const button = Array.from(section.querySelectorAll("button, [role='button'], a"))
          .find((item) => buttonPattern.test(textOf(item)) && isVisible(item));
        if (!button) break;
        clickLikeUser(button);
        await delay(280);
        const next = section.querySelectorAll(".experience_box > .info_list").length || detectedRecordCount(section, title);
        if (next <= current) break;
        added += next - current;
        current = next;
      }
    }

    if (!(resume?.internships?.length || 0)) {
      const section = document.getElementById("page-resume-sections5") || document.body;
      const text = Array.from(section.querySelectorAll("label, span, div"))
        .filter((item) => isVisible(item) && /无实习经历/.test(textOf(item)))
        .sort((a, b) => a.children.length - b.children.length)[0];
      const checkbox = text?.closest("label")?.querySelector("input[type='checkbox']")
        || text?.parentElement?.querySelector("input[type='checkbox']")
        || section.querySelector("input[type='checkbox'][value*='无实习']");
      if (text && !checkbox?.checked && !/is-checked|checked/.test(String(text.closest("label")?.className || ""))) {
        clickLikeUser(text.closest("label") || text);
        await delay(160);
      }
    }
    return added;
  }

  async function prepareBaiduRepeaters(resume) {
    if (detectPlatform() !== "baidu") return 0;
    const configs = [
      [/添加教育经历/, "教育经历", resume?.education?.length || 0],
      [/添加项目经验|添加项目经历/, "项目经历", resume?.projects?.length || 0]
    ];
    let added = 0;
    for (const [pattern, title, desired] of configs) {
      if (desired < 2) continue;
      let current = Math.max(1, ...collectCandidates().map((control) =>
        getSection(control) === title ? (getRecordIndex(control, title) ?? 0) + 1 : 0
      ));
      for (let attempt = 0; current < desired && attempt < desired; attempt += 1) {
        const button = Array.from(document.querySelectorAll("button, [role='button'], a, span"))
          .filter((item) => isVisible(item) && pattern.test(textOf(item)))
          .sort((a, b) => a.children.length - b.children.length)[0];
        if (!button) break;
        clickLikeUser(button.closest("button, [role='button'], a") || button);
        await delay(260);
        current += 1;
        added += 1;
      }
    }
    if (!(resume?.careers?.length || 0)) {
      const label = Array.from(document.querySelectorAll("label, span, div"))
        .filter((item) => isVisible(item) && textOf(item) === "无工作经历")
        .sort((a, b) => a.children.length - b.children.length)[0];
      const checkbox = label?.closest("label")?.querySelector("input[type='checkbox']")
        || label?.parentElement?.querySelector("input[type='checkbox']");
      if (label && !checkbox?.checked) clickLikeUser(label.closest("label") || label);
    }
    return added;
  }

  const repeaterSectionAttribute = "data-local-resume-repeater-section";
  const repeaterControlAttribute = "data-local-resume-repeater-control";
  const repeaterControlSelector = "button, [role='button'], a, [aria-label], [title], [id*='addButton'], [id*='add_button'], [class*='addButton']";
  let nextRepeaterSectionId = 1;
  let nextRepeaterControlId = 1;

  function ensureRepeaterMarker(element, attribute, prefix) {
    const existing = element?.getAttribute?.(attribute);
    if (existing) return existing;
    const id = `${prefix}-${prefix === "repeater-section" ? nextRepeaterSectionId++ : nextRepeaterControlId++}`;
    element?.setAttribute?.(attribute, id);
    return id;
  }

  function repeaterControlMeta(control, sectionKey) {
    const meta = {
      controlId: ensureRepeaterMarker(control, repeaterControlAttribute, "repeater-control"),
      text: textOf(control).slice(0, 120),
      ariaLabel: (control.getAttribute?.("aria-label") || "").slice(0, 120),
      title: (control.getAttribute?.("title") || "").slice(0, 120),
      name: (control.getAttribute?.("name") || "").slice(0, 100),
      id: String(control.id || "").slice(0, 100),
      className: String(control.className || "").slice(0, 180),
      disabled: Boolean(control.disabled || control.getAttribute?.("aria-disabled") === "true")
    };
    return { ...meta, score: globalThis.ResumeRepeaterEngine.addControlScore(meta, sectionKey) };
  }

  function formControlsInside(root) {
    return Array.from(root?.querySelectorAll?.(
      "input:not([type='hidden']):not([type='button']):not([type='submit']), textarea, select, [role='combobox'], [contenteditable='true']"
    ) || []).filter((element) => isVisible(element) && !element.disabled);
  }

  function recordStructureSignature(element) {
    const tokens = String(element?.className || "").split(/\s+/)
      .filter((token) => token && !/^(?:active|open|selected|current|error|valid|invalid|\w*\d{3,}\w*)$/i.test(token))
      .slice(0, 4).sort();
    return `${element?.tagName || ""}|${tokens.join(".")}`;
  }

  function structuralRecordCount(root) {
    let maximum = 0;
    const parents = [root, ...Array.from(root?.querySelectorAll?.("div, section, ul, ol, fieldset") || []).slice(0, 450)];
    for (const parent of parents) {
      const groups = new Map();
      for (const child of Array.from(parent?.children || [])) {
        if (child.matches?.(formItemSelector)) continue;
        const controlCount = formControlsInside(child).length;
        if (controlCount < 2) continue;
        const identity = `${child.id || ""} ${child.className || ""}`;
        const recordLike = /item|record|entry|card|experience|education|project|award|honor|intern|practice|language|skill/i.test(identity)
          || controlCount >= 3;
        if (!recordLike) continue;
        const signature = recordStructureSignature(child);
        groups.set(signature, (groups.get(signature) || 0) + 1);
      }
      for (const count of groups.values()) maximum = Math.max(maximum, count);
    }
    return maximum;
  }

  function currentRepeaterCount(root, sectionKey) {
    if (!root) return 0;
    if (root.matches?.("[class*='applyFormModuleWrapper']")) {
      return root.querySelectorAll("[class*='apply-form-array-card__']").length;
    }
    if (root.matches?.(".edit-resume-form-item")) {
      const title = textOf(root.querySelector(".edit-resume-form-item-title"));
      return indexedRecordCount(root, title) || root.querySelectorAll(".edit-resume-form-item-container").length;
    }
    if (root.matches?.("[id^='page-resume-sections']")) {
      const records = root.querySelectorAll(".experience_box > .info_list").length;
      if (records) return records;
    }
    if (root.matches?.("[data-nav-id]") && detectPlatform() === "moka-sd") {
      const title = getSection(root.querySelector("input, textarea, select") || root);
      return detectedRecordCount(root, title) || (root.querySelector(formItemSelector) ? 1 : 0);
    }
    const controls = collectCandidates(root);
    const knownTitle = {
      education: "教育经历", career: "工作经历", internship: "实习经历", project: "项目经历",
      award: "获奖情况", paper: "论文", patent: "专利", practice: "在校实践",
      language: "语言能力", certificate: "证书", skill: "技能"
    }[sectionKey] || "";
    const indexes = controls.map((control) => {
      const section = getSection(control);
      return { section, index: getRecordIndex(control, section) };
    }).filter((item) => !knownTitle || item.section === knownTitle || globalThis.ResumeRepeaterEngine.inferSection(item.section).key === sectionKey)
      .map((item) => item.index).filter(Number.isInteger);
    if (indexes.length) return Math.max(...indexes) + 1;
    const structural = structuralRecordCount(root);
    if (structural >= 2) return structural;
    return formControlsInside(root).length >= 2 ? 1 : 0;
  }

  function semanticAncestor(control) {
    const platformRoot = control.closest?.("[class*='applyFormModuleWrapper'], .edit-resume-form-item, [id^='page-resume-sections'], [data-nav-id]");
    if (platformRoot) {
      const platform = detectPlatform();
      const valid = platformRoot.matches("[class*='applyFormModuleWrapper'], .edit-resume-form-item")
        || platform === "tencent-element" && platformRoot.matches("[id^='page-resume-sections']")
        || platform === "moka-sd" && platformRoot.matches("[data-nav-id]");
      const heading = getSection(control);
      const inferred = globalThis.ResumeRepeaterEngine.inferSection(heading);
      if (valid && inferred.score >= 78) return { root: platformRoot, inferred, heading, knownPlatformRoot: true };
    }
    const localRoot = control.closest("[data-nav-id], .createFormSection-mutiple");
    if (localRoot) {
      const heading = getSection(control);
      const inferred = globalThis.ResumeRepeaterEngine.inferSection(heading);
      if (inferred.score >= 78) return { root: localRoot, inferred, heading };
    }
    let best = null;
    let node = control.parentElement;
    for (let depth = 0; node && node !== document.body && depth < 11; depth += 1, node = node.parentElement) {
      const sectionKinds = new Set(Array.from(node.querySelectorAll("legend, h1, h2, h3, h4, [role='heading'], [class*='title'], [class*='header']"))
        .map((heading) => globalThis.ResumeRepeaterEngine.inferSection(textOf(heading)))
        .filter((item) => item.score >= 92).map((item) => item.key));
      if (sectionKinds.size > 1) continue;
      const directHeading = node.querySelector?.(":scope > legend, :scope > h1, :scope > h2, :scope > h3, :scope > h4, :scope > [role='heading'], :scope > [class*='title'], :scope > [class*='header']");
      const previous = node.previousElementSibling;
      const hint = [node.id, node.className, textOf(directHeading), textOf(previous), getSection(control), textOf(control)]
        .filter(Boolean).join(" ").slice(0, 500);
      const inferred = globalThis.ResumeRepeaterEngine.inferSection(hint);
      if (inferred.score < 72) continue;
      const controls = formControlsInside(node).length;
      const hasHeading = Boolean(directHeading && textOf(directHeading));
      let boundaryScore = globalThis.ResumeRepeaterEngine.sectionScore(textOf(directHeading), inferred.key);
      let nearby = previous;
      for (let count = 0; nearby && count < 4; count += 1, nearby = nearby.previousElementSibling) {
        boundaryScore = Math.max(boundaryScore, globalThis.ResumeRepeaterEngine.sectionScore(textOf(nearby).slice(0, 220), inferred.key));
      }
      const rootScore = inferred.score + Math.min(18, controls * 3) + (hasHeading ? 14 : 0)
        + (boundaryScore >= 72 ? 24 : 0) - depth;
      if (!best || rootScore > best.rootScore) best = { root: node, inferred, heading: textOf(directHeading) || getSection(control), rootScore };
    }
    return best;
  }

  function discoverRepeaters(resume) {
    const grouped = new Map();
    for (const control of Array.from(document.querySelectorAll(repeaterControlSelector))) {
      if (!isVisible(control)) continue;
      const controlHint = [textOf(control), control.getAttribute?.("aria-label"), control.getAttribute?.("title"), control.id, control.className]
        .filter(Boolean).join(" ");
      if (!/添加|新增|增加|继续添加|再加|新建|创建|补充|add|new|create|plus|^\s*[+＋]\s*$/i.test(controlHint)) continue;
      const semantic = semanticAncestor(control);
      if (!semantic) continue;
      const sectionKey = semantic.inferred.key;
      const controlMeta = repeaterControlMeta(control, sectionKey);
      if (semantic.knownPlatformRoot && controlMeta.score > 0) {
        controlMeta.score = Math.max(controlMeta.score, 82);
      }
      if (!controlMeta.score && !controlMeta.disabled) continue;
      // A button and its nested icon/label represent one action, not two ambiguous entrances.
      const actionParent = control.parentElement?.closest("button, [role='button'], a");
      if (actionParent && semantic.root.contains(actionParent)
        && globalThis.ResumeRepeaterEngine.addControlScore(repeaterControlMeta(actionParent, sectionKey), sectionKey) > 0) continue;
      const sectionId = ensureRepeaterMarker(semantic.root, repeaterSectionAttribute, "repeater-section");
      const existing = grouped.get(sectionId) || {
        sectionId,
        sectionKey,
        sectionConfidence: semantic.inferred.score,
        heading: semantic.heading || sectionKey,
        currentCount: currentRepeaterCount(semantic.root, sectionKey),
        platform: detectPlatform(),
        frameUrl: location.href,
        controls: []
      };
      if (!existing.controls.some((item) => item.controlId === controlMeta.controlId)) existing.controls.push(controlMeta);
      grouped.set(sectionId, existing);
    }
    const discoveries = Array.from(grouped.values());
    const planned = globalThis.ResumeRepeaterEngine.buildPlans(discoveries, resume);
    return { discoveries, plans: planned.plans, unresolved: planned.unresolved };
  }

  function visibleRepeaterEditors() {
    return Array.from(document.querySelectorAll(
      "[role='dialog'], .ant-modal, .ant4-modal, .el-dialog, [class*='drawer'], [class*='modal']"
    )).filter((element) => isVisible(element) && formControlsInside(element).length > 0);
  }

  async function waitForRepeaterChange(root, sectionKey, beforeCount, beforeEditors, cancelToken) {
    const liveRoot = () => typeof root === "function" ? root() : root;
    return new Promise((resolve) => {
      let settled = false;
      let queued = null;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        observer.disconnect();
        clearInterval(poll);
        clearTimeout(timeout);
        clearTimeout(queued);
        resolve(value);
      };
      const inspect = () => {
        if (cancelToken?.cancelled) return finish({ type: "cancelled", count: currentRepeaterCount(liveRoot(), sectionKey) });
        const count = currentRepeaterCount(liveRoot(), sectionKey);
        if (count > beforeCount) return finish({ type: "inline", count });
        const editors = visibleRepeaterEditors();
        if (editors.length > beforeEditors) return finish({ type: "editor", count, editor: editors.at(-1) });
        return null;
      };
      const observer = new MutationObserver(() => {
        if (settled || queued !== null) return;
        queued = setTimeout(() => { queued = null; if (!settled) inspect(); }, 16);
      });
      observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true, attributeFilter: ["class", "style", "aria-hidden"] });
      const poll = setInterval(inspect, 100);
      const timeout = setTimeout(() => finish({ type: "none", count: currentRepeaterCount(liveRoot(), sectionKey) }), 2200);
      inspect();
    });
  }

  async function executeGenericRepeaterPlans(resume, suppliedPlans, cancelToken, deadline = Infinity) {
    const initial = discoverRepeaters(resume);
    const requestedPlans = Array.isArray(suppliedPlans) && suppliedPlans.length ? suppliedPlans : initial.plans;
    const plans = globalThis.ResumeRepeaterEngine.validatePlans({ plans: requestedPlans }, initial.discoveries, resume);
    let added = 0;
    const issues = [];
    for (const plan of plans) {
      if (cancelToken?.cancelled || Date.now() >= deadline) break;
      const original = initial.discoveries.find((item) => item.sectionId === plan.sectionId);
      const uniqueSection = initial.discoveries.filter((item) => item.sectionKey === plan.sectionKey && item.heading === original?.heading).length === 1;
      const findCurrent = () => {
        const report = discoverRepeaters(resume);
        const sameSection = report.discoveries.find((item) => item.sectionId === plan.sectionId);
        const replacements = report.discoveries.filter((item) => item.sectionKey === plan.sectionKey && item.heading === original?.heading);
        const discovery = sameSection || (uniqueSection && replacements.length === 1 ? replacements[0] : null);
        const livePlan = report.plans.find((item) => item.sectionId === discovery?.sectionId);
        const root = discovery && document.querySelector(`[${repeaterSectionAttribute}="${CSS.escape(discovery.sectionId)}"]`);
        const control = livePlan && document.querySelector(`[${repeaterControlAttribute}="${CSS.escape(livePlan.controlId)}"]`);
        return { root, control };
      };
      let attempts = 0;
      while (attempts < Math.max(1, plan.desiredCount - plan.currentCount)) {
        if (cancelToken?.cancelled || Date.now() >= deadline) break;
        const { root, control } = findCurrent();
        const current = root ? currentRepeaterCount(root, plan.sectionKey) : 0;
        if (current >= plan.desiredCount) break;
        if (!root || !control || !isVisible(control)) {
          issues.push({ sectionKey: plan.sectionKey, reason: "动态区块结构已变化，未继续点击添加按钮" });
          break;
        }
        const meta = repeaterControlMeta(control, plan.sectionKey);
        if (!globalThis.ResumeRepeaterEngine.addControlScore(meta, plan.sectionKey)) {
          issues.push({ sectionKey: plan.sectionKey, reason: "添加控件未通过安全复核" });
          break;
        }
        const beforeEditors = visibleRepeaterEditors().length;
        clickLikeUser(control);
        attempts += 1;
        const change = await waitForRepeaterChange(() => root.isConnected ? root : findCurrent().root, plan.sectionKey, current, beforeEditors, cancelToken);
        if (cancelToken?.cancelled || change.type === "cancelled") break;
        if (change.type === "inline") {
          added += Math.max(1, change.count - current);
          continue;
        }
        if (change.type === "editor") {
          issues.push({ sectionKey: plan.sectionKey, reason: "已安全打开新增记录弹层；请填写当前弹层后再次同步其余记录", editorOpened: true });
          break;
        }
        issues.push({ sectionKey: plan.sectionKey, reason: "点击后未观察到记录增加，已停止防止重复操作" });
        break;
      }
    }
    return { added, issues };
  }

  async function prepareRepeaters(resume, repeaterPlans = []) {
    const preventSubmit = (event) => {
      event.preventDefault();
      event.stopImmediatePropagation();
    };
    document.addEventListener("submit", preventSubmit, true);
    let platformAdded = 0;
    try {
      platformAdded += await prepareFormilyRepeaters(resume);
      platformAdded += await prepareKuaishouRepeaters(resume);
      platformAdded += await prepareMokaRepeaters(resume);
      platformAdded += await prepareTencentRepeaters(resume);
      platformAdded += await prepareBaiduRepeaters(resume);
    } finally {
      document.removeEventListener("submit", preventSubmit, true);
    }
    const feishuConfigs = [
      ["教育经历", resume?.education?.length || 0],
      ["工作经历", resume?.careers?.length || 0],
      ["实习经历", resume?.internships?.length || 0],
      ["项目经历", resume?.projects?.length || 0],
      ["获奖", resume?.awards?.length || 0],
      ["语言能力", resume?.basic?.englishLevel || resume?.certificates?.length ? 1 : 0]
    ];
    let added = platformAdded;
    for (const [sectionTitle, desired] of feishuConfigs) {
      if (!desired) continue;
      const findSection = () => {
        const sections = Array.from(document.querySelectorAll(".createFormSection-mutiple"))
          .filter((item) => textOf(item.querySelector(".createFormSection-text")) === sectionTitle);
        return sections.length === 1 ? sections[0] : null;
      };
      const section = findSection();
      if (!section) continue;
      const sectionKey = globalThis.ResumeRepeaterEngine.inferSection(sectionTitle).key;
      let current = currentRepeaterCount(section, sectionKey);
      let attempts = 0;
      while (current < desired && attempts < desired + 1) {
        const addControl = findSection()?.querySelector(".formOperate-addBtn, .createFormSection-addBtn");
        if (!addControl || !isVisible(addControl) || isDisabledControl(addControl)) break;
        const beforeEditors = visibleRepeaterEditors().length;
        clickLikeUser(addControl);
        attempts += 1;
        const change = await waitForRepeaterChange(findSection, sectionKey, current, beforeEditors);
        if (change.type !== "inline") break;
        const nextCount = change.count;
        added += nextCount - current;
        current = nextCount;
      }
    }

    const generic = await executeGenericRepeaterPlans(resume, repeaterPlans);
    return { added: added + generic.added, issues: generic.issues };
  }

  async function undo() {
    let restored = 0;
    const failed = [];
    for (const [fieldId, original] of originalValues.entries()) {
      const element = document.querySelector(`[${markerAttribute}="${CSS.escape(fieldId)}"]`);
      if (!element) {
        failed.push(fieldId);
        continue;
      }
      if (original.kind === "input" || original.kind === "textarea") {
        setNativeValue(element, original.value);
        dispatchInputEvents(element);
        restored += 1;
      } else if (original.kind === "radio") {
        const radios = element.name
          ? Array.from(document.querySelectorAll(`input[type='radio'][name="${CSS.escape(element.name)}"]`))
          : Array.from(getFormItem(element)?.querySelectorAll("input[type='radio']") || [element]);
        radios.forEach((radio) => { radio.checked = Boolean(original.value) && normalizedIncludes(`${radio.value} ${textOf(radio.closest("label") || radio.parentElement)}`, original.value); });
        radios.forEach(dispatchInputEvents);
        restored += 1;
      } else if (original.kind === "checkbox") {
        if (element.checked !== original.checked) element.click();
        dispatchInputEvents(element);
        restored += 1;
      } else if (original.kind === "phoenix-select" && !original.value) {
        const clear = getFormItem(element)?.querySelector(".phoenix-select__clearIcon");
        if (clear) {
          clear.click();
          restored += 1;
        } else failed.push(fieldId);
      } else failed.push(fieldId);
      element.classList.remove("local-resume-filled");
    }
    originalValues.clear();
    return { restored, failed };
  }

  async function fillSmsCode(rawCode) {
    const code = String(rawCode || "").trim();
    if (!/^\d{4,8}$/.test(code)) return { ok: false, found: false, error: "验证码格式无效" };
    const captchaPattern = /验证码|captcha|校验码|动态码|激活码|短信码|sms[\s\S]{0,6}code/i;
    let matched = null;
    for (const element of collectCandidates()) {
      const meta = toMeta(element);
      if (!["input", "contenteditable"].includes(meta.controlKind)) continue;
      if (element.disabled || element.readOnly) continue;
      const identity = [meta.label, meta.ariaLabel, meta.placeholder, meta.name, meta.id].join(" ");
      if (!captchaPattern.test(identity)) continue;
      matched = { element, meta };
      break;
    }
    if (!matched) return { ok: true, found: false };
    const result = await fillElement(matched.element, {
      fieldId: matched.meta.fieldId,
      value: code,
      label: "验证码",
      source: "sms"
    });
    return {
      ok: result.ok,
      found: true,
      actual: result.actual,
      reason: result.reason,
      strategy: result.strategy,
      controlKind: result.controlKind,
      platform: result.platform
    };
  }

  // ---- 投递记录：页面上下文猜测与快照 ----
  // 纯页面识别（不依赖任何公司清单）：meta → JSON-LD → 标题模式 → 页面品牌位 → 域名兜底。

  const RECRUIT_PLATFORM_BRAND_RE = /^(moka|moka招聘|北森|beisen|智业|zhiye|boss|boss直聘|智联|智联招聘|前程无忧|51job|牛客|nowcoder|猎聘|liepin|拉勾|lagou|海投网|应届生|linkedin|indeed|glassdoor)$/i;
  const RECRUIT_NAV_TITLE_RE = /(投递记录|投递查询|我的投递|申请记录|投递历史|应聘记录|求职进展|个人中心|我的申请|申请进度|职位列表|招聘职位|职位详情|搜索职位|职位搜索)/i;

  function recruitTextOf(el) {
    return String(el?.textContent || "").replace(/\s+/g, " ").trim();
  }

  function recruitJsonLd() {
    try {
      const scripts = document.querySelectorAll('script[type="application/ld+json"]');
      const items = [];
      for (const s of scripts) {
        try {
          const data = JSON.parse(s.textContent);
          const arr = Array.isArray(data) ? data : [data];
          for (const d of arr) {
            if (d && d["@graph"]) items.push(...d["@graph"].filter(Boolean));
            else if (d) items.push(d);
          }
        } catch { /* 忽略坏 JSON */ }
      }
      return items;
    } catch { return []; }
  }

  function recruitJsonLdCompany() {
    for (const item of recruitJsonLd()) {
      const org = item?.hiringOrganization || item?.organization || item?.publisher || (item?.["@type"] === "Organization" ? item : null);
      const name = org?.name || (typeof org === "string" ? org : "");
      if (typeof name === "string" && name.trim().length >= 2) return name.trim();
    }
    return "";
  }

  function recruitJsonLdJobTitle() {
    for (const item of recruitJsonLd()) {
      if (item?.["@type"] === "JobPosting" && typeof item.title === "string" && item.title.trim()) return item.title.trim();
    }
    return "";
  }

  function recruitCleanCompanySuffix(text) {
    let t = String(text || "").replace(/\s+/g, " ").trim();
    t = t.replace(/^欢迎加入\s*/, "");
    t = t.replace(/^[【\[（(《「『\s]+|[】\]）)》」』\s]+$/g, "");
    if (!t) return "";
    // 整段就是站点词（"校园招聘"/"招聘官网"）→ 不是公司名
    if (/^(首页|官网|内部推荐|校园招聘|社会招聘|校招|社招|人才招聘|招聘官网|招聘首页|招聘|人才发展|加入我们|职位列表|职位详情|投递记录|投递查询|我的投递|个人中心)$/i.test(t)) return "";
    const m = t.match(/^(.*?)(校园招聘|社会招聘|校招|社招|人才招聘|招聘官网|招聘首页|招聘|人才发展|加入我们|职位列表|职位详情|投递记录|投递查询|我的投递|个人中心)$/i);
    if (m && m[1].trim().length >= 2) return m[1].trim();
    return t;
  }

  function recruitLooksLikeCompany(text) {
    const t = String(text || "").trim();
    if (!t || t.length < 2 || t.length > 30) return "";
    if (RECRUIT_NAV_TITLE_RE.test(t)) return "";
    if (RECRUIT_PLATFORM_BRAND_RE.test(t)) return "";
    if (/^(首页|官网|内部推荐|校园招聘|社会招聘|校招|社招|人才招聘|招聘官网|招聘首页|招聘|人才发展|加入我们|职位列表|职位详情|投递记录|投递查询|我的投递|个人中心)$/i.test(t)) return "";
    if (/logo|二维码|图标|icon|扫描|扫码|手机|验证码|校验码|captcha|滑块|背景|障碍/i.test(t)) return "";
    return t;
  }

  function recruitCompanyFromTitle() {
    const raw = String(document.title || "").trim();
    if (!raw) return "";
    const parts = raw.split(/\s*(?:-|–|—|\||_|·|｜|丨|:|：)\s*/u).map((s) => s.trim()).filter(Boolean);
    // 优先取含「招聘/投递/人才」等站点词的那段（去掉后缀即是公司名）
    for (const p of parts) {
      if (!/(招聘|投递|申请|人才|加入我们|官网|职位)/i.test(p)) continue;
      const c = recruitLooksLikeCompany(recruitCleanCompanySuffix(p));
      if (c) return c;
    }
    // 兜底：从尾段往前找可用的公司名
    for (let i = parts.length - 1; i >= 0; i--) {
      const c = recruitLooksLikeCompany(recruitCleanCompanySuffix(parts[i]));
      if (c) return c;
    }
    return "";
  }

  function recruitCompanyFromHeader() {
    const nodes = document.querySelectorAll('img[alt], img[title], header a, .logo, [class*="logo"], [class*="company-name"], [class*="companyName"], [class*="org-name"], [class*="orgName"]');
    for (const el of nodes) {
      const attr = String(el.getAttribute?.("alt") || el.getAttribute?.("title") || "");
      const candidate = recruitLooksLikeCompany(recruitCleanCompanySuffix(attr || recruitTextOf(el)));
      if (candidate) return candidate;
    }
    return "";
  }

  function recruitCompanyFromFooter() {
    const nodes = document.querySelectorAll("footer, [class*='footer'], [id*='footer']");
    for (const node of nodes) {
      const text = recruitTextOf(node).slice(0, 1200).replace(/(?:©|Copyright)\s*\d{4}(?:-\d{4})?\s*/ig, " ");
      const match = /([\u4e00-\u9fa5A-Za-z0-9·]{2,30})(?:校园招聘|社会招聘|招聘官网|人才招聘)/i.exec(text);
      const candidate = recruitLooksLikeCompany(recruitCleanCompanySuffix(match?.[1] || ""));
      if (candidate) return candidate;
    }
    return "";
  }

  function recruitCompanyFromHostSlug() {
    return globalThis.ResumeFillerCore.recruitBrandFromHost(location.hostname);
  }

  function recruitCompanyCandidates(allowHostFallback = true) {
    const brandedTitle = /(?:校园招聘|社会招聘|校招|社招|人才招聘|招聘官网|招聘首页|招聘)\s*(?:[-|｜—·]|$)/i.test(document.title || "")
      ? recruitCompanyFromTitle() : "";
    const values = [
      String(document.querySelector('meta[property="og:site_name"]')?.content || "").trim(),
      recruitJsonLdCompany(),
      recruitCompanyFromFooter(),
      brandedTitle,
      recruitCompanyFromHeader(),
      recruitCompanyFromTitle(),
      allowHostFallback ? recruitCompanyFromHostSlug() : ""
    ].map((value) => recruitLooksLikeCompany(recruitCleanCompanySuffix(value))).filter(Boolean);
    return [...new Set(values)];
  }

  function recruitGuessCompany() {
    return recruitCompanyCandidates()[0] || "";
  }

  function recruitActiveStatusHint(root) {
    const selectors = [
      "[aria-current='step']", "[aria-current='true']", "[class*='target-view']",
      "[class*='current-step']", "[class*='active-step']", "[class~='current']", "[class~='active']"
    ];
    const statusLike = /投递|申请|简历|筛选|评估|测评|测试|笔试|面试|终试|洽谈|录用|offer|签约|淘汰|不合适|拒绝|结束|已挂/i;
    const blocked = /^(?:修改申请|撤回|撤回申请|取消申请|编辑|查看|详情)$/i;
    for (const element of root.querySelectorAll(selectors.join(","))) {
      const lines = String(element.innerText || element.textContent || "").split(/\n+/)
        .map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
      for (const line of lines.slice().reverse()) {
        if (line.length <= 50 && statusLike.test(line) && !blocked.test(line)) return line;
      }
    }
    return "";
  }

  function recruitStandaloneStatusHint(root) {
    const statusLike = /投递|申请|简历|筛选|评估|测评|测试|笔试|面试|终试|洽谈|录用|offer|签约|淘汰|不合适|拒绝|结束|已挂/i;
    const selectors = [
      "[class^='status-']", "[class*=' status-']",
      "[class^='recordStatus']", "[class*=' recordStatus']",
      "[class^='record-status']", "[class*=' record-status']"
    ];
    for (const element of root.querySelectorAll(selectors.join(","))) {
      const text = recruitTextOf(element);
      if (text && text.length <= 50 && !text.includes("\n") && statusLike.test(text)) return text;
    }
    return "";
  }

  function recruitRecordBlockTexts() {
    const dateRe = /20\d{2}[年./-]\s?\d{1,2}[月./-]\s?\d{1,2}日?/;
    const strongRecordRe = /(?:第\s*\d+\s*志愿[\s\S]*(?:状态\s*[:：]|申请成功|投递成功)|(?:状态\s*[:：]|申请成功|投递成功)[\s\S]*(?:修改申请|撤回|撤回申请|取消申请)|(?:修改申请|撤回|撤回申请|取消申请)[\s\S]*(?:状态\s*[:：]|申请成功|投递成功))/;
    const blocks = new Set();
    const titledBlocks = new Map();
    if (/candidateHome\/applications|position\/application|deliveryRecord/i.test(location.href)) {
      const titles = document.querySelectorAll("[class*='job-title'], [class*='jobTitle'], [class*='job-name'], [class*='jobName'], [class*='position-name'], [class*='positionName'], a[href*='#/job/']");
      for (const title of titles) {
        const name = recruitTextOf(title);
        if (!name || name.length > 90 || RECRUIT_NAV_TITLE_RE.test(name)) continue;
        for (let node = title.parentElement, depth = 0; node && node !== document.body && depth < 6; node = node.parentElement, depth++) {
          const text = String(node.innerText || node.textContent || "").trim();
          if (text.length > 1200) break;
          if (!dateRe.test(text) && !/已投递|投递简历|申请成功|投递成功/.test(text)) continue;
          if ([...titles].some(other => other !== title && node.contains(other) && recruitTextOf(other) !== name)) break;
          blocks.add(node);
          titledBlocks.set(node, name);
          break;
        }
      }
    }
    // 一些招聘系统把按钮实现为普通 div/span（例如 class 中含 button/btn，
    // 但没有 button 标签和 role）。同时保留操作文案白名单，避免把任意容器
    // 当成投递卡片入口。
    const actions = Array.from(document.querySelectorAll(
      "button, a, [role='button'], [class*='button'], [class*='Button'], [class*='btn'], [class*='Btn']"
    ))
      .filter((element) => /^(?:编辑|查看|查看\/打印|详情|修改申请|撤回|撤回申请|取消申请)$/.test(recruitTextOf(element)));
    for (const action of actions) {
      let node = action.parentElement;
      for (let depth = 0; node && node !== document.body && depth < 8; depth += 1, node = node.parentElement) {
        const text = String(node.innerText || node.textContent || "").trim();
        if (!dateRe.test(text) && !strongRecordRe.test(text)) continue;
        if (text.length <= 1200) blocks.add(node);
        break;
      }
    }
    const structural = document.querySelectorAll(
      "tr, article, li[class*='item'], li[class*='record'], li[class*='Record'], [class*='card'], [class*='Card'], [class*='record'], [class*='Record'], [class*='apply-item'], [class*='application'], [class*='preference']"
    );
    for (const node of structural) {
      const text = String(node.innerText || node.textContent || "").trim();
      if ((dateRe.test(text) || strongRecordRe.test(text)) && text.length <= 1200) blocks.add(node);
    }
    return Array.from(blocks).map((node) => {
      const raw = String(node.innerText || node.textContent || "").trim();
      const text = titledBlocks.has(node) ? `${titledBlocks.get(node)}\n${raw}` : raw;
      const activeStatus = recruitActiveStatusHint(node) || recruitStandaloneStatusHint(node);
      if (!activeStatus || globalThis.ResumeFillerCore.recruitStatusFromText(text)) return text;
      return `${text}\n当前进度：${activeStatus}`;
    });
  }

  function recruitRecordCandidates() {
    return globalThis.ResumeFillerCore.recruitRecordCandidates(recruitRecordBlockTexts())
      .map((candidate) => ({ ...candidate, source: "record-card" }));
  }

  function recruitTitleFromRecordTable() {
    return recruitRecordCandidates()[0]?.title || "";
  }

  function recruitTitleFromJobSelectors() {
    const sels = [
      '[class*="job-title"]', '[class*="jobTitle"]', '[class*="job-name"]', '[class*="jobName"]',
      '[class*="job_title"]', '[class*="job_name"]',
      '[class*="position-title"]', '[class*="positionTitle"]', '[class*="position-name"]', '[class*="positionName"]',
      '[class*="position_title"]', '[class*="position_name"]',
      '[class*="post-title"]', '[class*="postTitle"]', '[class*="job-detail"] h1', 'h1'
    ];
    for (const sel of sels) {
      for (const el of document.querySelectorAll(sel)) {
        const t = recruitTextOf(el);
        if (!t || t.length < 2 || t.length > 40) continue;
        if (RECRUIT_NAV_TITLE_RE.test(t) || /(招聘|加入我们|人才|官网|首页|校园招聘|社会招聘|投递|申请|记录|进度|状态|搜索)/i.test(t)) continue;
        return t;
      }
    }
    return "";
  }

  function recruitTitleFromDocument() {
    const raw = String(document.title || "").trim();
    if (!raw || RECRUIT_NAV_TITLE_RE.test(raw)) return "";
    const parts = raw.split(/\s*(?:-|–|—|\||_|·|｜|丨|:|：)\s*/u).map((s) => s.trim()).filter(Boolean);
    if (parts.length >= 2) {
      const first = parts[0];
      if (first && first.length >= 2 && first.length <= 40 && !RECRUIT_NAV_TITLE_RE.test(first) && !/(招聘|加入我们|人才|官网|首页|校园|社会|投递|申请|记录)/i.test(first)) return first;
    }
    if (raw.length >= 2 && raw.length <= 40 && !/(招聘|加入我们|人才|官网|首页|校园|社会|投递|申请|记录)/i.test(raw)) return raw;
    return "";
  }

  function recruitTitleMatchesCompany(title) {
    const compact = (value) => String(value || "")
      .toLowerCase()
      .replace(/[\s·|｜丨_—–-]+/g, "")
      .trim();
    const candidate = compact(title);
    return Boolean(candidate) && recruitCompanyCandidates()
      .some((company) => compact(company) === candidate);
  }

  function recruitGuessTitle() {
    return [
      recruitJsonLdJobTitle(),
      recruitTitleFromRecordTable(),
      recruitTitleFromJobSelectors(),
      recruitTitleFromDocument()
    ].find((title) => title && !recruitTitleMatchesCompany(title)) || "";
  }

  function recruitTitleCandidates() {
    const records = recruitRecordCandidates();
    const single = [recruitJsonLdJobTitle(), recruitTitleFromJobSelectors(), recruitTitleFromDocument()]
      .filter((title) => title && !recruitTitleMatchesCompany(title))
      .map((title) => ({ title, date: "", source: "page" }));
    const all = [...records, ...single];
    return all.filter((candidate, index) => all.findIndex((item) => item.title === candidate.title) === index);
  }

  function recruitPageContext(allowHostFallback = true) {
    const companies = recruitCompanyCandidates(allowHostFallback);
    return {
      url: location.href,
      title: document.title,
      ogSiteName: String(document.querySelector('meta[property="og:site_name"]')?.content || "").trim(),
      hostname: location.hostname,
      companyGuess: companies[0] || "",
      companyCandidates: companies,
      titleGuess: recruitGuessTitle(),
      titleCandidates: recruitTitleCandidates()
    };
  }

  function recruitSnapshot() {
    return {
      company: recruitGuessCompany(),
      title: recruitGuessTitle(),
      url: location.href,
      at: Date.now()
    };
  }

  async function recruitRememberContext() {
    try {
      await chrome.storage.session.set({ recruitLastContext: recruitSnapshot() });
    } catch {
      // storage.session 不可用时静默失败，不影响填写流程
    }
  }

  const messageHandler = (message, _sender, sendResponse) => {
    Promise.resolve().then(async () => {
      if (message?.type === "RESUME_SCAN") return { ok: true, ...scan(message.resume) };
      if (message?.type === "RESUME_DISCOVER_REPEATERS") return { ok: true, ...discoverRepeaters(message.resume) };
      if (message?.type === "RESUME_FILL") {
        const result = await fill(message.assignments || []);
        if (result.filled > 0) recruitRememberContext().catch(() => {});
        return { ok: true, ...result };
      }
      if (message?.type === "RESUME_PREPARE") return { ok: true, ...await prepareRepeaters(message.resume, message.repeaterPlans) };
      // Desktop preparation is add-only and limited to the validated plans.
      if (message?.type === "RESUME_PREPARE_SCOPED") return { ok: true, ...await executeGenericRepeaterPlans(message.resume, message.repeaterPlans, message.cancelToken, message.deadline) };
      if (message?.type === "RESUME_UPLOAD") return { ok: true, ...await uploadResumeFile(message.fieldIds || [], message.attachment) };
      if (message?.type === "RESUME_UNDO") return { ok: true, ...await undo() };
      if (message?.type === "SMS_FILL_CODE") return await fillSmsCode(message.code);
      if (message?.type === "RECRUIT_GET_PAGE_CONTEXT") {
        recruitRememberContext().catch(() => {});
        return { ok: true, ...recruitPageContext(message.allowHostFallback !== false) };
      }
      return { ok: false, error: "未知操作。" };
    }).then(sendResponse).catch((error) => sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) }));
    return true;
  };

  globalThis.__LOCAL_RESUME_FILLER_HANDLER__ = messageHandler;
  chrome.runtime.onMessage.addListener(messageHandler);
})();
