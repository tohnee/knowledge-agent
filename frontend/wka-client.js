/* ============================================================================
   WKA-Fused · Frontend API client
   Drop-in layer that replaces the HTML prototype's in-memory mock data with real
   calls to the fused backend (api/main.py). Include AFTER the prototype's <script>,
   then flip USE_API = true to route the six views through the live system.

   The role comes from the backend (JWT in prod). Here we pass it via header for demo;
   the frontend must NOT self-assign role in production.
============================================================================ */
const WKA = (() => {
  const BASE = window.WKA_API_BASE || "http://localhost:8000";
  let ROLE = "analyst";                       // prod: derived from JWT, set by backend

  const hdr = () => ({ "Content-Type": "application/json", "Authorization": "Role " + ROLE });

  async function jget(path) {
    const r = await fetch(BASE + path, { headers: hdr() });
    if (!r.ok) throw new Error(path + " → " + r.status);
    return r.json();
  }
  async function jpost(path, body) {
    const r = await fetch(BASE + path, { method: "POST", headers: hdr(), body: JSON.stringify(body) });
    if (!r.ok) throw new Error(path + " → " + r.status);
    return r.json();
  }

  return {
    setRole: (r) => { ROLE = r; },            // wire to the topbar role <select>
    getRole: () => ROLE,

    // ① Sources — upload triggers governed ingest (MinerU→llm-wiki→Action)
    upload: (doc) => jpost("/api/v1/documents/upload", doc),

    // ② Wiki / Object — fields already OPA/Vault-filtered server-side
    getObject: (oid) => jget("/api/v1/objects/" + encodeURIComponent(oid)),
    asof: (oid, year) => jget(`/api/v1/objects/${encodeURIComponent(oid)}/asof?year=${year}`),

    // ④ Workshop — pending actions / control candidates
    pending: () => jget("/api/v1/actions/pending"),

    // Actions (the only write channel) — high-risk returns {status:'pending_review'}; re-call with _confirmed
    action: (name, params, confirmed = false) =>
      jpost("/api/v1/actions/" + name, { ...params, _confirmed: confirmed }),

    // ⑤ Ask — grounded QA; contexts come back already secured per role
    ask: (question, department) => jpost("/api/v1/knowledge/qa", { question, department }),

    health: () => jget("/api/v1/health"),
  };
})();

/* ── How to retrofit the prototype (minimal edits) ──
   1. Topbar role <select> onchange:   WKA.setRole(value); rerenderAll();
   2. Ask view submit:                  const r = await WKA.ask(q); renderAnswer(r);
   3. Object open:                       const o = await WKA.getObject(id); renderWiki(o);
   4. as-of slider:                      const t = await WKA.asof(id, year); renderTemporal(t);
   5. Action button/modal commit:        let r = await WKA.action('mark', params);
                                         if (r.status==='pending_review') showSandbox(r.impact);
                                         else if confirmed: r = await WKA.action('mark', params, true);
   6. Upload:                            await WKA.upload({id,name,text,sourceTier,controlled});
   The mock objects (files/wikiPages/ontoNodes) become caches of these responses. */
window.WKA = WKA;
