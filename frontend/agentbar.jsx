import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const mins = (iso, now) => Math.round((new Date(iso) - now) / 60000);
const ago = (iso, now) => { const m = -mins(iso, now); return m < 1 ? "just now" : m === 1 ? "1 min ago" : m < 60 ? `${m} mins ago` : `${Math.round(m / 60)} h ago`; };
const inn = (iso, now) => { const m = mins(iso, now); return m <= 0 ? "any moment" : m < 60 ? `${m} min${m === 1 ? "" : "s"}` : `${(m / 60).toFixed(m % 60 ? 1 : 0)} h`; };

// A thin bar under the menu on every signed-in page: proof the agent is awake, what it is doing, and when it looks next.
export default function AgentBar() {
  const [p, setP] = useState(null);
  const [now, setNow] = useState(new Date());
  const [i, setI] = useState(0);

  useEffect(() => {
    let live = true;
    const load = () => api.agentPulse().then((r) => live && setP(r)).catch(() => live && setP((x) => (x ? { ...x, awake: false } : null)));
    load();
    const poll = setInterval(load, 8_000);
    const tick = setInterval(() => { setNow(new Date()); setI((n) => n + 1); }, 4_000);
    window.addEventListener("focus", load);
    return () => { live = false; clearInterval(poll); clearInterval(tick); window.removeEventListener("focus", load); };
  }, []);

  if (!p) return null;
  const lines = [];
  const live = p.awake && p.activity;
  if (!p.awake) {
    lines.push("Asleep. The server is waking up, or the background loop is off.");
  } else if (live) {
    lines.push(p.activity.text);
  } else {
    lines.push(p.last_look ? `Awake. Last looked at your plan ${ago(p.last_look, now)}${p.next_look ? `, looking again in ${inn(p.next_look, now)}` : ""}.` : "Awake and about to look at your plan.");
    if (p.next_search) lines.push(`Next: I ask the Researcher to search for new opportunities in ${inn(p.next_search, now)}.`);
    else lines.push("Waiting for your first search, then I keep looking for you.");
    lines.push(p.watching ? `Watching ${p.watching} programme${p.watching === 1 ? "" : "s"} that ${p.watching === 1 ? "hasn't" : "haven't"} opened yet${p.next_watch ? `, checking again in ${inn(p.next_watch, now)}` : ""}.` : "Ready to move your sessions if anything clashes.");
  }
  return (
    <div className={`agentbar${p.awake ? "" : " asleep"}${live ? " busy" : ""}`} role="status" aria-live="polite">
      <div className="wrap agentbar-in">
        <span className="agentbar-dot" aria-hidden="true" />
        <span className="agentbar-name">Planner Agent</span>
        <span key={i % lines.length} className="agentbar-text">{lines[i % lines.length]}</span>
      </div>
    </div>
  );
}
