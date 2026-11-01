import { $, $$, on, api, esc } from "./api.js?v=202611012";
import { S } from "./state.js?v=202611012";
import { switchView, shiftDay, shiftWeek, refreshView } from "./router.js?v=202611012";
import { loadDay, quickAdd } from "./today.js?v=202611012";
import { openForm } from "./forms.js?v=202611012";
import { openSettings } from "./settings.js?v=202611012";

$$("nav .tab").forEach(t => t.onclick = () => switchView(t.dataset.view));
on("#d-prev", "click", () => shiftDay(-1));
on("#d-next", "click", () => shiftDay(1));
on("#d-today", "click", () => { S.todayView = new Date(); loadDay(); });
on("#w-prev", "click", () => shiftWeek(-1));
on("#w-next", "click", () => shiftWeek(1));
on("#w-today", "click", () => { S.weekAnchor = new Date(); loadWeek(); });
on("#quick-btn", "click", quickAdd);
on("#quick-input", "keydown", e => { if (e.key === "Enter") quickAdd(); });
on("#btn-add", "click", () => openForm("event"));
on("#add-event-inline", "click", () => openForm("event"));
on("#add-todo-inline", "click", () => openForm("todo"));
on("#add-close", "click", () => $("#add-modal").hidden = true);
on("#add-modal", "click", e => { if (e.target.id === "add-modal") $("#add-modal").hidden = true; });
$$(".type-tab").forEach(b => b.onclick = () => setAddType(b.dataset.type));
on("#btn-settings", "click", () => openSettings());   // 不传事件对象，避免误触发定位跳转
let ME = null;
api("/api/me").then(me => {
  ME = me;
  $("#btn-user").textContent = me.username + (me.is_admin ? " ·管理员" : "");
  $("#user-menu .um-head").textContent = me.username + (me.is_admin ? "（管理员）" : "");
}).catch(() => {});
function toggleUserMenu(force) {
  $("#user-menu").hidden = force !== undefined ? !force : !$("#user-menu").hidden;
}
on("#btn-user", "click", () => toggleUserMenu());
document.addEventListener("click", e => {
  if (!e.target.closest("#user-menu") && !e.target.closest("#btn-user")) toggleUserMenu(false);
});
on("#um-pass", "click", () => { toggleUserMenu(false); openSettings(true, "sec-safe"); });
on("#um-logout", "click", async () => {
  toggleUserMenu(false);
  if (!confirm("退出登录？")) return;
  try { await api("/api/logout", { method: "POST" }); } catch (e) {}
  location.href = "/login";
});
on("#ob-open-settings", "click", openSettings);
on("#ob-dismiss", "click", () => {
  localStorage.setItem("ob_hide", "1");
  $("#onboard").hidden = true;
});
on("#btn-help", "click", () => $("#help-modal").hidden = false);
on("#ob-help", "click", () => $("#help-modal").hidden = false);
on("#help-close", "click", () => $("#help-modal").hidden = true);
on("#help-modal", "click", e => { if (e.target.id === "help-modal") $("#help-modal").hidden = true; });
document.addEventListener("keydown", e => {
  if (e.key === "?" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
    $("#help-modal").hidden = false;
  }
});

// ---------------- 日程搜索（按名称，未来优先可点跳当日） ----------------
let qTimer = null;
async function renderSearch() {
  const kw = $("#q-input").value.trim();
  const box = $("#q-results");
  if (kw.length < 2) { box.hidden = true; box.innerHTML = ""; return; }
  const r = await api(`/api/events/search?kw=${encodeURIComponent(kw)}`);
  const rows = [...r.results.map(x => ({ ...x, past: false })),
                ...r.past.map(x => ({ ...x, past: true }))];
  if (!rows.length) {
    box.innerHTML = `<div class="q-row">没找到“${esc(kw)}”相关日程</div>`;
  } else {
    const firstPast = rows.findIndex(x => x.past);
    box.innerHTML = rows.map((x, i) => {
      const d = new Date(x.date + "T00:00:00");
      const wd = "周" + "一二三四五六日"[d.getDay() === 0 ? 6 : d.getDay() - 1];
      const tm = x.start_time ? ` ${x.start_time}` : "";
      const div = (i === firstPast && firstPast > 0)
        ? '<div class="q-div">— 已过 —</div>' : "";
      return div +
        `<div class="q-row" data-date="${x.date}">` +
        `<b>${d.getMonth() + 1}/${d.getDate()}</b>（${wd}${tm}）${esc(x.title)}` +
        `</div>`;
    }).join("");
    box.querySelectorAll(".q-row").forEach(el => el.onclick = () => {
      S.todayView = new Date(el.dataset.date + "T00:00:00");
      switchView("today");
      refreshView();
      box.hidden = true;
      $("#q-input").value = "";
    });
  }
  box.hidden = false;
}
on("#btn-search", "click", () => {
  const inp = $("#q-input");
  const opening = inp.hidden;          // 初始隐藏用的是 hidden 属性，不是 style.display
  inp.hidden = !opening;
  $("#q-results").hidden = true;
  if (opening) {
    if (window.innerWidth <= 720) {          // 移动端面板是 fixed，JS 对齐输入框下方
      const r = inp.getBoundingClientRect();
      $("#q-results").style.top = (r.bottom + 8) + "px";
    }
    inp.focus(); renderSearch();
  }
});
on("#q-input", "input", () => {
  clearTimeout(qTimer);
  qTimer = setTimeout(renderSearch, 300);
});
on("#q-input", "keydown", e => {
  if (e.key === "Enter") { clearTimeout(qTimer); renderSearch(); }
  if (e.key === "Escape") { $("#q-results").hidden = true; $("#q-input").blur(); }
});
on("#modal-close", "click", () => $("#modal").hidden = true);
document.addEventListener("keydown", e => {
  if (e.key === "Escape") { $("#add-modal").hidden = true; $("#modal").hidden = true; $("#lesson-modal").hidden = true; }
});
loadDay();

