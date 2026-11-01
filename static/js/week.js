import { $, on, api, esc, fmt, WD, md, toMin } from "./api.js?v=202611012";
import { S } from "./state.js?v=202611012";
import { openForm, openLessonForm } from "./forms.js?v=202611012";

/* ---------------- 周视图（时间比例网格） ---------------- */

async function loadWeek() {
  const seq = ++S.reqSeq;
  const monday = new Date(S.weekAnchor);
  monday.setDate(monday.getDate() - ((monday.getDay() + 6) % 7));
  const sunday = new Date(monday); sunday.setDate(sunday.getDate() + 6);
  const data = await api(`/api/week?d=${fmt(monday)}`);
  if (seq !== S.reqSeq) return;

  $("#w-label").textContent = `${md(monday)} — ${md(sunday)}`;
  const wb = $("#w-week");
  if (data.term) { wb.textContent = `${data.term} · 第 ${data.week} 周`; wb.className = "weekbadge"; }
  else { wb.textContent = "假期中"; wb.className = "weekbadge holiday-term"; }

  if (window.matchMedia("(max-width: 720px)").matches) {
    renderWeekMobile(data);
    return;
  }

  const PX_H = 56;
  let minM = 8 * 60, maxM = 17.5 * 60;
  for (const s of (data.sections || [])) if (s.start && s.end) {
    minM = Math.min(minM, toMin(s.start)); maxM = Math.max(maxM, toMin(s.end));
  }
  for (const d of data.days) for (const it of d.items) {
    if (!it.start) continue;
    minM = Math.min(minM, toMin(it.start));
    maxM = Math.max(maxM, toMin(it.end || it.start) + (it.end ? 0 : 60));
  }
  minM = Math.floor(minM / 60) * 60;
  maxM = Math.ceil(maxM / 60) * 60;
  const bodyH = Math.round((maxM - minM) / 60 * PX_H);

  const todayStr = fmt(new Date());
  const now = new Date();
  const nowMin = now.getHours() * 60 + now.getMinutes();
  const showNow = nowMin >= minM && nowMin <= maxM;

  let html = `<div class="wk"><div class="wk-head"><div class="wk-corner"></div>`;
  data.days.forEach((d, i) => {
    const dt = new Date(d.date + "T00:00:00");
    const hol = d.holiday && d.holiday.length;
    const allday = d.items.filter(it => !it.start && (it.type === "calendar" || it.type === "event"));
    const dts = d.due_todos || [];
    html += `<div class="wk-h ${d.date === todayStr ? "today-col" : ""} ${hol ? "is-hol" : ""}">
      <span>${WD[i]}</span><small>${dt.getMonth() + 1}/${dt.getDate()}</small>
      ${hol ? `<i class="wk-hol" title="${esc(d.holiday.join("；"))}">${esc(d.holiday[0])}</i>` : ""}
      ${allday.map(e => `<i class="wk-ev" title="${esc(e.title)}">${esc(e.title)}</i>`).join("")}
      ${dts.map(t => `<i class="wk-todo" data-tid="${t.id}"
        title="截止 ${t.due} ｜ ${esc(t.title)}（点击编辑/删除）">⏰${t.due.slice(11, 16)} ${esc(t.title)}</i>`).join("")}
      <i class="wk-addtodo" data-date="${d.date}" title="新建当天截止的待办">＋待办</i></div>`;
  });
  html += `</div><div class="wk-body" style="height:${bodyH}px"><div class="wk-ticks">`;
  for (let m = minM; m < maxM; m += 60) {
    html += `<div class="wk-tick" style="top:${Math.round((m - minM) / 60 * PX_H)}px">${String(Math.floor(m / 60)).padStart(2, "0")}:00</div>`;
  }
  html += `</div>`;

  const isPastDate = ds => ds < todayStr;
  S.wkCtx.minM = minM; S.wkCtx.maxM = maxM; S.wkCtx.PX_H = PX_H;
  S.wkCtx.dayItems = data.days.map(d => d.items);
  S.wkCtx.sections = data.sections || [];
  S.wkCtx.dueTodos = data.days.flatMap(d => d.due_todos || []);   // 供点击查找
  data.days.forEach((d, di) => {
    const hol = d.holiday && d.holiday.length;
    const past = isPastDate(d.date);
    html += `<div class="wk-col ${d.date === todayStr ? "today-col" : ""} ${hol ? "is-hol" : ""} ${past ? "past" : ""}"
      data-date="${d.date}" data-di="${di}">`;
    if (d.date === todayStr && showNow)
      html += `<div class="wk-now" style="top:${Math.round((nowMin - minM) / 60 * PX_H)}px"></div>`;
    // 时间网格只放有起止的条目（课程/日程/校历），待办作为全天事项放列头
    const activeItems = d.items.filter(it => it.start && !it.cancelled);
    const movedOut = d.items.filter(it => it.cancelled && it.start);
    // 已调走的课：有保留课占据原时段 → 说明合并进该课（↩ 行）；无人占据 → 灰色独立显示
    const moveNotes = new Map();                       // 占位课 -> 调走说明
    const freeMoved = [];
    for (const m of movedOut) {
      const mS = toMin(m.start), mE = toMin(m.end || m.start);
      const occ = activeItems.find(x => {
        const xs = toMin(x.start), xe = toMin(x.end || x.start);
        return mS < xe && xs < mE;
      });
      if (occ) {
        const txt = `${m.start} ${m.title} 已调走`;
        moveNotes.set(occ, (moveNotes.get(occ) ? moveNotes.get(occ) + "；" : "") + txt);
      } else {
        freeMoved.push(m);
      }
    }
    let lastEnd = -1, lane = 0;
    const laid = activeItems.map(it => {
      const sM = toMin(it.start);
      const eM = it.end ? toMin(it.end) : Math.min(sM + 60, maxM);
      lane = sM < lastEnd - 2 ? Math.min(lane + 1, 1) : 0;
      lastEnd = Math.max(lastEnd, eM);
      return { it, sM, eM, lane };
    });
    const twoCols = laid.some(x => x.lane === 1);
    for (const { it, sM, eM, lane } of laid) {
      const top = Math.round((sM - minM) / 60 * PX_H);
      const h = Math.max(Math.round((eM - sM) / 60 * PX_H) - 2, 24);
      const cls = it.type === "lesson" ? "lesson" : (it.type === "calendar" ? "cal-" + it.kind : "user");
      const pastCls = (past || (d.date === todayStr && eM < nowMin)) ? "past" : "";
      const title = it.title + (it.location ? " @" + it.location : "") + (it.note ? " ｜ " + it.note : "");
      const left = lane ? 51 : 3;
      const width = twoCols ? 45 : 94;
      const noteTxt = it.note ? `（${esc(it.note)}）` : "";
      const moveNote = moveNotes.get(it)
        ? `<i class="wk-subnote">↩ ${esc(moveNotes.get(it))}</i>` : "";
      html += `<div class="wk-blk ${cls} ${pastCls}" style="top:${top}px;height:${h}px;left:${left}%;width:${width}%"
        data-di="${di}" data-idx="${d.items.indexOf(it)}" title="${esc(title)}"><b>${it.start}${it.end ? "–" + it.end : ""}</b><span>${esc(it.title)}${noteTxt}${moveNote}</span></div>`;
    }
    // 已调走的课：原节次空闲 → 全泳道灰显（该时段唯一的标记）；
    // 与主课或其他灰块时间重叠 → 收窄半宽置右，互相避让
    const renderedMoved = [];
    for (const it of freeMoved) {
      const sM = toMin(it.start);
      const eM = it.end ? toMin(it.end) : Math.min(sM + 60, maxM);
      const top = Math.round((sM - minM) / 60 * PX_H);
      const h = Math.max(Math.round((eM - sM) / 60 * PX_H) - 2, 24);
      const pastCls = (past || (d.date === todayStr && eM < nowMin)) ? "past" : "";
      const title = it.title + (it.location ? " @" + it.location : "") + (it.note ? " ｜ " + it.note : "");
      const timeHit = x => { const xs = toMin(x.start), xe = toMin(x.end || x.start);
        return sM < xe && xs < eM; };
      const clash = activeItems.some(timeHit) || renderedMoved.some(timeHit);
      const left = clash ? 51 : 3;
      const width = clash ? 45 : 94;
      html += `<div class="wk-blk cancelled ${pastCls}" style="top:${top}px;height:${h}px;left:${left}%;width:${width}%"
        title="${esc(title)}"><b>${it.start}${it.end ? "–" + it.end : ""}</b><span>${esc(it.title)}${it.note ? " ｜ " + it.note : ""}</span></div>`;
      renderedMoved.push({ start: it.start, end: it.end || it.start });
    }
    html += `</div>`;
  });
  html += `</div>`;                                     // 关闭 wk-body
  html += `</div>`;                                     // 关闭 .wk
  const movedSummary = data.days.map((d, di) => {
    const list = d.items.filter(it => it.cancelled);
    if (!list.length) return "";
    return `${WD[di]} ` + list.map(x => `${x.start} ${x.title}（${x.note || "已调"}）`).join("、");
  }).filter(Boolean).join("；");
  if (movedSummary)
    html = `<div class="wk-moved">↩ ${esc(movedSummary)}</div>` + html;
  $("#weekgrid").innerHTML = html;
  _wkMode = "grid";
}


const isMobileWeek = () => window.matchMedia("(max-width: 720px)").matches;

function renderWeekMobile(data) {
  // 点击分发依赖 wkCtx.dayItems/dueTodos 定位条目（与桌面路径一致）
  S.wkCtx.dayItems = data.days.map(d => d.items);
  S.wkCtx.dueTodos = data.days.flatMap(d => (d.due_todos || []));
  const todayStr = fmt(new Date());
  let html = `<div class="wkm">`;
  data.days.forEach((d, di) => {
    const dt = new Date(d.date + "T00:00:00");
    const hol = d.holiday && d.holiday.length;
    const isToday = d.date === todayStr;
    const dts = d.due_todos || [];
    // 已调走的课：原节次被新课占据 → 隐藏；节次空着 → 灰色显示（提示时段已空）
    const activeItems = d.items.filter(it => it.start && !it.cancelled);
    const slotTaken = sec => activeItems.some(x => x.section === sec);
    const visibleMoved = d.items.filter(it => it.cancelled && it.start && !slotTaken(it.section));
    const blocks = [...activeItems, ...visibleMoved]
        .sort((a, b) => a.start.localeCompare(b.start));
    const allday = d.items.filter(it => !it.start && (it.type === "calendar" || it.type === "event"));
    html += `<div class="wkm-day ${isToday ? "today" : ""} ${hol ? "is-hol" : ""} ${d.date < todayStr ? "past-day" : ""}" data-date="${d.date}">
      <div class="wkm-head"><b>${WD[di]}</b><span>${dt.getMonth() + 1}/${dt.getDate()}</span>
        ${isToday ? '<i class="wkm-today">今天</i>' : ""}${hol ? `<i class="wk-hol">${esc(d.holiday[0])}</i>` : ""}</div>
      <div class="wkm-items">`;
    for (const e of allday) {
      const mk = e.type === "event" && ((e.note || "").startsWith("换课：") || e.makeup_like);
      html += `<div class="wkm-it ${e.type === "calendar" && !mk ? "cal-" + e.kind : (mk || e.type === "lesson" ? "lesson" : "user")}">${esc(e.title)}${mk ? "（补）" : ""}</div>`;
    }
    const nowMin = new Date().getHours() * 60 + new Date().getMinutes();
    for (const it of blocks) {
      const idx = d.items.indexOf(it);          // 点击分发按 dayItems[di][idx] 定位
      const eM = toMin(it.end || it.start);
      const pastCls = (d.date < todayStr || (d.date === todayStr && eM < nowMin)) ? "past" : "";
      const mk = it.type === "event" && ((it.note || "").startsWith("换课：") || it.makeup_like);
      const cls = it.type === "lesson" || mk ? "lesson" : (it.type === "calendar" ? "cal-" + it.kind : "user");
      html += `<div class="wkm-it ${cls} ${pastCls} ${it.cancelled ? "cancelled" : ""}"
        data-di="${di}" data-idx="${idx}" data-date="${d.date}">
        <b>${it.start}${it.end ? "–" + it.end : ""}</b><span>${esc(it.title)}${mk ? "（补）" : ""}${it.cancelled ? "（已调课）" : ""}</span></div>`;
    }
    html += dts.map(t => `<div class="wkm-it wk-todo" data-tid="${t.id}" data-di="${di}">
        ⏰${t.due.slice(11, 16)} ${esc(t.title)}</div>`).join("");
    html += `<div class="wkm-it wk-addtodo" data-date="${d.date}">＋ 待办</div></div></div>`;
  });
  $("#weekgrid").innerHTML = html + `</div>`;
  _wkMode = "m";
}

// 周视图点击分发（委托一次绑定）：自己的日程块→编辑，课程块→调课，校历块→只读，空白→新建
function weekGridClick(ev) {
  const add = ev.target.closest(".wk-addtodo");
  if (add) {
    openForm("todo", null, { due: `${add.dataset.date} 17:00` });
    return;
  }
  const mIt = ev.target.closest(".wkm-it");
  if (mIt && (mIt.dataset.di !== undefined || mIt.dataset.tid)) {
    if (mIt.classList.contains("wk-addtodo")) {          // ＋待办（通用 handler 已在上方处理）
    } else if (mIt.dataset.tid) {
      const t = (S.wkCtx.dueTodos || []).find(x => x.id === +mIt.dataset.tid);
      if (t) openForm("todo", t);
      return;
    } else if (mIt.dataset.di !== undefined) {
      const it = (S.wkCtx.dayItems[+mIt.dataset.di] || [])[+mIt.dataset.idx];
      if (!it) return;
      if (it.type === "event") openForm("event", it);
      else if (it.type === "lesson") openLessonForm(it);
      return;
    }
  }
  const blk = ev.target.closest(".wk-blk");
  const col = ev.target.closest(".wk-col");
  if (blk) {
    if (blk.dataset.tid) {                    // 截止待办块 → 编辑/删除
      const tid = +blk.dataset.tid;
      let t = null;
      for (const arr of [...(S.wkCtx.gridTodos || []), ...(S.wkCtx.dueTodos || [])]) {
        t = (arr || []).find(x => x.id === tid);
        if (t) break;
      }
      if (t) openForm("todo", t);
      return;
    }
    const it = (S.wkCtx.dayItems[+blk.dataset.di] || [])[+blk.dataset.idx];
    if (!it) return;
    if (it.type === "event") openForm("event", it);
    else if (it.type === "lesson") openLessonForm(it);
    return;                                   // 校历块只读
  }
  if (col) {
    const rect = col.getBoundingClientRect();
    let m = S.wkCtx.minM + Math.round((ev.clientY - rect.top) / S.wkCtx.PX_H * 60 / 15) * 15;
    m = Math.max(S.wkCtx.minM, Math.min(m, S.wkCtx.maxM - 15));
    const sec = (S.wkCtx.sections || []).find(s => s.start && m >= toMin(s.start) && m < toMin(s.end));
    const start = sec ? sec.start : `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
    const end = sec ? sec.end : `${String(Math.floor((m + 45) / 60)).padStart(2, "0")}:${String((m + 45) % 60).padStart(2, "0")}`;
    const date = col.dataset.date;
    openForm("event", null, { date, start, end });
  }
}

on("#weekgrid", "click", weekGridClick);
let _wkRsTimer = null, _wkMode = null;
window.addEventListener("resize", () => {
  clearTimeout(_wkRsTimer);
  _wkRsTimer = setTimeout(() => {
    if (document.querySelector("#view-week").hidden) return;
    const m = isMobileWeek();
    if (_wkMode !== null && m !== (_wkMode === "m")) loadWeek();
  }, 150);
});

export { loadWeek };
