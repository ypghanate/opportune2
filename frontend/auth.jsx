import React, { useState } from "react";
import { api, setToken } from "./api.js";
import { useMeta, fullNameError, inList } from "./meta.js";

export default function AuthPage({ onAuth }) {
  const [isNew, setIsNew] = useState(location.hash.includes("?new"));
  const [form, setForm] = useState({ email: "", password: "", name: "", university: "", degree: "" });
  const { meta, error: metaError } = useMeta();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const set = (k) => (e) => { setForm({ ...form, [k]: e.target.value }); setError(""); };

  async function submit(e) {
    e.preventDefault();
    setError("");
    const email = form.email.trim().toLowerCase();
    if (!/^\S+@\S+\.\S+$/.test(email)) return setError("Enter a valid email address.");
    if (isNew) {
      const nameErr = fullNameError(form.name);
      if (nameErr) return setError(nameErr);
      if (!inList(meta?.universities, form.university)) return setError("Choose your university from the list.");
      if (!inList(meta?.degrees, form.degree)) return setError("Choose your degree from the list.");
      if (form.password.length < 10) return setError("Use a password of at least 10 characters.");
    }
    if (!form.password) return setError("Enter your password.");

    setBusy(true);
    try {
      const { access_token } = isNew
        ? await api.register(email, form.password, { name: form.name.trim(), university: form.university, degree: form.degree })
        : await api.login(email, form.password);
      setToken(access_token);
      onAuth(isNew);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="wrap center-page">
      <form className="form-card" onSubmit={submit} noValidate>
        <h1>{isNew ? "Create your account" : "Welcome back"}</h1>
        <p className="muted">
          {isNew ? "Next you'll set up your profile so the agent knows what to look for." : "Sign in to find opportunities."}
        </p>
        {isNew && (
          <>
            <label className="field">Full name
              <input value={form.name} onChange={set("name")} autoComplete="name" placeholder="First and last name" />
            </label>
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
            {metaError && <p className="notice notice-err" role="alert">Could not load the lists: {metaError}</p>}
          </>
        )}
        <label className="field">Email
          <input type="email" value={form.email} onChange={set("email")} autoComplete="email" />
        </label>
        <label className="field">Password
          <input type="password" value={form.password} onChange={set("password")} autoComplete={isNew ? "new-password" : "current-password"} />
          {isNew && <small className="muted">At least 10 characters.</small>}
        </label>
        {error && <p className="notice notice-err" role="alert">{error}</p>}
        <button className="pill pill-lg pill-solid full" disabled={busy}>
          {busy ? "Please wait…" : isNew ? "Create account" : "Sign in"}
        </button>
        <p className="muted center">
          {isNew ? "Already have an account?" : "New to Opportune?"}{" "}
          <button type="button" className="link" onClick={() => { setIsNew(!isNew); setError(""); }}>
            {isNew ? "Sign in" : "Create an account"}
          </button>
        </p>
      </form>
    </main>
  );
}
