import { $, $$, on, api, esc } from "./api.js?v=202611012";

/* ---------------- 设置弹层 ---------------- */

let me = { username: "", is_admin: false };

const _arr = (v, dflt) => {
  try { const a = JSON.parse(v || "null"); return Array.isArray(a) ? a : dflt; }
  catch (e) { return dflt; }
};

async function openSettings(focusSafe = false) {
  const cfg = await api("/api/config");
  me = await api("/api/me");
  $("#cfg-push_time").value = cfg.push_time || "21:00";
  renderSubjectGroups(cfg.subject_groups, cfg.my_subjects);
  $("#cfg-my-classes").value = _arr(cfg.my_classes, []).map(c => String(c).replace(/班$/, "")).join("、");
  $("#cfg-lesson-remind").value = cfg.lesson_remind_min || "";
  let times = {};
  try { times = JSON.parse(cfg.lesson_times || "{}"); } catch (e) { times = {}; }
  $("#cfg-times").innerHTML = Object.entries(times).map(([sec, t]) => timeRow(sec, t)).join("")
    || timeRow("上午1", ["08:00", "08:45"]);
  sortTimeRows();
  const _d = new Date();
  $("#cfg-schedule-eff").value = `${_d.getFullYear()}-${String(_d.getMonth() + 1).padStart(2, "0")}-${String(_d.getDate()).padStart(2, "0")}`;
  $("#cfg-calendar-year").value = _d.getMonth() >= 7 ? _d.getFullYear() : _d.getFullYear() + 1;
  let ovr = {};
  try { ovr = JSON.parse(cfg.schedule_overrides || "{}"); } catch (e) { ovr = {}; }
  const WD = ["一", "二", "三", "四", "五", "六", "日"];
  $("#cfg-overrides").innerHTML = Object.entries(ovr).map(([d, w]) =>
    `<div class="row ovr-row" data-date="${d}">
       <input type="date" class="ovr-date" value="${d}">
       <span>按周</span>
       <select class="ovr-wd">${WD.map((n, i) =>
         `<option value="${i}" ${i === w ? "selected" : ""}>${n}</option>`).join("")}</select>
       <button class="del ovr-del" title="删除">×</button>
     </div>`).join("") || `<span class="dim" style="font-size:12.5px">暂无调休安排</span>`;
  $("#cfg-pushplus_token").value = "";
  $("#cfg-token-masked").textContent = cfg.pushplus_token_masked || "未设置";
  $("#cfg-wxpusher_uid").value = cfg.wxpusher_uid || "";
  $("#cfg-wxpusher-masked").textContent = cfg.wxpusher_token_masked || "未设置";
  $("#cfg-clawbot_token").value = "";
  $("#cfg-clawbot_secret").value = "";
  $("#cfg-sf_key").value = "";
  $("#cfg-sf_model").value = cfg.systemone_model || "semif";
  $("#cfg-sf-state").textContent = cfg.systemone_enabled
    ? `已配置 · 模型 ${cfg.systemone_model || "semif"}`
    : "未配置（回退 DeepSeek）";
  $("#cfg-clawbot-state").textContent = cfg.clawbot_enabled
    ? (cfg.clawbot_bound ? "已配置并绑定" : "已配置，未绑定")
    : "未配置";
  $("#admin-section").hidden = !me.is_admin;
  $("#cfg-wxpusher-admin").hidden = !me.is_admin;      // appToken 仅管理员可见
  if (me.is_admin) renderAdmin();
  buildCfgNav();
  $("#modal").hidden = false;
  const card = $("#modal .modal-card");
  if (card) card.scrollTop = 0;                       // 显示后再复位滚动位置
  if (focusSafe === true) focusSec("sec-safe");   // 严格布尔：防止事件对象等真值误触发
}

/* ---------------- 作息时间表 ---------------- */

function _secKey(sec) {
  // 节次排序键：上午=0 下午=1 晚上=2，再按数字；不规范的排最后按名排
  const m = /^(上午|下午|晚上)(\d+)$/.exec((sec || "").trim());
  if (!m) return [9, 99, sec || ""];
  return [{ "上午": 0, "下午": 1, "晚上": 2 }[m[1]], parseInt(m[2], 10), ""];
}

function sortTimeRows() {
  const box = $("#cfg-times");
  if (!box) return;
  [...box.querySelectorAll(".time-row")]
    .sort((a, b) => {
      const ka = _secKey(a.querySelector(".t-sec").value);
      const kb = _secKey(b.querySelector(".t-sec").value);
      return ka[0] - kb[0] || ka[1] - kb[1] || String(ka[2]).localeCompare(String(kb[2]));
    })
    .forEach(row => box.appendChild(row));
}

function timeRow(sec, t) {
  return `<div class="row time-row" data-sec="${esc(sec)}">
    <input type="text" class="t-sec" value="${esc(sec)}" placeholder="节次名（如 上午1）" style="width:110px">
    <input type="time" class="t-start" value="${esc((t && t[0]) || "")}"> -
    <input type="time" class="t-end" value="${esc((t && t[1]) || "")}">
    <button class="del t-del" title="删除">×</button></div>`;
}

on("#cfg-time-add", "click", () =>
  $("#cfg-times").insertAdjacentHTML("beforeend", timeRow("", ["", ""])));
on("#cfg-times", "change", ev => {
  if (ev.target.classList.contains("t-sec") && ev.target.value.trim()) sortTimeRows();
});
on("#cfg-times", "click", ev => {
  if (ev.target.classList.contains("t-del")) ev.target.closest(".time-row").remove();
});
on("#cfg-time-save", "click", async () => {
  const times = {};
  let bad = false;
  $$("#cfg-times .time-row").forEach(row => {
    const sec = row.querySelector(".t-sec").value.trim();
    const s0 = row.querySelector(".t-start").value, s1 = row.querySelector(".t-end").value;
    if (!sec) { bad = true; return; }
    times[sec] = [s0, s1];
  });
  if (bad) { $("#cfg-time-msg").textContent = "节次名不能为空"; return; }
  await api("/api/config", { method: "PUT", body: {
    lesson_times: JSON.stringify(times),
    lesson_remind_min: $("#cfg-lesson-remind").value.trim(),
  }});
  $("#cfg-time-msg").textContent = "作息与提醒已保存";
});

/* ---------------- 保存设置 ---------------- */

on("#cfg-save", "click", async () => {
  const pushBody = {
    push_time: $("#cfg-push_time").value || "21:00",
    wxpusher_uid: $("#cfg-wxpusher_uid").value.trim(),          // 可清空=停用 APP 推送
  };
  const wpt = $("#cfg-wxpusher_app_token").value.trim();
  if (wpt) pushBody.wxpusher_app_token = wpt;                   // 留空=不修改
  await api("/api/config", { method: "PUT", body: pushBody });
  $("#cfg-msg").textContent = "推送设置已保存";
});

/* ---------------- 科目组（多科目：主名 + 课表叫法） ---------------- */

function _parseList(v, stripBan) {
  let items = (v || "").split(/[、,，;；\s]+/).map(t => t.trim()).filter(Boolean);
  if (stripBan) items = items.map(t => t.replace(/班$/, ""));
  return [...new Set(items)];
}

function subjRow(name, aliases) {
  const row = document.createElement("div");
  row.className = "row subj-row";
  row.innerHTML = `
    <input type="text" class="sj-name" placeholder="科目名（如 语文）" value="${esc(name || "")}">
    <input type="text" class="sj-alias" placeholder="课表叫法，顿号分隔（如 语、文）" value="${esc(aliases || "")}">
    <button class="del sj-del" title="删除">×</button>`;
  return row;
}

function renderSubjectGroups(groupsJson, flatSubjects) {
  const box = $("#cfg-subj-groups");
  if (!box) return;
  box.innerHTML = "";
  let groups = null;
  try { groups = JSON.parse(groupsJson || "null"); } catch (e) { groups = null; }
  if (!Array.isArray(groups) || !groups.length) {
    // 旧扁平格式兜底：第一个 = 主名，其余 = 别名
    const flat = _arr(flatSubjects, []);
    groups = flat.length ? [{ name: flat[0], aliases: flat.slice(1) }] : [];
  }
  for (const g of groups)
    box.appendChild(subjRow(g.name || "", (g.aliases || []).join("、")));
  if (!groups.length) box.appendChild(subjRow("", ""));
}

on("#cfg-subj-add", "click", () => {
  $("#cfg-subj-groups").appendChild(subjRow("", ""));
});
on("#cfg-subj-groups", "click", e => {
  if (e.target.classList.contains("sj-del")) {
    const rows = $$("#cfg-subj-groups .subj-row");
    if (rows.length > 1) e.target.closest(".subj-row").remove();
    else e.target.closest(".subj-row").querySelectorAll("input").forEach(i => i.value = "");
  }
});

on("#cfg-import-save", "click", async () => {
  const groups = $$("#cfg-subj-groups .subj-row").map(row => {
    const name = row.querySelector(".sj-name").value.trim();
    const aliases = _parseList(row.querySelector(".sj-alias").value, false);
    return name ? { name, aliases } : null;
  }).filter(Boolean);
  const classes = _parseList($("#cfg-my-classes").value, true)
    .map(Number).filter(n => !Number.isNaN(n));
  const flat = [];
  for (const g of groups) flat.push(g.name, ...g.aliases);
  await api("/api/config", { method: "PUT", body: {
    subject_groups: JSON.stringify(groups),
    my_subjects: JSON.stringify(flat),
    my_classes: JSON.stringify(classes),
  }});
  $("#cfg-import-msg").textContent =
    `已保存：${groups.length} 门课、${classes.length} 个班级`;
});

/* ---------------- 明文课表导入 ---------------- */

on("#cfg-schedule-text-import", "click", async () => {
  const msg = $("#cfg-schedule-msg");
  const text = $("#cfg-schedule-text").value;
  if (!text.trim()) { msg.textContent = "请先粘贴课表文本"; return; }
  try {
    const mode = document.querySelector('input[name="cfg-text-mode"]:checked');
    const replace = !mode || mode.value === "replace";
    const r = await api("/api/import/text", { method: "POST",
      body: { text, effective_from: $("#cfg-schedule-eff").value || undefined, replace } });
    msg.textContent = `已${replace ? "覆盖导入" : "追加"} ${r.lessons} 节（生效 ${r.effective_from}）` +
      (r.warnings && r.warnings.length ? `；注意：${r.warnings.join("；")}` : "");
    if (replace) $("#cfg-schedule-text").value = "";
  } catch (e) { msg.textContent = "错误：" + e.message; }
});

/* ---------------- WxPusher 扫码订阅 ---------------- */

let _qrPollTimer = null;

function stopQrPoll() {
  if (_qrPollTimer) { clearInterval(_qrPollTimer); _qrPollTimer = null; }
}

on("#cfg-wxpusher-qr", "click", async () => {
  const box = $("#cfg-wxpusher-qrbox");
  stopQrPoll();
  const since = Date.now() - 5000;                  // 时间窗起点（留 5 秒钟表偏差余量）
  $("#cfg-msg").textContent = "正在生成订阅二维码…";
  try {
    const r = await api("/api/wxpusher/qrcode", { method: "POST" });
    box.hidden = false;
    $("#cfg-wxpusher-qrurl").src = r.code_url;
    $("#cfg-msg").textContent = "";
    let tries = 0;
    _qrPollTimer = setInterval(async () => {
      if (++tries > 20) {
        stopQrPoll();
        $("#cfg-msg").textContent = "没有自动识别到订阅。手动兜底：WxPusher APP →「我的」→ 复制 UID 粘贴到上面输入框，保存即可。";
        return;
      }
      try {
        const pr = await api("/api/wxpusher/uid-poll", { method: "POST", body: { since_ms: since } });
        if (pr.uid) {
          stopQrPoll();
          $("#cfg-wxpusher_uid").value = pr.uid;
          box.hidden = true;
          $("#cfg-msg").textContent = "订阅成功，UID 已自动填入，点「保存推送设置」完成";
        }
      } catch (e) { /* 下一轮再试 */ }
    }, 3000);
  } catch (e) {
    $("#cfg-msg").textContent = "生成失败：" + e.message;
  }
});

on("#modal-close", "click", stopQrPoll);

/* ---------------- 推送渠道测试 ---------------- */

on("#cfg-wxpusher-test", "click", async () => {
  $("#cfg-msg").textContent = "正在推送到 WxPusher…";
  try {
    const r = await api("/api/wxpusher/test", { method: "POST" });
    $("#cfg-msg").textContent = r.ok ? "WxPusher 测试消息已发送，看 APP" : "失败：" + (r.detail || "未知");
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});
on("#cfg-pushplus-test", "click", async () => {
  $("#cfg-msg").textContent = "正在推送到 PushPlus…";
  try {
    const r = await api("/api/pushplus/test", { method: "POST" });
    $("#cfg-msg").textContent = r.ok ? "PushPlus 测试消息已发送，看微信" : "发送失败：检查 token";
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});

/* ---------------- Excel 上传导入 ---------------- */

on("#cfg-upload-btn", "click", async () => {
  const f = $("#cfg-upload-file").files[0];
  if (!f) { $("#cfg-msg").textContent = "请先选择 .xlsx / .xls 文件"; return; }
  if (f.size > 5 * 1024 * 1024) { $("#cfg-msg").textContent = "文件超过 5MB 上限"; return; }
  const fd = new FormData();
  fd.append("file", f);
  if ($("#cfg-upload-eff").value) fd.append("effective_from", $("#cfg-upload-eff").value);
  $("#cfg-msg").textContent = "正在上传导入…";
  try {
    const res = await fetch("/api/import/upload", { method: "POST", body: fd });
    const j = await res.json();
    if (!res.ok) throw new Error(typeof j.detail === "string" ? j.detail : res.statusText);
    const eff = j.report && j.report.effective_from ? `（生效 ${j.report.effective_from}）` : "";
    $("#cfg-msg").textContent = `导入完成${eff}`;
    $("#cfg-msg").classList.add("ok");
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});

/* ---------------- 推送测试 ---------------- */

on("#cfg-test", "click", async () => {
  $("#cfg-msg").textContent = "正在发送测试推送…";
  try {
    const r = await api("/api/digest/send", { method: "POST" });
    $("#cfg-msg").textContent = r.ok ? "已推送" : "未推送（今天可能没有日程，或 PushPlus 失败）";
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});

/* ---------------- 密码与登录 ---------------- */

on("#cfg-pass-save", "click", async () => {
  const v = $("#cfg-newpass").value.trim();
  if (!v) { $("#cfg-msg").textContent = "请输入新密码"; return; }
  try {
    await api("/api/config", { method: "PUT", body: {
      login_password: v, current_password: $("#cfg-curpass").value }});
    $("#cfg-msg").textContent = "密码已保存，本机已自动续登，其他设备需重新登录";
    $("#cfg-newpass").value = ""; $("#cfg-curpass").value = "";
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});

on("#cfg-logout", "click", async () => {
  await api("/api/logout", { method: "POST" });
  location.href = "/login";
});

/* ---------------- ClawBot / SystemOne ---------------- */

on("#cfg-clawbot-test", "click", async () => {
  $("#cfg-msg").textContent = "正在发送测试消息…";
  try {
    const r = await api("/api/clawbot/test", { method: "POST" });
    $("#cfg-msg").textContent = r.ok ? "测试消息已发送" : "发送失败，检查 token/secret 与绑定状态";
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});
on("#cfg-clawbot-poll", "click", async () => {
  $("#cfg-msg").textContent = "正在收一次消息…";
  try {
    const r = await api("/api/clawbot/poll", { method: "POST" });
    $("#cfg-msg").textContent = r.ok ? `已处理 ${r.count ?? 0} 条` : "处理失败";
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});
on("#cfg-sf-test", "click", async () => {
  $("#cfg-msg").textContent = "正在测试连通…";
  try {
    const r = await api("/api/systemone/test", { method: "POST" });
    $("#cfg-msg").textContent = r.ok ? "连通正常" : "连通失败，检查 Key";
  } catch (e) { $("#cfg-msg").textContent = "错误：" + e.message; }
});

/* ---------------- 用户管理（管理员） ---------------- */

async function renderAdmin() {
  const users = await api("/api/admin/users");
  $("#admin-invite").textContent = users.invite_code || "…";
  $("#admin-users").innerHTML = (users.users || []).map(u =>
    `<div class="stat-row"><b>${esc(u.username)}</b><span class="dim">${u.is_admin ? "管理员" : ""}</span>
     ${u.username !== me.username ? `<button class="del admin-del" data-u="${esc(u.username)}">删除</button>` : ""}
     </div>`).join("");
  $$("#admin-users .admin-del").forEach(b => b.onclick = async () => {
    const u = b.dataset.u;
    if (!confirm(`删除用户「${u}」？其全部数据将被删除，不可恢复。`)) return;
    try {
      await api(`/api/admin/users/${u}`, { method: "DELETE" });
      $("#admin-msg").textContent = `已删除 ${u}`;
      renderAdmin();
    } catch (e) { $("#admin-msg").textContent = "错误：" + e.message; }
  });
}

on("#admin-invite-reset", "click", async () => {
  try {
    const r = await api("/api/admin/invite/reset", { method: "POST" });
    $("#admin-invite").textContent = r.invite_code;
    $("#admin-msg").textContent = "邀请码已重置";
  } catch (e) { $("#admin-msg").textContent = "错误：" + e.message; }
});


export { openSettings };

on("#cfg-calendar-text-import", "click", async () => {
  const msg = $("#cfg-calendar-msg");
  const text = $("#cfg-calendar-text").value;
  if (!text.trim()) { msg.textContent = "请先粘贴校历文本"; return; }
  try {
    const r = await api("/api/import/calendar-text", { method: "POST",
      body: { text, year: +$("#cfg-calendar-year").value || undefined } });
    msg.textContent = `已导入 ${r.events} 条校历` +
      (r.warnings && r.warnings.length ? `；注意：${r.warnings.join("；")}` : "");
    $("#cfg-calendar-text").value = "";
  } catch (e) { msg.textContent = "错误：" + e.message; }
});

/* ---------------- 调休与补班 ---------------- */

on("#cfg-ovr-add", "click", () => {
  const row = document.createElement("div");
  row.className = "row ovr-row";
  row.innerHTML = `<input type="date" class="ovr-date">
    <span>按周</span>
    <select class="ovr-wd">${["一", "二", "三", "四", "五", "六", "日"].map((n, i) =>
      `<option value="${i}">${n}</option>`).join("")}</select>
    <button class="del ovr-del" title="删除">×</button>`;
  $("#cfg-overrides").appendChild(row);
});
on("#cfg-overrides", "click", ev => {
  if (ev.target.classList.contains("ovr-del")) {
    ev.target.closest(".ovr-row").remove();
  }
});
on("#cfg-ovr-save", "click", async () => {
  const ovr = {};
  let bad = false;
  $$("#cfg-overrides .ovr-row").forEach(row => {
    const d = row.querySelector(".ovr-date").value;
    const w = row.querySelector(".ovr-wd").value;
    if (!d) { bad = true; return; }
    ovr[d] = +w;
  });
  if (bad) { $("#cfg-ovr-msg").textContent = "有行缺日期"; return; }
  await api("/api/config", { method: "PUT", body: { schedule_overrides: JSON.stringify(ovr) } });
  $("#cfg-ovr-msg").textContent = "调休已保存";
});

/* ---------------- 设置分区导航 ---------------- */

function buildCfgNav() {
  const nav = $("#cfg-nav");
  if (!nav || nav.children.length) return;
  $$("#modal .cfg-sec").forEach(sec => {
    const b = document.createElement("button");
    b.className = "cfg-chip"; b.type = "button";
    b.textContent = sec.dataset.nav || "";
    b.onclick = () => {
      $$("#cfg-nav .cfg-chip").forEach(c => c.classList.remove("active"));
      b.classList.add("active");
      sec.scrollIntoView({ behavior: "smooth", block: "start" });
    };
    nav.appendChild(b);
  });
}

function focusSec(id) {
  const sec = document.getElementById(id);
  if (!sec) return;
  requestAnimationFrame(() => {
    sec.scrollIntoView({ behavior: "smooth", block: "start" });
    sec.classList.remove("flash"); void sec.offsetWidth; sec.classList.add("flash");
    const chip = [...$$("#cfg-nav .cfg-chip")].find(c => c.textContent === (sec.dataset.nav || ""));
    if (chip) { $$("#cfg-nav .cfg-chip").forEach(c => c.classList.remove("active")); chip.classList.add("active"); }
  });
}

on("#btn-import", "click", async () => {   // 顶栏「导入」直达课表导入分区
  await openSettings();
  focusSec("sec-import");
});
