# Architecture assessment

Blind run of the current engine against the real sheet `W501A0011` (clean PDF),
commit `c84ee72` plus the uncommitted embedded-font work.  Every number below is
read out of `artifacts/real/assess/analysis.json`, not estimated.

```
vectorObjects   30783
glyphs           4825   (345 unresolved)
textItems        1491
designations     1491        <-- identical to textItems
pipeCandidates    843
pipeRuns          802
physicalPipes     197
scale           SCALE_UNKNOWN   (metresPerPoint = null)
reconciliation  FAILED
                24 accepted candidates share a centerline
                66 runs belong to more than one physical pipe
elapsed         110.5 s
```

## 7. Why arbitrary text becomes a designation

Four independent defects, all of them consequences of the same inversion: the
engine decides what a designation is *before* it knows where any pipe is.

**7a. Emission is unconditional.**  `discover_designations` runs
`for t in items:` and appends a `Designation` for **every** text item with no
exit condition.  That is why `designations == textItems == 1491` exactly.  The
`role` field distinguishes them internally, but the report publishes the whole
list, so "1 379 designations" is really "1 379 pieces of text".

**7b. The pipe-designation role is decided from token structure alone.**
`_code_like` accepts any string with two or more alphanumeric runs joined by a
separator where at least one run contains a digit, and that alone scores 0.45.
On this sheet 153 items reach `PIPE_DESIGNATION`, among them:

```
2024-04-19            a date
ENL. PM-2             a note ("according to PM-2")
SF 9ITNINS W-50-1-4-0111.   a drawing cross-reference
B2-SDLV94NN4 1000X150 I4?I) a garbled duct schedule line
9D9S-NDMED?INS        text the glyph stage mis-read
```

**7c. Confirmation requires no pipe.**  Promotion to `CONFIRMED` is
`role_score >= 0.72`, and `role_score` is assembled purely from text-local
evidence: `has_leader` (any two-point stroke ending near the text),
`near_geometry` (any stroke within six cap heights), `repetition`.  Nothing in
that expression refers to a `PipeCandidate`, a `PipeRun` or a `PhysicalPipe`.
A note that happens to sit near linework is therefore confirmed.  Of the 57
CONFIRMED designations on this sheet, these are not designations at all:

```
ENL. PM-1     ENL. PM-2     M4TF9I4L + V49I4NTN9
EDLJ4N0F S4LLF9 DM FJ 4NN4T 4NSFS..      XX00-X00-000/X00      8-110
```

**7d. Text quality is not a gate.**  A glyph the classifier could not resolve is
excluded, but a glyph it resolved *wrongly* carries full confidence.  `S3-98` is
a mis-read of `S3-R8` and is the single most frequent "designation" on the
sheet (33 instances).  `1X��,-X�,` contains replacement characters and still
became a `PIPE_DESIGNATION`.  There is no per-character alternative set, so a
downstream stage cannot recover from a single wrong character.

## 8. Why only ~200 PhysicalPipes come out of 802 runs

`build_physical_pipes` groups runs by `(designation, diameter_mm)` and merges
members of a group whose centerlines touch within 1.5 pt.  Because only 153 of
1 491 text items are designations at all and few of those associate, **649 of
the 802 runs carry no designation**, so their group key is the single value
`("", -1.0)`.  Every unattributed run in the drawing therefore lands in one
group, and connected components inside that group fuse wherever endpoints
happen to coincide.

197 is consequently not a count of pipes.  It is a handful of large blobs of
unattributed geometry plus a tail of singletons.  The count is *small because
the designation is missing*, which is exactly backwards: a PhysicalPipe's
identity is currently derived from its label, so a pipe cannot exist until the
text stage has succeeded.

## 9. Why SCALE_UNKNOWN

`detect_scale` has exactly two hypotheses and both require text to be read
perfectly.

* `parse_ratio` matches the literal regex `1\s*:\s*(\d{1,5})`.  The sheet's note
  is `SKALA A1 (A3) 1:50 (1:100)`; the glyph stage reconstructs it as
  `J..50 (J..J00)` — `1` read as `J`, `:` as `.`.  No match.
* the scale-bar detector needs three or more congruent adjacent rectangles with
  numeric labels at the ends.  This sheet has no scale bar.

There is no third source (dimension annotations, sheet size plus title block,
structural grid module), no set of competing hypotheses, no distinction between
*conflict* and *unknown*, and no tolerance for a character that has plausible
alternatives.  Because `metres_per_point` is `None`, **every** PhysicalPipe is
stamped `SCALE_UNKNOWN` / `INSUFFICIENT` and the entire quantity output is void.
One unread colon destroys the measurement.

## 10. Why reconciliation fails

Both problems have one root cause: the engine has a content-addressed identity
model but never enforces it as a *set*.

* `24 accepted candidates share a centerline` — `PipeCandidate.canonical_key()`
  is `(page, direction-independent polyline, style)`.  Entries with identical
  keys are the same entity by the engine's own definition, yet both are kept and
  both enter the graph.  The drawing genuinely contains lines emitted twice.
* `66 runs belong to more than one physical pipe` — a consequence.
  `pipe_run_id = entity_id("run", (page, centerline))` is addressed on geometry
  alone, so two disjoint edge chains with identical coordinates receive the same
  id, and each of the two components containing one of them lists that id.

Every duplicate is a double-counted metre.  Separately, reconciliation is today
only *reported*: the pipeline emits quantities regardless of the outcome.  It
must be a gate.

## 11. Retain vs replace

**Retain** — sound, tested, and independent of the inversion:

| Component | Why |
| --- | --- |
| `canonical.py` | determinism foundation: quantisation, direction-independent keys, digests |
| `pdf_forensics/`, `vector_extraction/` | including the annotation-deletion fix that made marked and clean extraction byte-identical |
| `artwork.py` | grid-density logo suppression, 13 977 objects excluded in 0.2 s |
| `pipes/dashes.py` | per-pen gap estimation; per-system lengths land within ~1 % of facit |
| `glyph/features.py`, `thinning`, `prototypes.py` | feature extraction and embedded-font prototypes |
| `geometry/`, `topology/graph_build.py`, `topology/runs.py` | spatial index, graph, mutual-best chaining |
| `evaluation/facit.py`, fixtures, leakage test | post-hoc only, must stay out of the pipeline's import closure |

**Replace**:

| Component | Replaced by |
| --- | --- |
| `designations/discovery.py` confirmation logic | candidate generation only — it may propose, never confirm |
| `association/associate.py` | evidence-based association that runs *after* pipes exist and is the only thing that can confirm a designation |
| `topology/physical.py` grouping key | geometric/topological identity, independent of the label |
| `measurement/scale.py` | multi-hypothesis engine with SCALE_CONFIRMED / SCALE_CONFLICT / SCALE_UNKNOWN |
| output contract in `pipeline.py` | tiered text, coverage and confidence metrics instead of a flat designation list |

**Add**:

| Component | Purpose |
| --- | --- |
| `roles.py` | drawing-role classification for every vector object, so pipe geometry is found without consulting text |
| glyph alternatives | carry per-character candidate sets forward so scale and designation reading can recover from one wrong character |
| reconciliation gate | `ANALYSIS_STATUS = INVALID` on failure |

## Implementation order

1. Deduplicate content-addressed entities; make reconciliation a gate. (independent, unblocks measurement)
2. Drawing-role classification; pipe detection consumes roles, not text leftovers.
3. PhysicalPipe identity from geometry; designation attached afterwards.
4. Tiered text: TEXT_ONLY / DESIGNATION_CANDIDATE / CONFIRMED_DESIGNATION.
5. Multi-hypothesis scale.
6. Evidence records, coverage and confidence reporting.
7. Deployment worker and UI.
