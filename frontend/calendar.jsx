import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MAX_CHIPS = 3;

const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const keyOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const fmtTime = (d) => d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });

// All-day events come as "2026-11-25" (no time): read them as a local day, not as UTC midnight.
const parse = (s, allDay) => (allDay ? new Date(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) : new Date(s));

// The weeks shown for a month, Monday first, including the days of the neighbouring months that fill them.
function gridFor(month) {
  const first = new Date(month.getFullYear(), month.getMonth(), 1);
  const offset = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(month.getFullYear(), month.getMonth() + 1, 0).getDate();
  const total = Math.ceil((offset + daysInMonth) / 7) * 7;
  return Array.from({ length: total }, (_, i) => addDays(first, i - offset));
}

// Puts each event on every day it covers (an all-day event's end date is exclusive).
function byDay(events) {
  const map = {};
  for (const e of events) {
    const from = parse(e.start, e.all_day);
    const to = parse(e.end || e.start, e.all_day);
    const last = startOfDay(new Date(Math.max(to.getTime() - 1, from.getTime())));
    let n = 0;
    for (let d = startOfDay(from); d <= last && n < 62; d = addDays(d, 1), n++) {
      (map[keyOf(d)] ||= []).push({ ...e, from, to });
    }
  }
  for (const k in map) map[k].sort((a, b) => Number(b.all_day) - Number(a.all_day) || a.from - b.from);
  return map;
}

const KIND = {
  apply: "Apply", practice: "Practice", oa: "OA", test: "Test", interview_prep: "Interview prep",
  interview: "Interview", ac_prep: "AC prep", assessment_centre: "Assessment centre",
};
const chipStyle = (e) => (e.colour ? { background: e.colour, color: "#0b1a12" } : undefined);

const when = (e) => (e.all_day ? "All day" : `${fmtTime(e.from)} to ${fmtTime(e.to)}`);

export default function Calendar() {
  const today = useMemo(() => startOfDay(new Date()), []);
  const [month, setMonth] = useState(new Date(today.getFullYear(), today.getMonth(), 1));
  const [selected, setSelected] = useState(today);
  const [events, setEvents] = useState([]);
  const [state, setState] = useState({ loading: true, error: null });
  const days = useMemo(() => gridFor(month), [month]);

  const load = useCallback(() => {
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    api.calendar(days[0].toISOString(), addDays(days[days.length - 1], 1).toISOString())
      .then((r) => { if (live) { setEvents(r.events || []); setState({ loading: false, error: null }); } })
      .catch((e) => { if (live) setState({ loading: false, error: e.message }); });
    return () => { live = false; };
  }, [days]);

  useEffect(load, [load]);
  useEffect(() => {
    const again = () => load();
    window.addEventListener("opps-changed", again); // a booking just added blocks
    return () => window.removeEventListener("opps-changed", again);
  }, [load]);

  const perDay = useMemo(() => byDay(events), [events]);
  const companies = useMemo(() => {
    const seen = new Map();
    for (const e of events) if (e.opportune && e.company && e.colour && !seen.has(e.company)) seen.set(e.company, e.colour);
    return [...seen.entries()];
  }, [events]);
  const dayEvents = perDay[keyOf(selected)] || [];
  const step = (n) => setMonth(new Date(month.getFullYear(), month.getMonth() + n, 1));
  const goToday = () => { setMonth(new Date(today.getFullYear(), today.getMonth(), 1)); setSelected(today); };
  const needsGoogle = state.error && /connect your google|reconnect google/i.test(state.error);

  return (
    <main className="wrap inner">
      <h1>Calendar</h1>
      <p className="lede">Your Google Calendar. Each company has its own colour, and its blocks (apply, practice, online assessment, interview) share it.</p>

      <div className="cal-top">
        <h2 className="cal-title" aria-live="polite">{month.toLocaleDateString("en-GB", { month: "long", year: "numeric" })}</h2>
        <div className="cal-nav">
          <button className="pill pill-sm" onClick={() => step(-1)} aria-label="Previous month">‹ Prev</button>
          <button className="pill pill-sm" onClick={goToday}>Today</button>
          <button className="pill pill-sm" onClick={() => step(1)} aria-label="Next month">Next ›</button>
          <button className="pill pill-sm" onClick={() => load()} disabled={state.loading}>{state.loading ? "Loading…" : "Refresh"}</button>
        </div>
      </div>

      {state.error && (
        <p className="notice notice-err" role="alert">
          {state.error} {needsGoogle && <a className="link" href="#/profile">Go to Profile</a>}
        </p>
      )}

      <div className="cal-layout">
        <div>
          <div className={`cal-grid ${state.loading ? "cal-loading" : ""}`} role="grid" aria-label={month.toLocaleDateString("en-GB", { month: "long", year: "numeric" })}>
            {DOW.map((d) => <div className="cal-dow" key={d} role="columnheader">{d}</div>)}
            {days.map((d) => {
              const list = perDay[keyOf(d)] || [];
              const cls = ["cal-cell"];
              if (d.getMonth() !== month.getMonth()) cls.push("cal-out");
              if (keyOf(d) === keyOf(today)) cls.push("cal-today");
              if (keyOf(d) === keyOf(selected)) cls.push("cal-sel");
              return (
                <button
                  key={keyOf(d)}
                  className={cls.join(" ")}
                  onClick={() => setSelected(d)}
                  aria-pressed={keyOf(d) === keyOf(selected)}
                  aria-label={`${d.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}, ${list.length} ${list.length === 1 ? "event" : "events"}`}
                >
                  <span className="cal-num">{d.getDate()}</span>
                  {list.slice(0, MAX_CHIPS).map((e) => (
                    <span key={e.id + keyOf(d)} className={`cal-chip ${e.opportune ? "cal-chip-opp" : ""}`} style={chipStyle(e)}>
                      {e.all_day ? "" : `${fmtTime(e.from)} `}{e.title}
                    </span>
                  ))}
                  {list.length > MAX_CHIPS && <span className="cal-more">+{list.length - MAX_CHIPS} more</span>}
                  {list.length > 0 && <span className="cal-count">{list.length}</span>}
                </button>
              );
            })}
          </div>
          <p className="cal-legend">
            {companies.length === 0 && <><span className="cal-chip cal-chip-opp">Apply / Practice</span> booked by Opportune</>}
            {companies.map(([name, colour]) => <span key={name} className="cal-chip" style={{ background: colour, color: "#0b1a12" }}>{name}</span>)}
          </p>
        </div>

        <aside className="tile cal-day" aria-label="Selected day">
          <h2>{selected.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}</h2>
          {dayEvents.length === 0 ? (
            <p className="muted">{state.loading ? "Loading…" : "Nothing on this day."}</p>
          ) : (
            <ul className="cal-list">
              {dayEvents.map((e) => (
                <li key={e.id + e.start} className={`cal-ev ${e.opportune ? "cal-ev-opp" : ""}`} style={e.colour ? { borderLeftColor: e.colour } : undefined}>
                  <strong>{e.title}</strong>
                  <small>{when(e)}</small>
                  {e.location && <small>{e.location}</small>}
                  {e.opportune && <span className="tag tag-ok">{KIND[e.kind] ? `${KIND[e.kind]} · ` : ""}{e.company || "booked by Opportune"}</span>}
                  {e.practice_url && <a className="cal-practice" href={e.practice_url} target="_blank" rel="noopener noreferrer">Open {e.practice_label || "the practice test"} ↗</a>}
                  {e.link && <a href={e.link} target="_blank" rel="noreferrer">Open in Google Calendar ↗</a>}
                </li>
              ))}
            </ul>
          )}
        </aside>
      </div>
    </main>
  );
}
