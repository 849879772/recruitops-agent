(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const kinds = {personal: "个人经历材料", reference: "外部参考", notes: "学习笔记"};
  const statuses = {processing: "正在建立索引", ready: "可使用", failed: "处理失败"};
  let documents = [], editing = null, preview = null, timer = null, loading = false, pendingImport = null;

  async function post(path, payload = {}) {
    const response = await fetch(`/api/local-ui/knowledge/${path}`, {
      method: "POST", credentials: "same-origin",
      headers: {"Content-Type": "application/json", "X-RecruitOps-Local-UI": "1"},
      body: JSON.stringify(payload), signal: AbortSignal.timeout(30000),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "输入格式有误");
    return result;
  }
  function message(text, error = false) {
    $("knowledge-message").textContent = text;
    $("knowledge-message").className = error ? "configuration-error" : "configuration-message";
  }
  function button(label, action) {
    const node = document.createElement("button");
    node.type = "button"; node.className = "button button--ghost"; node.textContent = label;
    node.addEventListener("click", action); return node;
  }
  function openView(view) {
    document.querySelector(`[data-view="${view}"]`)?.click();
  }
  function selectDocument(doc) {
    $("assistant-knowledge-enabled").checked = true;
    $("assistant-knowledge-document").value = doc.id;
    openView("assistant"); $("assistant-message").focus();
  }
  function cancelEdit() {
    editing = null; $("knowledge-import-form").reset();
    $("knowledge-import").textContent = "导入"; $("knowledge-cancel-edit").hidden = true;
  }
  function render() {
    const body = $("knowledge-documents"); body.replaceChildren();
    const scope = $("assistant-knowledge-document"), selected = scope.value;
    scope.replaceChildren(new Option("全部资料", ""));
    for (const doc of documents) {
      if (doc.status === "ready") scope.add(new Option(doc.filename, doc.id));
      const row = document.createElement("tr");
      const name = document.createElement("td"); name.textContent = doc.filename;
      const kind = document.createElement("td"); kind.textContent = kinds[doc.kind] || doc.kind;
      const status = document.createElement("td"); status.textContent = statuses[doc.status] || doc.status;
      if (doc.error) { const detail = document.createElement("small"); detail.textContent = doc.error; status.append(detail); }
      const time = document.createElement("td"); time.textContent = new Date(doc.updated_at).toLocaleString("zh-CN");
      const actions = document.createElement("td"); actions.className = "knowledge-actions";
      if (doc.status === "ready") {
        actions.append(button("查看", () => void showDocument(doc.id, 1, doc.revision)), button("用于对话", () => selectDocument(doc)));
      }
      if (doc.status === "failed") actions.append(button("重试", async () => {
        try { await post("retry", {document_id: doc.id, revision: doc.revision}); await load(); }
        catch (error) { message(error.message, true); }
      }));
      if (doc.status !== "processing") actions.append(button("更新", () => {
        editing = doc; $("knowledge-file").value = ""; $("knowledge-text").value = "";
        $("knowledge-name").value = doc.filename; $("knowledge-kind").value = doc.kind;
        $("knowledge-import").textContent = "更新资料"; $("knowledge-cancel-edit").hidden = false;
        $("knowledge-import-form").scrollIntoView({block: "center"});
      }));
      actions.append(button("删除", async () => {
        if (!confirm(`删除“${doc.filename}”及其检索索引？`)) return;
        try {
          await post("delete", {document_id: doc.id, revision: doc.revision});
          if (editing?.id === doc.id) cancelEdit();
          if (preview?.id === doc.id) $("knowledge-preview").close();
          await load(); message("已删除资料及索引");
        } catch (error) { message(error.message, true); }
      }));
      row.append(name, kind, status, time, actions); body.append(row);
    }
    if (selected && documents.some((doc) => doc.id === selected && doc.status === "ready")) scope.value = selected;
    else if (selected) { $("assistant-knowledge-enabled").checked = false; message("已选资料不可用，请重新选择", true); }
    if (!documents.length) {
      const row = document.createElement("tr"), cell = document.createElement("td");
      cell.colSpan = 5; cell.textContent = "尚无资料"; row.append(cell); body.append(row);
    }
  }
  async function load() {
    if (loading) return;
    loading = true; clearTimeout(timer);
    try {
      const payload = await post("list"); documents = payload.documents; render();
      const imported = documents.find((doc) => doc.id === pendingImport);
      if (imported && imported.status !== "processing") {
        message(imported.status === "ready" ? "索引已完成，可以用于对话" : imported.error, imported.status === "failed");
        pendingImport = null;
      }
      $("knowledge-model").textContent = payload.embedding_model?.includes("Qwen/Qwen3-Embedding-0.6B")
        ? "Qwen3-Embedding-0.6B · 1024 维" : payload.local_embedding ? "本地语义索引" : "已配置外部向量服务";
      if (documents.some((doc) => doc.status === "processing")) timer = setTimeout(load, 2500);
    } catch (error) { message(error.message, true); }
    finally { loading = false; }
  }
  async function showDocument(id, page = 1, revision = null) {
    const dialog = $("knowledge-preview");
    $("knowledge-preview-title").textContent = "资料原文";
    $("knowledge-preview-text").textContent = "正在读取…";
    $("knowledge-preview-meta").textContent = "";
    $("knowledge-page-prev").disabled = $("knowledge-page-next").disabled = true;
    if (!dialog.open) dialog.showModal();
    try {
      preview = await post("read", {document_id: id, revision, page});
      $("knowledge-preview-title").textContent = preview.filename;
      $("knowledge-preview-meta").textContent = `${kinds[preview.kind]} · ${new Date(preview.updated_at).toLocaleString("zh-CN")}`;
      $("knowledge-preview-text").textContent = preview.text;
      $("knowledge-page-label").textContent = `${preview.page} / ${preview.page_count}`;
      $("knowledge-page-prev").disabled = preview.page <= 1;
      $("knowledge-page-next").disabled = preview.page >= preview.page_count;
      $("knowledge-preview-text").scrollTop = 0;
    } catch (error) { preview = null; $("knowledge-preview-text").textContent = error.message; $("knowledge-page-label").textContent = ""; }
  }
  $("knowledge-import-form").addEventListener("submit", async (event) => {
    event.preventDefault(); const submit = $("knowledge-import"); submit.disabled = true;
    try {
      const file = $("knowledge-file").files[0], text = $("knowledge-text").value.trim();
      if (file && text) throw new Error("请只选择上传文件或粘贴笔记其中一种");
      if (!file && !text) throw new Error("请选择文件或粘贴笔记");
      if (file && file.size > 5000000) throw new Error("文件超过 5 MB");
      let filename, content;
      if (file) {
        filename = file.name;
        content = await new Promise((resolve, reject) => {
          const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(",")[1]);
          reader.onerror = () => reject(new Error("读取文件失败")); reader.readAsDataURL(file);
        });
      } else {
        filename = $("knowledge-name").value.trim();
        if (!filename) throw new Error("请填写笔记名称");
        if (!/\.(md|txt)$/i.test(filename)) filename += ".md";
        content = btoa(Array.from(new TextEncoder().encode(text), (b) => String.fromCharCode(b)).join(""));
      }
      const result = await post("upload", {filename, content_base64: content, kind: $("knowledge-kind").value,
        document_id: editing?.id || null, revision: editing?.revision || null});
      pendingImport = result.reused ? null : result.id;
      cancelEdit(); message(result.reused ? "相同资料已存在，已复用索引" : "资料已接收，正在建立索引");
      await load();
    } catch (error) { message(error.message, true); }
    finally { submit.disabled = false; }
  });
  $("knowledge-refresh").addEventListener("click", load);
  $("knowledge-cancel-edit").addEventListener("click", cancelEdit);
  document.querySelector('[data-view="knowledge"]').addEventListener("click", load);
  $("assistant-knowledge-enabled").addEventListener("change", load);
  $("assistant-knowledge-document").addEventListener("change", () => { $("assistant-knowledge-enabled").checked = true; });
  $("knowledge-preview-close").addEventListener("click", () => $("knowledge-preview").close());
  $("knowledge-page-prev").addEventListener("click", () => preview && showDocument(preview.id, preview.page - 1, preview.revision));
  $("knowledge-page-next").addEventListener("click", () => preview && showDocument(preview.id, preview.page + 1, preview.revision));
  let jobRequest = 0;
  window.addEventListener("recruitops:assistant-job", async (event) => {
    const id = event.detail.id, request = ++jobRequest;
    $("assistant-selected-job").hidden = $("assistant-clear-job").hidden = !id;
    $("assistant-selected-job").textContent = id ? "当前岗位" : "";
    if (!id) return;
    try {
      const response = await fetch(`/api/jobs/${encodeURIComponent(id)}`);
      if (!response.ok) throw new Error();
      const value = await response.json(), job = value.job || value;
      if (request === jobRequest) $("assistant-selected-job").textContent = job.title || id;
    } catch (_) { if (request === jobRequest) $("assistant-selected-job").textContent = "岗位详情暂不可用"; }
  });
  $("assistant-clear-job").addEventListener("click", () => {
    $("assistant-job-id").value = "";
    window.dispatchEvent(new CustomEvent("recruitops:assistant-job", {detail: {id: ""}}));
  });
  document.addEventListener("click", (event) => {
    const link = event.target.closest("a[href]"); if (!link) return;
    let url; try { url = new URL(link.href, location.href); } catch (_) { return; }
    if (url.origin !== location.origin || !url.searchParams.has("knowledge")) return;
    event.preventDefault(); void showDocument(url.searchParams.get("knowledge"), Number(url.searchParams.get("page") || 1), url.searchParams.get("revision"));
  });
  const query = new URLSearchParams(location.search);
  if (query.has("knowledge")) {
    openView("knowledge"); void showDocument(query.get("knowledge"), Number(query.get("page") || 1), query.get("revision"));
  }
  void load();
})();
