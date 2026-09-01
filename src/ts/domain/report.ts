/**
 * Domain helpers over an analysis report.
 *
 * The rules the engine enforces are re-asserted here rather than assumed, so
 * the UI can never present a number the engine did not stand behind:
 * `verifiedQuantities` drops every row whose state or scale leaves it
 * unverified, and `layerSummary` counts what each toggle would show.
 */

import type {
  AnalysisReport,
  IdentityState,
  PhysicalPipe,
  QuantityRow,
} from "./types.js";

export const VERIFIED_STATES: ReadonlySet<IdentityState> = new Set([
  "CONFIRMED",
  "HIGH_CONFIDENCE",
]);

export interface QuantityTotals {
  readonly horizontalM: number;
  readonly verticalM: number;
  readonly totalM: number;
  readonly pipeCount: number;
}

export function isVerified(row: QuantityRow): boolean {
  return (
    VERIFIED_STATES.has(row.state) &&
    row.designation !== null &&
    row.totalM !== null &&
    row.reasons.length === 0
  );
}

export function verifiedQuantities(report: AnalysisReport): readonly QuantityRow[] {
  return report.quantities.filter(isVerified);
}

export function unresolvedQuantities(report: AnalysisReport): readonly QuantityRow[] {
  return report.quantities.filter((row) => !isVerified(row));
}

/** Totals per designation, folding the size rows together. */
export function totalsByDesignation(
  rows: readonly QuantityRow[],
): ReadonlyMap<string, QuantityTotals> {
  const out = new Map<string, QuantityTotals>();
  for (const row of rows) {
    const key = row.designation ?? "(unresolved)";
    const prev = out.get(key) ?? { horizontalM: 0, verticalM: 0, totalM: 0, pipeCount: 0 };
    out.set(key, {
      horizontalM: prev.horizontalM + (row.horizontalM ?? 0),
      verticalM: prev.verticalM + (row.verticalM ?? 0),
      totalM: prev.totalM + (row.totalM ?? 0),
      pipeCount: prev.pipeCount + row.pipeCount,
    });
  }
  return out;
}

export function pipesByState(
  report: AnalysisReport,
): ReadonlyMap<IdentityState, readonly PhysicalPipe[]> {
  const out = new Map<IdentityState, PhysicalPipe[]>();
  for (const pipe of report.physicalPipes) {
    const bucket = out.get(pipe.identityState) ?? [];
    bucket.push(pipe);
    out.set(pipe.identityState, bucket);
  }
  return out;
}

export interface LayerSummary {
  readonly glyphs: number;
  readonly designations: number;
  readonly legendEntries: number;
  readonly pipeCandidates: number;
  readonly pipeRuns: number;
  readonly physicalPipes: number;
  readonly verticals: number;
  readonly unresolvedVerticals: number;
  readonly unresolvedGlyphs: number;
}

export function layerSummary(report: AnalysisReport): LayerSummary {
  return {
    glyphs: report.glyphs.length,
    designations: report.designations.filter((d) => !d.isLegend).length,
    legendEntries: report.designations.filter((d) => d.isLegend).length,
    pipeCandidates: report.pipeCandidates.length,
    pipeRuns: report.pipeRuns.length,
    physicalPipes: report.physicalPipes.length,
    verticals: report.verticals.length,
    unresolvedVerticals: report.verticals.filter((v) => v.lengthM === null).length,
    unresolvedGlyphs: report.glyphs.filter((g) => g.character === null).length,
  };
}

/** Scale is a property of a page; a report is only measurable if all pages resolved. */
export function isMeasurable(report: AnalysisReport): boolean {
  return report.scale.length > 0 && report.scale.every((s) => s.state === "RESOLVED");
}

export function quantitiesCsv(rows: readonly QuantityRow[]): string {
  const header = [
    "designation",
    "diameterMm",
    "horizontalM",
    "verticalM",
    "totalM",
    "pipeCount",
    "state",
    "reasons",
    "confidence",
  ];
  const lines = [header.join(",")];
  for (const row of rows) {
    lines.push(
      [
        row.designation ?? "",
        row.diameterMm ?? "",
        row.horizontalM ?? "",
        row.verticalM ?? "",
        row.totalM ?? "",
        row.pipeCount,
        row.state,
        row.reasons.join(";"),
        row.confidence.overall,
      ]
        .map((cell) => {
          const text = String(cell);
          return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
        })
        .join(","),
    );
  }
  return `${lines.join("\n")}\n`;
}
