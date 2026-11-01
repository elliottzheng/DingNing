import { $, $$, on, api, esc, fmt, WD } from "./api.js?v=202611012";
import { S } from "./state.js?v=202611012";
import { refreshView } from "./router.js?v=202611012";

/* ---------------- 添加 / 编辑弹层（日程 / 待办共用） ---------------- */

function setAddType(t) {
  S.addType = t;
  $$(".type-tab").forEach(b => b.classList.toggle("active", b.dataset.type === t));
  $("#add-event-fields").hidden = t !== "event";
  $("#add-todo-fields").hidden = t !== "todo";
}

function openForm(type, item = null, prefill = null) {
  S.editing = item ? { type, item } : null;
  setAddType(type);
  $("#form-head").textContent = item
    ? (type === "event" ? "编辑日程" : "编辑待办")
    : (type === "event" ? "添加日程" : "添加待办");
  $("#add-save").textContent = item ? "保存修改" : "保存";
  $("#add-del").hidden = !item;
  $("#add-title").value = item ? item.title : "";
  $("#add-note").value = item ? (item.note || "") : "";
  if (type === "event") {
    // 今日视图 items 用 start/end（无 date）；预填时用当前浏览日期
    $("#add-date").value = item ? (item.date || fmt(S.todayView)) : (prefill && prefill.date ? prefill.date : fmt(S.todayView));
    $("#add-start").value = item ? (item.start_time || item.start || "") : (prefill && prefill.start || "");
    $("#add-end").value = item ? (item.end_time || item.end || "") : (prefill && prefill.end || "");
    $("#add-loc").value = item ? (item.location || "") : "";
    $("#add-remind").value = item && item.remind_min != null ? String(item.remind_min) : "";
  } else {
    $("#add-due").value = item && item.due ? item.due.replace(" ", "T")
      : (prefill && prefill.due ? prefill.due : "");
    $("#add-prio").value = item ? String(item.priority) : "1";
    $("#add-remind").value = item && item.remind_min != null ? String(item.remind_min) : "";
  }
  $("#add-msg").textContent = "";
  $("#add-modal").hidden = false;
  setTimeout(() => $("#add-title").focus(), 60);
}

on("#add-save", "click", async () => {
  const title = $("#add-title").value.trim();
  if (!title) { $("#add-msg").textContent = "请填写标题"; return; }
  const btn = $("#add-save");
  if (btn.disabled) return;                       // 防连点重复提交
  btn.disabled = true;
  try {
    if (S.addType === "event") {
      const body = {
        title, date: $("#add-date").value || fmt(new Date()),
        start_time: $("#add-start").value || "",
        end_time: $("#add-end").value || "",
        location: $("#add-loc").value.trim(),
        note: $("#add-note").value.trim(),
        remind_min: S.editing
          ? ($("#add-remind").value === "" ? -1 : +$("#add-remind").value)   // PATCH: -1=清除
          : ($("#add-remind").value === "" ? null : +$("#add-remind").value),
      };
      await api(S.editing ? `/api/events/${S.editing.item.id}` : "/api/events",
        { method: S.editing ? "PATCH" : "POST", body });
    } else {
      const body = {
        title, due: $("#add-due").value ? $("#add-due").value.replace("T", " ") : "",
        priority: +$("#add-prio").value, note: $("#add-note").value.trim(),
        remind_min: S.editing
          ? ($("#add-remind").value === "" ? -1 : +$("#add-remind").value)
          : ($("#add-remind").value === "" ? null : +$("#add-remind").value),
      };
      await api(S.editing ? `/api/todos/${S.editing.item.id}` : "/api/todos",
        { method: S.editing ? "PATCH" : "POST", body });
    }
    $("#add-modal").hidden = true;
    refreshView();
  } catch (e) { $("#add-msg").textContent = "错误：" + e.message; }
  finally { btn.disabled = false; }
});

on("#add-del", "click", async () => {
  if (!S.editing) return;
  const what = S.addType === "event" ? "日程" : "待办";
  if (!confirm(`确定删除这条${what}？（${S.editing.item.title}）`)) return;
  try {
    await api(`/api/${S.addType === "event" ? "events" : "todos"}/${S.editing.item.id}`, { method: "DELETE" });
    $("#add-modal").hidden = true;
    refreshView();
  } catch (e) { $("#add-msg").textContent = "错误：" + e.message; }
  finally { btn.disabled = false; }
});
/* ---------------- 调课弹层（停课 / 换课） ---------------- */


async function openLessonForm(it) {
  S.lessonCtx = it;
  $("#ls-head").textContent = it.cancelled ? "调课 · 已调走" : "调课 / 停课";
  $("#ls-info").innerHTML = `<b>${esc(it.date)} ${WD[it.weekday]} ${esc(it.sub)}</b>（${it.start}–${it.end}）<br>${esc(it.title)}`
    + (it.cancelled ? `<br><span class="offtag">已调课</span> ${esc(it.note || "")}` : "");
  $("#ls-main").hidden = !!it.cancelled;
  $("#ls-restore-row").hidden = !it.cancelled;
  $("#ls-move-form").hidden = true;
  $("#ls-time-form").hidden = true;
  $("#ls-msg").textContent = "";
  // 节次下拉（用于换课目标）
  const cfg = await api("/api/config");
  try { S.lessonSecTimes = JSON.parse(cfg.lesson_times || "{}"); } catch (e) { S.lessonSecTimes = {}; }
  const sel = $("#ls-new-sec");
  sel.innerHTML = Object.keys(S.lessonSecTimes).map(k =>
    `<option value="${k}">${k} ${S.lessonSecTimes[k][0]}–${S.lessonSecTimes[k][1]}</option>`).join("");
  const nd = new Date(it.date + "T00:00:00");
  nd.setDate(nd.getDate() + 7);
  $("#ls-new-date").value = fmt(nd);          // 默认换到下周同一天
  $("#ls-note").value = "";
  // "改时间"表单默认填当前起止
  $("#ls-new-start2").value = it.start || "";
  $("#ls-new-end2").value = it.end || "";
  $("#ls-time-note").value = "";
  $("#lesson-modal").hidden = false;
}

on("#ls-close", "click", () => $("#lesson-modal").hidden = true);
on("#lesson-modal", "click", e => { if (e.target.id === "lesson-modal") $("#lesson-modal").hidden = true; });

on("#ls-cancel-btn", "click", async () => {
  if (!S.lessonCtx) return;
  if (!confirm(`确定 ${S.lessonCtx.date} ${S.lessonCtx.sub}（${S.lessonCtx.title}）本次停课？`)) return;
  try {
    await api("/api/lesson_cancel", { method: "POST", body: {
      date: S.lessonCtx.date, weekday: S.lessonCtx.weekday, section: S.lessonCtx.section, note: "临时停课" }});
    $("#lesson-modal").hidden = true;
    refreshView();
  } catch (e) { $("#ls-msg").textContent = "错误：" + e.message; }
});

on("#ls-move-btn", "click", () => {
  $("#ls-move-form").hidden = false;
  $("#ls-time-form").hidden = true;
  $("#ls-main").hidden = true;
});

on("#ls-time-btn", "click", () => {
  $("#ls-time-form").hidden = false;
  $("#ls-move-form").hidden = true;
  $("#ls-main").hidden = true;
});

on("#ls-time-save", "click", async () => {
  if (!S.lessonCtx) return;
  const s = $("#ls-new-start2").value, e = $("#ls-new-end2").value;
  if (!s || !e || e <= s) { $("#ls-msg").textContent = "请填写有效的新开始/结束时间"; return; }
  try {
    const r = await api("/api/lesson_move", { method: "POST", body: {
      date: S.lessonCtx.date, weekday: S.lessonCtx.weekday, section: S.lessonCtx.section,
      new_date: S.lessonCtx.date, new_start: s, new_end: e,
      note: $("#ls-time-note").value.trim() || "调整上课时间",
    }});
    $("#lesson-modal").hidden = true;
    alert(`已调整：${r.moved} 本次改为 ${r.to}`);
    refreshView();
  } catch (e) { $("#ls-msg").textContent = "错误：" + e.message; }
});

on("#ls-restore", "click", async () => {
  if (!S.lessonCtx || !S.lessonCtx.cancel_id) return;
  try {
    await api(`/api/lesson_cancel/${S.lessonCtx.cancel_id}`, { method: "DELETE" });
    $("#lesson-modal").hidden = true;
    refreshView();
  } catch (e) { $("#ls-msg").textContent = "错误：" + e.message; }
});

on("#ls-move-save", "click", async () => {
  if (!S.lessonCtx) return;
  const newDate = $("#ls-new-date").value;
  const sec = $("#ls-new-sec").value;
  if (!newDate || !sec || !S.lessonSecTimes[sec]) { $("#ls-msg").textContent = "请选择换到的日期和节次"; return; }
  try {
    const r = await api("/api/lesson_move", { method: "POST", body: {
      date: S.lessonCtx.date, weekday: S.lessonCtx.weekday, section: S.lessonCtx.section,
      new_date: newDate, new_start: S.lessonSecTimes[sec][0],
      new_end: S.lessonSecTimes[sec][1],
      note: $("#ls-note").value.trim(),
    }});
    $("#lesson-modal").hidden = true;
    alert(`换课完成：${r.moved} 已换至 ${r.to}\n原时间已标为调课（可点击恢复）`);
    refreshView();
  } catch (e) { $("#ls-msg").textContent = "错误：" + e.message; }
});
$$(".type-tab").forEach(b => b.onclick = () => setAddType(b.dataset.type));

export { setAddType, openForm, openLessonForm };
