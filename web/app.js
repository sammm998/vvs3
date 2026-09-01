// Deployment UI.
//
// Every figure on screen is read straight out of the engine's report.  Nothing
// is recomputed here and nothing is rounded into a friendlier shape: where the
// engine says it could not tell, the interface says so too, because a take-off
// that quietly presents a guess as a measurement is worse than one that admits
// the gap.

const $ = (id) => document.getElementById(id);
const show = (id, on = true) => { $(id).hidden = !on; };

const dash = "—";
const num = (v, digits = 0) =>
  v === null || v === undefined ? dash : Number(v).toFixed(digits);
const metres = (v) => (v === null || v === undefined ? dash : `${Number(v).toFixed(2)} m`);
const pct = (v) => (v === null || v === undefined ? dash : `${Math.round(Number(v) * 100)}%`);
const esc = (s) =>
  String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---------------------------------------------------------------- rendering

function table(headers, rows, empty = "Nothing to show.") {
  if (!rows.length) return `<p class="empty">${esc(empty)}</p>`;
  const head = headers.map((h) => `<th>${esc(h.label ?? h)}</th>`).join("");
  const body = rows
    .map((r) => {
      const cls = r.className ? ` class="${r.className}"` : "";
      const cells = (r.cells ?? r)
        .map((c, i) => {
          const kind = headers[i]?.kind ? ` class="${headers[i].kind}"` : "";
          return `<td${kind}>${c}</td>`;
        })
        .join("");
      return `<tr${cls}>${cells}</tr>`;
    })
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

const kv = (pairs) =>
  `<dl>${pairs
    .map(([k, v]) => `<div class="kv"><dt>${esc(k)}</dt><dd>${v}</dd></div>`)
    .join("")}</dl>`;

function meter(label, value) {
  if (value === null || value === undefined) {
    return `<div class="meter"><div class="meter-top"><span>${esc(label)}</span><span>${dash}</span></div></div>`;
  }
  const v = Math.max(0, Math.min(1, Number(value)));
  const tone = v >= 0.7 ? "" : v >= 0.4 ? " is-warn" : " is-bad";
  return `
    <div class="meter">
      <div class="meter-top"><span>${esc(label)}</span><span>${pct(v)}</span></div>
      <div class="meter-track"><div class="meter-fill${tone}" style="width:${v * 100}%"></div></div>
    </div>`;
}

// ------------------------------------------------------------------ verdict

// The three states mean different things and must not be blurred together:
// INVALID is a broken engine invariant, INCOMPLETE is a drawing that did not
// supply what measurement needed.
const VERDICTS = {
  VALID: {
    tone: "VALID",
    title: "Take-off complete",
    body: "Every metre reconciles, and the scale was established from the drawing itself.",
  },
  INCOMPLETE: {
    tone: "INCOMPLETE",
    title: "Take-off incomplete",
    body:
      "The internal checks all pass, but something the drawing did not supply stops part of it " +
      "from being measured. The quantities below are the part that could be established.",
  },
  INVALID: {
    tone: "INVALID",
    title: "Take-off not usable",
    body:
      "An internal check failed, which means a length may be counted twice or not at all. " +
      "The numbers are still shown so the defect is visible, but they must not be relied on.",
  },
};

function renderVerdict(report) {
  const status = report.analysisStatus ?? "INVALID";
  const v = VERDICTS[status] ?? VERDICTS.INVALID;
  const problems = report.diagnostics?.reconciliation?.problems ?? [];
  const detail = problems.length
    ? `<p class="note">${problems.map(esc).join(" · ")}</p>`
    : "";
  $("verdict").className = `verdict verdict-${v.tone}`;
  $("verdict").innerHTML = `
    <div>
      <h2>${esc(v.title)}</h2>
      <p>${esc(v.body)}</p>
      ${detail}
    </div>`;
}

// -------------------------------------------------------------------- scale

const SCALE_TONE = {
  SCALE_CONFIRMED: ["pill-ok", "Two independent sources agree."],
  RESOLVED: ["pill-ok", "One source, uncontradicted."],
  SCALE_CONFLICT: ["pill-bad", "Sources disagree, so no scale is chosen."],
  SCALE_AMBIGUOUS: ["pill-bad", "Sources disagree, so no scale is chosen."],
  SCALE_UNKNOWN: ["pill-warn", "The drawing states no scale this engine can read."],
};

function renderScale(report) {
  $("scale").innerHTML = report.scale
    .map((s) => {
      const [tone, why] = SCALE_TONE[s.state] ?? ["pill-quiet", ""];
      const ratio = s.ratioDenominator ? `1:${num(s.ratioDenominator)}` : dash;
      const notes = (s.provenance?.notes ?? [])
        .map((n) => `<li>${esc(n)}</li>`)
        .join("");
      return `
        <div class="tier-top">
          <span class="pill ${tone}">${esc(s.state.replace("SCALE_", ""))}</span>
          <span class="mono">${esc(ratio)}</span>
        </div>
        <p class="tier-why">${esc(why)}</p>
        ${notes ? `<ul class="stage-list">${notes}</ul>` : ""}`;
    })
    .join("");
}

// ----------------------------------------------------------------- coverage

function renderCoverage(report) {
  const m = report.metrics ?? {};
  const c = m.detectionCoverage ?? {};
  const i = m.identity ?? {};
  $("coverage").innerHTML =
    kv([
      ["Vector objects read", `<span class="mono">${num(c.vectorObjects)}</span>`],
      ["Pipe runs", `<span class="mono">${num(c.pipeRuns)}</span>`],
      ["Physical pipes", `<span class="mono">${num(c.physicalPipes)}</span>`],
      ["Characters resolved", `<span class="mono">${num(i.glyphs - i.unresolvedGlyphs)} / ${num(i.glyphs)}</span>`],
      ["Still ambiguous", `<span class="mono">${num(i.ambiguousEntities)}</span>`],
    ]) +
    meter("Pipes carrying a designation", c.namedFraction) +
    meter("Pipes that could be measured", c.measuredFraction) +
    `<p class="note">A pipe with no designation is still a real pipe and is still measured; it is
     listed unnamed rather than attached to a label no evidence supports.</p>`;
}

// -------------------------------------------------------------------- tiers

const TIER_COPY = {
  TEXT_ONLY: "Read, and not a pipe designation — room names, notes, the title block.",
  DESIGNATION_CANDIDATE: "Spelled like a designation, but no pipe pointed back at it.",
  CONFIRMED_DESIGNATION: "A pipe found from geometry accepted this label.",
};

function renderTiers(report) {
  const tiers = report.metrics?.designationTiers ?? {};
  $("tiers").innerHTML = ["CONFIRMED_DESIGNATION", "DESIGNATION_CANDIDATE", "TEXT_ONLY"]
    .map(
      (name) => `
      <div class="tier">
        <div class="tier-top">
          <span class="tier-name">${esc(name.replace(/_/g, " ").toLowerCase())}</span>
          <span class="tier-count">${num(tiers[name])}</span>
        </div>
        <p class="tier-why">${esc(TIER_COPY[name])}</p>
      </div>`,
    )
    .join("");
}

function renderConfidence(report) {
  const c = report.metrics?.confidence ?? {};
  $("confidence").innerHTML =
    meter("Geometry", c.geometry) +
    meter("Association", c.association) +
    meter("Designation reading", c.designation) +
    meter("Measurement", c.measurement) +
    `<p class="note">Reported separately on purpose. A run can have sound geometry and no scale,
     or a perfect scale and unreadable labels; one averaged score would hide exactly that.</p>`;
}

// --------------------------------------------------------------- quantities

function renderQuantities(report) {
  const rows = report.quantities.map((q) => ({
    className: q.designation ? "" : "unnamed",
    cells: [
      q.designation ? `<span class="mono">${esc(q.designation)}</span>` : "unnamed",
      num(q.diameterMm, 1),
      metres(q.horizontalM),
      q.verticalM === null ? dash : metres(q.verticalM),
      `<b>${metres(q.totalM)}</b>`,
      num(q.pipeCount),
      esc(q.state),
      q.reasons.length ? esc(q.reasons.join(", ")) : dash,
    ],
  }));
  const named = report.quantities.filter((q) => q.designation).length;
  $("quantity-note").textContent = `${named} named, ${report.quantities.length - named} unnamed`;
  $("quantities").innerHTML = table(
    [
      "Designation",
      { label: "DN", kind: "num" },
      { label: "Horizontal", kind: "num" },
      { label: "Vertical", kind: "num" },
      { label: "Total", kind: "num" },
      { label: "Pipes", kind: "num" },
      "State",
      { label: "Reasons", kind: "reasons" },
    ],
    rows,
    "No pipework was found on this drawing.",
  );
}

function renderVerticals(report) {
  $("verticals").innerHTML = table(
    ["From", "To", { label: "Length", kind: "num" }, "State"],
    report.verticals.map((v) => [
      num(v.fromElevationM, 3),
      num(v.toElevationM, 3),
      v.lengthM === null ? "unknown" : metres(v.lengthM),
      esc(v.state),
    ]),
    "No vertical drops were identified.",
  );
}

function renderProvenance(report, id, job) {
  $("provenance").innerHTML = kv([
    ["File", esc(job.fileName ?? report.drawing.file)],
    ["PDF SHA-256", `<span class="mono">${esc(report.drawing.pdfSha256.slice(0, 16))}…</span>`],
    ["Result digest", `<span class="mono">${esc(report.determinism.canonicalDigest.slice(0, 16))}…</span>`],
    [
      "Ground truth used",
      report.blind.facitUsedDuringDetection
        ? '<span class="pill pill-bad">YES</span>'
        : '<span class="pill pill-ok">no</span>',
    ],
    ["Reconciliation", report.diagnostics.reconciliation.ok
      ? '<span class="pill pill-ok">OK</span>'
      : '<span class="pill pill-bad">FAILED</span>'],
    ["Job", `<span class="mono">${esc(id)}</span>`],
  ]);
}

// ------------------------------------------------------------------- flow

let polling = null;

async function upload(file) {
  if (polling) clearTimeout(polling);
  show("landing", false);
  show("results", false);
  show("failure", false);
  show("working", true);
  $("working-file").textContent = file.name;
  $("working-stage").textContent = "Uploading";
  $("stage-list").innerHTML = "";

  let job;
  try {
    const res = await fetch(`/api/jobs?name=${encodeURIComponent(file.name)}`, {
      method: "POST",
      body: await file.arrayBuffer(),
    });
    job = await res.json();
    if (!res.ok) throw new Error(job.error ?? res.statusText);
  } catch (err) {
    return fail(`The upload was rejected: ${err.message}`);
  }
  poll(job.id);
}

function fail(detail) {
  show("working", false);
  show("failure", true);
  $("failure-detail").textContent = detail;
}

async function poll(id) {
  let job;
  try {
    job = await (await fetch(`/api/jobs/${id}`)).json();
  } catch {
    // A dropped request is not a failed analysis; the work continues in its own
    // process, so keep asking rather than reporting a failure that did not
    // happen.
    polling = setTimeout(() => poll(id), 3000);
    return;
  }

  const stages = job.progress ?? [];
  $("working-stage").textContent = stages.length ? stages.at(-1).stage : "Queued";
  $("working-elapsed").textContent = job.elapsedSeconds ? `${job.elapsedSeconds}s` : "";
  $("stage-list").innerHTML = stages
    .map((p) => `<li>${esc(p.stage)}<time>${esc((p.at ?? "").slice(11, 19))}</time></li>`)
    .join("");

  if (job.state === "succeeded") return present(id, job);
  if (job.state === "failed") return fail(job.error ?? "The analysis process stopped without saying why.");
  polling = setTimeout(() => poll(id), 1200);
}

async function present(id, job) {
  const report = await (await fetch(`/api/jobs/${id}/report`)).json();
  show("working", false);
  show("results", true);

  // Fit the sheet to the frame; a drawing opened at the viewer's default
  // zoom shows one corner of an A1 page and reads as an empty panel.
  $("viewer").src = `/api/jobs/${id}/artifacts/marked.pdf#view=Fit`;
  $("dl-pdf").href = `/api/jobs/${id}/artifacts/marked.pdf`;
  $("dl-csv").href = `/api/jobs/${id}/artifacts/quantities.csv`;
  $("dl-json").href = `/api/jobs/${id}/artifacts/analysis.json`;

  renderVerdict(report);
  renderScale(report);
  renderCoverage(report);
  renderTiers(report);
  renderConfidence(report);
  renderQuantities(report);
  renderVerticals(report);
  renderProvenance(report, id, job);
}

// ------------------------------------------------------------------ wiring

$("file").addEventListener("change", (e) => {
  const file = e.target.files?.[0];
  if (file) upload(file);
  e.target.value = "";
});

const zone = $("dropzone");
for (const type of ["dragenter", "dragover"]) {
  zone.addEventListener(type, (e) => {
    e.preventDefault();
    zone.classList.add("over");
  });
}
for (const type of ["dragleave", "drop"]) {
  zone.addEventListener(type, () => zone.classList.remove("over"));
}
zone.addEventListener("drop", (e) => {
  e.preventDefault();
  const file = e.dataTransfer?.files?.[0];
  if (file) upload(file);
});
zone.addEventListener("click", () => $("file").click());

// A broken engine and a broken route look identical from the browser, so the
// health endpoint's own answer is shown rather than left to be guessed at.
fetch("/healthz")
  .then((r) => r.json())
  .then((h) => {
    if (h.ok) return;
    $("engine-health").hidden = false;
    $("engine-health").className = "pill pill-bad";
    $("engine-health").textContent = "engine unavailable";
  })
  .catch(() => {});
