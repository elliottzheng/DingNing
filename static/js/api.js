/* 共享工具与后端调用 */
const $ = (s, el = document) => el.querySelector(s);

const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

function fmt(d) { return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; }

function md(d) { return `${d.getMonth() + 1}月${d.getDate()}日`; }

function esc(s) { return (s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"); }

function nowStr() {

  const n = new Date();

  return `${fmt(n)} ${String(n.getHours()).padStart(2, "0")}:${String(n.getMinutes()).padStart(2, "0")}`;

}

function remaining(due) {

  const diff = new Date(due.replace(" ", "T")) - new Date();

  const overdue = diff < 0;

  const s = Math.abs(diff) / 1000;

  let txt;

  if (s >= 86400) txt = `${Math.floor(s / 86400)} 天`;

  else if (s >= 3600) txt = `${Math.floor(s / 3600)} 小时`;

  else txt = `${Math.max(Math.floor(s / 60), 1)} 分钟`;

  return (overdue ? "已逾期 " : "还剩 ") + txt;

}

function dueLabel(due) {

  const [d, t] = due.split(" ");

  const base = t && t !== "00:00" ? ` ${t}` : "";

  const today = fmt(new Date());

  const tomorrow = fmt(new Date(Date.now() + 864e5));

  if (d === today) return `今天${base}`;

  if (d === tomorrow) return `明天${base}`;

  return `${d.slice(5).replace("-", "/")} ${t || ""}`.trim();

}

function toMin(t) { if (!t) return null; const [h, m] = t.split(":").map(Number); return h * 60 + m; }

function fmtMin(m) { return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`; }

async function api(path, opts = {}) {

  const res = await fetch(path, {

    headers: { "Content-Type": "application/json" }, ...opts,

    body: opts.body ? JSON.stringify(opts.body) : undefined,

  });

  if (!res.ok) {

    if (res.status === 401) { location.href = "/login"; throw new Error("请重新登录"); }

    const j = await res.json().catch(() => ({}));

    const msg = typeof j.detail === "string" ? j.detail

      : (Array.isArray(j.detail) ? j.detail.map(d => d.msg).join("；") : res.statusText);

    throw new Error(msg);

  }

  return res.json();

}

function on(sel, ev, fn) { const el = document.querySelector(sel); if (el) el.addEventListener(ev, fn); }


export { $, $$, WD, fmt, md, esc, nowStr, remaining, dueLabel, toMin, fmtMin, api, on };
