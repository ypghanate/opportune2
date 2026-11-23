import React, { useCallback, useEffect, useState } from "react";
import { api } from "./api.js";

const fmt = (d) => d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });

// Shown under the nav on the signed-in pages only (App renders it when authed).
export default function DashHeader({ path }) {
  const [profile, setProfile] = useState(null);
  const [opps, setOpps] = useState(null);
  const [apps, setApps] = useState(null);

  const load = useCallback(() => {
    api.getProfile().then(setProfile).catch(() => {});
    api.opportunities().then(setOpps).catch(() => {});
    api.applications().then(setApps).catch(() => {});
  }, []);

  useEffect(load, [load, path]);
  useEffect(() => {
    window.addEventListener("opps-changed", load); // fired by api.js after a search or an approval
    return () => window.removeEventListener("opps-changed", load);
  }, [load]);

  const first = (profile?.name || "").trim().split(/\s+/)[0];
  const list = opps || [];
  const pending = list.filter((o) => o.status === "pending");
  const active = (apps || []).filter((a) => ["to_apply", "applied", "oa", "interview", "assessment_centre"].includes(a.stage));
  const now = Date.now();
  const next = pending
    .map((o) => ({ o, t: new Date(o.deadline).getTime() }))
    .filter((x) => Number.isFinite(x.t) && x.t > now)
    .sort((a, b) => a.t - b.t)[0];

  const setup = profile && [
    ["Profile", !!(profile.name && profile.university && profile.degree && profile.graduation_year)],
    ["CV", !!profile.cv_name],
    ["Google", !!profile.google_connected],
  ];
  const incomplete = setup ? setup.filter(([, ok]) => !ok) : [];

  return (
    <section className="dh" aria-label="Your dashboard">
      <div className="wrap dh-in">
        <div className="dh-top">
          <div>
            <p className="dh-kicker">Dashboard</p>
            <h2 className="dh-title">{first ? `Welcome back, ${first}` : "Welcome back"}</h2>
          </div>
          <a className="pill pill-solid" href="#/find">Run the agent</a>
        </div>

        <div className="dh-stats">
          <a className="tile dh-stat" href="#/opportunities">
            <span className="dh-num">{opps ? pending.length : "–"}</span>
            <span className="dh-label">Waiting for approval</span>
          </a>
          <a className="tile dh-stat" href="#/applications">
            <span className="dh-num">{apps ? active.length : "–"}</span>
            <span className="dh-label">Applications in progress</span>
          </a>
          <div className="tile dh-stat">
            <span className="dh-num dh-num-sm">{next ? fmt(new Date(next.t)) : "None"}</span>
            <span className="dh-label">{next ? `Next deadline: ${next.o.title}` : "Next deadline"}</span>
          </div>
        </div>

        {setup && (
          <div className="dh-setup" role="status">
            {incomplete.length === 0 ? (
              <span className="dh-ok">✓ Profile, CV and Google are all set up</span>
            ) : (
              <>
                <span className="muted">Finish setting up:</span>
                {setup.map(([label, ok]) =>
                  ok ? (
                    <span key={label} className="tag tag-ok">✓ {label}</span>
                  ) : (
                    <a key={label} className="tag dh-todo" href="#/profile">{label}: not done</a>
                  )
                )}
              </>
            )}
          </div>
        )}
      </div>
    </section>
  );
}
