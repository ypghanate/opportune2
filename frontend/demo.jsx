import React, { useEffect, useState } from "react";

// A self-playing walkthrough for the Overview header: type a request, watch the agent work,
// approve, see the calendar blocks. It loops. With "reduce motion" on, it shows the end state.
const QUERY = "Find spring weeks and internships in quant finance";
const STEPS = ["Read your CV", "Searched the web: 10 sources", "Checked 7 links and your eligibility", "Matched 2 opportunities"];
const ROWS = [
  ["Spring Week, Markets", "Example firm · London · Deadline 30 Nov"],
  ["Summer Internship, Quantitative Research", "Example firm · London · Deadline 12 Dec"],
];
const BOOKED = [
  ["Apply: Spring Week, Markets", "Tue 24 Nov · 18:00 to 19:30"],
  ["Apply: Summer Internship, Quant Research", "Thu 3 Dec · 19:00 to 20:00"],
];

// stage: 1-4 = steps shown, 5 = results, 6 = ticked, 7 = booked
const LAST = 7;
const STAGE_MS = 900;

const prefersReducedMotion = () =>
  typeof window !== "undefined" && window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export default function RunDemo() {
  const [typed, setTyped] = useState(0);
  const [stage, setStage] = useState(0);
  const [run, setRun] = useState(0);
  const reduced = prefersReducedMotion();

  useEffect(() => {
    if (reduced) {
      setTyped(QUERY.length);
      setStage(LAST);
      return undefined;
    }
    let live = true;
    const timers = [];
    const at = (ms, fn) => timers.push(setTimeout(() => live && fn(), ms));

    setTyped(0);
    setStage(0);
    for (let i = 1; i <= QUERY.length; i++) at(500 + i * 30, () => setTyped(i));
    const start = 500 + QUERY.length * 30 + 500;
    for (let s = 1; s <= LAST; s++) at(start + (s - 1) * STAGE_MS, () => setStage(s));
    at(start + LAST * STAGE_MS + 4500, () => setRun((r) => r + 1)); // hold the end state, then loop

    return () => {
      live = false;
      timers.forEach(clearTimeout);
    };
  }, [run, reduced]);

  return (
    <div className="rd" aria-label="Demo of the agent finding and booking opportunities">
      <div className="rd-head">
        <span className="rd-live"><span className="rd-dot" aria-hidden="true" />Live demo</span>
        <button type="button" className="rd-replay" onClick={() => setRun((r) => r + 1)}>Replay</button>
      </div>

      <div className="rd-body" role="img" aria-label="Example: you ask for quant spring weeks, the agent reads your CV, searches and checks listings, you approve two, and they appear in your calendar.">
        <div className="mock-query">
          {QUERY.slice(0, typed)}
          {typed < QUERY.length && <span className="rd-caret" aria-hidden="true" />}
        </div>

        <ul className="mock-steps rd-steps">
          {STEPS.slice(0, Math.min(stage, 4)).map((s) => (
            <li key={s} className="rd-in"><span className="tick">✓</span>{s}</li>
          ))}
        </ul>

        {stage >= 5 && ROWS.map(([t, m]) => (
          <div className="tile mock-row rd-in" key={t}>
            <span className={`box ${stage >= 6 ? "rd-box-on" : ""}`}>{stage >= 6 ? "✓" : ""}</span>
            <div>
              <strong>{t}</strong>
              <p>{m}</p>
            </div>
            <span className={`tag ${stage >= 7 ? "tag-ok" : ""}`}>{stage >= 7 ? "approved" : "Verified link"}</span>
          </div>
        ))}

        {stage >= 5 && (
          <div className="mock-foot rd-in">
            <span className={`pill pill-sm pill-solid ${stage === 6 ? "rd-press" : ""}`}>
              {stage >= 7 ? "Added to your calendar ✓" : "Approve 2 for calendar"}
            </span>
          </div>
        )}

        {stage >= 7 && (
          <ul className="rd-cal rd-in">
            {BOOKED.map(([t, when]) => (
              <li key={t}><span className="rd-cal-dot" aria-hidden="true" /><strong>{t}</strong><span>{when}</span></li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
