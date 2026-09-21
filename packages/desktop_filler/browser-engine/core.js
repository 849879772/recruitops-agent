(function initResumeFillerCore(root) {
  "use strict";

  const FIELD_RULES = [
    { key: "highestStudyMode", aliases: ["最高学历培养方式", "最高学历学习形式"] },
    { key: "educationIsHighest", aliases: ["是否最高学历", "是否最高学位"] },
    { key: "educationIsFirst", aliases: ["是否第一学历"] },
    { key: "graduationYear", aliases: ["第几届应届生", "毕业届别", "毕业届次"] },
    { key: "projectOrganization", aliases: ["项目经历公司名称", "项目经历单位名称", "项目合作单位"] },
    { key: "projectOngoing", aliases: ["项目经历至今", "课题项目经验至今"] },
    { key: "hasProjects", aliases: ["是否有项目经历"] },
    { key: "hasCertificates", aliases: ["是否有证书"] },
    { key: "languageSummary", aliases: ["外语水平"] },
    { key: "credentialSummary", aliases: ["获得资格证书/职称/荣誉情况"] },
    { key: "patentDescription", aliases: ["专利/论文描述", "专利描述"] },
    { key: "englishScore", aliases: ["语言能力得分"] },
    { key: "acceptLocationTransfer", aliases: ["是否接受地域调配"] },
    { key: "acceptRoleTransfer", aliases: ["是否接受岗位调配", "是否愿意接受岗位调剂"] },
    { key: "fullName", aliases: ["姓名", "真实姓名", "应聘人姓名", "申请人姓名", "full name", "applicant name", "candidate name"] },
    { key: "gender", aliases: ["性别", "gender", "sex"] },
    { key: "age", aliases: ["年龄", "age"] },
    { key: "birthDate", aliases: ["出生日期", "出生年月", "生日", "date of birth", "birth date"] },
    { key: "nationality", aliases: ["民族", "ethnicity"] },
    { key: "countryRegion", aliases: ["国籍（地区）", "国籍地区", "国籍", "国家地区", "country/region", "citizenship", "nationality"] },
    { key: "height", aliases: ["身高", "身高cm", "height"] },
    { key: "weight", aliases: ["体重", "体重kg", "weight"] },
    { key: "idNumber", aliases: ["证件号", "证件号码", "身份证", "身份证号", "身份证号码", "id number", "identity number"] },
    { key: "idType", aliases: ["证件类型", "个人证件类型", "identity type", "id type"] },
    { key: "phone", aliases: ["手机号", "手机号码", "联系电话", "联系电话号码", "移动电话", "手机", "phone number", "mobile phone", "telephone"] },
    { key: "phoneType", aliases: ["手机号码类型", "手机号类型", "电话类型", "phone type"] },
    { key: "email", aliases: ["电子邮箱", "邮箱地址", "联系邮箱", "邮箱", "email address", "email"] },
    { key: "nativePlace", aliases: ["籍贯", "生源地", "家乡", "户籍所在地", "native place", "hometown"] },
    { key: "currentResidence", aliases: ["现居住地", "现居地", "目前所在地", "当前所在地", "当前所处地", "所在地点", "所在城市", "current residence", "current location"] },
    { key: "applicantStatus", aliases: ["应届/往届", "应届往届", "毕业生类型", "求职者类型", "applicant status"] },
    { key: "failedCourses", aliases: ["挂科门数", "挂科数", "failed courses"] },
    { key: "passedCET4", aliases: ["是否通过英语四级", "是否通过四级", "passed cet4"] },
    { key: "englishLevel", aliases: ["最高英语证书", "所获英语证书", "语言等级", "英语证书名称", "英语等级", "英语水平", "外语考试/等级", "外语考试等级", "english level"] },
    { key: "englishScore", aliases: ["英语等级成绩", "英语成绩", "四级成绩", "六级成绩", "成绩", "english score"] },
    { key: "languageName", aliases: ["语言能力语言", "语言类型", "语种", "语言类别", "语言名称", "语言", "language"] },
    { key: "languageProficiency", aliases: ["语言能力精通程度", "精通程度", "掌握程度", "语言熟练程度", "language proficiency"] },
    { key: "emergencyContact", aliases: ["紧急联系人", "emergency contact"] },
    { key: "emergencyPhone", aliases: ["紧急联系电话", "紧急联系人电话", "emergency phone"] },
    { key: "recruitmentSource", aliases: ["获取职位信息的渠道", "招聘信息的来源", "招聘来源", "招聘渠道", "信息来源", "招聘信息渠道", "您从什么渠道获取了我们的招聘信息", "recruitment source"] },
    { key: "relativesAtCompany", aliases: ["是否有亲友受雇于本公司", "亲友受雇", "relatives at company"] },
    { key: "healthStatus", aliases: ["健康状况", "身体状况", "health status"] },
    { key: "desiredSalary", aliases: ["期望月薪", "期望薪资", "期望薪酬", "desired salary", "expected salary"] },
    { key: "interviewSite", aliases: ["面试站点", "面试地点", "interview site"] },
    { key: "preferredCity", aliases: ["意向城市", "期望城市", "工作地点", "意向工作地点", "期望工作地点", "期待工作城市", "期望工作地", "意向工作地", "preferred city", "preferred location"] },
    { key: "acceptLocationTransfer", aliases: ["是否接受地点调剂", "接受地点调剂", "是否接受工作地点调剂", "接受调剂到其他城市", "是否接受城市调剂", "工作城市是否服从分配", "是否还接受其他城市分配", "接受其他城市分配", "location transfer"] },
    { key: "acceptRoleTransfer", aliases: ["是否接受岗位调剂", "接受岗位调剂", "是否接受部门调剂", "接受部门调剂", "role transfer", "department transfer"] },
    { key: "targetRole", aliases: ["求职方向", "求职岗位", "意向岗位方向", "意向岗位", "期望岗位", "目标职位类别", "应聘职位", "申请职位", "target role", "desired position"] },
    { key: "highestDegree", aliases: ["最高学历", "highest degree", "highest education"] },
    { key: "highestSchool", aliases: ["最高学历毕业院校", "最高学历学校", "最高学历院校", "highest education school"] },
    { key: "highestMajor", aliases: ["最高学历专业", "highest education major"] },
    { key: "school", aliases: ["毕业院校", "学校名称", "就读学校", "院校名称", "所在学校", "学校", "university", "college", "school name", "institution"] },
    { key: "major", aliases: ["所学专业", "专业名称", "就读专业", "专业", "major", "field of study"] },
    { key: "college", aliases: ["学院名称", "所在学院", "院系", "college name", "faculty"] },
    { key: "educationLocation", aliases: ["目前就读地", "学校所在地", "学校所在省", "学校所在市", "院校所在地", "education location", "school location"] },
    { key: "educationLevel", aliases: ["教育经历学历", "教育背景学历", "所获学历", "学历层次", "学历", "education level"] },
    { key: "academicDegree", aliases: ["教育经历学位", "教育背景学位", "学位", "academic degree"] },
    { key: "studyMode", aliases: ["学历性质", "学历类型", "学习形式", "培养方式", "就读形式", "是否全日制", "study mode"] },
    { key: "educationStartDate", aliases: ["入学时间", "教育开始时间", "就读开始时间", "入校时间", "教育经历开始时间", "教育背景开始时间", "education start date", "academic start date"] },
    { key: "graduationDate", aliases: ["最高学历毕业日期", "最高学历毕业时间", "毕业日期", "毕业时间", "预计毕业时间", "graduation date"] },
    { key: "educationEndDate", aliases: ["教育结束时间", "就读结束时间", "离校时间", "毕业时间", "教育经历结束时间", "教育背景结束时间", "education end date", "academic end date"] },
    { key: "overseasStudy", aliases: ["是否为海外留学经历", "是否海外留学", "overseas study"] },
    { key: "gpa", aliases: ["成绩GPA", "GPA成绩", "GPA", "平均绩点", "grade point average"] },
    { key: "gpaPersonalScore", aliases: ["绩点个人得分", "个人绩点得分", "个人绩点", "gpa personal score"] },
    { key: "gpaFullScore", aliases: ["绩点满分", "绩点总分", "gpa满分", "GPA-BASE", "gpa base", "gpa scale", "maximum gpa"] },
    { key: "gradeRank", aliases: ["成绩排名", "专业排名", "grade rank"] },
    { key: "advisor", aliases: ["导师姓名", "导师", "研究生导师", "指导教师", "advisor", "supervisor"] },
    { key: "projectName", aliases: ["项目名称", "project name"] },
    { key: "projectRole", aliases: ["项目经历职务", "课题项目经验职务", "项目职务", "项目经历角色", "项目角色", "在项目中担任的角色", "项目中担任的角色", "project role"] },
    { key: "projectStartDate", aliases: ["项目经历开始时间", "课题项目经验开始时间", "项目开始时间", "project start date"] },
    { key: "projectEndDate", aliases: ["项目经历结束时间", "课题项目经验结束时间", "项目结束时间", "project end date"] },
    { key: "projectResponsibility", aliases: ["项目中职责", "项目经历职责", "项目职责", "职责描述", "主要职责", "承担职责", "project responsibility", "project duties"] },
    { key: "projectDescription", aliases: ["项目经历描述", "项目描述", "项目内容", "project description"] },
    { key: "projectResults", aliases: ["项目成果", "项目业绩", "project results"] },
    { key: "projectLink", aliases: ["项目链接", "项目地址", "project link", "project url"] },
    { key: "awardName", aliases: ["奖项", "获奖项", "奖项名称", "获奖名称", "竞赛名称", "赛事名称", "竞赛奖项", "award name"] },
    { key: "awardType", aliases: ["获奖类型", "奖项类型", "award type"] },
    { key: "awardLevel", aliases: ["奖励级别", "获奖级别", "奖项级别", "竞赛获奖等级", "竞赛获奖级别", "竞赛等级", "award level"] },
    { key: "awardDate", aliases: ["获奖时间", "获奖日期", "竞赛获奖时间", "竞赛获奖日期", "award date"] },
    { key: "awardDescription", aliases: ["获奖描述", "奖项描述", "赛事描述", "奖项说明", "award description"] },
    { key: "certificateName", aliases: ["技能证书", "证书名称", "certificate name"] },
    { key: "certificateType", aliases: ["证书种类", "证书类型", "certificate type"] },
    { key: "certificateDate", aliases: ["证书获得时间", "获得时间", "取证时间", "certificate date"] },
    { key: "patentName", aliases: ["专利名称", "patent name"] },
    { key: "patentType", aliases: ["专利类型", "专利种类", "patent type"] },
    { key: "patentDate", aliases: ["专利时间", "专利日期", "专利申请时间", "专利授权时间", "patent date"] },
    { key: "patentAuthorOrder", aliases: ["专利作者顺序", "发明人顺序", "发明人排名", "patent author order"] },
    { key: "publicationName", aliases: ["论文/专著名称", "论文名称", "专著名称", "publication name"] },
    { key: "publicationType", aliases: ["论文/专著所属期刊", "所属期刊", "期刊名称", "publication type"] },
    { key: "publicationDate", aliases: ["论文/专著发布时间", "发布时间", "发表时间", "publication date"] },
    { key: "authorOrder", aliases: ["论文/专著作者顺序", "作者顺序", "作者排名", "author order"] },
    { key: "hasScholarship", aliases: ["是否获得校级及以上奖学金", "是否获得奖学金", "has scholarship"] },
    { key: "hasCorePublication", aliases: ["是否在核心期刊有论文发表", "核心期刊论文", "core publication"] },
    { key: "hasInternship", aliases: ["是否有实习经历", "有无实习经历", "has internship"] },
    { key: "practiceName", aliases: ["实践名称", "校内实践名称", "practice name"] },
    { key: "practiceStartDate", aliases: ["在校实践开始时间", "实践开始时间", "practice start date"] },
    { key: "practiceEndDate", aliases: ["在校实践结束时间", "实践结束时间", "practice end date"] },
    { key: "practiceDescription", aliases: ["实践描述", "校内实践描述", "practice description"] },
    { key: "internshipCompany", aliases: ["实习经历单位名称", "实习单位", "单位名称", "internship company"] },
    { key: "internshipRole", aliases: ["实习经历职位名称", "实习职位", "职位名称", "internship role"] },
    { key: "internshipStartDate", aliases: ["实习经历开始时间", "实习开始时间", "internship start date"] },
    { key: "internshipEndDate", aliases: ["实习经历结束时间", "实习结束时间", "internship end date"] },
    { key: "internshipDescription", aliases: ["实习内容", "实习描述", "internship description"] },
    { key: "skillName", aliases: ["技能名称", "skill name"] },
    { key: "skillLevel", aliases: ["掌握程度", "熟练程度", "skill level"] },
    { key: "developerLanguages", aliases: ["开发语言", "编程语言", "programming languages", "developer languages"] },
    { key: "aiTools", aliases: ["常用的AI工具&模型", "常用AI工具模型", "AI工具与模型", "ai tools", "ai models"] },
    { key: "aiCollaboration", aliases: ["与AI协作完成的项目或任务", "AI协作项目", "AI协作任务", "ai collaboration"] },
    { key: "languageSpeaking", aliases: ["听说能力", "听说", "口语能力", "听力口语", "speaking ability"] },
    { key: "languageReadingWriting", aliases: ["读写能力", "读写", "阅读写作", "reading and writing"] },
    { key: "skills", aliases: ["技能特长", "专业技能", "技能", "个人技能", "skills", "technical skills"] },
    { key: "honors", aliases: ["荣誉证书", "获奖经历", "所获荣誉", "奖项汇总", "honors", "awards summary", "certificates summary"] },
    { key: "projectExperience", aliases: ["项目经历", "项目经验", "项目介绍", "project experience", "projects"] },
    { key: "selfIntroduction", aliases: ["自我介绍", "自我评价", "个人总结", "个人陈述", "个人优势", "self introduction", "personal statement", "summary"] }
  ];

  function normalize(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[\s\u00a0:_：*＊()（）\[\]【】/\\.\-|]+/g, "")
      .trim();
  }

  const PROVINCE_LEVEL_REGIONS = [
    "北京市", "天津市", "河北省", "山西省", "内蒙古自治区", "辽宁省", "吉林省", "黑龙江省",
    "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省", "河南省", "湖北省",
    "湖南省", "广东省", "广西壮族自治区", "海南省", "重庆市", "四川省", "贵州省", "云南省",
    "西藏自治区", "陕西省", "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区",
    "台湾省", "香港特别行政区", "澳门特别行政区"
  ];

  function controlCategory(kind) {
    if ([
      "phoenix-radio", "ud-radio", "atsx-radio", "radio", "phoenix-select",
      "combobox", "ant-select", "ud-select", "atsx-select", "multi-select",
      "feishu-year", "feishu-month", "ud-date", "ant-date", "calendar-date", "select", "checkbox",
      "moka-select", "moka-month", "moka-location", "element-select", "element-cascader",
      "element-date", "element-radio", "element-dropdown", "brick-select", "brick-radio"
    ].includes(kind)) return "choice";
    if (["input", "textarea", "contenteditable"].includes(kind)) return "custom";
    if (kind === "file") return "upload";
    return "unsupported";
  }

  function isAntPickerRootClass(className) {
    return String(className || "").split(/\s+/).some((token) => /^ant[\w-]*-picker$/.test(token));
  }

  function isFeishuPeriodRootClass(className) {
    return String(className || "").split(/\s+/).some((token) =>
      ["atsx-date-picker-period-month", "atsx-date-picker-period-line"].includes(token));
  }

  function fieldDisposition(meta) {
    if (meta.disabled || meta.readOnly || isFeishuPeriodRootClass(meta.className)) return "ignore";
    const label = String(meta.label || meta.placeholder || meta.ariaLabel || "");
    if (/^(?:输入|搜索)(?:职位|岗位)(?:关键字|关键词)/.test(label)) return "ignore";
    if (meta.controlKind === "file" || meta.type === "file") return "attachment";
    if (/隐私政策|本人承诺|校招声明|同意.*协议/.test(label)) return "confirmation";
    return "field";
  }

  function unmatchedReason(meta, resume) {
    const disposition = fieldDisposition(meta);
    if (disposition === "attachment") return "需要另行选择对应的证明附件，不属于文字填写故障";
    if (disposition === "confirmation") return "声明或协议需要本人阅读并手动确认";
    const { rule, score } = bestCanonicalRule(meta);
    if (rule && score >= 55) {
      const raw = buildResumeValues(resume)[rule.key];
      const value = Array.isArray(raw) ? raw[meta.recordIndex ?? 0] : raw;
      if (!value) return "已识别字段含义，但资料中尚无答案；请自行填写或补充资料";
      return "资料已有候选答案，但字段归属或重复匹配需要复核";
    }
    return "尚无可靠的字段映射或已确认答案；请补充问题与答案，不能自动推断";
  }

  function fieldHasIdentity(meta) {
    return Boolean([meta?.label, meta?.ariaLabel, meta?.placeholder, meta?.name, meta?.id]
      .some((value) => String(value || "").trim()));
  }

  const DATE_KEYS = new Set([
    "birthDate", "graduationDate", "educationStartDate", "educationEndDate",
    "projectStartDate", "projectEndDate", "awardDate", "certificateDate", "patentDate",
    "publicationDate", "practiceStartDate", "practiceEndDate",
    "internshipStartDate", "internshipEndDate"
  ]);

  function calculateAge(birthDate, now = new Date()) {
    const match = /^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$/.exec(String(birthDate || "").trim());
    if (!match || Number.isNaN(now?.getTime?.())) return "";
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    let age = now.getFullYear() - year;
    if (now.getMonth() + 1 < month || (now.getMonth() + 1 === month && now.getDate() < day)) age -= 1;
    return age >= 0 && age < 150 ? String(age) : "";
  }

  function formatDateForControl(value, meta = {}) {
    const text = String(value || "").trim().replace(/[/.]/g, "-");
    const match = /^(\d{4})-(\d{1,2})(?:-(\d{1,2}))?$/.exec(text);
    if (!match) return text;
    const year = match[1];
    const month = String(Number(match[2])).padStart(2, "0");
    const day = match[3] ? String(Number(match[3])).padStart(2, "0") : "";
    const placeholder = String(meta.placeholder || "").toUpperCase();
    const kind = String(meta.controlKind || "");
    if (placeholder === "YYYY" || /year/.test(kind)) return year;
    if (placeholder.includes("YYYY-MM") && !placeholder.includes("DD")) return `${year}-${month}`;
    if (/month/.test(kind)) return `${year}-${month}`;
    return day ? `${year}-${month}-${day}` : `${year}-${month}`;
  }

  function calendarTarget(value) {
    const match = /^(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?$/.exec(String(value || "").trim());
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]);
    const sourceHasDay = Boolean(match[3]);
    const day = sourceHasDay ? Number(match[3]) : 1;
    if (month < 1 || month > 12 || day < 1 || day > 31) return null;
    return {
      year,
      month,
      day,
      sourceHasDay,
      monthIndex: year * 12 + month - 1,
      formatted: `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`
    };
  }

  function calendarNavigation(visibleYear, visibleMonth, target) {
    const current = Number(visibleYear) * 12 + Number(visibleMonth) - 1;
    if (!Number.isFinite(current) || !target?.monthIndex) return null;
    const difference = target.monthIndex - current;
    if (!difference) return { direction: "none", preferYear: false, difference: 0 };
    return {
      direction: difference < 0 ? "previous" : "next",
      preferYear: Math.abs(difference) >= 12,
      difference
    };
  }

  function recruitBrandFromHost(hostname) {
    const labels = String(hostname || "").toLowerCase().replace(/^www\./, "").split(".").filter(Boolean);
    if (labels.length < 2) return "";
    const compoundSuffix = /^(?:com|net|org|gov|edu|ac)\.(?:cn|hk|uk|au|jp)$/i.test(labels.slice(-2).join("."));
    const brandIndex = labels.length - (compoundSuffix ? 3 : 2);
    const brand = labels[brandIndex] || "";
    if (!brand || /^(?:job|jobs|career|careers|apply|campus|join|zhipin|boss|moka|mokahr|beisen|zhiye|liepin|lagou|51job|zhilian|nowcoder|feishu|linkedin|indeed|glassdoor)$/i.test(brand)) return "";
    return brand;
  }

  function recruitCompanyCacheKey(rawUrl) {
    try {
      const url = new URL(String(rawUrl || ""));
      const host = url.hostname.toLowerCase();
      if (host !== "app.mokahr.com") return host;
      const match = url.pathname.match(/^\/(campus-recruitment|social-recruitment)\/([^/]+)\/([^/]+)/i);
      return match ? `${host}/${match[1].toLowerCase()}/${match[2].toLowerCase()}/${match[3].toLowerCase()}` : `${host}${url.pathname}`;
    } catch {
      return "";
    }
  }

  function recruitRecordPageScore(tab) {
    const url = String(tab?.url || "").trim();
    const title = String(tab?.title || "").trim();
    if (!/^https?:\/\//i.test(url)) return -1;
    if (/(?:msn\.com\/widgets|\/newtab(?:\/|$)|gameassist|gaming\/widget)/i.test(url)) return -1;

    let score = tab?.active ? 20 : 0;
    if (/(?:deliveryrecord|deliver-query|candidatehome\/applications|position\/application|candidate\/applications|application(?:s)?\/(?:record|history|list)|application-record)/i.test(url)) {
      score += 100;
    }
    if (/(?:投递记录|应聘记录|申请记录|投递查询|我的申请|申请进度|求职进展)/i.test(title)) score += 60;
    if (/(?:job|jobs|career|careers|campus|recruit|apply|moka|mokahr|zhiye|beisen|feishu)/i.test(url)) score += 5;
    return score;
  }

  function bestRecruitPageContext(contexts) {
    const entries = (Array.isArray(contexts) ? contexts : [])
      .filter((entry) => entry?.context?.ok)
      .map((entry) => {
        const context = entry.context;
        const candidateCount = Array.isArray(context.titleCandidates) ? context.titleCandidates.length : 0;
        let score = candidateCount * 100;
        if (context.titleGuess) score += 10;
        if (context.companyGuess) score += 5;
        if (/^https?:\/\//i.test(String(context.url || ""))) score += 2;
        if (entry.frameId === 0) score += 1;
        return { ...entry, score };
      })
      .sort((left, right) => right.score - left.score);
    return entries[0] || null;
  }

  function normalizeRecruitStatus(rawStatus) {
    const parts = String(rawStatus || "").trim()
      .split(/(\s*(?:-|—|–|→|>|｜|\|)\s*)/)
      .filter(Boolean);
    if (parts.length < 3) return parts.join("");
    let result = parts[0];
    let previous = normalize(parts[0]);
    for (let index = 1; index + 1 < parts.length; index += 2) {
      const separator = parts[index];
      const value = parts[index + 1];
      const current = normalize(value);
      if (current && current === previous) continue;
      result += separator + value;
      previous = current;
    }
    return result.trim();
  }

  function recruitStatusFromText(rawText) {
    const lines = String(rawText || "").replace(/\r/g, "").split(/\n+/)
      .map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
    for (let index = 0; index < lines.length; index += 1) {
      const match = lines[index].match(/^(?:当前进度|申请进度|应聘进度|当前状态|状态)\s*[:：]?\s*(.*)$/i);
      if (!match) continue;
      const inline = String(match[1] || "").trim();
      if (inline) return normalizeRecruitStatus(inline);
      const next = String(lines[index + 1] || "").trim();
      if (next && next.length <= 120 && !/^(?:项目|投递时间|申请时间|修改申请|撤回|编辑|查看)\s*[:：]?$/i.test(next)) {
        return normalizeRecruitStatus(next);
      }
    }
    for (let index = lines.length - 1; index >= 0; index -= 1) {
      if (/^(?:流程终止|流程结束|申请终止|已淘汰|淘汰|不合适|未通过|拒绝|已挂|撤回成功|已撤回)$/.test(lines[index])) {
        return normalizeRecruitStatus(lines[index]);
      }
    }
    return "";
  }

  function recruitStatusStage(sourceStatus) {
    const status = String(sourceStatus || "").replace(/\s+/g, "").toLowerCase();
    if (!status) return { stage: "", result: "" };
    if (/淘汰|不合适|未通过|流程终止|流程结束|申请终止|拒绝|已挂|撤回成功|已撤回/.test(status)) {
      return { stage: "rejected", result: "挂" };
    }
    if (/offer|录用|拟录用|签约|待入职|已入职/.test(status)) {
      return { stage: "offer", result: "通过" };
    }
    if (/hr面|人力面|终面|终试|洽谈/.test(status)) return { stage: "hr", result: "进行中" };
    if (/三面|第三轮面试/.test(status)) return { stage: "interview3", result: "进行中" };
    if (/二面|第二轮面试/.test(status)) return { stage: "interview2", result: "进行中" };
    if (/一面|第一轮面试|面试/.test(status)) return { stage: "interview1", result: "进行中" };
    if (/笔试|测评|在线测试|综合测试|性格测试/.test(status)) return { stage: "written", result: "进行中" };
    if (/投递|申请成功|已申请|简历|筛选|初筛|评估|待处理|处理中/.test(status)) {
      return { stage: "applied", result: "进行中" };
    }
    return { stage: "", result: "" };
  }

  function recruitExistingApplications(items, pageUrl) {
    const identity = value => {
      try {
        const url = new URL(value);
        return { site: url.origin + url.pathname.replace(/\/$/, ""),
          job: url.hash.match(/^#\/job\/([^/?]+)/)?.[1] || "", href: url.href };
      } catch { return null; }
    };
    const page = identity(pageUrl);
    return (items || []).map(item => {
      const detail = identity(item.detail_url);
      const record = identity(item.record_url);
      const exact = Boolean(page && detail && page.site === detail.site &&
        (page.job ? page.job === detail.job : !page.href.includes("candidateHome") && page.href === detail.href));
      const sameSite = Boolean(page && [detail, record].some(value => value?.site === page.site));
      return { ...item, exact, sameSite };
    }).sort((a, b) => Number(b.exact) - Number(a.exact) || Number(b.sameSite) - Number(a.sameSite));
  }

  function recruitRecordCandidates(blockTexts) {
    const datePattern = /(?:20\d{2}[年./-]\s?\d{1,2}[月./-]\s?\d{1,2}日?(?:\s+\d{1,2}:\d{2})?|\d{1,2}月\d{1,2}日|\d{1,2}[-/.]\d{1,2}\s+\d{1,2}:\d{2})/;
    const jobPattern = /工程师|开发|算法|产品|设计|运营|测试|研究|研发|技术|顾问|销售|市场|采购|财务|人力|法务|实习|管培|项目经理|架构|数据|运维|机器人|嵌入式|软件|硬件|视觉|岗位|职位|\b(?:engineer|developer|designer|manager|intern|analyst|researcher|builder|architect|scientist|specialist|consultant|lead|director)\b/i;
    const blocked = /^(?:投递记录|我的投递|申请记录|投递历史|已完成的投递|校园招聘|社会招聘|编辑|查看|查看\/打印|修改申请|撤回|撤回申请|取消申请|修改志愿顺序|第\s*\d+\s*志愿|没有更多了|当前进度.*)$/i;
    const results = [];
    for (const raw of Array.isArray(blockTexts) ? blockTexts : []) {
      const text = String(raw || "").replace(/\r/g, "").trim();
      const date = text.match(datePattern)?.[0] || "";
      const strongRecord = (/第\s*\d+\s*志愿/.test(text)
        && /(?:状态\s*[:：]|申请成功|投递成功|修改申请|撤回)/.test(text))
        || (/(?:状态\s*[:：]|申请成功|投递成功)/.test(text)
          && /(?:修改申请|撤回|撤回申请|取消申请)/.test(text));
      if (!date && !strongRecord && !/已投递|投递简历/.test(text)) continue;
      const lines = text.split(/\n+/).map((line) => line.replace(/\s+/g, " ").trim()).filter(Boolean);
      let best = null;
      lines.forEach((line, index) => {
        if (line.length < 2 || line.length > 90 || blocked.test(line) || datePattern.test(line)) return;
        if (/^(?:当前进度|投递时间|申请时间|状态|待筛选|待处理|已通过|已淘汰|进行中|已投递|简历|测评|笔试|面试|offer)/i.test(line)) return;
        let score = jobPattern.test(line) ? 10 : 0;
        if (/[（(][A-Za-z]?\d{4,}[）)]/.test(line)) score += 6;
        if (index === 0) score += 2;
        if (!best || score > best.score) best = { title: line, score };
      });
      if (!best || best.score < 8) continue;
      const sourceStatus = recruitStatusFromText(text) || "已投递";
      const mapped = recruitStatusStage(sourceStatus);
      const candidate = { title: best.title, date };
      candidate.sourceStatus = sourceStatus;
      if (mapped.stage) candidate.stage = mapped.stage;
      if (mapped.result) candidate.result = mapped.result;
      if (!results.some((item) => item.title === candidate.title && item.date === candidate.date)) results.push(candidate);
    }
    return results;
  }

  function buildRecruitRecords({ company = "", manualTitle = "", recordUrl = "", candidates = [] } = {}) {
    const selected = (Array.isArray(candidates) ? candidates : [])
      .filter((candidate) => candidate?.selected && String(candidate.title || "").trim())
      .filter((candidate, index, all) => all.findIndex((item) =>
        String(item.title || "").trim() === String(candidate.title || "").trim()) === index);
    const entries = selected.length ? selected : [{ title: String(manualTitle || "").trim() }];
    return entries.map((candidate) => {
      const record = {
        company: String(company || "").trim(),
        title: String(candidate.title || "").trim(),
        record_url: String(recordUrl || "").trim()
      };
      if (candidate.date) record.applied_at = String(candidate.date).trim();
      if (candidate.sourceStatus) record.source_status = normalizeRecruitStatus(candidate.sourceStatus);
      if (candidate.stage) record.stage = String(candidate.stage).trim();
      if (candidate.result) record.result = String(candidate.result).trim();
      return record;
    });
  }

  function choiceOptionScore(optionText, desiredValue, key = "", label = "") {
    const option = normalize(optionText);
    const desired = normalize(desiredValue);
    if (!option || !desired) return 0;
    if (option === desired) return 100;

    if (["languageProficiency", "languageSpeaking", "languageReadingWriting"].includes(key)
      && /^(日常会话|良好|熟练)$/.test(String(desiredValue).trim())
      && /^熟练$/.test(String(optionText).trim())) return 94;

    const groups = [
      ["良好", "健康", "正常", "无病史"],
      ["官网", "官方网站", "公司官网", "招聘官网"],
      ["中国", "中国大陆", "中华人民共和国"],
      ["硕士", "硕士研究生"],
      ["本科", "大学本科", "学士"],
      ["全日制", "普通全日制", "全国普通高等院校全日制"],
      ["前30", "前30%", "前百分之三十", "TOP30", "TOP30%"],
      ["六级", "英语六级", "CET6", "CET-6"],
      ["是", "接受", "可以", "愿意"],
      ["否", "不接受", "不可以", "不愿意"]
    ];
    const group = groups.find((items) => items.some((item) => normalize(item) === desired));
    if (group?.some((item) => {
      const candidate = normalize(item);
      return option === candidate || option.includes(candidate) || candidate.includes(option);
    })) return 94;
    if (option.includes(desired) || desired.includes(option)) return 80;
    if (key === "studyMode" && /全日制/.test(String(desiredValue))) {
      if (/^(是|yes)$/i.test(String(optionText).trim())) return 92;
      if (/^(否|no)$/i.test(String(optionText).trim())) return 0;
    }
    if (key === "targetRole" && /职位类别|岗位类别/.test(label)) {
      if (/技术/.test(String(optionText)) && /工程师|开发|算法|技术|软件|硬件|研发|研究|机器人|ros/i.test(String(desiredValue))) return 92;
    }
    if (key === "gradeRank" && /其他/.test(optionText) && /前?\d+%?/.test(String(desiredValue))) return 70;
    if ((key === "desiredSalary" || /期望月薪|期望薪资|期望薪酬/.test(label))) {
      return salaryOptionScore(optionText, desiredValue);
    }
    return 0;
  }

  function splitRegionPath(value) {
    const normalized = String(value || "").replace(/[\s,/，-]+/g, "").trim();
    if (!normalized) return [];
    const province = PROVINCE_LEVEL_REGIONS
      .slice()
      .sort((a, b) => b.length - a.length)
      .find((item) => normalized.startsWith(item));
    if (!province) return [normalized];

    const parts = [province];
    let remaining = normalized.slice(province.length);
    while (remaining) {
      const match = /^(.+?(?:自治州|地区|新区|市|区|县|旗))/.exec(remaining);
      if (!match) {
        parts.push(remaining);
        break;
      }
      parts.push(match[1]);
      remaining = remaining.slice(match[1].length);
    }
    return parts;
  }

  function parseSalaryRange(value) {
    const text = String(value || "").replace(/,/g, "");
    const numbers = text.match(/\d+(?:\.\d+)?/g)?.map(Number) || [];
    if (numbers.length >= 2) return [numbers[0], numbers[1]];
    if (numbers.length === 1 && /以上|及以上|起/.test(text)) return [numbers[0], Number.POSITIVE_INFINITY];
    return null;
  }

  function salaryOptionScore(optionText, desiredValue) {
    const option = parseSalaryRange(optionText);
    const desired = parseSalaryRange(desiredValue);
    if (!option || !desired) return 0;
    const [optionStart, optionEnd] = option;
    const [desiredStart, desiredEnd] = desired;
    const overlap = Math.max(0, Math.min(optionEnd, desiredEnd) - Math.max(optionStart, desiredStart));
    if (!overlap) return 0;
    const optionWidth = Number.isFinite(optionEnd) ? Math.max(1, optionEnd - optionStart) : Math.max(1, desiredEnd - optionStart);
    const desiredWidth = Number.isFinite(desiredEnd) ? Math.max(1, desiredEnd - desiredStart) : optionWidth;
    const coverage = Math.min(1, overlap / optionWidth);
    const desiredCoverage = Math.min(1, overlap / desiredWidth);
    const endpointBonus = optionEnd === desiredEnd ? 14 : optionStart === desiredStart ? 8 : 0;
    return Math.min(99, Math.round(60 + coverage * 18 + desiredCoverage * 7 + endpointBonus));
  }

  function formatProject(project) {
    const heading = [project.startDate && `${project.startDate} - ${project.endDate || ""}`, project.name, project.role]
      .filter(Boolean)
      .join(" | ");
    const parts = [
      heading,
      project.organization && `合作单位：${project.organization}`,
      project.summary && `项目描述：${project.summary}`,
      Array.isArray(project.details) && project.details.length
        ? `主要工作：\n${project.details.map((item, index) => `${index + 1}）${item}`).join("\n")}`
        : "",
      project.results && `项目成果：${project.results}`
    ];
    return parts.filter(Boolean).join("\n");
  }

  function buildResumeValues(resume) {
    const basic = resume?.basic || {};
    const education = Array.isArray(resume?.education) ? resume.education : [];
    const projects = Array.isArray(resume?.projects) ? resume.projects : [];
    const awards = Array.isArray(resume?.awards) ? resume.awards : [];
    const publications = Array.isArray(resume?.publications) ? resume.publications : [];
    const papers = publications.filter((item) => !/专利|patent/i.test(`${item.type || ""}${item.name || ""}`));
    const patents = publications.filter((item) => /专利|patent/i.test(`${item.type || ""}${item.name || ""}`));
    const certificates = Array.isArray(resume?.certificates) ? resume.certificates : [];
    const practices = Array.isArray(resume?.practices) ? resume.practices : [];
    const internships = Array.isArray(resume?.internships) ? resume.internships : [];
    const structuredSkills = Array.isArray(resume?.structuredSkills) ? resume.structuredSkills : [];
    const englishCredential = basic.englishLevel || certificates.find((item) => /英语|外语|六级|四级/i.test(`${item.type || ""}${item.name || ""}`))?.name || "";
    const educationLevels = education.map((item) => {
      if (item.degree === "硕士") return "硕士研究生";
      if (item.degree === "博士") return "博士研究生";
      return item.degree || "";
    });
    const academicDegrees = education.map((item) => {
      if (/博士/.test(item.degree || "")) return "博士";
      if (/硕士/.test(item.degree || "")) return "硕士";
      if (/本科|学士/.test(item.degree || "")) return "学士";
      return item.degree || "";
    });
    const projectDescriptions = projects.map((project) => [
      project.summary || "",
      ...(Array.isArray(project.details) ? project.details : []),
      project.results ? `项目成果：${project.results}` : ""
    ].filter(Boolean).join("\n"));
    const projectResponsibilities = projects.map((project) =>
      (Array.isArray(project.details) ? project.details : []).join("\n")
    );
    const degreeRank = (item) => /博士/.test(item.degree) ? 4 : /硕士/.test(item.degree) ? 3 : /本科|学士/.test(item.degree) ? 2 : /专科|大专/.test(item.degree) ? 1 : 0;
    const highestRank = Math.max(0, ...education.map(degreeRank));

    return {
      highestStudyMode: education[0]?.studyMode || basic.studyMode || "",
      graduationYear: (basic.graduationDate || education[0]?.endDate || "").match(/^\d{4}/)?.[0] || "",
      educationIsHighest: education.map((item) => item.isHighest || (highestRank && degreeRank(item) ? degreeRank(item) === highestRank ? "是" : "否" : "")),
      educationIsFirst: education.map((item) => item.isFirst || ""),
      projectOrganization: projects.map((item) => item.organization || ""),
      projectOngoing: projects.map((item) => item.endDate ? /^(至今|present|current)$/i.test(item.endDate) ? "是" : "否" : ""),
      hasProjects: projects.map(() => "是"),
      hasCertificates: certificates.length ? "是" : "",
      languageSummary: englishCredential ? `英语${englishCredential}${basic.englishScore ? `，${basic.englishScore} 分` : ""}` : "",
      credentialSummary: resume?.honors || "",
      patentDescription: patents.map((item) => [item.name, item.type, item.date, item.authorOrder].filter(Boolean).join("；")).join("\n"),
      fullName: basic.fullName || "",
      gender: basic.gender || "",
      age: calculateAge(basic.birthDate) || basic.age || "",
      birthDate: basic.birthDate || "",
      nationality: basic.nationality || "",
      countryRegion: basic.countryRegion || "",
      height: basic.height || "",
      weight: basic.weight || "",
      idNumber: basic.idNumber || "",
      idType: basic.idType || (basic.idNumber ? "中国-居民身份证" : ""),
      phone: basic.phone || "",
      phoneType: basic.phoneType || "",
      email: basic.email || "",
      nativePlace: basic.nativePlace || "",
      currentResidence: basic.currentResidence || "",
      applicantStatus: basic.applicantStatus || "",
      failedCourses: basic.failedCourses || "",
      passedCET4: basic.passedCET4 || "",
      englishLevel: basic.englishLevel || certificates[0]?.name || "",
      englishScore: basic.englishScore || "",
      languageName: englishCredential ? ["英语"] : [],
      languageProficiency: /六级|cet\s*6/i.test(englishCredential) ? ["日常会话"] : [],
      emergencyContact: basic.emergencyContact || "",
      emergencyPhone: basic.emergencyPhone || "",
      recruitmentSource: basic.recruitmentSource || "",
      relativesAtCompany: basic.relativesAtCompany || "",
      healthStatus: basic.healthStatus || "",
      desiredSalary: basic.desiredSalary || "",
      interviewSite: basic.interviewSite || "",
      preferredCity: basic.preferredCity || "",
      acceptLocationTransfer: basic.acceptLocationTransfer || "",
      acceptRoleTransfer: basic.acceptRoleTransfer || "",
      targetRole: basic.targetRole || "",
      highestDegree: educationLevels[0] || "",
      highestSchool: education[0]?.school || "",
      highestMajor: education[0]?.major || "",
      graduationDate: basic.graduationDate || education[0]?.endDate || "",
      school: education.map((item) => item.school || ""),
      major: education.map((item) => item.major || ""),
      college: education.map((item) => item.college || ""),
      educationLocation: education.map((item) => item.location || ""),
      educationLevel: educationLevels,
      academicDegree: academicDegrees,
      studyMode: education.map((item) => item.studyMode || basic.studyMode || ""),
      educationStartDate: education.map((item) => item.startDate || ""),
      educationEndDate: education.map((item) => item.endDate || ""),
      overseasStudy: education.map((item) => item.overseasStudy || ""),
      gpa: education.map((item) => item.gpa || ""),
      gpaPersonalScore: education.map((item) => item.gpaPersonalScore || ""),
      gpaFullScore: education.map((item) => item.gpaFullScore || ""),
      gradeRank: education.map((item) => item.gradeRank || ""),
      advisor: education.map((item) => item.advisor || ""),
      projectName: projects.map((item) => item.name || ""),
      projectRole: projects.map((item) => item.role || ""),
      projectStartDate: projects.map((item) => item.startDate || ""),
      projectEndDate: projects.map((item) => item.endDate || ""),
      projectResponsibility: projectResponsibilities,
      projectDescription: projectDescriptions,
      projectResults: projects.map((item) => item.results || ""),
      projectLink: projects.map((item) => item.link || ""),
      awardName: awards.map((item) => item.name || "").filter(Boolean),
      awardSelection: awards.map((item) => item.competitionName || item.name || "").filter(Boolean),
      awardType: awards.map((item) => item.type || "").filter(Boolean),
      awardLevel: awards.map((item) => item.level || "").filter(Boolean),
      awardDate: awards.map((item) => item.date || "").filter(Boolean),
      awardDescription: awards.map((item) => item.description || "").filter(Boolean),
      certificateName: certificates.map((item) => item.name || "").filter(Boolean),
      certificateType: certificates.map((item) => item.type || "").filter(Boolean),
      certificateDate: certificates.map((item) => item.date || "").filter(Boolean),
      patentName: patents.map((item) => item.name || "").filter(Boolean),
      patentType: patents.map((item) => item.type || "").filter(Boolean),
      patentDate: patents.map((item) => item.date || "").filter(Boolean),
      patentAuthorOrder: patents.map((item) => item.authorOrder || "").filter(Boolean),
      publicationName: papers.map((item) => item.name || "").filter(Boolean),
      publicationType: papers.map((item) => item.type || "").filter(Boolean),
      publicationDate: papers.map((item) => item.date || "").filter(Boolean),
      authorOrder: papers.map((item) => item.authorOrder || "").filter(Boolean),
      hasScholarship: basic.hasScholarship || "",
      hasCorePublication: basic.hasCorePublication || "",
      hasInternship: basic.hasInternship || "",
      practiceName: practices.map((item) => item.name || ""),
      practiceStartDate: practices.map((item) => item.startDate || ""),
      practiceEndDate: practices.map((item) => item.endDate || ""),
      practiceDescription: practices.map((item) => item.description || ""),
      internshipCompany: internships.map((item) => item.company || ""),
      internshipRole: internships.map((item) => item.role || ""),
      internshipStartDate: internships.map((item) => item.startDate || ""),
      internshipEndDate: internships.map((item) => item.endDate || ""),
      internshipDescription: internships.map((item) => item.description || ""),
      skillName: structuredSkills.map((item) => item.name || ""),
      skillLevel: structuredSkills.map((item) => item.level || ""),
      developerLanguages: basic.developerLanguages || "",
      aiTools: basic.aiTools || "",
      aiCollaboration: basic.aiCollaboration || "",
      languageSpeaking: englishCredential ? [basic.languageSpeaking || "良好"] : [],
      languageReadingWriting: englishCredential ? [basic.languageReadingWriting || "良好"] : [],
      skills: resume?.skills || "",
      honors: resume?.honors || "",
      projectExperience: projects.map(formatProject).join("\n\n"),
      selfIntroduction: resume?.selfIntroduction || ""
    };
  }

  function containsAlias(source, alias) {
    const normalizedSource = normalize(source);
    const normalizedAlias = normalize(alias);
    if (!normalizedSource || !normalizedAlias) return 0;
    if (normalizedSource === normalizedAlias) return 100;
    if (!normalizedSource.includes(normalizedAlias)) return 0;
    const coverage = normalizedAlias.length / Math.max(normalizedSource.length, normalizedAlias.length);
    return Math.round(64 + Math.min(24, coverage * 24));
  }

  function scoreRule(meta, rule) {
    const sectionLabel = meta.section && meta.label ? `${meta.section} ${meta.label}` : "";
    const primary = [meta.label, sectionLabel, meta.ariaLabel, meta.placeholder].filter(Boolean);
    const secondary = [meta.name, meta.id].filter(Boolean);
    const contextual = [meta.context].filter(Boolean);
    let best = 0;

    for (const alias of rule.aliases) {
      for (const value of primary) best = Math.max(best, containsAlias(value, alias));
      for (const value of secondary) best = Math.max(best, Math.round(containsAlias(value, alias) * 0.88));
      // Context helps rank a field, but context alone must never authorize a fill.
      for (const value of contextual) best = Math.max(best, Math.round(containsAlias(value, alias) * 0.5));
    }

    return best;
  }

  function isRuleCompatible(meta, rule) {
    const label = normalize(`${meta.section || ""}${meta.label || ""}${meta.ariaLabel || ""}${meta.placeholder || ""}`);
    const fieldLabel = normalize(meta.label || meta.ariaLabel || meta.placeholder || "");
    const section = normalize(meta.section || "");
    const kind = String(meta.controlKind || "");
    if (!label) return true;
    if (["fullName", "phone", "email", "idNumber"].includes(rule.key) && /家庭|亲属|家属|成员/.test(section)) return false;
    if (["highestDegree", "educationLevel", "academicDegree"].includes(rule.key)
      && /是否|性质|类型|资质|培养|院校|学校|证书|学位证|学历证明/.test(fieldLabel)) return false;
    if (rule.key === "idNumber" && /select/.test(kind) && !/号码类型/.test(fieldLabel)) return false;
    if (rule.key === "projectOngoing" && kind !== "checkbox") return false;
    if (["hasProjects", "hasCertificates", "educationIsHighest", "educationIsFirst"].includes(rule.key) && !/是否/.test(fieldLabel)) return false;
    if (rule.key === "fullName" && /导师|指导教师|紧急联系人|推荐人|证明人/.test(label)) return false;
    if (rule.key === "phone" && /紧急|导师|推荐人|证明人/.test(label)) return false;
    if (rule.key === "highestDegree" && /毕业日期|毕业时间|预计毕业/.test(label)) return false;
    if (rule.key === "educationLevel" && /学历性质|学历类型|学习形式|培养方式|就读形式/.test(fieldLabel)) return false;
    if (rule.key === "studyMode" && /最高学历/.test(fieldLabel)) return false;
    if (["school", "highestSchool"].includes(rule.key) && /学号|学校所在省|学校所在市|学校所在地|院校所在地/.test(fieldLabel)) return false;
    if (rule.key === "college" && /学生干部|干部经历|社团/.test(fieldLabel)) return false;
    if (rule.key === "graduationDate" && section === "教育经历" && Number.isInteger(meta.recordIndex)) return false;
    if (rule.key === "languageName") {
      if (/同步|更新|无需|没有|无需要|作品|链接|附件|上传|至今/.test(fieldLabel)) return false;
      if (!/^(语言|语种|语言类型|语言类别|语言名称|语言能力语言|language)$/.test(fieldLabel)) return false;
    }
    if (rule.key === "englishScore" && fieldLabel === "成绩" && !/语言能力|英语能力/.test(section)) return false;
    if (DATE_KEYS.has(rule.key) && kind === "checkbox") return false;
    const isPatentField = /专利|patent/.test(`${section}${fieldLabel}`);
    if (["publicationName", "publicationType", "publicationDate", "authorOrder"].includes(rule.key) && isPatentField) return false;
    if (["patentName", "patentType", "patentDate", "patentAuthorOrder", "patentDescription"].includes(rule.key) && !isPatentField) return false;
    if (rule.key === "projectExperience") {
      const aggregateLabels = new Set(["项目经历", "项目经验", "项目介绍", "projectexperience", "projects"]);
      if (!aggregateLabels.has(fieldLabel) || /项目经历|课题项目经验/.test(section)) return false;
    }
    if (rule.key === "skills") {
      const aggregateLabels = new Set(["技能特长", "专业技能", "个人技能", "技能", "skills", "technicalskills"]);
      if (!aggregateLabels.has(fieldLabel) || /技能证书|it技能/.test(section)) return false;
    }
    if (rule.key === "honors") {
      const aggregateLabels = new Set(["荣誉证书", "获奖经历", "所获荣誉", "奖项汇总", "honors", "awardssummary", "certificatessummary"]);
      if (!aggregateLabels.has(fieldLabel) || /获奖|竞赛|证书/.test(section)) return false;
    }
    if (!DATE_KEYS.has(rule.key) && /日期|年月|时间/.test(label) && /date|month|picker/.test(kind)) return false;
    if (DATE_KEYS.has(rule.key) && /学历|学位/.test(label) && !/毕业|开始|结束|入学|离校|获得|发布|发表/.test(label)) return false;
    return true;
  }

  function bestCanonicalRule(meta) {
    let bestRule = null;
    let bestScore = 0;
    for (const rule of FIELD_RULES) {
      if (!isRuleCompatible(meta, rule)) continue;
      const score = scoreRule(meta, rule);
      if (score > bestScore) {
        bestRule = rule;
        bestScore = score;
      }
    }
    return { rule: bestRule, score: bestScore };
  }

  function customFieldScore(meta, customKey) {
    const target = normalize(customKey);
    if (!target) return 0;
    const sources = [
      meta.section && meta.label ? `${meta.section} ${meta.label}` : "",
      meta.label,
      meta.ariaLabel,
      meta.placeholder
    ].map(normalize).filter(Boolean);
    let best = 0;
    for (const source of sources) {
      if (source === target) return 100;
      const shorter = Math.min(source.length, target.length);
      if (shorter >= 4 && (source.includes(target) || target.includes(source))) {
        const coverage = shorter / Math.max(source.length, target.length);
        best = Math.max(best, Math.round(84 + coverage * 12));
        continue;
      }
      if (shorter < 5) continue;
      const sourcePairs = new Map();
      for (let index = 0; index < source.length - 1; index += 1) {
        const pair = source.slice(index, index + 2);
        sourcePairs.set(pair, (sourcePairs.get(pair) || 0) + 1);
      }
      let overlap = 0;
      for (let index = 0; index < target.length - 1; index += 1) {
        const pair = target.slice(index, index + 2);
        const available = sourcePairs.get(pair) || 0;
        if (!available) continue;
        overlap += 1;
        sourcePairs.set(pair, available - 1);
      }
      const similarity = (2 * overlap) / (source.length + target.length - 2);
      if (similarity >= 0.72) best = Math.max(best, Math.round(72 + similarity * 16));
    }
    const customMeta = { label: customKey, section: "", ariaLabel: "", placeholder: "", name: "", id: "", context: "" };
    const customCanonical = bestCanonicalRule(customMeta);
    const pageCanonical = bestCanonicalRule(meta);
    if (
      customCanonical.rule?.key === pageCanonical.rule?.key
      && customCanonical.score >= 68
      && pageCanonical.score >= 55
    ) {
      best = Math.max(
        best,
        Math.min(94, Math.round((customCanonical.score + pageCanonical.score) / 2))
      );
    }
    return best;
  }

  function matchFields(metas, resume) {
    const values = buildResumeValues(resume);
    const occurrences = new Map();
    const customFields = resume?.customFields || {};

    const candidates = metas.map((meta) => {
      if (fieldDisposition(meta) !== "field") return null;
      const customMatch = Object.entries(customFields).map(([key, storedValue]) => ({
        key,
        storedValue,
        score: customFieldScore(meta, key)
      })).filter((item) => item.score >= 72 && (!Number.isInteger(meta.recordIndex) || Array.isArray(item.storedValue)))
        .sort((a, b) => b.score - a.score)[0];
      if (customMatch && customMatch.storedValue !== "") {
        const customIndex = Number.isInteger(meta.recordIndex) ? meta.recordIndex : 0;
        const value = Array.isArray(customMatch.storedValue)
          ? customMatch.storedValue[customIndex] || ""
          : customMatch.storedValue;
        if (value) return {
          ...meta,
          key: `custom:${normalize(customMatch.key)}`,
          value,
          score: customMatch.score,
          confidence: customMatch.score >= 82 ? "high" : "medium",
          repeatable: Array.isArray(customMatch.storedValue)
        };
      }

      let bestRule = null;
      let bestScore = 0;

      for (const rule of FIELD_RULES) {
        if (!isRuleCompatible(meta, rule)) continue;
        const score = scoreRule(meta, rule);
        if (score > bestScore) {
          bestRule = rule;
          bestScore = score;
        }
      }

      if (!bestRule || bestScore < 55) return null;
      const rawValue = values[bestRule.key];
      const index = Number.isInteger(meta.recordIndex) && Array.isArray(rawValue)
        ? meta.recordIndex
        : occurrences.get(bestRule.key) || 0;
      occurrences.set(bestRule.key, index + 1);
      const value = Array.isArray(rawValue)
        ? rawValue[index] || ""
        : rawValue || "";
      const selectionValue = bestRule.key === "awardName"
        ? values.awardSelection?.[index] || value
        : value;

      if (!value) return null;
      return {
        ...meta,
        key: bestRule.key,
        value,
        selectionValue,
        score: bestScore,
        confidence: bestScore >= 82 ? "high" : bestScore >= 68 ? "medium" : "low",
        repeatable: Array.isArray(rawValue)
      };
    }).filter(Boolean);

    const bestScalar = new Map();
    candidates.forEach((candidate, index) => {
      if (candidate.repeatable) return;
      const current = bestScalar.get(candidate.key);
      if (!current || candidate.score > current.score) bestScalar.set(candidate.key, { index, score: candidate.score });
    });
    return candidates.filter((candidate, index) => candidate.repeatable || bestScalar.get(candidate.key)?.index === index)
      .filter((candidate) => !(candidate.key === "projectEndDate" && /^(至今|present|current)$/i.test(candidate.value)
        && Number.isInteger(candidate.recordIndex) && candidates.some((item) => item.key === "projectOngoing"
          && item.section === candidate.section && item.recordIndex === candidate.recordIndex)))
      .map(({ repeatable, ...candidate }) => candidate);
  }

  const api = {
    FIELD_RULES,
    PROVINCE_LEVEL_REGIONS,
    bestRecruitPageContext,
    buildRecruitRecords,
    buildResumeValues,
    calculateAge,
    calendarNavigation,
    calendarTarget,
    choiceOptionScore,
    controlCategory,
    fieldDisposition,
    unmatchedReason,
    fieldHasIdentity,
    formatDateForControl,
    formatProject,
    isAntPickerRootClass,
    isFeishuPeriodRootClass,
    matchFields,
    normalize,
    normalizeRecruitStatus,
    recruitBrandFromHost,
    recruitCompanyCacheKey,
    recruitRecordPageScore,
    recruitRecordCandidates,
    recruitExistingApplications,
    recruitStatusFromText,
    recruitStatusStage,
    scoreRule,
    salaryOptionScore,
    splitRegionPath
  };
  root.ResumeFillerCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
