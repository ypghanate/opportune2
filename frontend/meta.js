import { useEffect, useState } from "react";
import { api } from "./api.js";

// The university and degree lists come from the backend (GET /api/meta), so the form and the API never disagree.
let cache = null;
export function useMeta() {
  const [meta, setMeta] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    cache = cache || api.meta();
    cache.then(setMeta).catch((e) => { cache = null; setError(e.message); });
  }, []);
  return { meta, error };
}

// Same rule as backend/rules.py: two or more words, letters only (plus ' - .), each at least 2 characters.
export function fullNameError(value) {
  const parts = (value || "").trim().split(/\s+/).filter(Boolean);
  if (parts.length < 2) return "Enter your full name (first and last name).";
  for (const p of parts) {
    if (!/^\p{L}[\p{L}'’.\-]*$/u.test(p)) return "Names can only contain letters, spaces, hyphens, apostrophes and full stops.";
    if (p.length < 2) return "Write each part of your name in full (an initial needs a full stop, like J.).";
  }
  return "";
}

// Shown if a saved value is not in the list (for example from before the dropdowns existed).
export const inList = (list, value) => (list && list.includes(value) ? value : "");
