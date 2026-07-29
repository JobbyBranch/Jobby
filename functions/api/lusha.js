const HM_TITLES = /recruit|talent|hr|human res|people|hiring|it manager|ict|cto|cio|engineering manager|head of (it|engineering|development|software)|team lead/i;

async function lushaSearch(key, filters) {
  const res = await fetch("https://api.lusha.com/prospecting/contact/search", {
    method: "POST",
    headers: { api_key: key, "content-type": "application/json" },
    body: JSON.stringify({ pages: { page: 0, size: 40 }, filters }),
  });
  const data = await res.json().catch(() => ({}));
  return { ok: res.ok, status: res.status, data };
}

export async function onRequestPost({ request, env }) {
  const key = env.LUSHA_API_KEY;
  if (!key) {
    return json({ error: "LUSHA_API_KEY ontbreekt — voeg het geheim toe in Cloudflare (project → Settings → Variables and Secrets) en herdeploy." }, 500);
  }
  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid json" }, 400); }
  const company = String(body.company || "").trim();
  const domain = String(body.domain || "").trim().toLowerCase();
  if (!company && !domain) return json({ error: "geen bedrijf opgegeven" }, 400);

  const cacheKey = "lusha:" + (domain || company.toLowerCase());
  const cached = await env.WORKFLOW_KV.get(cacheKey);
  if (cached) return json({ ...JSON.parse(cached), cached: true });

  // zoekladder: domein (precies) -> naam + België -> naam wereldwijd
  const attempts = [];
  if (domain) attempts.push({ via: `domein ${domain}`, filters: { companies: { include: { domains: [domain] } } } });
  if (company) {
    attempts.push({ via: `naam + België`, filters: { companies: { include: { names: [company], locations: [{ country: "Belgium" }] } } } });
    attempts.push({ via: `naam wereldwijd`, filters: { companies: { include: { names: [company] } } } });
  }

  let found = [], requestId = null, via = "";
  let lastErr = null;
  for (const a of attempts) {
    const r = await lushaSearch(key, a.filters);
    if (!r.ok) { lastErr = { status: r.status, data: r.data }; continue; }
    const rows = r.data.data || r.data.contacts || [];
    if (rows.length) {
      found = rows;
      requestId = r.data.requestId || r.data.request_id;
      via = a.via;
      break;
    }
  }
  if (!found.length) {
    if (lastErr) return json({ error: lushaError(lastErr.status, lastErr.data) }, 502);
    return json({ contacts: [], via: "alle zoekpogingen" });
  }

  const ranked = found
    .map((c) => ({ raw: c, title: c.jobTitle || c.job_title || "", id: c.contactId || c.contact_id || c.id }))
    .filter((c) => c.id)
    .sort((a, b) => (HM_TITLES.test(b.title) ? 1 : 0) - (HM_TITLES.test(a.title) ? 1 : 0));
  const picked = ranked.slice(0, 5);

  const enrichRes = await fetch("https://api.lusha.com/prospecting/contact/enrich", {
    method: "POST",
    headers: { api_key: key, "content-type": "application/json" },
    body: JSON.stringify({ requestId, contactIds: picked.map((c) => c.id) }),
  });
  const enrichData = await enrichRes.json().catch(() => ({}));
  if (!enrichRes.ok) {
    return json({ error: lushaError(enrichRes.status, enrichData) }, 502);
  }
  const enriched = enrichData.contacts || enrichData.data || [];
  const byId = {};
  enriched.forEach((c) => { byId[c.contactId || c.contact_id || c.id] = c; });

  const contacts = picked.map((p) => {
    const e = byId[p.id] || p.raw || {};
    const emails = e.emailAddresses || e.email_addresses || e.emails || [];
    const phones = e.phoneNumbers || e.phone_numbers || e.phones || [];
    const first = (x) => (Array.isArray(x) && x.length ? (x[0].email || x[0].emailAddress || x[0].number || x[0].internationalNumber || x[0]) : null);
    return {
      name: e.name || [e.firstName || e.first_name, e.lastName || e.last_name].filter(Boolean).join(" ") || p.raw?.name || "Onbekend",
      title: p.title,
      email: typeof first(emails) === "string" ? first(emails) : null,
      phone: typeof first(phones) === "string" ? first(phones) : null,
    };
  });

  const payload = { contacts, via };
  await env.WORKFLOW_KV.put(cacheKey, JSON.stringify(payload), { expirationTtl: 60 * 60 * 24 * 30 });
  return json(payload);
}

function lushaError(status, data) {
  const msg = data?.message || data?.error || "";
  if (status === 401) return "Lusha weigert de API-sleutel (401) — controleer LUSHA_API_KEY.";
  if (status === 403) return "Lusha-plan geeft geen API-toegang (403) — de API vereist een Scale/API-abonnement. " + msg;
  if (status === 429) return "Lusha-quotum bereikt (429) — probeer later opnieuw. " + msg;
  return `Lusha-fout ${status}: ${msg || "onbekende fout"}`;
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}
