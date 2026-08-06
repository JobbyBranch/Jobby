// JobRadar AI-assistent — Pages Function
// Pad: functions/api/chat.js — Secret: ANTHROPIC_API_KEY (staat er al voor manual.js)

const LATEST = "https://raw.githubusercontent.com/JobbyBranch/Jobby/main/output/latest.json";
const SNIPPETS = "https://raw.githubusercontent.com/JobbyBranch/Jobby/main/output/snippets.json";

export async function onRequestPost({ request, env }) {
  if (!env.ANTHROPIC_API_KEY) return json({ error: "ANTHROPIC_API_KEY ontbreekt" }, 500);
  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid json" }, 400); }
  const messages = (body.messages || []).slice(-8).map(m => ({ role: m.role === "assistant" ? "assistant" : "user", content: String(m.content || "").slice(0, 2000) }));
  if (!messages.length) return json({ error: "geen vraag" }, 400);
  const question = messages[messages.length - 1].content.toLowerCase();

  let jobs = [];
  try { jobs = await (await fetch(LATEST)).json(); } catch {}
  let snippets = {};
  try { const r = await fetch(SNIPPETS); if (r.ok) snippets = await r.json(); } catch {}

  // voorselectie: score op woordoverlap met de vraag, plus de nieuwste vacatures
  const toks = question.split(/[^a-z0-9\+#\.]+/).filter(w => w.length > 2);
  const scored = jobs.map((j, i) => {
    const hay = (j.company + " " + j.title + " " + (j.stack || []).join(" ") + " " + (snippets[j.url] || "")).toLowerCase();
    let s = 0; toks.forEach(t => { if (hay.includes(t)) s++; });
    return [s, i, j];
  });
  const byScore = scored.filter(x => x[0] > 0).sort((a, b) => b[0] - a[0]).slice(0, 120);
  const newest = scored.slice(0, 40);
  const chosen = []; const seen = new Set();
  [...byScore, ...newest].forEach(([s, i, j]) => { if (!seen.has(j.url)) { seen.add(j.url); chosen.push(j); } });

  const catalog = chosen.slice(0, 150).map((j, n) => {
    const snip = snippets[j.url] ? " | tekst: " + String(snippets[j.url]).slice(0, 260).replace(/\s+/g, " ") : "";
    const top = (j.ai_matches || [])[0];
    return `[${n + 1}] ${j.company} — ${j.title} | stack: ${(j.stack || []).join(", ") || "?"} | sinds ${j.first_seen || "?"} | ${j.url}${top ? ` | beste match ${top.score}%` : ""}${snip}`;
  }).join("\n");

  const system = `Je bent de JobRadar-assistent van IT-consultancy Branch. Je beantwoordt vragen over de live vacaturedatabase (dagelijks gescand van Vlaamse bedrijfssites plus handmatige klantopdrachten).
Regels: antwoord in het Nederlands, bondig en concreet. Verwijs naar vacatures als "Bedrijf — Titel" met de URL erbij. Baseer je UITSLUITEND op de catalogus hieronder; verzin niets. Als de catalogus onvoldoende detail bevat (bv. certificaten die alleen in de volledige vacaturetekst staan en waar geen tekstfragment beschikbaar is), zeg dat eerlijk en geef het beste alternatief (bv. vacatures met de juiste stack). Vandaag is ${new Date().toISOString().slice(0, 10)}.

CATALOGUS (${chosen.length} meest relevante van ${jobs.length} open vacatures):
${catalog}`;

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": env.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({ model: "claude-sonnet-4-6", max_tokens: 1200, system, messages }),
  });
  const rdata = await resp.json().catch(() => ({}));
  if (!resp.ok) return json({ error: "AI-fout: " + ((rdata.error || {}).message || resp.status) }, 502);
  const answer = ((rdata.content || [])[0] || {}).text || "";
  return json({ answer, considered: chosen.length, total: jobs.length });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}
