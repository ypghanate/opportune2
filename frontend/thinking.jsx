import React, { useEffect, useState } from "react";

const STEPS = ["Plan", "Search", "Read", "Rank", "Check"];

const PILL = {
  considering: ["Considering", "pill-wait"],
  ranking: ["Ranking", "pill-wait"],
  checking: ["Checking", "pill-wait"],
  open: ["Open now", "pill-open"],
  watching: ["Watching out", "pill-watch"],
};

function Elapsed({ since }) {
  const [s, setS] = useState(0);
  useEffect(() => {
    const t = setInterval(() => setS(Math.floor((Date.now() - since) / 1000)), 500);
    return () => clearInterval(t);
  }, [since]);
  return <span className="ag-time">{s}s</span>;
}

// The agent's work shown live: where it is in the five stages, what it is doing, what it is searching for, and what it has found.
export default function Thinking({ live, since }) {
  const phase = Math.min(live.phase ?? 0, STEPS.length - 1);
  const notes = live.notes || [];
  const has = notes.length > 0 || (live.queries || []).length > 0;
  return (
    <section className="ag" aria-live="polite">
      <header className="ag-head">
        <span className="ag-orb" aria-hidden="true" />
        <div>
          <h2>The agent is working</h2>
          <p>{live.stage || "Getting started"}</p>
        </div>
        <Elapsed since={since} />
      </header>

      <ol className="ag-steps" aria-label="Progress">
        {STEPS.map((name, i) => (
          <li key={name} className={i < phase ? "done" : i === phase ? "now" : ""}>
            <span className="ag-dot">{i < phase ? "✓" : i + 1}</span>
            <span className="ag-step-name">{name}</span>
          </li>
        ))}
      </ol>

      {has && (
        <div className="ag-cols">
          <div className="ag-feed">
            <h3>Thinking</h3>
            <ul>
              {notes.map((n, i) => (
                <li key={i} className={i === notes.length - 1 ? "now" : "done"}>
                  <span className="ag-bullet">{i === notes.length - 1 ? <i className="ag-spin" /> : "✓"}</span>
                  <span>{n}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="ag-looking">
            {(live.queries || []).length > 0 && (
              <>
                <h3>Searching for</h3>
                <div className="ag-chips">
                  {live.queries.map((q, i) => <span key={q} className="ag-chip" style={{ animationDelay: `${Math.min(i, 10) * 60}ms` }}>{q}</span>)}
                </div>
              </>
            )}
            {(live.employers || []).length > 0 && (
              <>
                <h3>Looking up directly</h3>
                <div className="ag-chips">
                  {live.employers.map((q, i) => <span key={q} className="ag-chip ag-chip-co" style={{ animationDelay: `${Math.min(i, 12) * 50}ms` }}>{q}</span>)}
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {(live.items || []).length > 0 && (
        <div className="ag-found">
          <h3>Being considered <span className="ag-count">{live.items.length}</span></h3>
          <ul>
            {live.items.map((o, i) => {
              const [label, cls] = PILL[o.state] || PILL.checking;
              return (
                <li key={`${o.company}-${i}`} className="ag-card">
                  <span className="ag-avatar" aria-hidden="true">{(o.company || "?").trim().charAt(0).toUpperCase()}</span>
                  <div className="ag-card-main">
                    <strong>{o.company}</strong>
                    <small>{o.state === "considering" ? "Looking up their programme" : [o.title, o.location].filter(Boolean).join(" · ")}</small>
                  </div>
                  <span className={`ag-pill ${cls}`}>{label}</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}