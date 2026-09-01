import assert from "node:assert/strict";
import test from "node:test";

import {
  isMeasurable,
  isVerified,
  layerSummary,
  quantitiesCsv,
  totalsByDesignation,
  unresolvedQuantities,
  verifiedQuantities,
} from "../domain/report.js";
import type { AnalysisReport, Confidence, QuantityRow } from "../domain/types.js";

const conf: Confidence = { overall: 0.9 };

function row(partial: Partial<QuantityRow>): QuantityRow {
  return {
    designation: "AA1-B2-110",
    diameterMm: 110,
    horizontalM: 10,
    verticalM: null,
    totalM: 10,
    pipeCount: 1,
    physicalPipeIds: ["pp_1"],
    state: "HIGH_CONFIDENCE",
    reasons: [],
    confidence: conf,
    ...partial,
  };
}

function report(partial: Partial<AnalysisReport>): AnalysisReport {
  return {
    schema: "vvs-pipe/analysis/1",
    drawing: {
      file: "d.pdf",
      pdfSha256: "0".repeat(64),
      pages: [{ page: 0, width: 100, height: 100, rotation: 0 }],
      vectorObjectCount: 1,
      textSpanCount: 0,
      excludedAnnotationObjects: 0,
    },
    forensicsDigest: "abc",
    glyphs: [],
    designations: [],
    pipeCandidates: [],
    pipeRuns: [],
    physicalPipes: [],
    verticals: [],
    quantities: [],
    scale: [],
    diagnostics: { reconciliation: { ok: true, problems: [] }, pages: [] },
    determinism: { canonicalDigest: "d", quantitiesDigest: "q", physicalPipesDigest: "p" },
    blind: { facitUsedDuringDetection: false, mode: "blind" },
    ...partial,
  };
}

test("a row with an unresolved state is never verified", () => {
  assert.equal(isVerified(row({})), true);
  assert.equal(isVerified(row({ state: "AMBIGUOUS" })), false);
  assert.equal(isVerified(row({ state: "INSUFFICIENT" })), false);
  assert.equal(isVerified(row({ designation: null })), false);
  assert.equal(isVerified(row({ totalM: null })), false);
  assert.equal(isVerified(row({ reasons: ["VERTICAL_HEIGHT_UNKNOWN"] })), false);
});

test("verified and unresolved rows partition the quantity list", () => {
  const rows = [row({}), row({ state: "AMBIGUOUS" }), row({ designation: null })];
  const r = report({ quantities: rows });
  assert.equal(verifiedQuantities(r).length, 1);
  assert.equal(unresolvedQuantities(r).length, 2);
  assert.equal(verifiedQuantities(r).length + unresolvedQuantities(r).length, rows.length);
});

test("totals fold the size rows of one designation together", () => {
  const totals = totalsByDesignation([
    row({ diameterMm: 110, totalM: 10, horizontalM: 10 }),
    row({ diameterMm: 160, totalM: 5, horizontalM: 5 }),
    row({ designation: null, totalM: 2, horizontalM: 2 }),
  ]);
  assert.equal(totals.get("AA1-B2-110")?.totalM, 15);
  assert.equal(totals.get("AA1-B2-110")?.pipeCount, 2);
  assert.equal(totals.get("(unresolved)")?.totalM, 2);
});

test("a report is only measurable when every page resolved its scale", () => {
  assert.equal(isMeasurable(report({ scale: [] })), false);
  assert.equal(
    isMeasurable(
      report({
        scale: [
          { state: "RESOLVED", metresPerPoint: 0.017, ratioDenominator: 50, sources: [], reasons: [] },
        ],
      }),
    ),
    true,
  );
  assert.equal(
    isMeasurable(
      report({
        scale: [
          { state: "RESOLVED", metresPerPoint: 0.017, ratioDenominator: 50, sources: [], reasons: [] },
          { state: "SCALE_UNKNOWN", metresPerPoint: null, ratioDenominator: null, sources: [], reasons: [] },
        ],
      }),
    ),
    false,
  );
});

test("csv escapes separators inside reasons", () => {
  const csv = quantitiesCsv([row({ reasons: ["A", "B"] })]);
  const lines = csv.trim().split("\n");
  assert.equal(lines.length, 2);
  assert.ok(lines[1]?.includes("A;B"));
  assert.ok(csv.startsWith("designation,diameterMm"));
});

test("layer summary counts what each toggle would show", () => {
  const summary = layerSummary(
    report({
      glyphs: [
        {
          glyphId: "g1",
          page: 0,
          bbox: [0, 0, 1, 1],
          character: null,
          alternatives: [],
          confidence: 0,
          state: "UNRESOLVED",
          reasons: ["UNRESOLVED_GLYPH"],
          sourceObjectIds: [],
          provenance: { stage: "glyph", rule: "r", sourceObjectIds: [], inputs: [], notes: [] },
        },
      ],
      verticals: [
        {
          verticalId: "v1",
          page: 0,
          point: [0, 0],
          attachedRunIds: [],
          fromElevationM: null,
          toElevationM: null,
          lengthM: null,
          state: "INSUFFICIENT",
          reasons: ["VERTICAL_HEIGHT_UNKNOWN"],
        },
      ],
    }),
  );
  assert.equal(summary.glyphs, 1);
  assert.equal(summary.unresolvedGlyphs, 1);
  assert.equal(summary.unresolvedVerticals, 1);
});
