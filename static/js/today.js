import { $, on, api, esc, fmt, WD, md, nowStr, remaining, dueLabel } from "./api.js?v=202611012";
import { S } from "./state.js?v=202611012";
import { switchView } from "./router.js?v=202611012";
import { openForm, openLessonForm } from "./forms.js?v=202611012";

/* ---------------- 今日视图 ---------------- */

async function loadDay() {
  const seq = ++S.reqSeq;
  localStorage.setItem("dbg_ld", `start seq=${seq} reqSeq=${S.reqSeq}`);
  const ds = fmt(S.todayView);
  const data = await api(`/api/day?d=${ds}`);
  if (seq !== S.reqSeq) return;                       // 已有更新的请求，丢弃旧响应

  $("#d-label").textContent = `${md(S.todayView)} ${WD[data.weekday]}` + (data.date === fmt(new Date()) ? " · 今天" : "");
  const wb = $("#d-week");
  if (data.term) { wb.textContent = `${data.term} · 第 ${data.week} 周`; wb.className = "weekbadge"; }
  else { wb.textContent = "假期中"; wb.className = "weekbadge holiday-term"; }

  const bn = $("#banners");
  bn.innerHTML = "";
  for (const b of data.banners) {
    const t = b.time ? `${b.time} ` : "";
    bn.insertAdjacentHTML("beforeend",
      `<div class="banner ${b.kind}">【${b.label}】${t}${esc(b.title)}</div>`);
  }

  const sc = $("#schedule");
  sc.innerHTML = "";
  const isHoliday = data.holiday && data.holiday.length;
  // 已调走的课（节次被占据的）汇总为一行摘要，不再占用主列表
  const movedList = (data.items || []).filter(it => it.cancelled);
  if (movedList.length)
    sc.innerHTML = `<div class="day-moved">↩ 今日调课：` + movedList.map(it =>
      `${it.start} ${it.title}（${esc((it.note || "已调").slice(0, 30))}）`).join("；") + `</div>`;
  if (!data.items.length) {
    const tip = isHoliday ? `假期停课（${esc(data.holiday[0])}），好好休息 ` : "这一天没有课程或日程 ";
    sc.innerHTML = `<div class="empty">${tip}</div>`;
  }
  data.items.forEach((it, idx) => {
    const time = it.start ? (it.end ? `${it.start}–${it.end}` : it.start) : "全天";
    const sub = [it.sub, it.location, it.note].filter(Boolean).join(" · ");
    const caltag = it.type === "calendar"
      ? `<span class="caltag ${it.kind}">${it.kind === "exam" ? "考试" : "校历"}</span>` : "";
    const isLesson = it.type === "lesson";
    const clickable = (it.type === "event" && it.id) || isLesson ? ` clickable` : "";
    // 已调走的课：原节次被新课占据 → 不显示；节次空着 → 灰色保留
    if (it.cancelled && (data.items || []).some(x => !x.cancelled && x.start === it.start)) return;                                     // forEach：return 即跳过该项
    const dimmed = it.past ? " past" : "";
    const reason = (it.note || "").replace(/（[^）]*）|\([^)]*\)/g, "").trim();
    const badge = it.cancelled ? `<span class="offtag">${esc(reason || "已调课")}</span>` : "";
    sc.insertAdjacentHTML("beforeend", `
      <div class="item ${it.type} ${it.ongoing ? "ongoing" : ""}${dimmed}">
        <span class="time">${time}</span>
        <span class="body${clickable}" data-idx="${idx}">
          <span class="title">${esc(it.title)}</span>${caltag}${badge}${it.ongoing ? '<span class="live">进行中</span>' : ""}
          ${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</span>
        ${it.type === "event" && it.id ? `<button class="del" data-idx="${idx}" title="删除此日程">×</button>` : ""}
      </div>`);
  });
  sc.querySelectorAll(".item .body.clickable").forEach(b => b.onclick = () => {
    const it = data.items[+b.dataset.idx];
    if (!it) return;
    if (it.type === "lesson") openLessonForm(it);
    else if (it.id) openForm("event", it);
  });
  sc.querySelectorAll(".item .del").forEach(b => b.onclick = async ev => {
    ev.stopPropagation();
    const it = data.items[+b.dataset.idx];
    if (!confirm(`删除日程「${it.title}」？`)) return;
    await api(`/api/events/${it.id}`, { method: "DELETE" });
    loadDay();
  });

  renderDueToday(data.due_todos || [], data.date);
  renderTodos(data.todos);
  maybeShowOnboarding(data);
  localStorage.setItem("dbg_ld", localStorage.getItem("dbg_ld") + " -> done");
}

// 当日截止条：浏览哪天就显示那天截止的待办（与周视图的每日提示一致）
function renderDueToday(list, dateStr) {
  const box = $("#due-today");
  if (!box) return;
  const label = `${dateStr.slice(5).replace("-", "/")} 截止`;
  if (!list.length) {
    box.innerHTML = `<span class="due-today-empty">${label}：无</span>`;
    return;
  }
  box.innerHTML = `<span class="due-today-label">${label}</span>` + list.map(t =>
    `<button class="due-chip" data-tid="${t.id}" title="点击编辑/删除">⏰${t.due.slice(11, 16)} ${esc(t.title)}</button>`).join("");
  box.querySelectorAll(".due-chip").forEach(b => b.onclick = () => {
    const t = list.find(x => x.id === +b.dataset.tid);
    if (t) openForm("todo", t);
  });
}

function renderTodos(todos) {
  const ul = $("#todos");
  ul.innerHTML = "";
  const todayStr = fmt(new Date());
  for (const t of todos) {
    const li = document.createElement("li");
    li.className = "todo" + (t.done ? " done" : "");
    if (!t.done && t.due) {
      if (t.due < nowStr()) li.classList.add("overdue");
      else if (t.due.startsWith(todayStr)) li.classList.add("today-due");
    }
    const dueTxt = t.due ? `${dueLabel(t.due)}（${remaining(t.due)}）` : "";
    const prio = t.priority === 2 ? "p2" : (t.priority === 0 ? "p0" : "");
    li.innerHTML = `
      <div class="todo-row">
        <span class="prio ${prio}"></span>
        <input type="checkbox" class="check" ${t.done ? "checked" : ""}>
        <span class="t-title" title="点击编辑">${esc(t.title)}</span>
        ${dueTxt ? `<span class="t-due" title="截止 ${t.due}">${dueTxt}</span>` : ""}
      </div>`;
    li.querySelector(".check").onchange = e =>
      api(`/api/todos/${t.id}`, { method: "PATCH", body: { done: e.target.checked } }).then(loadDay);
    li.querySelector(".t-title").onclick = () => openForm("todo", t);
    ul.appendChild(li);
  }
  if (!todos.length) ul.innerHTML = `<div class="empty">暂无待办，点“＋ 待办”或用上方输入框添加</div>`;
}


// ---------------- 快速输入框：微信同款智能链路 ----------------
function showQuickReply(text) {
  const box = $("#quick-reply");
  box.textContent = text;
  box.hidden = false;
}
async function quickAdd() {
  const inp = $("#quick-input");
  const text = inp.value.trim();
  if (!text) return;
  const btn = $("#quick-btn");
  btn.disabled = true;
  showQuickReply("处理中…");
  try {
    const r = await api("/api/chat", { method: "POST", body: { text } });
    showQuickReply(r.reply);
    inp.value = "";
    loadDay();
  } catch (e) {
    showQuickReply("错误：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

export { loadDay, quickAdd };

// ---------------- 新手引导（今日无任何内容时显示） ----------------
function maybeShowOnboarding(data) {
  const ob = document.getElementById("onboard");
  if (!ob) return;
  const empty = !(data.items || []).length && !(data.todos || []).length;
  ob.hidden = !empty || localStorage.getItem("ob_hide") === "1";
}
