/**
 * Browser UI.
 *
 * Deliberately dependency-free: it uploads a PDF, polls the job, then renders
 * the marked drawing beside the engine's own tables.  Every number shown is
 * accompanied by the engine's state and reasons - the UI never presents an
 * unverified quantity as a verified one, and never computes a quantity itself.
 */

import {
  isMeasurable,
  layerSummary,
  quantitiesCsv,
  totalsByDesignation,
  unresolvedQuantities,
  verifiedQuantities,
} from "../domain/report.js";
import type { AnalysisReport, Job } from "../domain/types.js";

type View = "marked" | "debug" | "original";

interface State {
  job: Job | null;
  report: AnalysisReport | null;
  view: View;
  layers: Record<string, boolean>;
}

const state: State = {
  job: null,
  report: null,
  view: "marked",
  layers: {
    designations: true,
    physicalPipes: true,
    verticals: true,
    unresolved: true,
    legend: false,
    glyphs: false,
  },
};

function $(id: string): HTMLElement {
  const el = document.getElementById(id);
  if (!el) throw new Error(`missing element #${id}`);
  return el;
}

function text(value: unknown): string {
  return value === null || value === undefined ? "-" : String(value);
}

function metres(value: number | null): string {
  return value === null ? "-" : `${value.toFixed(3)} m`;
}

async function upload(file: File): Promise<void> {
  setStatus(`uploading ${file.name} ...`);
  const res = await fetch(`/api/jobs?name=${encodeURIComponent(file.name)}`, {
    method: "POST",
    body: await file.arrayBuffer(),
  });
  // A Job carries its own `error` field, so the response cannot be discriminated
  // on that key; the HTTP status is the signal.
  const payload = (await res.json()) as Job & { readonly error?: string | null };
  if (!res.ok) {
    setStatus(`upload failed: ${payload.error ?? res.statusText}`);
    return;
  }
  state.job = payload;
  await poll(payload.id);
}

async function poll(id: string): Promise<void> {
  for (;;) {
    const res = await fetch(`/api/jobs/${id}`);
    const job = (await res.json()) as Job;
    state.job = job;
    setStatus(`${job.state}: ${job.progress.at(-1)?.stage ?? "queued"}`);
    renderProgress(job);
    if (job.state === "succeeded") {
      const reportRes = await fetch(`/api/jobs/${id}/report`);
      state.report = (await reportRes.json()) as AnalysisReport;
      render();
      return;
    }
    if (job.state === "failed") {
      setStatus(`failed: ${job.error ?? "unknown error"}`);
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

function setStatus(message: string): void {
  $("status").textContent = message;
}

function renderProgress(job: Job): void {
  $("progress").textContent = job.progress.map((p) => `• ${p.stage}`).join("\n");
}

function render(): void {
  const report = state.report;
  const job = state.job;
  if (!report || !job) return;

  const summary = layerSummary(report);
  $("summary").innerHTML = [
    ["file", report.drawing.file],
    ["sha256", report.drawing.pdfSha256.slice(0, 16)],
    ["vector objects", String(report.drawing.vectorObjectCount)],
    ["scale", report.scale.map((s) => s.state).join(", ")],
    ["designations", String(summary.designations)],
    ["legend entries", String(summary.legendEntries)],
    ["pipe runs", String(summary.pipeRuns)],
    ["physical pipes", String(summary.physicalPipes)],
    ["verticals", `${String(summary.verticals)} (${String(summary.unresolvedVerticals)} unknown)`],
    ["unresolved glyphs", String(summary.unresolvedGlyphs)],
    ["reconciled", report.diagnostics.reconciliation.ok ? "yes" : "NO"],
    ["digest", report.determinism.canonicalDigest.slice(0, 16)],
    ["facit used", report.blind.facitUsedDuringDetection ? "YES" : "no"],
  ]
    .map(([k, v]) => `<div class="kv"><span>${k}</span><strong>${text(v)}</strong></div>`)
    .join("");

  const viewer = $("viewer") as HTMLIFrameElement;
  const file = state.view === "original" ? "input.pdf" : `${state.view}.pdf`;
  viewer.src =
    state.view === "original"
      ? `/api/jobs/${job.id}/artifacts/marked.pdf`
      : `/api/jobs/${job.id}/artifacts/${file}`;

  const measurable = isMeasurable(report);
  $("measurable").textContent = measurable
    ? "scale resolved - quantities are measured"
    : "scale not resolved - lengths are reported in points only";

  const verified = verifiedQuantities(report);
  const unresolved = unresolvedQuantities(report);
  $("quantities").innerHTML = table(
    ["designation", "DN", "horizontal", "vertical", "total", "pipes", "state", "reasons"],
    [...verified, ...unresolved].map((row) => [
      text(row.designation),
      text(row.diameterMm),
      metres(row.horizontalM),
      metres(row.verticalM),
      metres(row.totalM),
      String(row.pipeCount),
      row.state,
      row.reasons.join(", ") || "-",
    ]),
  );

  const totals = totalsByDesignation(report.quantities);
  $("totals").innerHTML = table(
    ["designation", "total", "pipes"],
    [...totals.entries()].map(([name, t]) => [
      name,
      `${t.totalM.toFixed(3)} m`,
      String(t.pipeCount),
    ]),
  );

  const designations = report.designations.filter(
    (d) => (state.layers.legend ? true : !d.isLegend) && (d.isLegend ? state.layers.legend : true),
  );
  $("designations").innerHTML = table(
    ["designation", "role", "legend", "DN", "state", "confidence"],
    designations.map((d) => [
      d.designation,
      d.role,
      d.isLegend ? "yes" : "no",
      text(d.diameterMm),
      d.state,
      d.confidence.overall.toFixed(2),
    ]),
  );

  $("pipes").innerHTML = table(
    ["designation", "DN", "total", "state", "runs", "reasons"],
    report.physicalPipes
      .filter((p) => state.layers.unresolved || p.identityState !== "UNRESOLVED")
      .map((p) => [
        text(p.designation),
        text(p.diameterMm),
        metres(p.totalLengthM),
        p.identityState,
        String(p.pipeRunIds.length),
        p.reasons.join(", ") || "-",
      ]),
  );

  $("verticals").innerHTML = table(
    ["from", "to", "length", "state", "reasons"],
    report.verticals.map((v) => [
      text(v.fromElevationM),
      text(v.toElevationM),
      v.lengthM === null ? "UNKNOWN" : `${v.lengthM.toFixed(3)} m`,
      v.state,
      v.reasons.join(", ") || "-",
    ]),
  );

  const csvLink = $("download-csv") as HTMLAnchorElement;
  csvLink.href = URL.createObjectURL(
    new Blob([quantitiesCsv(report.quantities)], { type: "text/csv" }),
  );
  csvLink.download = `${report.drawing.file}-quantities.csv`;

  const jsonLink = $("download-json") as HTMLAnchorElement;
  jsonLink.href = `/api/jobs/${job.id}/artifacts/analysis.json`;
  const pdfLink = $("download-pdf") as HTMLAnchorElement;
  pdfLink.href = `/api/jobs/${job.id}/artifacts/marked.pdf`;
}

function table(headers: readonly string[], rows: readonly (readonly string[])[]): string {
  const head = headers.map((h) => `<th>${h}</th>`).join("");
  const body = rows
    .map((row) => `<tr>${row.map((cell) => `<td>${cell}</td>`).join("")}</tr>`)
    .join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

export function mount(): void {
  const input = $("file") as HTMLInputElement;
  input.addEventListener("change", () => {
    const file = input.files?.[0];
    if (file) void upload(file);
  });

  for (const view of ["marked", "debug", "original"] as const) {
    $(`view-${view}`).addEventListener("click", () => {
      state.view = view;
      render();
    });
  }
  for (const layer of Object.keys(state.layers)) {
    const box = document.getElementById(`layer-${layer}`);
    if (box instanceof HTMLInputElement) {
      box.checked = state.layers[layer] ?? false;
      box.addEventListener("change", () => {
        state.layers[layer] = box.checked;
        render();
      });
    }
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", mount);
}
