/**
 * Cross-language contract test.
 *
 * Drives the real Python engine through the service and validates the JSON it
 * produces against the TypeScript domain types.  If either side changes the
 * contract, this fails rather than the UI silently rendering `undefined`.
 */

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { layerSummary, verifiedQuantities } from "../domain/report.js";
import { AnalysisService } from "../api/service.js";
import { defaultPythonBin } from "../api/worker.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const PYTHON = defaultPythonBin(REPO_ROOT);

function buildFixtures(): string {
  const dir = mkdtempSync(path.join(tmpdir(), "vvs-fixtures-"));
  const result = spawnSync(PYTHON, ["-m", "tests.fixtures.make_drawings", dir], {
    cwd: REPO_ROOT,
    encoding: "utf8",
    env: { ...process.env, PYTHONPATH: path.join(REPO_ROOT, "src", "python") },
  });
  assert.equal(result.status, 0, result.stderr);
  return dir;
}

test(
  "the service runs the Python engine and returns a report matching the domain contract",
  { timeout: 600_000 },
  async () => {
    if (!existsSync(PYTHON) && PYTHON === "python3") {
      // No interpreter available in this environment; nothing to contract-test.
      return;
    }
    const fixtures = buildFixtures();
    const pdf = path.join(fixtures, "drawing_a_clean.pdf");
    assert.ok(existsSync(pdf));

    const storage = mkdtempSync(path.join(tmpdir(), "vvs-jobs-"));
    const service = new AnalysisService({
      repoRoot: REPO_ROOT,
      storageRoot: storage,
      pythonBin: PYTHON,
      forensics: true,
      timeoutMs: 540_000,
    });

    const submitted = await service.submit("drawing_a_clean.pdf", readFileSync(pdf));
    assert.equal(submitted.state, "queued");
    await service.waitForIdle();

    const job = await service.get(submitted.id);
    assert.ok(job);
    assert.equal(job.state, "succeeded", job.error ?? "");
    assert.ok(job.artifacts["analysis.json"]);
    assert.ok(job.artifacts["marked.pdf"]);
    assert.ok(job.artifacts["quantities.csv"]);
    assert.ok(job.progress.length > 0);

    const report = await service.report(submitted.id);
    assert.ok(report);
    assert.equal(report.schema, "vvs-pipe/analysis/1");
    assert.equal(report.blind.facitUsedDuringDetection, false);
    assert.equal(report.diagnostics.reconciliation.ok, true);
    assert.equal(report.drawing.pdfSha256.length, 64);

    const summary = layerSummary(report);
    assert.ok(summary.physicalPipes > 0);
    assert.ok(summary.designations > 0);
    assert.ok(summary.legendEntries > 0);

    // Every field the UI reads must actually be present and correctly typed.
    for (const pipe of report.physicalPipes) {
      assert.equal(typeof pipe.physicalPipeId, "string");
      assert.ok(Array.isArray(pipe.geometry));
      assert.equal(typeof pipe.identityState, "string");
      assert.equal(typeof pipe.confidence.overall, "number");
      assert.ok(pipe.totalLengthM === null || typeof pipe.totalLengthM === "number");
    }
    for (const row of report.quantities) {
      assert.ok(row.designation === null || typeof row.designation === "string");
      assert.equal(typeof row.pipeCount, "number");
      assert.ok(Array.isArray(row.reasons));
    }
    assert.ok(verifiedQuantities(report).length > 0);

    // Resubmitting the same bytes reuses the finished job rather than re-running.
    const again = await service.submit("drawing_a_clean.pdf", readFileSync(pdf));
    assert.equal(again.id, submitted.id);
    assert.equal(again.state, "succeeded");
  },
);
