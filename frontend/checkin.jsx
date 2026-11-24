import React, { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api.js";

const KIND = {
  apply: "Apply", practice: "Practice", oa: "OA", test: "Test", interview_prep: "Interview prep",
  interview: "Interview", ac_prep: "AC prep", assessment_centre: "Assessment centre",
};
const FIXED = ["interview", "assessment_centre"];
const clock = (iso) => new Date(iso).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

// Shown on every signed-in page while a finished block is waiting for an answer: did you do it?
export default function CheckIn({ path }) {
  const [due, setDue] = useState([]);
  const [asks, setAsks] = useState([]); // questions the agent is waiting on, e.g. "have you heard from Barclays?"
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState("");
  const [msg, setMsg] = useState(null);
  const title = useRef(document.title);
  const pinged = useRef({}); // block id -> when we last pinged about it
  const [canPing, setCanPing] = useState(typeof Notification !== "undefined" ? Notification.permission : "denied");

  const load = useCallback(() => {
    api.checkins().then(setDue).catch(() => {});
    api.agentQuestions().then(setAsks).catch(() => {});
  }, []);
  useEffect(() => { load(); }, [load, path]);
  useEffect(() => {
    const timer = setInterval(load, 60_000); // a block can end while the page is open
    window.addEventListener("opps-changed", load);
    window.addEventListener("focus", load);
    return () => { clearInterval(timer); window.removeEventListener("opps-changed", load); window.removeEventListener("focus", load); };
  }, [load]);

  // The tab title shows how many are waiting, so it catches your eye from another tab.
  useEffect(() => {
    const n = due.length + asks.length;
    document.title = n ? `(${n}) Needs you · Opportune` : title.current;
    return () => { document.title = title.current; };
  }, [due.length, asks.length]);

  // Chasing: a block that has ended and has no answer pings you now, then again every 30 minutes while this page is open.
  useEffect(() => {
    if (canPing !== "granted") return;
    const now = Date.now();
    due.forEach((b) => {
      if (now - (pinged.current[b.id] || 0) < 30 * 60_000) return;
      pinged.current[b.id] = now;
      try {
        new Notification(`Did you finish ${KIND[b.kind] || b.kind.toLowerCase()} for ${b.company}?`, { body: "Tap to tell the agent. If you don't, it will book it again.", tag: `block-${b.id}` });
      } catch { /* some browsers only allow notifications from a service worker */ }
    });
  }, [due, canPing]);

  // A new programme the agent found: ping once (not every 30 minutes) because it is good news, not a nag.
  useEffect(() => {
    if (canPing !== "granted") return;
    asks.filter((q) => q.kind === "opportunity").forEach((q) => {
      if (pinged.current[`o${q.id}`]) return;
      pinged.current[`o${q.id}`] = Date.now();
      try { new Notification(q.message.startsWith("I would skip") ? "Planner Agent: I would skip one" : "Planner Agent: new opportunity", { body: `${q.message.replace("New opportunity detected: ", "")}`, tag: `opp-${q.id}` }); } catch { /* not allowed here */ }
    });
  }, [asks, canPing]);

  const current = due[0];
  const ask = !current ? asks[0] : null;
  const oppOf = (q) => { try { return JSON.parse(q.data || "{}"); } catch { return {}; } };

  async function planIt() {
    setBusy("plan");
    setMsg(null);
    try {
      const id = oppOf(ask).opportunity_id;
      const r = await api.approve([id]);
      await api.answerQuestion(ask.id, "Planned").catch(() => {});
      setAsks((list) => list.filter((q) => q.id !== ask.id));
      if (!(r?.booked || []).includes(id)) setMsg({ ok: false, text: "There was no free slot before its deadline, so I could not book it. It stays in My opportunities." });
    } catch (e) {
      setMsg({ ok: false, text: e.message });
    } finally {
      setBusy("");
    }
  }

  async function notThis() {
    setBusy("skip");
    setMsg(null);
    try {
      await api.skipOpportunity(oppOf(ask).opportunity_id).catch(() => {});
      await api.answerQuestion(ask.id, "Skipped").catch(() => {});
      setAsks((list) => list.filter((q) => q.id !== ask.id));
    } finally {
      setBusy("");
    }
  }

  const optionsOf = (q) => { try { return JSON.parse(q.data || "{}").options || []; } catch { return []; } };
  const whyOf = (q) => { try { return JSON.parse(q.data || "{}").reason || ""; } catch { return ""; } };

  async function choose(option) {
    setBusy(option);
    setMsg(null);
    try {
      await api.answerQuestion(ask.id, option);
      setAsks((list) => list.filter((q) => q.id !== ask.id));
    } catch (e) {
      setMsg({ ok: false, text: e.message });
    } finally {
      setBusy("");
    }
  }

  async function reply(kind) {
    setBusy(kind);
    setMsg(null);
    try {
      if (kind === "rejected") await api.changeStage(ask.application_id, (() => { const f = new FormData(); f.append("stage", "rejected"); return f; })());
      await api.answerQuestion(ask.id);
      setAsks((list) => list.filter((q) => q.id !== ask.id));
    } catch (e) {
      setMsg({ ok: false, text: e.message });
    } finally {
      setBusy("");
    }
  }

  if (!current && !ask && !msg) return null;

  async function answer(done) {
    setBusy(done ? "yes" : "no");
    setMsg(null);
    try {
      const r = await api.checkin(current.id, done, note.trim());
      setMsg({ ok: true, text: r.message });
      setDue((list) => list.filter((b) => b.id !== current.id));
      setNote("");
    } catch (e) {
      setMsg({ ok: false, text: e.message });
    } finally {
      setBusy("");
    }
  }

  const fixed = FIXED.includes(current?.kind);
  return (
    <section className="checkin" aria-live="polite" style={{ "--co": current?.colour || "var(--accent)" }}>
      <div className="wrap checkin-in">
        {current && (
          <>
            <div className="checkin-text">
              <strong>{fixed ? "Did it happen?" : "Did you get this done?"}</strong>
              {Date.now() - new Date(current.end_at || current.start_at) > 30 * 60_000 && <span className="checkin-overdue">Overdue. I'm waiting on your answer.</span>}
              <span>
                <span className="app-kind">{KIND[current.kind] || current.kind}</span> {current.company} · {clock(current.start_at)}
                {current.resource_url && <> · <a href={current.resource_url} target="_blank" rel="noopener noreferrer">Open the test ↗</a></>}
              </span>
              {!fixed && (
                <input className="checkin-note" value={note} maxLength={300} onChange={(e) => setNote(e.target.value)}
                  placeholder="How did it go? (optional, e.g. numerical 9/12)" aria-label="Note about how it went" />
              )}
            </div>
            <div className="checkin-actions">
              <button className="pill pill-solid pill-sm" disabled={!!busy} onClick={() => answer(true)}>{busy === "yes" ? "Saving…" : fixed ? "Yes" : "Yes, done"}</button>
              <button className="pill pill-sm" disabled={!!busy} onClick={() => answer(false)}>
                {busy === "no" ? "Rebooking…" : fixed ? "No, missed it" : "Not yet, rebook it"}
              </button>
              {due.length > 1 && <small className="muted">{due.length - 1} more waiting</small>}
              {canPing === "default" && <button className="link" onClick={() => Notification.requestPermission().then(setCanPing)}>Remind me with a notification</button>}
            </div>
          </>
        )}
        {ask && ask.kind === "ask" && (
          <>
            <div className="checkin-text" style={{ "--co": ask.colour }}>
              <strong>{ask.message}</strong>
              {whyOf(ask) && <span className="log-why">Why I'm asking: {whyOf(ask)}</span>}
            </div>
            <div className="checkin-actions">
              {optionsOf(ask).map((o, i) => (
                <button key={o} className={`pill pill-sm ${i === 0 ? "pill-solid" : ""}`} disabled={!!busy} onClick={() => choose(o)}>{busy === o ? "Saving…" : o}</button>
              ))}
            </div>
          </>
        )}
        {ask && ask.kind === "opportunity" && (() => {
          const o = oppOf(ask);
          const skip = o.verdict === "pass";
          return (
            <>
              <div className="checkin-text" style={{ "--co": skip ? "var(--muted)" : "var(--accent)" }}>
                <span className="checkin-from">Planner Agent · {skip ? "I would skip this one" : o.verdict === "recommend" ? "I recommend this one" : "New opportunity detected"}</span>
                <strong>{ask.message.split(". ")[0].replace(/\.$/, "")}</strong>
                {o.reason && <span className="log-why">Why: {o.reason}</span>}
                <span>{ask.message.split(". ").slice(1).join(". ")}</span>
                {o.match && <span className="app-kind">{o.match} match</span>}
              </div>
              <div className="checkin-actions">
                <button className={`pill pill-sm ${skip ? "" : "pill-solid"}`} disabled={!!busy} onClick={planIt}>{busy === "plan" ? "Planning…" : skip ? "Plan it anyway" : "Yes, plan it"}</button>
                <button className={`pill pill-sm ${skip ? "pill-solid" : ""}`} disabled={!!busy} onClick={notThis}>{busy === "skip" ? "Saving…" : skip ? "OK, skip it" : "Not for me"}</button>
                {o.url && <a className="link" href={o.url} target="_blank" rel="noopener noreferrer">View listing ↗</a>}
                <a className="link" href="#/opportunities">See all</a>
                {asks.filter((q) => q.kind === "opportunity").length > 1 && <small className="muted">{asks.filter((q) => q.kind === "opportunity").length - 1} more from the Planner Agent</small>}
                {canPing === "default" && <button className="link" onClick={() => Notification.requestPermission().then(setCanPing)}>Notify me when one appears</button>}
              </div>
            </>
          );
        })()}
        {ask && ask.kind !== "ask" && ask.kind !== "opportunity" && (
          <>
            <div className="checkin-text" style={{ "--co": ask.colour }}>
              <strong>{ask.message.split(".")[0]}.</strong>
              <span>{ask.message.split(". ").slice(1).join(". ")}</span>
            </div>
            <div className="checkin-actions">
              <a className="pill pill-solid pill-sm" href="#/applications">Yes, update it</a>
              <button className="pill pill-sm" disabled={!!busy} onClick={() => reply("waiting")}>Still waiting</button>
              <button className="pill pill-sm" disabled={!!busy} onClick={() => reply("rejected")}>{busy === "rejected" ? "Saving…" : "Rejected"}</button>
            </div>
          </>
        )}
        {msg && <p className={`notice ${msg.ok ? "" : "notice-err"}`} role={msg.ok ? "status" : "alert"}>{msg.text}</p>}
      </div>
    </section>
  );
}
