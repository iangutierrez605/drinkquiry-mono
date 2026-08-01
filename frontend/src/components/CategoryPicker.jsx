import { useEffect, useRef, useState } from "react";
import { api, errorText } from "../lib/api";
import { useDebounced } from "../lib/hooks";

/**
 * §F7 (Handoff #15): the shared searchable category multi-select — replaces
 * the fetch-all `catpick` checkbox fieldsets (CreatePage's question form and
 * ModeratePage's theme form both rendered EVERY visible category; at 2,000
 * categories that's 40 sequential requests and a 2,000-row fieldset, C21).
 *
 * Server-side search (debounced ?search=), ONE page of matches as checkable
 * rows, Load more appends. Selection follows the §F6 pinning rule: `value`
 * is a Map(id → {id, name}) captured AT CLICK — chips of the chosen render
 * above the results and survive any search, and a result matching a chosen
 * id still shows checked. Deselect from either place.
 *
 * No client-side eligibility filter, ON PURPOSE: for the question form, the
 * authed /api/categories/ visible set (public-approved | own | official) is
 * exactly the set the server's accepts_questions_from rule allows — the old
 * client filter was a no-op, and the server stays the gate (rule 3) either
 * way. Fetches carry a sequence number (§G stale-response trap).
 *
 * Props: auth; value: Map(id → {id, name}); onChange(nextMap);
 * legend: fieldset label; hint: optional muted suffix.
 */
export default function CategoryPicker({ auth, value, onChange, legend, hint }) {
  const [search, setSearch] = useState("");
  const debounced = useDebounced(search);
  const [params, setParams] = useState({ search: "", page: 1 }); // atomic: search change resets page
  const [rows, setRows] = useState(null); // accumulated pages; null = first load
  const [count, setCount] = useState(0);
  const [hasNext, setHasNext] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const seq = useRef(0);

  useEffect(() => {
    setParams((p) => (p.search === debounced ? p : { search: debounced, page: 1 }));
  }, [debounced]);

  useEffect(() => {
    const mySeq = ++seq.current;
    setLoading(true);
    api
      .categoriesPage(auth.token, { search: params.search, page: params.page })
      .then((d) => {
        if (seq.current !== mySeq) return;
        setRows((prev) => (params.page === 1 ? d.results : [...(prev ?? []), ...d.results]));
        setCount(d.count);
        setHasNext(!!d.next);
        setError(null);
        setLoading(false);
      })
      .catch((err) => {
        if (seq.current !== mySeq) return;
        setError(errorText(err));
        setLoading(false);
      });
  }, [auth.token, params]);

  const toggle = (cat) => {
    const next = new Map(value);
    if (next.has(cat.id)) next.delete(cat.id);
    else next.set(cat.id, { id: cat.id, name: cat.name });
    onChange(next);
  };

  return (
    <fieldset className="field catpick">
      <legend>
        {legend}
        {hint && <span className="field__hint"> {hint}</span>}
      </legend>
      {value.size > 0 && (
        <div className="catpicker__chips">
          {[...value.values()].map((c) => (
            <button key={c.id} type="button" className="pinnedchip" title="Tap to remove" onClick={() => toggle(c)}>
              {c.name} ✕
            </button>
          ))}
        </div>
      )}
      <input
        className="catpicker__search"
        type="search"
        placeholder="Search categories…"
        value={search}
        onChange={(e) => setSearch(e.target.value)}
        aria-label="Search categories"
      />
      {error && <p className="formerror">{error}</p>}
      {rows == null && !error && <p className="footnote">Loading categories…</p>}
      <div className="catpick__list">
        {rows?.map((c) => {
          const on = value.has(c.id);
          return (
            <label key={c.id} className={`catpick__item ${on ? "catpick__item--on" : ""}`}>
              <input type="checkbox" checked={on} onChange={() => toggle(c)} />
              {c.name}
              {c.owner === null ? " (official)" : ""}
            </label>
          );
        })}
      </div>
      {rows != null && count > 0 && (
        <div className="loadmore loadmore--tight">
          <span className="listmeta">
            Showing {rows.length} of {count}
          </span>
          {hasNext && (
            <button
              type="button"
              className="btn btn--ghost btn--sm"
              disabled={loading}
              onClick={() => setParams((p) => ({ ...p, page: p.page + 1 }))}
            >
              {loading ? "Loading…" : "Load more"}
            </button>
          )}
        </div>
      )}
      {rows != null && count === 0 && !loading && (
        <p className="footnote">
          {params.search ? `No categories match “${params.search}”.` : "No categories yet."}
        </p>
      )}
    </fieldset>
  );
}
