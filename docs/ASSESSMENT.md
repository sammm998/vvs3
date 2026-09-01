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

---

# Outcome

Same sheet, same blind run, after the corrections above.

| | before | after |
| --- | --- | --- |
| `ANALYSIS_STATUS` | (none) | **VALID** |
| reconciliation | FAILED — 24 shared centerlines, 66 runs in two pipes | **OK** |
| scale | `SCALE_UNKNOWN` | **RESOLVED, 1:50** |
| designations reported | 1 491 (= every text item) | 28 confirmed, 114 candidates, 1 333 text-only |
| pipe runs | 802 | 405 |
| physical pipes measured | 0 | 225 of 225 |
| physical pipes named | 0 | 42 |
| lettering | `S3-98-75` | `S3-R8-75` |

## What each fix actually was

**Reconciliation.** Not a tolerance and not a threshold: the engine addresses
entities by content but never enforced that identity as a set.  Duplicates
entered at three points - identical candidates, concentric candidates sharing a
midline, and coincident graph edges produced by splitting overlapping
candidates at tees - and a run's own address dropped its width, so two
different runs could collide on one id.  All four are now closed, and
reconciliation is a gate rather than a note.

**Scale.** The sheet's note reads `SKALA A1 (A3) 1:50 (1:100)`.  Two separate
things were wrong.  The colon is set as two zero-width vertical hairlines, and
the stacked-part merge tested horizontal alignment as a ratio of the narrower
part's width - undefined at zero - so it silently rejected every such pair and
the note read `1..50`.  Then, with the colon recovered, the note offers two
ratios, which is not a contradiction: it is one drawing issued at two sheet
sizes.  The sheet measures 841.0 x 594.1 mm, which is A1, and the note pairs
`A1 (A3)` with `1:50 (1:100)`, so the sheet selects its own ratio.

Fixing the colon also fixed the lettering: `S3-98` had been a misreading of
`S3-R8`.

**Designations.**  Emission was unconditional and confirmation needed no pipe.
Text now tops out at DESIGNATION_CANDIDATE on its own evidence; only the
association stage, working from geometry, can promote it, and only when the
label points at its pipe - by a leader, or by being set along the pipe's axis.
Proximity confirms nothing, because proximity is what puts a note beside
whatever runs past it.

**Pipe geometry.**  Detection took whatever the text stages left over, so the
architectural wall layer paired into 160 mm "pipes".  Roles are now classified
from layer signatures - never from layer *names* - and the discriminator that
does the work is connectivity: wall strokes never join end to end
(chainFraction 0.00) while pipe layers chain at 0.88.

## Blind run against the facit

Run blind, then compared - never the other way round.  The facit is the manual
Bluebeam take-off of the same sheet, nine rows.

Exact matches on designation *and* nominal size: `S1-P2-75`, `S3-P2-160`,
`S3-R8-75`, `S3-R8-110`, `S3-R8-160`.

Missed: `KV1-X31-16`, `KV2-X31-16`, `VV1-X31-16` - all three misread at the
character level, `K` as `X` and `V` as `Y` - and `S1-P2-110`, which is read
correctly four times but never associates.

**The most useful number in the whole comparison is a length.**  For system
S3-R8 the engine reports 30.1 m at DN 110 and 49.8 m at DN 75; the facit says
59.8 m and 21.3 m.  Individually both are badly wrong.  Together they are
79.9 m against 81.1 m - **1.2 m, or 1.5 %, apart**.

The geometry is right and the measurement is right.  What is wrong is which
size each run was attributed to: the two are very nearly swapped.  That places
the remaining error squarely in designation-to-run association, not in
detection, not in the scale, and not in measurement.

### The association work made recall better and precision worse

Stated plainly, because it is the one place where a change traded one thing for
another rather than simply fixing a defect:

| | before the association work | after |
| --- | --- | --- |
| exact matches | 4 | **5** |
| missed | 5 | **4** |
| reported and not in the facit | 6 | 10 |
| recall | 0.44 | **0.56** |
| precision | **0.40** | 0.33 |
| F1 | 0.42 | 0.42 |

The starting precision was high partly by abstention: the engine associated
almost nothing (17 named pipes of 215), so it was rarely wrong because it
rarely spoke.  An inline label - written on the pipe, no leader - could never
associate at all, which is how most of a real sheet is labelled, and that was a
defect rather than caution.  Fixing it named 42 pipes and found one more of the
facit's rows.

F1 lands where it started, at 0.42, with one more of the facit's rows found and
a quantity list that is now one row per designation and size rather than one
per measured width.

The false positives it costs are mostly not inventions.  They are *partial
readings of real systems*: `S3-R8` without its size line, `S1-P2` without its
size line.  The facit compares on (designation, size) pairs, so a partial
reading is scored exactly as harshly as a fabricated one.  The genuinely wrong
entries are four: two "ENL. PM" notes, a date, and one string of unresolved
characters.

## What is still wrong

* **Association still names only a fifth of the pipes** - 42 of 225.  The
  geometry is found and measured; most of it is reported unnamed.  That is the
  honest state, and it is the largest remaining gap between this and a manual
  take-off.
* **A label's size line often fails to resolve.**  Thirty-four instances of
  `S3-R8` on the sheet have their size written on a second line directly below,
  and that line reconstructs as a single unresolved glyph 9.96 x 9.12 pt.  The
  cause is now known exactly: the drawing brackets each stacked size line
  between two horizontal **rules**, 9.96 pt wide and 0.00 pt tall, and
  ``_merge_stacked_parts`` absorbs them into the characters between them.
  Fixing this would turn a whole class of partial readings into complete ones,
  and it is the largest single remaining error on the sheet.

  One approach was tried and rejected, which is worth recording because it
  looks right.  Requiring the overlap to cover the *wider* part as well as the
  narrower excludes a rule cleanly - a rule covers the character but extends
  far beyond it - but it also fragments real characters, whose stacked parts
  are routinely very different widths: an ``R``'s bowl against its leg, a
  ``G``'s bar against its arc.  Measured, it cost the whole alphabet: ``R``
  read as ``9`` again, the scale note's colon lost again, scale back to
  SCALE_CONFLICT, and F1 against the facit down from 0.42 to 0.16.  It is
  reverted, and ``tests/python/test_w_identity_and_promotion.py`` now pins the
  behaviour it broke.  Width alone cannot separate the two either: those rules
  are 1.5 cap heights wide and the drawing's own hyphens reach 1.8.  A rule has
  to be *recognised* - a flat stroke with a parallel partner bracketing content
  between them - rather than excluded by tightening this test.
* **K/X and V/Y are confused**, which is what loses `KV1-X31-16`,
  `KV2-X31-16` and `VV1-X31-16`.  The drawing's own font would settle it, but
  the embedded ISOCPEUR subset maps only `-.0124579ABELMNP`: the lettering was
  exported as outlines, so the characters that appear only in the lettering
  were never added to the subset.  The glyph program may still hold them, but
  with no cmap entry there is no way to know which glyph index is which
  character, and guessing is not available to this engine.  Those characters
  fall back to base-14 shapes.
* **Two labels on one line are read as one text item** - `VV1-X31 XY2-X31` -
  costing three more entries.
* **Four genuinely wrong confirmations remain**: two "ENL. PM" notes, a date,
  and one unresolved string.  Each has a leader that lands on a pipe, so the
  present evidence model cannot separate them from a designation.  A structural
  signal does separate them cleanly on this sheet - a real designation's token
  pattern recurs 32 to 38 times while a note's recurs twice - and it is
  recorded as evidence (`sharesItsStructureWith`), but it is deliberately *not*
  used as a gate: where the cut would have to fall to separate them here is a
  fact about this sheet, and a drawing carrying a single system would fail any
  such test.
* **386 of 4 789 glyphs are unresolved** (8 %), and some resolved characters
  are still wrong.
* **The role classifier's WALL and TEXT signatures are unreliable** on this
  sheet and deliberately excluded from the exclusion set; only REFERENCE_LINE,
  GRID, HATCH and panel roles gate detection today.
* **The 20 synthetic drawings A-T and gates G1-G16 are not built.**  Two
  fixture drawings and 107 tests stand in for them.
