import React, { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "./api.js";

const COLUMNS = [
  ["to_apply", "To apply"],
  ["applied", "Applied"],
  ["oa", "Online assessment"],
  ["interview", "Interview"],
  ["assessment_centre", "Assessment centre"],
  ["offer", "Offer"],
  ["rejected", "Rejected"],
];
const STAGE_NAME = { ...Object.fromEntries(COLUMNS), withdrawn: "Withdrawn" };
const NEXT = { to_apply: "applied", applied: "oa", oa: "interview", interview: "assessment_centre", assessment_centre: "offer" };
const NEXT_LABEL = { applied: "Mark applied", oa: "Got an OA", interview: "Got an interview", assessment_centre: "Got an assessment centre", offer: "Got an offer" };
const KIND = {
  apply: "Apply", practice: "Practice", oa: "OA", test: "Test", interview_prep: "Interview prep",
  interview: "Interview", ac_prep: "AC prep", assessment_centre: "Assessment centre",
};

const when = (iso, withTime = true) => {
  const d = new Date(iso);
  if (!iso || isNaN(d)) return "";
  const day = d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
  return withTime ? `${day}, ${d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : day;
};

// Stages that ask for a date: an online assessment has a deadline, a live event has a start time.
const FIELDS = {
  oa: { shotOnly: true },
  interview: { when: "start_time", label: "Interview date and time", minutes: "How long it lasts (minutes)" },
  assessment_centre: { when: "start_time", label: "Assessment centre date and time", minutes: "How long it lasts (minutes)" },
};

function StageForm({ app, stage, onDone, onCancel }) {
  const spec = FIELDS[stage];
  const [date, setDate] = useState("");
  const [minutes, setMinutes] = useState("");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function submit(e) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("stage", stage);
      if (spec && !spec.shotOnly && date) form.append(spec.when, date);
      if (spec && !spec.shotOnly && minutes) form.append("duration_minutes", minutes);
      if (file) form.append("screenshot", file);
      onDone(await api.changeStage(app.id, form));
    } catch (err) {
      setError(err.message);
      setBusy(false);
    }
  }

  return (
    <form className="stage-form" onSubmit={submit}>
      <strong>{STAGE_NAME[stage]}</strong>
      {spec?.shotOnly && (
        <label className="field">Screenshot of the employer's email
          <input type="file" required accept="image/png,image/jpeg,image/webp,image/gif" onChange={(e) => setFile(e.target.files?.[0] || null)} />
          <small className="muted">Claude reads the deadline and how long it takes from the screenshot, so there is nothing to type. The image is deleted straight after.</small>
        </label>
      )}
      {spec && !spec.shotOnly && (
        <>
          <label className="field">{spec.label}
            <input type="datetime-local" value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
          <label className="field">{spec.minutes}
            <input type="number" min="5" max="600" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
          </label>
          <label className="field">Screenshot of the email (optional)
            <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" onChange={(e) => setFile(e.target.files?.[0] || null)} />
            <small className="muted">Claude reads the dates from it. Anything you type above wins. The image is deleted straight after.</small>
          </label>
        </>
      )}
      {error && <p className="notice notice-err" role="alert">{error}</p>}
      <div className="stage-actions">
        <button className="pill pill-solid pill-sm" disabled={busy}>{busy ? (file ? "Reading the email…" : "Updating…") : "Save and update calendar"}</button>
        <button type="button" className="pill pill-sm" disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}

function Card({ app, onChange }) {
  const [picking, setPicking] = useState(null); // the stage a form is open for
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const next = NEXT[app.stage];

  async function move(stage) {
    if (FIELDS[stage]) return setPicking(stage); // needs a date first
    setBusy(true);
    setError("");
    try {
      const form = new FormData();
      form.append("stage", stage);
      onChange(await api.changeStage(app.id, form));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const planned = app.blocks.filter((b) => b.status === "planned");
  const sessions = app.blocks.filter((b) => b.kind === "practice" && ["planned", "due", "done"].includes(b.status));
  const sessionsDone = sessions.filter((b) => b.status === "done").length;
  const practice = app.next_block?.kind === "practice" && app.next_block.resource_url ? app.next_block : null; // the next session's test, one click away
  return (
    <li className="app-card tile" style={{ "--co": app.colour }}>
      <div className="app-head">
        <span className="app-dot" aria-hidden="true" />
        <div>
          <strong className="app-company">{app.company}</strong>
          <small className="muted">{app.title}</small>
        </div>
      </div>

      {app.next_block ? (
        <p className="app-next"><span className="app-kind">{KIND[app.next_block.kind] || app.next_block.kind}</span> {when(app.next_block.start_at)}</p>
      ) : (
        <p className="app-next muted">Nothing planned</p>
      )}
      {practice && (
        <a className="pill pill-solid pill-sm app-practice" href={practice.resource_url} target="_blank" rel="noopener noreferrer">
          Practise: {practice.resource_label || "open the test"} ↗
        </a>
      )}
      {sessions.length > 0 && <p className="app-meta">Practice: {sessionsDone} of {sessions.length} done</p>}
      {app.stage_deadline && (
        <p className="app-meta">{FIELDS[app.stage]?.when === "start_time" ? "Starts" : "Due"} {when(app.stage_deadline)}{app.deadline_assumed ? " (assumed)" : ""}</p>
      )}
      {app.note && <p className="app-meta">{app.note}</p>}
      {error && <p className="notice notice-err" role="alert">{error}</p>}

      {picking ? (
        <StageForm app={app} stage={picking} onDone={(a) => { setPicking(null); onChange(a); }} onCancel={() => setPicking(null)} />
      ) : (
        <div className="stage-actions">
          {next && <button className="pill pill-solid pill-sm" disabled={busy} onClick={() => move(next)}>{NEXT_LABEL[next]}</button>}
          {app.stage !== "withdrawn" && <button className="link-btn" disabled={busy} onClick={() => move("withdrawn")}>Withdraw</button>}
        </div>
      )}

      {planned.length > 0 && (
        <>
          <button className="link-btn" aria-expanded={open} onClick={() => setOpen(!open)}>{open ? "Hide" : "Show"} {planned.length} planned {planned.length === 1 ? "block" : "blocks"}</button>
          {open && (
            <ul className="block-list">
              {planned.map((b) => (
                <li key={b.id}>
                  <span className="app-kind">{KIND[b.kind] || b.kind}</span> {when(b.start_at)}
                  {b.resource_url && <> · <a href={b.resource_url} target="_blank" rel="noopener noreferrer">{b.resource_label || "Practice"} ↗</a></>}
                  {b.link && <> · <a href={b.link} target="_blank" rel="noopener noreferrer">Calendar ↗</a></>}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </li>
  );
}

const whyOf = (l) => { try { return l.data ? JSON.parse(l.data).reason || "" : ""; } catch { return ""; } };

export default function Applications() {
  const [apps, setApps] = useState(null);
  const [error, setError] = useState("");
  const [warnings, setWarnings] = useState([]);
  const [showWithdrawn, setShowWithdrawn] = useState(false);
  const [status, setStatus] = useState([]);
  const [thinking, setThinking] = useState(null);

  const load = useCallback(() => {
    api.applications().then(setApps).catch((e) => setError(e.message));
    api.agentStatus().then(setStatus).catch(() => {});
    api.agentThinking().then(setThinking).catch(() => {});
  }, []);

  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    window.addEventListener("opps-changed", load);
    return () => window.removeEventListener("opps-changed", load);
  }, [load]);

  const changed = (updated) => {
    setWarnings(updated.warnings || []);
    setApps((all) => all.map((a) => (a.id === updated.id ? updated : a)));
  };

  const by = useMemo(() => {
    const out = {};
    for (const [s] of COLUMNS) out[s] = [];
    for (const a of apps || []) (out[a.stage] ||= []).push(a);
    return out;
  }, [apps]);
  const withdrawn = (apps || []).filter((a) => a.stage === "withdrawn");

  return (
    <main className="wrap inner">
      <h1>Applications</h1>
      <p className="lede">Every company you are applying to, with its stage. Each has its own colour in your calendar and its own set of blocks.</p>
      {error && <p className="notice notice-err" role="alert">{error}</p>}
      {warnings.map((w) => <p key={w} className="notice notice-err" role="alert">{w}</p>)}
      {!apps && !error && <p className="muted">Loading…</p>}
      {apps && apps.length === 0 && (
        <p className="empty">No applications yet. <a className="link" href="#/opportunities">Approve an opportunity</a> and it will appear here with its plan.</p>
      )}
      {apps && apps.length > 0 && (
        <>
          <div className="kanban" role="list">
            {COLUMNS.map(([stage, label]) => (
              <section key={stage} className="kan-col" role="listitem" aria-label={`${label}, ${by[stage].length}`}>
                <h2 className="kan-title">{label} <span className="kan-count">{by[stage].length}</span></h2>
                <ul className="kan-list">
                  {by[stage].map((a) => <Card key={a.id} app={a} onChange={changed} />)}
                </ul>
              </section>
            ))}
          </div>
          {withdrawn.length > 0 && (
            <p><button className="link-btn" onClick={() => setShowWithdrawn(!showWithdrawn)}>{showWithdrawn ? "Hide" : "Show"} {withdrawn.length} withdrawn</button></p>
          )}
          {showWithdrawn && <ul className="kan-list kan-withdrawn">{withdrawn.map((a) => <Card key={a.id} app={a} onChange={changed} />)}</ul>}
        </>
      )}
      <div className="agent-panels">
      {thinking && (thinking.assessment || thinking.mode === "rules") && (
        <section className="tile thinking" aria-label="How the agent sees your plan">
          <div className="agent-head">
            <h2>How I see your plan</h2>
            <span className={`tag ${thinking.mode === "rules" ? "" : "tag-ok"}`}>{thinking.mode === "rules" ? "Using simple rules" : "Planner Agent on"}</span>
          </div>
          {thinking.assessment
            ? <p className="thinking-text">{thinking.assessment}</p>
            : <p className="muted">The planner hit a problem, so I am using simple rules for now.</p>}
          {thinking.concerns?.length > 0 && (
            <ul className="concerns">{thinking.concerns.map((c) => <li key={c}>{c}</li>)}</ul>
          )}
          {thinking.at && <small className="muted">Looked at {new Date(thinking.at).toLocaleString("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}. I look again whenever something changes: a new event, a finished block, a programme opening, or an answer from you.</small>}
        </section>
      )}
      <section className="tile agent-log" aria-label="What the agent is doing now">
        <h2>What the agent is doing now</h2>
        <ul className="status-list">
          {status.map((s) => (
            <li key={s.key} className={`${s.on ? "on" : ""} ${s.key === "planner" ? "main" : ""}`}>
              <span className="status-dot" aria-hidden="true" />
              <span><strong>{s.title}.</strong> {s.text}</span>
            </li>
          ))}
        </ul>
      </section>
      </div>
    </main>
  );
}