import React from "react";
import RunDemo from "./demo.jsx";

const WHAT = [
  ["Searches for you", "Live searches for internships and spring weeks, shaped by your CV, degree and interests."],
  ["Checks every result", "Closed, broken and ineligible listings are filtered out before you see them."],
  ["Plans every stage", "Each company gets colour-coded blocks in your calendar: apply, practice, online assessment, interview."],
  ["Tracks each application", "A board shows where every company is, from To apply through to Offer."],
];

const AGENTS = [
  { name: "Researcher", role: "Finds opportunities for you", tag: "Live", points: ["Searches the web for spring weeks and internships", "Keeps the ones that fit you, one per company", "Drops closed, wrong-year and out-of-area listings", "Looks again every 2 hours"] },
  { name: "Scheduler", role: "Finds the right time for every step", tag: "Live", points: ["Books an apply block and practice sessions", "Works around every event in your calendar", "Keeps clear of exams and interviews", "Never piles more than three blocks on a day"] },
  { name: "Planner Agent", role: "Maintains your plan, in the background", tag: "Live", main: true, points: [
    "Fixes any clashes automatically",
    "Books or suggests programmes to you as soon as they open",
    "Smartly makes room, deliberately, before an exam or assignment",
    "Nudges you when a session ends and you have not responded",
  ] },
  { name: "Application Filler", role: "Fills in the forms for you", tag: "Coming soon", soon: true, points: ["Reads your CV and profile", "Pre-fills application forms", "You review before anything is sent"] },
];

const SYNC = [
  ["Researcher finds", "New programmes that fit your interests"],
  ["You approve", "One tap in the pop-up"],
  ["Scheduler books", "Apply and practice blocks in your free time"],
  ["Planner looks after it", "Watches, reschedules and chases"],
];

const STEPS = [
  ["Set up your profile", "Add your university, degree, graduation year and interests, then upload your CV."],
  ["Ask the agent", "Describe what you want, for example spring weeks in quant finance."],
  ["Approve what you want", "Pick the ones worth applying for. Nothing reaches your calendar until you do."],
  ["Let it run", "It plans each stage, then keeps your calendar working as things change."],
];

export default function Overview({ authed }) {
  const start = authed ? "#/find" : "#/auth?new";
  return (
    <main>
      <section className="wrap hero">
        <p className="eyebrow">Personal agent ecosystem for all your applications</p>
        <h1>Your next opportunity starts <span className="accent">here.</span></h1>
        <p className="sub">
          A team of agents finds internships and spring weeks that fit you, plans every stage of each application in your
          calendar, then keeps working in the background so nothing slips.
        </p>
        <div className="hero-actions">
          <a className="pill pill-lg pill-solid" href={start}>{authed ? "Find opportunities" : "Create your account"}</a>
          {authed && <a className="pill pill-lg" href="#/opportunities">My opportunities</a>}
          <a className="pill pill-lg" href="#/" onClick={(e) => { e.preventDefault(); document.getElementById("how")?.scrollIntoView({ behavior: "smooth" }); }}>See how it works</a>
        </div>
        <RunDemo />
      </section>

      <section className="wrap section" aria-labelledby="what">
        <h2 id="what">What you get</h2>
        <div className="cards">
          {WHAT.map(([t, d]) => (
            <div className="tile card" key={t}>
              <h3>{t}</h3>
              <p>{d}</p>
            </div>
          ))}
        </div>
      </section>

      <section className="wrap section" aria-labelledby="auto">
        <h2 id="auto">Meet your agents</h2>
        <p className="lede">Your personal agent ecosystem for all your applications. Three agents work together, a fourth is on the way, and you only make the decisions.</p>
        <div className="agents">
          {AGENTS.map((a) => (
            <article className={`agent-card${a.main ? " main" : ""}${a.soon ? " soon" : ""}`} key={a.name}>
              <span className="agent-tag">{a.tag}</span>
              <h3>{a.name}</h3>
              <p className="agent-role">{a.role}</p>
              <ul>{a.points.map((p) => <li key={p}>{p}</li>)}</ul>
            </article>
          ))}
        </div>
        <ol className="sync" aria-label="How the agents work together">
          {SYNC.map(([t, d], i) => (
            <li key={t}><strong>{t}</strong><span>{d}</span></li>
          ))}
        </ol>
        <p className="muted sync-note">The Planner keeps working in the background. They share one picture of your plan, so the Researcher never suggests something that clashes and the Planner always knows what is new. Interviews are never moved.</p>
      </section>

      <section className="wrap section" id="how" aria-labelledby="how-h">
        <h2 id="how-h">How it works</h2>
        <ol className="steps">
          {STEPS.map(([t, d], i) => (
            <li className="tile" key={t}>
              <span className="step-n">{i + 1}</span>
              <h3>{t}</h3>
              <p>{d}</p>
            </li>
          ))}
        </ol>
      </section>

      <section className="wrap section cta-band">
        <h2>Stop searching. Start applying.</h2>
        <a className="pill pill-lg pill-solid" href={start}>{authed ? "Find opportunities" : "Create your account"}</a>
      </section>
    </main>
  );
}