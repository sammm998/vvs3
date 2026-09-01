// Deployment UI: upload a drawing, watch the stages, read the take-off.
// Every number shown is the engine's own; nothing is computed here.

const $ = (id) => document.getElementById(id);
const text = (v) => (v === null || v === undefined ? "-" : String(v));
const metres = (v) => (v === null || v === undefined ? "-" : `${Number(v).toFixed(3)} m`);

function table(headers, rows) {
  const head = headers.map((h) => `<th>${h}</th>`).join("");
  const body = rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

async function upload(file) {
  $("status").textContent = `uploading ${file.name} ...`;
  const res = await fetch(`/api/jobs?name=${encodeURIComponent(file.name)}`, {
    method: "POST",
    body: await file.arrayBuffer(),
  });
  const job = await res.json();
  if (!res.ok) {
    $("status").textContent = `upload failed: ${job.error ?? res.statusText}`;
    return;
  }
  poll(job.id);
}

async function poll(id) {
  for (;;) {
    const job = await (await fetch(`/api/jobs/${id}`)).json();
    $("status").textContent = `${job.state}: ${job.progress.at(-1)?.stage ?? "queued"}`;
    $("progress").textContent = job.progress.map((p) => `• ${p.stage}`).join("\n");
    if (job.state === "succeeded") return show(id, job);
    if (job.state === "failed") {
      $("status").innerHTML = `<span class="warn">failed</span>`;
      $("progress").textContent = job.error ?? "unknown error";
      return;
    }
    await new Promise((r) => setTimeout(r, 1200));
  }
}

async function show(id, job) {
  const report = await (await fetch(`/api/jobs/${id}/report`)).json();
  $("viewer").src = `/api/jobs/${id}/artifacts/marked.pdf`;
  $("dl-pdf").href = `/api/jobs/${id}/artifacts/marked.pdf`;
  $("dl-csv").href = `/api/jobs/${id}/artifacts/quantities.csv`;
  $("dl-json").href = `/api/jobs/${id}/artifacts/analysis.json`;

  const scale = report.scale.map((s) => s.state + (s.ratioDenominator ? ` 1:${s.ratioDenominator}` : "")).join(", ");
  $("summary").innerHTML = [
    ["file", report.drawing.file],
    ["sha256", report.drawing.pdfSha256.slice(0, 16)],
    ["vector objects", report.drawing.vectorObjectCount],
    ["scale", scale],
    ["designations", report.designations.filter((d) => !d.isLegend).length],
    ["pipe runs", report.pipeRuns.length],
    ["physical pipes", report.physicalPipes.length],
    ["reconciled", report.diagnostics.reconciliation.ok ? "yes" : "NO"],
    ["facit used", report.blind.facitUsedDuringDetection ? "YES" : "no"],
    ["digest", report.determinism.canonicalDigest.slice(0, 16)],
  ]
    .map(([k, v]) => `<div class="kv"><span>${k}</span><strong>${text(v)}</strong></div>`)
    .join("");

  $("quantities").innerHTML = table(
    ["designation", "DN", "horizontal", "vertical", "total", "n", "state", "reasons"],
    report.quantities.map((q) => [
      text(q.designation),
      text(q.diameterMm),
      metres(q.horizontalM),
      metres(q.verticalM),
      metres(q.totalM),
      q.pipeCount,
      q.state,
      q.reasons.join(", ") || "-",
    ]),
  );

  $("verticals").innerHTML = table(
    ["from", "to", "length", "state", "reasons"],
    report.verticals.map((v) => [
      text(v.fromElevationM),
      text(v.toElevationM),
      v.lengthM === null ? "UNKNOWN" : `${Number(v.lengthM).toFixed(3)} m`,
      v.state,
      v.reasons.join(", ") || "-",
    ]),
  );
  $("status").textContent = `done — ${job.reconciled ? "reconciled" : "RECONCILIATION FAILED"}`;
}

$("file").addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  if (file) upload(file);
});
