import React, { StrictMode, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { getToken, setToken, setAuthErrorHandler } from "./api.js";
import { Nav, Footer, usePath, go, read } from "./layout.jsx";
import Overview from "./overview.jsx";
import AuthPage from "./auth.jsx";
import Profile from "./profile.jsx";
import Find from "./find.jsx";
import DashHeader from "./dashheader.jsx";
import MyOpportunities from "./opps.jsx";
import Calendar from "./calendar.jsx";
import Applications from "./applications.jsx";
import CheckIn from "./checkin.jsx";
import "./style.css";

const PROTECTED = ["/find", "/opportunities", "/applications", "/calendar", "/profile"];

function App() {
  const raw = usePath();
  const path = raw.split("?")[0];
  const [authed, setAuthed] = useState(() => !!getToken());

  // An expired or invalid token sends you back to sign in.
  useEffect(() => {
    setAuthErrorHandler(() => { setAuthed(false); go("/auth", true); });
  }, []);

  // Redirect using the real address bar so a go() made just before this runs isn't overridden.
  useEffect(() => {
    const current = read().split("?")[0];
    if (PROTECTED.includes(current) && !authed) go("/auth", true);
    else if (current === "/auth" && authed) go("/find", true);
  }, [raw, authed]);

  const signOut = () => { setToken(null); setAuthed(false); go("/"); };
  const route = PROTECTED.includes(path) && !authed ? "/auth" : path === "/auth" && authed ? "/find" : path;

  const pages = {
    "/": <Overview authed={authed} />,
    "/auth": <AuthPage onAuth={(isNew) => { setAuthed(true); go(isNew ? "/profile" : "/find"); }} />,
    "/profile": authed && <Profile />,
    "/find": authed && <Find />,
    "/opportunities": authed && <MyOpportunities />,
    "/applications": authed && <Applications />,
    "/calendar": authed && <Calendar />,
  };

  return (
    <>
      <Nav path={route} authed={authed} onSignOut={signOut} />
      {authed && PROTECTED.includes(route) && <CheckIn path={route} />}
      {authed && PROTECTED.includes(route) && <DashHeader path={route} />}
      {pages[route] ?? <Overview authed={authed} />}
      <Footer />
    </>
  );
}

createRoot(document.getElementById("root")).render(
  <StrictMode>
    <App />
  </StrictMode>
);
