# AUTOMATIC VECTOR PIPE ANALYSIS — FINAL REPORT

Generated from `scripts/acceptance.sh`; every figure below is copied from that
run's artifacts, not written by hand.

---

## ⚠ FIRST, THE MOST IMPORTANT THING

**No real drawing was available.** At the start of this work the repository
`sammm998/vvs3` was completely empty — no commits, no code, no PDF, no Excel
facit. There is no `W-50-1-A-0024` in the project, and there never was.

The engine was therefore built from scratch and validated against **generated
CAD-like drawings** produced by a test-only generator (`tests/fixtures/`) that
imitates what a CAD export actually looks like:

* text drawn as single-stroke vector polylines with **no PDF text layer at all**
  (drawing A: 0 text objects, 823 line segments — every character among them);
* pipes as **true-scale double lines** with mitred corners;
* a legend repeating the pipe codes, a title block, a scale bar;
* riser symbols with elevation notes, one resolvable and one deliberately not;
* previous manual take-off stored as **PDF annotations** on a second variant.

The fixture's title block carries the string `W-50-1-A-0024` as a stand-in so
the sheet reads like the drawing the brief named. **Nothing here has been
validated against a genuine CAD export.** Point the engine at a real PDF and it
will run — `vvs-pipe analyse yours.pdf --out out/` — but the numbers below
describe synthetic sheets, and the limitations section says exactly which of
them a real drawing is likely to break.

---

## PDF

| | drawing_a_clean.pdf | drawing_b_clean.pdf |
| --- | --- | --- |
| sha256 | `53b913faab20b356…f058e26` | `9ab909529afd7653…921f010` |
| pages | 1 | 1 |
| vector objects | 230 | 210 |
| line segments | 823 | 575 |
| **text objects** | **0** | 4 |
| scale | 1:50 | 1:100 |

Drawing B additionally carries a real text layer over the same callouts (the
"searchable CAD PDF" pattern), which exercises the native-span path and the
span/glyph merge.

## DISCOVERED DESIGNATIONS

Never supplied to the engine; recovered from vector outlines.

**drawing_a** — 142 glyph candidates, 3 unresolved (the two riser symbols and
one scale-bar label, correctly refused):

| designation | glyphs | confidence | legend instance also found |
| --- | --- | --- | --- |
| `S1-P2-75` | 8 | 0.32 | yes |
| `S1-P2-110` | 9 | 0.32 | yes |
| `S1-P2-160` | 9 | 0.32 | yes |

**drawing_b** — 122 glyph candidates, 2 unresolved. Entirely different codes,
**no code change to the engine**:

| designation | glyphs | confidence | legend instance also found |
| --- | --- | --- | --- |
| `KV1-X7` | 6 | 1.00 | yes |
| `VV1-X7` | 6 | 1.00 | yes |
| `VVC1-X7` | 7 | 1.00 | yes |
| `VS1-S13` | 7 | 0.90 | yes |

Every code appears twice on its sheet — once in the legend, once as a callout —
and the engine separates them from evidence (panel containment, leader
attachment, adjacent geometry), not from position.

## PIPE DETECTION

| | drawing_a | drawing_b |
| --- | --- | --- |
| candidates accepted | 5 | 4 |
| of which double-line | 5 | 4 |
| single-line (no drawn width) | 0 | 0 |
| false positives | **0** | **0** |
| objects excluded (text, panels, frame, symbols, leaders) | 225 | 206 |
| graph nodes / edges | 8 / 7 | 8 / 5 |
| pipe runs | 5 | 4 |

Corner healing recovers the mitre a double-line offset loses: branch C's two
runs measure 290.0 + 230.0 pt against a drawn 520.0 pt — exact.

## PHYSICAL PIPES

**drawing_a** (4 pipes, 1 460.0 pt of centerline, reconciled exactly):

| designation | DN | runs | total | state |
| --- | --- | --- | --- | --- |
| `S1-P2-160` | 160.0 | 1 | 10.4069 m | HIGH_CONFIDENCE |
| `S1-P2-110` | 110.0 | 2 | 9.1722 m | AMBIGUOUS |
| `S1-P2-110` | 110.0 | 1 | 3.8806 m | HIGH_CONFIDENCE |
| `S1-P2-75` | 75.0 | 1 | 4.9431 m | HIGH_CONFIDENCE |

**drawing_b** (4 pipes, 1 390.0 pt, reconciled exactly):

| designation | DN | runs | total | state |
| --- | --- | --- | --- | --- |
| `KV1-X7` | 63.0 | 1 | 17.6389 m | HIGH_CONFIDENCE |
| `VV1-X7` | 50.0 | 1 | 14.1111 m | HIGH_CONFIDENCE |
| `VVC1-X7` | 32.0 | 1 | 10.5833 m | HIGH_CONFIDENCE |
| `VS1-S13` | 40.0 | 1 | 9.4028 m | HIGH_CONFIDENCE |

Sizes are reconciled between the label and the measured wall separation. On
drawing B the labels carry no plausible nominal size (`X7` = 7 mm is below any
real pipe) so the measurement supplies all four; on `VS1-S13` the label's `13`
contradicts the drawn 40.0 mm, and the engine reports the **measurement** with
`DIMENSION_CONFLICT` rather than silently preferring either.

## VERTICALS

| | drawing_a | drawing_b |
| --- | --- | --- |
| risers found | 2 | 1 |
| resolved | 1 (2.650 m, from `VG+2.800` and `VG+0.150`) | 1 (2.700 m) |
| unresolved | 1 | 0 |
| total vertical length | 2.650 m | 2.700 m |

The unresolved riser carries a single elevation note. The engine reports
`VERTICAL_HEIGHT_UNKNOWN`, does not invent a storey height, and downgrades the
pipe carrying it to `AMBIGUOUS` — which is why one `S1-P2-110` row above is not
HIGH_CONFIDENCE.

## QUANTITIES

**drawing_a**

| designation | diameter | horizontal | vertical | total | confidence | state |
| --- | --- | --- | --- | --- | --- | --- |
| S1-P2-110 | 110.0 | 9.1722 | – | 9.1722 | 0.20 | AMBIGUOUS (`VERTICAL_HEIGHT_UNKNOWN`) |
| S1-P2-110 | 110.0 | 3.8806 | – | 3.8806 | 0.46 | HIGH_CONFIDENCE |
| S1-P2-160 | 160.0 | 10.4069 | – | 10.4069 | 0.57 | HIGH_CONFIDENCE |
| S1-P2-75 | 75.0 | 2.2931 | 2.6500 | 4.9431 | 0.49 | HIGH_CONFIDENCE |

**drawing_b**

| designation | diameter | horizontal | vertical | total | confidence | state |
| --- | --- | --- | --- | --- | --- | --- |
| KV1-X7 | 63.0 | 17.6389 | – | 17.6389 | 0.65 | HIGH_CONFIDENCE |
| VS1-S13 | 40.0 | 6.7028 | 2.7000 | 9.4028 | 0.65 | HIGH_CONFIDENCE (`DIMENSION_CONFLICT`) |
| VV1-X7 | 50.0 | 14.1111 | – | 14.1111 | 0.65 | HIGH_CONFIDENCE |
| VVC1-X7 | 32.0 | 10.5833 | – | 10.5833 | 0.65 | HIGH_CONFIDENCE |

A size is split across two rows when part of it is verified and part is not;
the take-off shows the unverified metres rather than hiding them. Confidence is
the *minimum* of the decomposed parts, so it reflects the weakest link.

## MARKED DRAWING

* `artifacts/acceptance/drawing_a/marked.pdf` — original sheet intact, pipework
  highlighted by state, each pipe captioned with designation, DN, total length
  (split into horizontal + vertical where both exist) and state, plus a state
  legend and the detected scale.
* `artifacts/acceptance/drawing_a/debug.pdf` — glyph boxes, designation boxes,
  detected panels, centerlines, graph nodes, verticals, run ids and rejected
  single-line candidates.
* `artifacts/acceptance/drawing_a/forensics/` — 13 JSON files: raw vectors,
  glyph candidates, designation candidates, pipe candidates, centerlines,
  graph, runs, physical pipes, dimensions, associations, verticals,
  measurement segments, rejected geometry.

## DETERMINISM

Canonical digest of the full analysis, drawing A:

```
original            cd94f597c8e11f04ce618b18e4cccd6c671de0653dd9d9b312adddad26bb2e1a
reversed            cd94f597c8e11f04ce618b18e4cccd6c671de0653dd9d9b312adddad26bb2e1a
shuffled-20240917   cd94f597c8e11f04ce618b18e4cccd6c671de0653dd9d9b312adddad26bb2e1a
shuffled-811        cd94f597c8e11f04ce618b18e4cccd6c671de0653dd9d9b312adddad26bb2e1a
repeat run          cd94f597c8e11f04ce618b18e4cccd6c671de0653dd9d9b312adddad26bb2e1a
```

`original = repeat = reversed = permutation`, at the whole-pipeline level and
separately at glyph segmentation, text reconstruction, designation discovery,
pipe detection, graph, runs, scale detection, alphabet assignment and quantity
aggregation.

## BLIND STATUS

**FACIT USED DURING DETECTION = NO**, and this is a proven property rather than
a claim. `tests/python/test_q_blind_leakage.py` walks the transitive import
closure of `vvs_pipe.pipeline` and fails if it can reach `vvs_pipe.evaluation`,
the fixtures package, `openpyxl`, `xlrd` or `pandas`; it also asserts that
importing the pipeline does not pull a spreadsheet library into `sys.modules`,
that the CLI reaches the evaluator only lazily, and that running the comparison
leaves the analysis digest unchanged.

## POST-TEST

Only here is the facit opened.

| metric | drawing_a | drawing_b |
| --- | --- | --- |
| true positives | 3 | 4 |
| false positives | 0 | 0 |
| false negatives | 0 | 0 |
| precision / recall / F1 | 1.0 / 1.0 / 1.0 | 1.0 / 1.0 / 1.0 |
| designation coverage | 1.0 | 1.0 |
| pipe coverage | 1.0 | 1.0 |
| measurement accuracy | 1.0 | 1.0 |
| vertical accuracy | 1.0 | 1.0 |
| duplicate rate | 0.0 | 0.0 |
| unknown rate | 0.0 | 0.0 |
| ambiguous rate | 0.25 | 0.0 |

Length differences, every row, both sheets: **0.0000 m**. Diameters: exact.

That is a perfect score on drawings the engine has never been told anything
about — but they are drawings this repository generated. It is evidence the
pipeline is internally correct end to end; it is **not** evidence that it reads
real CAD output, and it must not be read as such.

## LIMITATIONS

Honest, and specific.

1. **No real drawing has ever been analysed.** Everything above is synthetic.
   This is the single largest caveat and it dominates all the others.
2. **Confusable characters depend on the drawing containing both.** The
   S/5 and 1/I confusions are resolved by the exclusive alphabet assignment
   *because both characters appear on the sheet*. A sheet containing `1` but no
   `I` is measurably harder; `scripts/stress_probe.py` reproduces exactly that.
   Held-out cross-typeface character accuracy is ≥ 80 % (measured on Times-Bold,
   Courier-Bold and Helvetica-Oblique, none of which are in the prototype bank).
3. **Single-linkage glyph clustering can chain.** On the dense probe sheet the
   `P` glyphs split into two clusters (201 + 99) whose merged representative
   scored above the reject threshold, leaving 303 of 2 636 glyphs unresolved.
   The fix is complete-linkage refinement inside each component
   (`glyph/alphabet.py::cluster_glyphs`); it is not implemented because the two
   reference drawings do not exercise it and changing it blind risks the
   behaviour that *is* verified.
4. **Pipework drawn as a closed contour is not detected.** Closed contours are
   excluded from pipe detection so a scale bar or symbol cannot pair its own
   opposite sides into a phantom pipe. A pipe drawn as one filled/closed outline
   is therefore missed, and reported only as excluded geometry.
5. **Dashed pipes are not re-joined.** The dash pattern is extracted and carried
   through, but a dashed wall arrives as many short strokes and no stage stitches
   them back into one wall.
6. **Slope is ignored.** A gravity drain's fall means its true length slightly
   exceeds its plan projection. The engine measures the plan projection plus any
   *annotated* vertical, and models no fall.
7. **Cross-sheet continuation is not modelled.** Pages are analysed
   independently; a riser continuing onto another sheet is two unrelated
   unresolved verticals.
8. **A legend without a box is not recognised.** Panel detection needs an
   axis-aligned bounding rectangle; a legend laid out without one would be read
   as callouts.
9. **Scale comes from a ratio note or a scale bar only.** Calibration from a
   dimension line, or from known building geometry, is not implemented.
10. **Touching glyphs would break segmentation.** Projection-profile splitting
    assumes characters do not overlap along the baseline — true of CAD stroke
    fonts, not of tight kerning or script faces.
11. **OCR is not used at all.** The specification permits it as a fallback; the
    engine instead reports `UNRESOLVED_GLYPH`. That is more conservative but it
    forgoes a genuine second opinion on hard glyphs.
12. **Association propagation is width-based.** Two different systems at the
    same DN meeting at a junction could cross-propagate a designation. The
    equal-distance rule marks such a run `AMBIGUOUS`, which contains the failure
    but does not eliminate it.
13. **Performance is adequate, not tuned.** 4 305 vector objects and 2 636
    glyphs take ~18 s, dominated by glyph rasterisation and clustering. A sheet
    with tens of thousands of glyphs would need the clustering work batched onto
    the GPU or a coarse-to-fine cascade.
14. **The recogniser's weights were fitted on characters.** They were tuned
    against rendered glyphs, and the held-out check uses typefaces outside the
    bank — but the tuning set was small, and no threshold anywhere was ever
    moved to improve a quantity against a facit.

## FINAL VERDICT

**PARTIALLY_READY**

Every acceptance criterion in the brief passes on the drawings available:
build, typecheck, 84 Python tests, 11 TypeScript tests, blind pipeline,
order-independence, forensics, glyph discovery, designation discovery, pipe
detection, topology, physical pipes, vertical analysis, measurement, marked
drawing, quantity report, no facit leakage, no silent guessing. The post-hoc
comparison is a perfect match on both sheets.

It is **not** `AUTOMATIC_PIPE_MEASUREMENT_READY`, for one reason that outweighs
all the passing tests: **the engine has never seen a real drawing.** A perfect
score against drawings this repository generated establishes that the pipeline
is internally consistent, deterministic and conservative — not that it reads
what a CAD package actually emits. Real exports bring clipped and transformed
content streams, hatch fills, xrefs, layer soup, dashed and closed pipework,
touching glyphs, sloped drains and legends without boxes, and limitations 2–12
name where those will bite first.

Supply one clean PDF and its facit and this verdict can be settled properly:
`vvs-pipe analyse yours.pdf --out out/ --ground-truth facit.xlsx` produces the
same report against real evidence.
