export async function onRequestGet({ env }) {
  const raw = await env.WORKFLOW_KV.get("hookly:index");
  return json({ recent: raw ? JSON.parse(raw) : [] });
}

export async function onRequestPost({ request, env }) {
  if (!env.ANTHROPIC_API_KEY) return json({ error: "ANTHROPIC_API_KEY ontbreekt" }, 500);
  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid json" }, 400); }
  const name = String(body.name || "").trim().slice(0, 80);
  const company = String(body.company || "").trim().slice(0, 80);
  const linkedin = String(body.linkedin || "").trim().slice(0, 200);
  if (!name || !company) return json({ error: "naam en bedrijf zijn verplicht" }, 400);

  const key = "hookly:" + (name + "|" + company).toLowerCase();
  if (!body.force) {
    const cached = await env.WORKFLOW_KV.get(key);
    if (cached) return json({ ...JSON.parse(cached), cached: true });
  }

  const system = `Je bent Hookly, de sales-intelligence-assistent van IT-consultancy Branch. Je onderzoekt een hiring manager grondig via webresearch en levert een beknopt Nederlands dossier waarmee een salespersoon een warm, persoonlijk gesprek kan starten (in plaats van cold calling).
Zoek breed: professionele achtergrond en carrière, publieke optredens (podcasts, conferenties, interviews, artikels), sportactiviteiten en uitslagen (marathons, wielrennen, triatlon), hobby's, muziek, verenigingen en goede doelen, publicaties en uitgesproken meningen (bv. over AI).
LET OP naamgenoten: verifieer dat gevonden info écht over de persoon bij dit bedrijf gaat; twijfel = zeg het.
Gebruik UITSLUITEND publieke informatie. Vermijd gevoelige persoonsgegevens (gezondheid, politieke/religieuze overtuiging, gezinssituatie).
Structuur exact:
## Profiel
(2-4 zinnen: rol, achtergrond, opvallends)
## Persoonlijke interesses & raakvlakken
(bullets met concrete vondsten, elk met korte bronvermelding)
## Gespreksopeners
(3-5 genummerde, direct bruikbare openingszinnen in het Nederlands, van sterk-persoonlijk naar veilig-professioneel)
## Bronnen
(lijst met URL's)
Als er weinig te vinden is: wees eerlijk en geef de beste professionele invalshoek.`;

  const resp = await fetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": env.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json" },
    body: JSON.stringify({
      model: "claude-sonnet-4-6", max_tokens: 2500, system,
      tools: [{ type: "web_search_20250305", name: "web_search", max_uses: 8 }],
      messages: [{ role: "user", content: `Onderzoek: ${name}, werkzaam bij ${company}.${linkedin ? ` LinkedIn (referentie): ${linkedin}.` : ""} Maak het Hookly-dossier.` }],
    }),
  });
  const rdata = await resp.json().catch(() => ({}));
  if (!resp.ok) return json({ error: "AI-fout: " + ((rdata.error || {}).message || resp.status) }, 502);
  const dossier = (rdata.content || []).filter((b) => b.type === "text").map((b) => b.text).join("\n").trim();
  if (!dossier) return json({ error: "leeg resultaat — probeer opnieuw" }, 502);

  const payload = { name, company, dossier, at: new Date().toISOString() };
  await env.WORKFLOW_KV.put(key, JSON.stringify(payload), { expirationTtl: 60 * 60 * 24 * 30 });
  const idxRaw = await env.WORKFLOW_KV.get("hookly:index");
  const idx = (idxRaw ? JSON.parse(idxRaw) : []).filter((x) => !(x.name === name && x.company === company));
  idx.unshift({ name, company, at: payload.at });
  await env.WORKFLOW_KV.put("hookly:index", JSON.stringify(idx.slice(0, 25)));
  return json(payload);
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });
}
