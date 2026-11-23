import React from "react";
import RunDemo from "./demo.jsx";

const WHAT = [
  ["Searches for you", "Live searches for internships and spring weeks, shaped by your CV, degree and interests."],
  ["Checks every result", "Closed, broken and ineligible listings are filtered out before you see them."],
  ["Plans every stage", "Each company gets colour-coded blocks in your calendar: apply, practice, online assessment, interview."],
  ["Tracks each application", "A board shows where every company is, from To apply through to Offer."],
];

const AUTO = [
  ["Thinks for itself", "The Planner Agent reads your whole situation: deadlines, exams, what you've finished. It decides what matters most, acts, and tells you why. When it isn't sure, it asks you."],
  ["Repairs clashes", "Add a lecture, shift or society event, even on another calendar. If it lands on one of your blocks, the agent moves that block to the next free slot. Interviews are never moved."],
  ["Watches for new openings", "It waits on programmes that haven't opened yet and looks again every hour, and searches for new ones every 2 hours using your interests. When one opens, it books time to apply."],
  ["Reschedules when you go quiet", "After each block it asks whether you applied or did your practice. Say no, or don't answer, and it finds a new slot."],
  ["Chases you", "When a block ends, it asks whether you did it. No answer? It nudges you again and again, then books it for you once more."],
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
        <h1>Your next opportunity starts <span className="accent">here.</span></h1>
        <p className="sub">
          Opportune finds internships and spring weeks that fit you, plans every stage of each application in your
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
        <h2 id="auto">What the agent does on its own</h2>
        <p className="lede">Once you've approved your applications, you don't have to manage them. The Planner Agent watches your whole situation, decides what to do, and tells you why.</p>
        <div className="cards">
          {AUTO.map(([t, d]) => (
            <div className="tile card" key={t}>
              <h3>{t}</h3>
              <p>{d}</p>
            </div>
          ))}
        </div>
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
