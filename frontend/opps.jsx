import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import PlanLive from "./planLive.jsx";

const fmtDate = (s) => {
  const d = new Date(s);
  return s && !isNaN(d) ? d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" }) : s || "No deadline listed";
};
const label = (c) => (c || "").replace(/_/g, " ");

// Shared by Find and My opportunities: a list with checkboxes and an Approve button.
export function ResultsPanel({ items, onApproved, grouped = false }) {
  const [sel, setSel] = useState(new Set());
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [steps, setSteps] = useState([]);   // what the planner has decided so far (read from the server while it works)
  const [shown, setShown] = useState(0);    // how many of those the student has been shown: they appear one at a time
  const live = useRef({ n: 0, total: 0 });
  live.current = { n: shown, total: steps.length };
  const pending = items.filter((i) => i.status === "pending");
  const allOn = pending.length > 0 && pending.every((i) => sel.has(i.id));

  const toggle = (id) => setSel((s) => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });

  // While planning runs, ask the server what it has placed so far and reveal it one block at a time.
  useEffect(() => {
    if (!busy) return;
    let stop = false;
    const poll = async () => {
      try {
        const p = await api.progress("plan");
        if (!stop && p?.steps) setSteps((old) => (p.steps.length >= old.length ? p.steps : old));
      } catch { /* the page keeps waiting */ }
    };
    const a = setInterval(poll, 700);
    const b = setInterval(() => setShown((n) => (n < live.current.total ? n + 1 : n)), 450);
    return () => { stop = true; clearInterval(a); clearInterval(b); };
  }, [busy]);

  async function approve() {
    setBusy(true);
    setMsg(null);
    setSteps([]);
    setShown(0);
    try {
      const ids = [...sel];
      const r = await api.approve(ids);
      try {  // pick up the last decisions, then let the student see the rest of the plan before it closes
        const p = await api.progress("plan");
        if (p?.steps) setSteps(p.steps);
        const end = Date.now() + 7000;
        while (Date.now() < end && live.current.n < (p?.steps?.length || 0)) await new Promise((res) => setTimeout(res, 150));
      } catch { /* not important */ }
      const booked = r?.booked ?? ids;
      onApproved(booked);
      setSel(new Set());
      const left = ids.length - booked.length;
      setMsg({
        ok: true,
        text: `Planned ${booked.length} ${booked.length === 1 ? "application" : "applications"}: an apply block and practice sessions in your calendar.` +
          (left > 0 ? ` ${left} had no free slot before the deadline and stay pending.` : ""),
        link: true,
      });
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  }

  const watching = items.filter((i) => i.status === "watching");
  const open = items.filter((i) => i.status !== "watching");
  const groups = !grouped ? [{ key: "all", list: items }] : [
    watching.length > 0 && { key: "watch", title: "Watching out", hint: "Not open yet. The agent checks these and books your application when they open.", list: watching },
    open.length > 0 && { key: "open", title: "Found and open", hint: "Open now, with a deadline.", list: open },
  ].filter(Boolean);

  return (
    <div>
      {busy && <PlanLive steps={steps.slice(0, shown)} total={steps.length} />}
      {pending.length > 0 && (
        <div className="bar">
          <label className="check">
            <input type="checkbox" checked={allOn} onChange={() => setSel(allOn ? new Set() : new Set(pending.map((i) => i.id)))} />
            Select all pending
          </label>
          <button className="pill pill-solid" disabled={busy || sel.size === 0} onClick={approve}>
            {busy ? "Planning…" : `Apply to ${sel.size || ""} selected`.replace("  ", " ")}
          </button>
        </div>
      )}
      {msg && <p className={`notice ${msg.ok ? "" : "notice-err"}`} role={msg.ok ? "status" : "alert"}>{msg.text} {msg.link && <a className="link" href="#/applications">See your applications</a>}</p>}
      {groups.map((g) => (
        <section key={g.key} className="opp-group">
          {g.title && <h3 className="group-h">{g.title} <span className="group-n">{g.list.length}</span></h3>}
          {g.hint && <p className="group-hint">{g.hint}</p>}
          <ul className="opp-list">
            {g.list.map((o) => (
              <li key={o.id} className="tile opp-row">
                {o.status === "pending" ? (
                  <input type="checkbox" className="opp-check" checked={sel.has(o.id)} onChange={() => toggle(o.id)} aria-label={`Select ${o.title}`} />
                ) : <span className="opp-check" />}
                <div className="opp-main">
                  <span className="opp-title">{o.title}</span>
                  <p>{[o.company, label(o.category), o.location].filter(Boolean).join(" · ")}</p>
                  {o.description && <p className="opp-desc">{o.description}</p>}
                  {o.url && <a className="opp-link" href={o.url} target="_blank" rel="noopener noreferrer">View original listing ↗</a>}
                </div>
                <div className="opp-side">
                  {o.status === "watching" ? <span className="deadline">Not open yet<small>watching</small></span> : <span className="deadline">{fmtDate(o.deadline)}<small>deadline</small></span>}
                  <span className={`tag ${o.status === "approved" ? "tag-ok" : ""}`} title={o.status === "watching" ? "The agent checks the page and books your application when it opens" : undefined}>{o.status}</span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

export default function MyOpportunities() {
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");
  const [filter, setFilter] = useState("all");
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function deleteAll() {
    setDeleting(true);
    setError("");
    try {
      await api.deleteAllOpportunities();
      setItems([]);
      setFilter("all");
    } catch (e) {
      setError(e.message);
    } finally {
      setDeleting(false);
      setConfirming(false);
    }
  }

  useEffect(() => {
    api.opportunities().then(setItems).catch((e) => setError(e.message));
  }, []);

  const approved = (ids) => setItems((all) => all.map((o) => (ids.includes(o.id) ? { ...o, status: "approved" } : o)));
  const shown = items && (filter === "all" ? items : items.filter((o) => o.status === filter));

  return (
    <main className="wrap inner">
      <h1>My opportunities</h1>
      <p className="lede">Everything the agent has found for you. Pick the ones you will apply to and each gets a plan in your calendar.</p>
      {error && <p className="notice notice-err" role="alert">{error}</p>}
      {!items && !error && <p className="muted">Loading…</p>}
      {items && items.length === 0 && (
        <p className="empty">Nothing here yet. <a className="link" href="#/find">Run the agent</a> to find opportunities.</p>
      )}
      {items && items.length > 0 && (
        <>
          <div className="bar">
            <div className="chips" role="group" aria-label="Filter by status">
              {["all", "pending", "approved", "watching"].map((s) => (
                <button key={s} className={`chip ${filter === s ? "chip-on" : ""}`} aria-pressed={filter === s} onClick={() => setFilter(s)}>{s}</button>
              ))}
            </div>
            {confirming ? (
              <span className="confirm-row" role="alert">
                Delete all {items.length}? Events already in your calendar stay.
                <button className="pill pill-danger pill-sm" disabled={deleting} onClick={deleteAll}>{deleting ? "Deleting…" : "Yes, delete all"}</button>
                <button className="pill pill-sm" disabled={deleting} onClick={() => setConfirming(false)}>Cancel</button>
              </span>
            ) : (
              <button className="pill pill-sm pill-outline-danger" onClick={() => setConfirming(true)}>Delete all</button>
            )}
          </div>
          {shown.length === 0 ? <p className="empty">No {filter} opportunities.</p> : <ResultsPanel key={filter} items={shown} onApproved={approved} />}
        </>
      )}
    </main>
  );
}