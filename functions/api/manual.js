// JobRadar handmatige klantopdrachten — Pages Function
// Pad: functions/api/manual.js
// Secrets (Pages → Settings → Variables and Secrets): ANTHROPIC_API_KEY, CANDIDATES_CSV_URL
// Opslag: WORKFLOW_KV key "manualjobs". Matching gebeurt DIRECT bij toevoegen.

const TECH = ["java","spring","python","django","c#",".net","php","laravel","javascript","typescript","react","angular","vue","node.js","next.js","kubernetes","docker","terraform","aws","azure","gcp","sql","postgresql","mysql","oracle","mongodb","kafka","jenkins","gitlab","linux","sap","salesforce","power bi","tableau","airflow","spark","databricks","graphql","rest","microservices","scrum","agile","devops","security","iam","dbt","flutter","ionic","drupal","wordpress"];

async function readJobs(env) {
  const raw = await env.WORKFLOW_KV.get("manualjobs");
  return raw ? JSON.parse(raw) : [];
}

function parseCsvText(text) {
  const rows = []; let row = [], field = "", inq = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inq) { if (ch === '"') { if (text[i+1] === '"') { field += '"'; i++; } else inq = false; } else field += ch; }
    else if (ch === '"') inq = true;
    else if (ch === ",") { row.push(field); field = ""; }
    else if (ch === "\n" || ch === "\r") { if (ch === "\r" && text[i+1] === "\n") i++; row.push(field); field = ""; if (row.some(c => c.trim())) rows.push(row); row = []; }
    else field += ch;
  }
  row.push(field); if (row.some(c => c.trim())) rows.push(row);
  return rows;
}

async function loadCandidates(env) {
  const r = await fetch(env.CANDIDATES_CSV_URL);
  const rows = parseCsvText(await r.text());
  const header = rows[0].map(h => h.trim().toLowerCase());
  const col = (names) => header.findIndex(h => names.some(n => h.includes(n)));
  const iN = col(["name","naam"]), iR = col(["role","functie"]), iY = col(["year","ervaring"]), iS = col(["skill"]), iP = col(["profile","digest"]);
  const out = [];
  rows.slice(1).forEach((r2, idx) => {
    const get = (i) => (i >= 0 && i < r2.length ? r2[i].trim() : "");
    if (!get(iN)) return;
    out.push({ row: idx, name: get(iN), role: get(iR),
      years: parseInt((get(iY).match(/\d+/) || ["0"])[0], 10) || 0,
      skills: get(iS).split(/[,;]+/).map(x => x.trim().toLowerCase()).filter(Boolean),
      profile: get(iP) });
  });
  return out;
}

function prefilter(text, title, cands, top = 15) {
  const t = (title + " " + text).toLowerCase();
  const scored = cands.map(c => {
    let s = 0;
    c.skills.forEach(sk => { if (sk.length > 2 && t.includes(sk)) s += 2; });
    c.role.toLowerCase().split(/[^a-z\+#\.]+/).forEach(w => { if (w.length > 3 && t.includes(w)) s += 2; });
    return [s, c];
  }).sort((a, b) => b[0] - a[0]);
  const picked = scored.filter(x => x[0] > 0).slice(0, top).map(x => x[1]);
  if (picked.length) return picked;
  const step = Math.max(1, Math.floor(cands.length / top));
  return cands.filter((_, i) => i % step === 0).slice(0, top);
}

export async function onRequestGet({ env }) {
  return json(await readJobs(env));
}

export async function onRequestPost({ request, env }) {
  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid json" }, 400); }

  if (body.action === "delete" && body.id) {
    const jobs = (await readJobs(env)).filter(j => j.id !== body.id);
    await env.WORKFLOW_KV.put("manualjobs", JSON.stringify(jobs));
    return json(jobs);
  }

  const company = String(body.company || "").trim().slice(0, 120);
  const title = String(body.title || "").trim().slice(0, 200);
  const text = String(body.text || "").trim().slice(0, 12000);
  if (!company || !title || !text) return json({ error: "bedrijf, titel en vacaturetekst zijn verplicht" }, 400);
  if (!env.ANTHROPIC_API_KEY) return json({ error: "ANTHROPIC_API_KEY ontbreekt in Cloudflare (Settings → Variables and Secrets)" }, 500);
  if (!env.CANDIDATES_CSV_URL) return json({ error: "CANDIDATES_CSV_URL ontbreekt in Cloudflare (Settings → Variables and Secrets)" }, 500);

  const low = " " + text.toLowerCase() + " " + title.toLowerCase() + " ";
  const stack = TECH.filter(tt => low.includes(" " + tt + " ") || low.includes(tt + ",") || low.includes(tt + ".")).slice(0, 10);

  const cands = await loadCandidates(env);
  const shortlist = prefilter(text, title, cands);
  const lines = shortlist.map(c =>
    `ROW=${c.row} | ${c.role} | ${c.years} yrs | skills: ${c.skills.slice(0, 14).join(", ")} | history: ${(c.profile || "(geen digest)").slice(0, 2400)}`);

  const prompt = `You are a senior IT recruiter at a Belgian consultancy. Pick the 3 best candidates for this vacancy.
Score = the likelihood a client would interview this candidate: 85-100 submit immediately; 70-84 strong fit; 55-69 worth pitching; 35-54 partial; <35 weak.
Years-of-experience requirements are indicative, NOT hard bars (within ~70% of asked years with matching stack still scores strong). Concrete past work outweighs keyword overlap. Be honest.
In the JSON 'row' field use the exact ROW= numbers shown (sheet rows, NOT positions). In 'reason' (Dutch, max 20 words) refer to the person only as 'deze kandidaat'.
Reply ONLY with JSON: {"matches":[{"row":<int>,"score":<0-100>,"reason":"<één zin Nederlands>"}]} with exactly 3 entries, best first.

VACANCY: ${title} at ${company}
FULL TEXT:
${text.slice(0, 6000)}

CANDIDATES:
${lines.join("\n")}`;

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": env.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({ model: "claude-sonnet-4-6", max_tokens: 800, messages: [{ role: "user", content: prompt }] }),
  });
  const rdata = await resp.json().catch(() => ({}));
  if (!resp.ok) return json({ error: "AI-matching faalde: " + ((rdata.error || {}).message || resp.status) }, 502);
  const rtext = ((rdata.content || [])[0] || {}).text || "";
  let parsed;
  try { parsed = JSON.parse(rtext.slice(rtext.indexOf("{"))); } catch { return json({ error: "AI-antwoord onleesbaar — probeer opnieuw" }, 502); }

  const byRow = {}; shortlist.forEach(c => { byRow[c.row] = c; });
  const seen = new Set();
  const ai_matches = (parsed.matches || []).slice(0, 3).flatMap(m => {
    const row = parseInt(m.row, 10);
    if (!(row in byRow) || seen.has(row)) return [];
    seen.add(row);
    const c = byRow[row];
    let reason = String(m.reason || "").slice(0, 300);
    cands.forEach(cc => { if (cc.name && reason.includes(cc.name)) reason = reason.split(cc.name).join("deze kandidaat"); });
    return [{ row, score: Math.max(0, Math.min(100, parseInt(m.score, 10) || 0)), reason, check: (c.name[0] || "?").toLowerCase() + String(c.years) }];
  });

  const job = { id: "m" + Date.now().toString(36), company, title, text: text.slice(0, 4000), stack,
                first_seen: new Date().toISOString().slice(0, 10), ai_matches };
  const jobs = await readJobs(env);
  jobs.unshift(job);
  await env.WORKFLOW_KV.put("manualjobs", JSON.stringify(jobs.slice(0, 100)));
  return json(job);
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}
