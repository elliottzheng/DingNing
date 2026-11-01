import { $, $$, fmt } from "./api.js?v=202611012";
import { S } from "./state.js?v=202611012";
import { loadDay } from "./today.js?v=202611012";
import { loadWeek } from "./week.js?v=202611012";

function shiftDay(n) { S.todayView.setDate(S.todayView.getDate() + n); loadDay(); }
function shiftWeek(n) { S.weekAnchor.setDate(S.weekAnchor.getDate() + n * 7); loadWeek(); }
function refreshView() { $("#view-today").hidden ? loadWeek() : loadDay(); }
function switchView(v) {
  $$("nav .tab").forEach(x => x.classList.toggle("active", x.dataset.view === v));
  $("#view-today").hidden = v !== "today";
  $("#view-week").hidden = v !== "week";
  if (v === "today") loadDay();
  else if (v === "week") loadWeek();
}

export { switchView, refreshView, shiftDay, shiftWeek };
