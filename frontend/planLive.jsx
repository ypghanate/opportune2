import React from "react";

const hm = (iso) => new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "Europe/London" });
const dayName = (iso) => new Date(iso + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });

// Shows the planner working: one card per company, and under it each day it chose, with the events it is keeping on that day
// (your own, muted) and the block it just placed there (highlighted).
export default function PlanLive({ steps, total }) {
  const companies = [];
  for (const s of steps) {
    let c = companies.find((x) => x.name === s.company);
    if (!c) companies.push((c = { name: s.company, days: [] }));
    c.days.push(s);
  }
  return (
    <section className="plan-live" aria-live="polite">
      <p className="plan-live-h"><span className="spinner" aria-hidden="true" /> {steps.length === 0 ? "Reading your calendar…" : `Planning your applications (${steps.length} of ${total || "…"} blocks placed)`}</p>
      {companies.map((c, ci) => (
        <div key={c.name} className="tile plan-co">
          <h3>{c.name}{ci === companies.length - 1 && steps.length < total ? " …" : ""}</h3>
          {c.days.map((s, i) => (
            <div key={i} className="plan-day">
              <div className="plan-date">{dayName(s.day)}<small>{s.kind}</small></div>
              <ul className="plan-events">
                {s.day_events.map((e, k) => (
                  <li key={k} className={e.new ? (e.title === s.title && e.start === s.start ? "ev ev-new" : "ev ev-mine") : "ev ev-kept"}>
                    <span>{hm(e.start)}–{hm(e.end)}</span> {e.title}{!e.new && <em> kept</em>}
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </div>
      ))}
    </section>
  );
}