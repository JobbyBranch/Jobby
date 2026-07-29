const KEY = "v1";

async function readDoc(env) {
  const raw = await env.WORKFLOW_KV.get(KEY);
  const doc = raw ? JSON.parse(raw) : {};
  doc.interests = doc.interests || {};
  doc.claims = doc.claims || {};
  return doc;
}

export async function onRequestGet({ env }) {
  const doc = await readDoc(env);
  return new Response(JSON.stringify(doc), {
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

export async function onRequestPost({ request, env }) {
  let body;
  try {
    body = await request.json();
  } catch (e) {
    return new Response(JSON.stringify({ error: "invalid json" }), { status: 400 });
  }
  const doc = await readDoc(env);
  const at = new Date().toISOString();

  if (body.action === "interest" && body.jobUrl != null && body.row != null) {
    const k = `${body.jobUrl}|${body.row}`;
    if (body.status === "yes" || body.status === "no") {
      doc.interests[k] = { status: body.status, by: String(body.by || "?").slice(0, 40), at };
    } else {
      delete doc.interests[k];
    }
  } else if (body.action === "claim" && body.company) {
    const k = String(body.company).slice(0, 120);
    if (body.owner) {
      doc.claims[k] = { owner: String(body.owner).slice(0, 40), at };
    } else {
      delete doc.claims[k];
    }
  } else {
    return new Response(JSON.stringify({ error: "unknown action" }), { status: 400 });
  }

  await env.WORKFLOW_KV.put(KEY, JSON.stringify(doc));
  return new Response(JSON.stringify(doc), {
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}
