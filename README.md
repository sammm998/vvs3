# Automatic vector-PDF pipe analysis

An open-world engine that reads technical VVS/VA piping drawings straight out
of clean PDFs and produces a quantity take-off, a marked drawing and a fully
auditable trail - with no human in the loop and no list of known designations
anywhere in the code.

```
CLEAN PDF -> forensics -> vector extraction -> glyph segmentation ->
glyph reconstruction -> designation discovery -> pipe geometry ->
dimension reconciliation -> leader/association -> topology -> pipe runs ->
vertical analysis -> measurement -> quantity aggregation ->
marked drawing -> report -> (optional, separate) facit comparison
```

## Why it is open-world

The engine never matches a designation against a catalogue.  It recognises
*characters* (against a bank rendered from the PDF base-14 fonts at run time),
assembles them into tokens, and scores each token for a role from the shape of
its token structure and the geometry around it.  A drawing whose codes have
never been seen before is handled by exactly the same code path.

`tests/python/test_d_open_world.py` enforces this two ways: it runs the same
engine over two drawings with disjoint code sets, and it parses every engine
module's AST to prove no executable string constant is shaped like a drawing
code.

## Layout

```
src/python/vvs_pipe/      the analysis engine (the blind pipeline)
  canonical.py            total ordering, quantisation, content-addressed ids
  states.py               identity states and machine-readable reason codes
  model.py                canonical intermediate representation
  geometry/               primitives + uniform-grid spatial index
  pdf_forensics/          forensic report, produced before any detection
  vector_extraction/      content stream -> canonical polylines
  glyph/                  segmentation, skeleton features, alphabet assignment
  text_reconstruction/    glyphs -> strings, native-span merge, token structure
  designations/           legend/title panels, open-world role scoring
  pipes/                  double-line pairing, centerline reconstruction
  topology/               graph, pipe runs, physical pipes
  dimensions/             label vs measured wall separation
  association/            designation <-> pipe, with evidence
  vertical/               riser + elevation inference
  measurement/            scale detection, quantity aggregation
  rendering/              marked and debug drawings
  validation/             reconciliation, order-independence harness
  evaluation/             POST-HOC ONLY - never imported by the pipeline
  pipeline.py, cli.py     orchestration and entry point

src/ts/                   API, domain and UI over the engine
  domain/                 types mirroring the engine's JSON + report helpers
  api/                    job queue, Python worker adapter, storage, HTTP
  ui/                     dependency-free browser UI
  test/                   domain, queue and cross-language contract tests

tests/fixtures/           deterministic CAD-like drawing generator (test only)
tests/python/             the A-V test suite
```

## Running it

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# generate the CAD-like test drawings
.venv/bin/python -m tests.fixtures.make_drawings artifacts/fixtures

# blind analysis with all intermediates exported
RUN_BLIND_TEST=1 RUN_FORENSICS=1 PYTHONPATH=src/python \
  .venv/bin/python -m vvs_pipe.cli analyse \
  artifacts/fixtures/drawing_a_clean.pdf --out artifacts/run --forensics

# post-hoc comparison - a separate step, never part of detection
PYTHONPATH=src/python .venv/bin/python -m vvs_pipe.cli analyse \
  artifacts/fixtures/drawing_a_clean.pdf --out artifacts/run \
  --ground-truth artifacts/fixtures/drawing_a_truth.json
```

Outputs: `forensics.json`, `analysis.json`, `marked.pdf`, `debug.pdf`,
`quantities.csv`, and a `forensics/` directory with every intermediate.

### The web app

Two front ends speak the same JSON contract:

```bash
python main.py                 # http://localhost:8080 - no build step
npm install && npm run serve   # http://localhost:8080 - typed TS layer
```

`main.py` is the **deployment entry point**: pure standard library plus the
engine, so a PaaS that detects Python (Railway/Railpack, Heroku, Fly, Render)
runs it with no Node build. `railpack.json` and `Procfile` both declare
`python main.py`, and the service honours `PORT`, binds `0.0.0.0` and exposes
`/healthz`. Analyses run on a single background worker, so an upload returns a
job immediately and a large sheet does not block the request.

`src/ts` is the typed domain/API/UI layer required by the architecture: the
same endpoints, with the report contract expressed as TypeScript types and
checked against real engine output by `src/ts/test/service.test.ts`.

Either way: upload a PDF, watch the stages, then read the marked drawing beside
the engine's tables and download the marked PDF, the CSV or the JSON report.

## Guarantees the tests enforce

| Guarantee | Test |
| --- | --- |
| No facit reachable from the pipeline's import closure | `test_q_blind_leakage.py` |
| No drawing code hardcoded in the engine | `test_d_open_world.py` |
| Previous take-off annotations cannot enter the geometry | `test_u_annotation_leakage.py` |
| Original = reversed = two seeded permutations, at every stage | `test_r_determinism.py` |
| Repeated runs are byte-identical | `test_r_determinism.py` |
| A metre is counted exactly once | `test_o_measurement.py` |
| Nothing is CONFIRMED without evidence | `test_v_end_to_end.py` |
| No scale means no metres | `test_l_dimensions_scale.py` |
| One elevation means VERTICAL_HEIGHT_UNKNOWN | `test_m_vertical.py` |
| Equally supported pipes give AMBIGUOUS, not a guess | `test_k_association.py` |

## Conservatism

The engine reports `UNKNOWN`, `AMBIGUOUS`, `NOT_MEASURABLE`, `SCALE_UNKNOWN`
or `UNRESOLVED_GLYPH` rather than inventing a value, and every non-confirmed
entity carries a machine-readable reason.  Confidence is decomposed into
geometry / text / association / topology / dimension / vertical, and the
overall figure is the *minimum* of the parts.

See `docs/DESIGN.md` for the algorithms and `docs/FINAL_REPORT.md` for the run
report and the honest list of limitations.
