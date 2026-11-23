import React, { useEffect, useState } from "react";

export const read = () => location.hash.replace(/^#/, "") || "/";

// Tiny hash router: every page has its own URL and back/forward work.
export function usePath() {
  const [path, setPath] = useState(read);
  useEffect(() => {
    const onChange = () => {
      setPath(read());
      window.scrollTo(0, 0);
    };
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return path;
}

export function go(path, replace = false) {
  if (replace) location.replace(`#${path}`);
  else location.hash = path;
}

const GUEST_LINKS = [["/", "Home"], ["/auth", "Sign in"]];
const USER_LINKS = [["/", "Home"], ["/find", "Find opportunities"], ["/opportunities", "My opportunities"], ["/applications", "Applications"], ["/calendar", "Calendar"], ["/profile", "Profile"]];

export function Nav({ path, authed, onSignOut }) {
  const links = authed ? USER_LINKS : GUEST_LINKS;
  return (
    <header className="nav">
      <div className="wrap nav-in">
        <a className="logo" href="#/">opportune.</a>
        <nav className="nav-links" aria-label="Main">
          {links.map(([to, label]) => (
            <a key={to} href={`#${to}`} aria-current={path === to ? "page" : undefined}>{label}</a>
          ))}
        </nav>
        {authed ? (
          <button className="pill nav-cta" onClick={onSignOut}>Sign out</button>
        ) : (
          <a className="pill nav-cta" href="#/auth?new">Get started</a>
        )}
      </div>
    </header>
  );
}

export function Footer() {
  return (
    <footer className="footer">
      <div className="wrap">© {new Date().getFullYear()} Opportune</div>
    </footer>
  );
}
