import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { useMeta, fullNameError, inList } from "./meta.js";

const YEARS = ["First year", "Second year", "Third year", "Final year", "Postgraduate"];
const EMPTY = { name: "", university: "", degree: "", year: "First year", graduation_year: 2029, location: "London", gender: "", interests: "", autopilot: true };

export default function Profile() {
  const [form, setForm] = useState(EMPTY);
  const [cv, setCv] = useState("");
  const [google, setGoogle] = useState(false);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);
  const fileRef = useRef(null);
  const { meta } = useMeta();

  useEffect(() => {
    api.getProfile()
      .then((p) => {
        setForm({ ...EMPTY, ...p, interests: (p.interests || []).join(", ") });
        setCv(p.cv_name || "");
        setGoogle(!!p.google_connected);
      })
      .catch((e) => setMsg({ ok: false, text: e.message }))
      .finally(() => setLoading(false));
  }, []);

  // Google sends the browser back here as #/profile?google=connected (or =error&reason=...).
  useEffect(() => {
    const q = new URLSearchParams(location.hash.split("?")[1] || "");
    if (q.get("google") === "connected") setMsg({ ok: true, text: "Google connected." });
    else if (q.get("google") === "error") setMsg({ ok: false, text: q.get("reason") || "Could not connect Google." });
    if (q.has("google")) history.replaceState(null, "", "#/profile");
  }, []);

  async function connectGoogle() {
    setMsg(null);
    try {
      const { url } = await api.googleConnect();
      location.href = url;
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    }
  }

  async function disconnectGoogle() {
    setMsg(null);
    try {
      await api.googleDisconnect();
      setGoogle(false);
      setMsg({ ok: true, text: "Google disconnected." });
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    }
  }

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  async function save(e) {
    e.preventDefault();
    setMsg(null);
    const nameErr = fullNameError(form.name);
    if (nameErr) return setMsg({ ok: false, text: nameErr });
    if (!inList(meta?.universities, form.university)) return setMsg({ ok: false, text: "Choose your university from the list." });
    if (!inList(meta?.degrees, form.degree)) return setMsg({ ok: false, text: "Choose your degree from the list." });
    const year = Number(form.graduation_year);
    if (!Number.isInteger(year) || year < 2020 || year > 2040) return setMsg({ ok: false, text: "Enter a graduation year between 2020 and 2040." });
    setBusy(true);
    try {
      await api.saveProfile({
        ...form,
        graduation_year: year,
        interests: form.interests.split(",").map((s) => s.trim()).filter(Boolean),
      });
      setMsg({ ok: true, text: "Profile saved." });
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    } finally {
      setBusy(false);
    }
  }

  async function upload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setMsg(null);
    if (!/\.(pdf|docx)$/i.test(file.name)) return setMsg({ ok: false, text: "Upload a PDF or DOCX." });
    if (file.size > 8 * 1024 * 1024) return setMsg({ ok: false, text: "Maximum CV size is 8 MB." });
    try {
      const r = await api.uploadCv(file);
      setCv(r.filename);
      setMsg({ ok: true, text: "CV uploaded." });
    } catch (err) {
      setMsg({ ok: false, text: err.message });
    }
  }

  if (loading) return <main className="wrap inner"><p className="muted">Loading your profile…</p></main>;

  return (
    <main className="wrap inner">
      <h1>Profile</h1>
      <p className="lede">The agent uses this to decide what to search for and which listings you're eligible for.</p>
      <form className="form-card wide" onSubmit={save} noValidate>
        <div className="grid-2">
          <label className="field">Full name<input value={form.name} onChange={set("name")} autoComplete="name" placeholder="First and last name" /></label>
          <label className="field">University
            <select value={inList(meta?.universities, form.university)} onChange={set("university")} disabled={!meta}>
              <option value="">{meta ? "Choose your university" : "Loading…"}</option>
              {(meta?.universities || []).map((u) => <option key={u} value={u}>{u}</option>)}
            </select>
          </label>
          <label className="field">Degree
            <select value={inList(meta?.degrees, form.degree)} onChange={set("degree")} disabled={!meta}>
              <option value="">{meta ? "Choose your degree" : "Loading…"}</option>
              {(meta?.degrees || []).map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </label>
          <label className="field">Year of study
            <select value={form.year} onChange={set("year")}>{YEARS.map((y) => <option key={y}>{y}</option>)}</select>
          </label>
          <label className="field">Graduation year<input type="number" value={form.graduation_year} onChange={set("graduation_year")} /></label>
          <label className="field">Location<input value={form.location} onChange={set("location")} /></label>
          <label className="field">Gender (optional)
            <select value={form.gender || ""} onChange={set("gender")}>
              <option value="">Not set</option>
              {["Female", "Male", "Non-binary", "Prefer not to say"].map((g) => <option key={g}>{g}</option>)}
            </select>
            <small className="muted">Only used to hide schemes you aren't eligible for, such as women-only programmes.</small>
          </label>
        </div>
        <label className="check autopilot-check">
          <input type="checkbox" checked={form.autopilot !== false} onChange={(e) => setForm({ ...form, autopilot: e.target.checked })} />
          <span>
            <strong>Autopilot</strong>
            <small className="muted">The agent works in the background. It moves blocks that clash with new events in any of your calendars, rebooks blocks you don't answer about, reminds you about quiet applications, and books new programmes when they open. Everything it does is listed on the Applications page.</small>
          </span>
        </label>
        <label className="field">Interests
          <input value={form.interests} onChange={set("interests")} placeholder="quant finance, trading, machine learning" />
          <small className="muted">Separate with commas.</small>
        </label>

        <div className="field">
          <span>CV</span>
          <div className="cv-row">
            <span className="muted">{cv ? `Uploaded: ${cv}` : "No CV uploaded yet (PDF or DOCX, up to 8 MB)."}</span>
            <button type="button" className="pill pill-sm" onClick={() => fileRef.current.click()}>{cv ? "Replace CV" : "Upload CV"}</button>
            <input ref={fileRef} type="file" accept=".pdf,.docx" onChange={upload} hidden aria-label="CV file" />
          </div>
        </div>

        <div className="field">
          <span>Google (Gmail and Calendar)</span>
          <div className="cv-row">
            <span className="muted">
              {google ? "Connected. The agent reads assessment emails and books time in your calendar." : "Not connected. Needed to check your inbox and book time in your calendar."}
            </span>
            {google ? (
              <button type="button" className="pill pill-sm" onClick={disconnectGoogle}>Disconnect</button>
            ) : (
              <button type="button" className="pill pill-sm" onClick={connectGoogle}>Connect Google</button>
            )}
          </div>
        </div>

        {msg && <p className={`notice ${msg.ok ? "" : "notice-err"}`} role={msg.ok ? "status" : "alert"}>{msg.text}</p>}
        <div><button className="pill pill-lg pill-solid" disabled={busy}>{busy ? "Saving…" : "Save profile"}</button></div>
      </form>
    </main>
  );
}
