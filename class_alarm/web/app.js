"use strict";

// The key comes from the link printed on the laptop (?key=...). It is remembered so the
// Home Screen icon keeps working; iOS saves the full link when you add it.
const KEY_STORE = "class-alarm-key";
const REFRESH_MS = 15000;

function readKey() {
  const fromUrl = new URLSearchParams(location.search).get("key");
  if (fromUrl) {
    try { localStorage.setItem(KEY_STORE, fromUrl); } catch (e) { /* private mode */ }
    return fromUrl;
  }
  try { return localStorage.getItem(KEY_STORE); } catch (e) { return null; }
}

const key = readKey();
const $ = (id) => document.getElementById(id);

let ttRows = [];        // timetable being edited
let ttDirty = false;    // unsaved edits: don't overwrite them on refresh

async function api(path, body) {
  const opts = { headers: { "X-Key": key || "" }, cache: "no-store" };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch (e) { /* empty body */ }
  if (!res.ok) {
    const err = new Error(data.error || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) if (c) node.append(c);
  return node;
}

function show(id, visible) { $(id).hidden = !visible; }

function setConn(text, cls) {
  const c = $("conn");
  c.textContent = text;
  c.className = "conn" + (cls ? " " + cls : "");
}

// --- rendering ---------------------------------------------------------------------

function classLine(a) {
  return `${a.class} at ${a.class_time}` + (a.location ? `, ${a.location}` : "");
}

function renderNext(s) {
  const n = s.next;
  show("next", !!n);
  show("none", !n && !s.error);
  if (!n) return;
  $("next-wake").textContent = n.wake;
  $("next-when").textContent = `${n.day}, in ${n.in}`;
  const cls = $("next-class");
  cls.textContent = classLine(n);
  if (n.shampoo) cls.append(el("span", { class: "tag", text: "Shampoo day" }));
  const btn = $("next-shampoo");
  btn.setAttribute("aria-pressed", String(n.shampoo));
  btn.textContent = "Shampoo day";
  btn.title = `Wake ${s.shampoo_lead} min before class instead of ${s.lead}`;
  btn.onclick = () => setShampoo(n.date, !n.shampoo, btn);
}

function renderWeek(s) {
  show("week-card", s.alarms.length > 0);
  $("week-rule").textContent = `Alarm ${s.lead} min before your first class, ${s.shampoo_lead} min on shampoo days.`;
  const list = $("week");
  list.replaceChildren();
  for (const a of s.alarms) {
    const main = el("div", { class: "row-main" },
      el("div", { class: "row-time", text: `${a.day}  ${a.wake}` }),
      el("div", { class: "row-sub", text: classLine(a) }));
    const btn = el("button", {
      class: "btn btn-small toggle", type: "button", "aria-pressed": String(a.shampoo),
      "aria-label": `Shampoo day on ${a.day}`, text: "Shampoo",
    });
    btn.onclick = () => setShampoo(a.date, !a.shampoo, btn);
    list.append(el("li", { class: a.wake_passed ? "row-past" : "" }, main, btn));
  }
}

function renderSleep(s) {
  const sl = s.sleep;
  show("sleep-card", true);
  $("sleep-bed").textContent = sl.bedtime
    ? `Go to bed by ${sl.bedtime} for ${sl.hours}h of sleep before your ${sl.for_alarm} alarm.`
    : "No alarm coming up, so no bedtime to suggest.";
  let usual;
  if (sl.pattern) {
    usual = `You usually put your phone down around ${sl.pattern.usual}, then pick it up about ${sl.pattern.hours}h later (${sl.pattern.nights} nights).`;
  } else if (sl.tracking) {
    usual = "Not enough phone data yet. It needs at least 3 nights.";
  } else {
    usual = "Phone behavior tracking is off, so there's no sleep pattern yet.";
  }
  $("sleep-usual").textContent = usual;
  const table = $("sleep-nights");
  table.hidden = sl.nights.length === 0;
  const body = table.querySelector("tbody");
  body.replaceChildren(...sl.nights.map((n) =>
    el("tr", {}, el("td", { text: n.night }), el("td", { text: n.quiet }), el("td", { text: n.first }), el("td", { text: n.duration }))));
}

function field(row, name, label, cls, placeholder) {
  const input = el("input", { type: "text", value: row[name] || "", placeholder, autocomplete: "off", autocapitalize: "off", spellcheck: "false" });
  input.oninput = () => { row[name] = input.value; markDirty(); };
  return el("label", { class: cls || "" }, label, input);
}

function renderTimetableRows() {
  const box = $("tt-rows");
  box.replaceChildren();
  ttRows.forEach((row, i) => {
    const remove = el("button", { class: "btn btn-small remove", type: "button", text: "Remove" });
    remove.onclick = () => { ttRows.splice(i, 1); markDirty(); renderTimetableRows(); };
    box.append(el("div", { class: "tt-row" },
      field(row, "course", "Class", "wide", "MATH 101"),
      field(row, "day", "Days", "wide", "Mon/Wed/Fri"),
      field(row, "start", "Starts", "", "07:30"),
      field(row, "end", "Ends", "", "08:45"),
      field(row, "location", "Room", "wide", "Room 204"),
      remove));
  });
}

function renderTimetable(s) {
  const t = s.timetable;
  show("tt-card", true);
  if (!t.editable) {
    $("tt-help").textContent = `Your timetable is ${t.file}. Calendar files are edited in the calendar they came from.`;
    show("tt-add", false); show("tt-save", false); $("tt-rows").replaceChildren();
    return;
  }
  if (!ttDirty) {
    ttRows = t.rows.map((r) => ({ ...r }));
    renderTimetableRows();
  }
  const n = ttDirty ? ttRows.length : t.rows.length;
  $("tt-count").textContent = `${n} ${n === 1 ? "class" : "classes"}, tap to edit`;
}

function render(s) {
  show("locked", false);
  const err = $("error");
  err.hidden = !s.error;
  err.textContent = s.error ? `Timetable problem: ${s.error}` : "";
  show("ringing", s.status.ringing);
  $("ringing-msg").textContent = s.status.message || "";
  show("awake", s.status.awake_check);
  if (!s.status.awake_check) {
    const btn = $("awake-btn");
    btn.disabled = false;
    btn.textContent = "I'm awake";
  }
  renderNext(s);
  renderWeek(s);
  renderSleep(s);
  renderTimetable(s);
}

// --- actions -----------------------------------------------------------------------

async function refresh() {
  if (!key) {
    show("locked", true);
    setConn("Not connected", "bad");
    return;
  }
  try {
    render(await api("/api/state"));
    const t = new Date();
    setConn(`Connected to your laptop, updated ${t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`, "ok");
  } catch (e) {
    if (e.status === 401) {
      show("locked", true);
      setConn("This link's key is wrong or out of date", "bad");
    } else {
      setConn("Can't reach your laptop. Is it on, awake and on the same Wi-Fi?", "bad");
    }
  }
}

async function setShampoo(date, on, btn) {
  btn.disabled = true;
  try {
    await api("/api/shampoo", { date, on });
    await refresh();
  } catch (e) {
    setConn(`Couldn't change shampoo day: ${e.message}`, "bad");
  } finally {
    btn.disabled = false;
  }
}

function markDirty() {
  ttDirty = true;
  $("tt-save").disabled = false;
  const msg = $("tt-msg");
  msg.textContent = "Unsaved changes";
  msg.className = "form-msg";
}

async function saveTimetable() {
  const btn = $("tt-save");
  const msg = $("tt-msg");
  btn.disabled = true;
  try {
    await api("/api/timetable", { rows: ttRows });
    ttDirty = false;
    msg.textContent = "Saved. Alarms updated.";
    msg.className = "form-msg good";
    await refresh();
  } catch (e) {
    msg.textContent = e.message;
    msg.className = "form-msg bad";
    btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("tt-add").onclick = () => {
    ttRows.push({ day: "", start: "", end: "", course: "", location: "" });
    markDirty();
    renderTimetableRows();
    const inputs = $("tt-rows").querySelectorAll(".tt-row:last-child input");
    if (inputs.length) inputs[0].focus();
  };
  $("tt-save").onclick = saveTimetable;
  $("awake-btn").onclick = async () => {
    const btn = $("awake-btn");
    btn.disabled = true;
    try {
      await api("/api/awake", {});
      btn.textContent = "Thanks. Confirmed.";
    } catch (e) {
      btn.disabled = false;
      setConn(`Couldn't confirm: ${e.message}`, "bad");
    }
  };
  refresh();
  setInterval(refresh, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
});
