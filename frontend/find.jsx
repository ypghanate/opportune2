import React, { useEffect, useState } from "react";
import { api } from "./api.js";
import { ResultsPanel } from "./opps.jsx";

const CATEGORIES = ["internship", "spring_week"];

export default function Find() {
  const [profile, setProfile] = useState(null);
  const [query, setQuery] = useState("Find spring weeks and internships for me.");
  const [cats, setCats] = useState(["internship", "spring_week"]);
  const [run, setRun] = useState({ phase: "idle" });

  useEffect(() => { api.getProfile().then(setProfile).catch(() => {}); }, []);

  const ready = !profile || (profile.name && profile.university);
  const toggle = (c) => setCats((l) => (l.includes(c) ? l.filter((x) => x !== c) : [...l, c]));

  async function search(e) {
    e.preventDefault();
    if (cats.length === 0) return setRun({ phase: "error", message: "Pick at least one type of opportunity." });
    setRun({ phase: "running" });
    try {
      const r = await api.search(query.trim(), cats);
      setRun({ phase: "done", items: r.items || [], message: r.message });
    } catch (err) {
      setRun({ phase: "error", message: err.message });
    }
  }

  const approved = (ids) =>
    setRun((r) => ({ ...r, items: r.items.map((o) => (ids.includes(o.id) ? { ...o, status: "approved" } : o)) }));

  return (
    <main className="wrap inner">
      <h1>Find opportunities</h1>
      <p className="lede">Tell the agent what you're after. It searches the web, checks each listing and your eligibility, and shows what's left.</p>

      {!ready && (
        <p className="notice notice-err" role="alert">
          Complete your profile first. <a className="link" href="#/profile">Go to your profile</a>
        </p>
      )}

      <form className="form-card wide" onSubmit={search}>
        <label className="field">What should it look for?
          <textarea rows={2} value={query} onChange={(e) => setQuery(e.target.value)} />
        </label>
        <div className="field">
          <span>Types</span>
          <div className="chips" role="group" aria-label="Types of opportunity">
            {CATEGORIES.map((c) => (
              <button type="button" key={c} className={`chip ${cats.includes(c) ? "chip-on" : ""}`} aria-pressed={cats.includes(c)} onClick={() => toggle(c)}>
                {c.replace("_", " ")}
              </button>
            ))}
          </div>
        </div>
        <div><button className="pill pill-lg pill-solid" disabled={run.phase === "running" || !ready}>Run the agent</button></div>
      </form>

      {run.phase === "running" && (
        <p className="notice" role="status"><span className="spinner" aria-hidden="true" /> The agent is searching and checking listings. This can take a minute or two.</p>
      )}
      {run.phase === "error" && <p className="notice notice-err" role="alert">{run.message}</p>}
      {run.phase === "done" && (
        <section aria-live="polite">
          {run.items.length === 0 ? (
            <p className="empty">{run.message || "No matching opportunities found. Try a broader request or more types."}</p>
          ) : (
            <>
              <h2 className="results-h">{run.items.length} {run.items.length === 1 ? "opportunity" : "opportunities"} found</h2>
              <ResultsPanel items={run.items} onApproved={approved} />
            </>
          )}
        </section>
      )}
    </main>
  );
}