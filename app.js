const state = { token: "", settings: { groups: [] }, providers: [], audits: [], editing: null };
const $ = (selector) => document.querySelector(selector);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${state.token}`,
      ...(options.headers || {}),
    },
  });
  let payload = {};
  try { payload = await response.json(); } catch (_) { /* keep a useful fallback */ }
  if (!response.ok) throw new Error(payload.error || `请求失败：${response.status}`);
  return payload;
}

function toast(message) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.remove("hidden");
  window.setTimeout(() => el.classList.add("hidden"), 2300);
}

function providerOptions(selected = "", inheritLabel = "继承上级模型设置") {
  const options = [`<option value="">${escapeHtml(inheritLabel)}</option>`];
  for (const provider of state.providers) {
    const recommended = provider.recommended_small_model ? " · 推荐小模型" : "";
    options.push(`<option value="${escapeHtml(provider.id)}" ${provider.id === selected ? "selected" : ""}>${escapeHtml(provider.id)} · ${escapeHtml(provider.model || "未标注模型")}${recommended}</option>`);
  }
  return options.join("");
}

async function bootstrap() {
  const payload = await api("/api/bootstrap");
  state.settings = payload.settings;
  state.providers = payload.providers;
  state.audits = payload.audits;
  render();
}

function render() {
  renderMetrics();
  renderProvider();
  renderGroups();
  renderAudits();
}

function renderMetrics() {
  const enabledGroups = state.settings.groups.filter((group) => group.enabled).length;
  const approved = state.audits.filter((item) => item.status === "approved").length;
  const rejected = state.audits.filter((item) => item.status === "rejected").length;
  const manual = state.audits.filter((item) => item.status === "manual").length;
  $("#metrics").innerHTML = [
    ["已启用群", enabledGroups],
    ["最近自动通过", approved],
    ["最近自动拒绝", rejected],
    ["待人工关注", manual],
  ].map(([label, value]) => `<article class="panel metric"><p class="muted">${label}</p><strong>${value}</strong></article>`).join("");
}

function renderProvider() {
  $("#global-provider").innerHTML = providerOptions(
    state.settings.global_provider_id,
    "回退到 AstrBot 插件配置 / 默认聊天模型",
  );
}

function renderGroups() {
  const root = $("#group-list");
  if (!state.settings.groups.length) {
    root.innerHTML = `<article class="panel empty"><h3>还没有群规则</h3><p class="muted">从添加群开始。建议先保存规则，再用模拟审查验证几个典型答案。</p></article>`;
    return;
  }
  root.innerHTML = state.settings.groups.map((group) => {
    const rules = group.rules.filter((rule) => rule.enabled).length;
    return `<article class="panel group-card">
      <div class="group-card-head">
        <div>
          <p class="group-id">QQ群 ${escapeHtml(group.group_id)}</p>
          <h3>${escapeHtml(group.group_name || "未命名群")}</h3>
        </div>
        <span class="chip ${group.enabled ? "" : "off"}">${group.enabled ? "自动审查中" : "已停用"}</span>
      </div>
      ${group.extra_prompt ? `<p class="muted group-note">${escapeHtml(group.extra_prompt)}</p>` : ""}
      <div class="group-meta">
        <span class="chip">${rules} 条启用规则</span>
        <span class="chip">${escapeHtml(group.provider_id || "继承全局模型")}</span>
      </div>
      <button class="button ghost edit-group" data-group-id="${escapeHtml(group.group_id)}">编辑群规则</button>
    </article>`;
  }).join("");
  document.querySelectorAll(".edit-group").forEach((button) => {
    button.addEventListener("click", () => openGroupDialog(button.dataset.groupId));
  });
}

function renderAudits() {
  const root = $("#audit-list");
  if (!state.audits.length) {
    root.innerHTML = `<tr><td colspan="6" class="muted">暂无审查记录。</td></tr>`;
    return;
  }
  const labels = { approved: "已通过", rejected: "已拒绝", manual: "人工处理", ignored: "未接管" };
  root.innerHTML = state.audits.map((entry) => `<tr>
    <td>${escapeHtml(new Date(entry.created_at).toLocaleString())}</td>
    <td>${escapeHtml(entry.group_id)}</td>
    <td>${escapeHtml(entry.user_id)}</td>
    <td class="audit-answer">${escapeHtml(entry.answer || "（空）")}</td>
    <td><span class="result ${escapeHtml(entry.status)}">${escapeHtml(labels[entry.status] || entry.status)}</span></td>
    <td class="audit-answer">${escapeHtml(entry.reason || "")}</td>
  </tr>`).join("");
}

function randomId() {
  const bytes = new Uint8Array(16);
  if (window.crypto && typeof window.crypto.getRandomValues === "function") {
    window.crypto.getRandomValues(bytes);
    return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}${Math.random().toString(16).slice(2)}`.slice(0, 32);
}

function newRule() {
  return { id: randomId(), name: "", description: "", enabled: true };
}

function openGroupDialog(groupId = "") {
  const existing = state.settings.groups.find((group) => group.group_id === groupId);
  state.editing = existing ? structuredClone(existing) : {
    group_id: "", group_name: "", enabled: true, provider_id: "",
    reject_reason: "", extra_prompt: "", rules: [newRule()],
  };
  $("#dialog-title").textContent = existing ? "编辑群规则" : "添加群";
  $("#group-id").value = state.editing.group_id;
  $("#group-id").disabled = Boolean(existing);
  $("#group-name").value = state.editing.group_name;
  $("#group-enabled").checked = state.editing.enabled;
  $("#group-provider").innerHTML = providerOptions(state.editing.provider_id);
  $("#reject-reason").value = state.editing.reject_reason;
  $("#extra-prompt").value = state.editing.extra_prompt;
  $("#test-answer").value = "";
  $("#test-result").textContent = existing ? "等待测试" : "请先保存群配置";
  $("#delete-group").classList.toggle("hidden", !existing);
  $("#form-error").textContent = "";
  renderRules();
  $("#group-dialog").showModal();
}

function renderRules() {
  const root = $("#rule-list");
  if (!state.editing.rules.length) {
    root.innerHTML = `<p class="muted">暂无规则。至少添加并启用一条规则后，插件才会自动审查。</p>`;
    return;
  }
  root.innerHTML = state.editing.rules.map((rule, index) => `<div class="rule-row">
    <input class="rule-name" data-index="${index}" value="${escapeHtml(rule.name)}" placeholder="规则名称，例如：同意群规" />
    <textarea class="rule-description" data-index="${index}" rows="2" placeholder="例如：答案中出现与同意、遵守群规相关的明确表达时通过">${escapeHtml(rule.description)}</textarea>
    <div class="rule-actions">
      <label title="启用规则"><input class="rule-enabled" data-index="${index}" type="checkbox" ${rule.enabled ? "checked" : ""} /></label>
      <button class="button danger remove-rule" data-index="${index}" type="button">删除</button>
    </div>
  </div>`).join("");
  document.querySelectorAll(".remove-rule").forEach((button) => button.addEventListener("click", () => {
    state.editing.rules.splice(Number(button.dataset.index), 1);
    renderRules();
  }));
}

function collectEditingGroup() {
  document.querySelectorAll(".rule-name").forEach((input) => {
    state.editing.rules[Number(input.dataset.index)].name = input.value;
  });
  document.querySelectorAll(".rule-description").forEach((input) => {
    state.editing.rules[Number(input.dataset.index)].description = input.value;
  });
  document.querySelectorAll(".rule-enabled").forEach((input) => {
    state.editing.rules[Number(input.dataset.index)].enabled = input.checked;
  });
  return {
    ...state.editing,
    group_id: $("#group-id").value,
    group_name: $("#group-name").value,
    enabled: $("#group-enabled").checked,
    provider_id: $("#group-provider").value,
    reject_reason: $("#reject-reason").value,
    extra_prompt: $("#extra-prompt").value,
  };
}

async function saveGroup(event) {
  event.preventDefault();
  try {
    const group = collectEditingGroup();
    await api(`/api/groups${state.editing.group_id ? `/${encodeURIComponent(state.editing.group_id)}` : ""}`, {
      method: state.editing.group_id ? "PUT" : "POST",
      body: JSON.stringify(group),
    });
    $("#group-dialog").close();
    await bootstrap();
    toast("群规则已保存");
  } catch (error) {
    $("#form-error").textContent = error.message;
  }
}

async function deleteGroup() {
  if (!state.editing.group_id || !confirm(`确定删除群 ${state.editing.group_id} 的配置吗？`)) return;
  await api(`/api/groups/${encodeURIComponent(state.editing.group_id)}`, { method: "DELETE" });
  $("#group-dialog").close();
  await bootstrap();
  toast("群配置已删除");
}

async function runTest() {
  try {
    const group = collectEditingGroup();
    if (!state.settings.groups.some((item) => item.group_id === group.group_id)) {
      throw new Error("请先保存群配置，再运行模拟审查");
    }
    $("#test-result").textContent = "模型审查中...";
    const payload = await api("/api/test-review", {
      method: "POST",
      body: JSON.stringify({ group_id: group.group_id, answer: $("#test-answer").value }),
    });
    $("#test-result").textContent = JSON.stringify(payload, null, 2);
  } catch (error) {
    $("#test-result").textContent = `测试失败：${error.message}`;
  }
}

async function login(token) {
  state.token = token.trim();
  await bootstrap();
  localStorage.setItem("smart-group-verify-token", state.token);
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
}

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("#login-error").textContent = "";
  try { await login($("#login-token").value); }
  catch (error) { $("#login-error").textContent = error.message; }
});
$("#logout-button").addEventListener("click", () => {
  localStorage.removeItem("smart-group-verify-token");
  location.href = "/";
});
document.querySelectorAll(".tab").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab === button));
  document.querySelectorAll(".tab-panel").forEach((panel) => panel.classList.add("hidden"));
  $(`#${button.dataset.tab}-tab`).classList.remove("hidden");
}));
$("#save-global-provider").addEventListener("click", async () => {
  await api("/api/settings", { method: "PUT", body: JSON.stringify({ global_provider_id: $("#global-provider").value }) });
  await bootstrap();
  toast("全局模型已保存");
});
$("#add-group").addEventListener("click", () => openGroupDialog());
$("#close-dialog").addEventListener("click", () => $("#group-dialog").close());
$("#cancel-dialog").addEventListener("click", () => $("#group-dialog").close());
$("#add-rule").addEventListener("click", () => { collectEditingGroup(); state.editing.rules.push(newRule()); renderRules(); });
$("#delete-group").addEventListener("click", deleteGroup);
$("#run-test").addEventListener("click", runTest);
$("#group-form").addEventListener("submit", saveGroup);
$("#clear-audits").addEventListener("click", async () => {
  if (!confirm("确定清空审计记录吗？")) return;
  await api("/api/audits", { method: "DELETE" });
  await bootstrap();
  toast("审计记录已清空");
});

const queryToken = new URLSearchParams(location.search).get("token");
if (queryToken) {
  history.replaceState({}, "", "/");
  $("#login-token").value = queryToken;
}
const rememberedToken = queryToken || localStorage.getItem("smart-group-verify-token");
if (rememberedToken) {
  login(rememberedToken).catch((error) => {
    localStorage.removeItem("smart-group-verify-token");
    $("#login-error").textContent = error.message;
  });
}
