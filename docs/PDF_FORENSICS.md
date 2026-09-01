# `pdf_forensics` — searching the PDF instead of guessing from a picture

A vector PDF of a VVS drawing already contains everything a take-off needs:
every stroke, every character outline, every transform.  This package treats
that file as the primary evidence and never as a picture.  It builds a
searchable representation of the whole document, asks thousands of local
geometric questions of it, and reports what it found together with the reason
it believes it — or says plainly that it does not know.

```
PDF
 └─ objects ─ glyphs ─ text ─ designation candidates
                                   │
     paths ─ segments ─ roles ─ leaders ─┐
                │                        │
        wall pairing ─ fragments ─ candidates ─ graph ─ runs ─ PHYSICAL PIPES
                                                              │
                                     scale ─ dimensions ─ measurement ─ quantities
                                                              │
                                    evidence graph ─ validation ─ marked drawing
```

## The three commands

```bash
# 1. the microscope: what is actually in this file
python -m pdf_forensics.inspect sheet.pdf
python -m pdf_forensics.inspect sheet.pdf --glyphs --dump artifacts/inspect

# 2. the whole analysis, with every intermediate written out
python -m pdf_forensics.analyze sheet.pdf --out artifacts/run --crops 8
python -m pdf_forensics.analyze sheet.pdf --why pipe:866e0b017872:0

# 3. one place on the sheet, in full detail
python -m pdf_forensics.inspect_region sheet.pdf --near 412 380 --radius 60 --detail
python -m pdf_forensics.inspect_region sheet.pdf --object seg:8717bc869bad:0 --crop out.png

# and the search engine the pipeline itself uses
python -m pdf_forensics.search sheet.pdf --text "S3-98"
python -m pdf_forensics.search sheet.pdf --kind path --line-width 0.35 --vertical
python -m pdf_forensics.search sheet.pdf --near 600 620 --radius 25 --json
python -m pdf_forensics.search sheet.pdf --leaders --dimensions
```

`--out` writes `analysis.json`, `quantities.csv`, `marked.pdf`, optional
`crops/`, and a `forensics/` directory holding every stage: objects, segments,
glyphs, text, roles, panels, leaders, designation candidates, pipe candidates,
runs, physical pipes, associations, risers, measurements and the evidence graph.

## Modules

| Module | What it answers |
| --- | --- |
| `canonical.py` | quantisation, content-addressed ids, canonical ordering |
| `model.py` | the intermediate representation and the state/reason vocabulary |
| `loader.py` | opens one clean PDF; refuses a facit, a marked copy, a spreadsheet |
| `objects.py` | every object in the file, with a conservation check against it |
| `paths.py` | paths reduced to straight segments and mechanical facts |
| `glyphs.py` | individual glyphs: from the text layer, and from lettering drawn as geometry |
| `text_reconstruction.py` | glyphs → strings by position; token structure |
| `spatial_index.py` | STRtree or uniform grid, identical answers from both |
| `geometry_search.py` | parallel, collinear, connected, continuing, crossing, vertical |
| `neighbourhood.py` | `inspect_neighbourhood` — everything around an object |
| `designation_search.py` | open-world candidates, and bidirectional association |
| `leader_search.py` | tracing the line that ties a label to a place |
| `fragment_search.py` | joining the pieces a CAD export split a pipe into |
| `pipes.py` | roles, panels, wall pairing, candidate centerlines |
| `topology.py` | nodes, edges, runs, physical pipes, the reconciliation gate |
| `dimensions.py` | label evidence and measured-wall evidence, kept apart |
| `scale.py` | ratio notes, scale bars, agreement or `SCALE_UNKNOWN` |
| `measurement.py` | elevations, risers, horizontal/vertical/total, aggregation |
| `evidence.py` | the evidence graph and `why()` |
| `validation.py` | gates, coverage, and the determinism harness |
| `render.py` | local crops and the marked drawing |
| `analyze.py` | the orchestrator, the workspace, the CLI |

## The rules the code keeps

**Open world.** Nothing anywhere holds a list of designations.  A candidate is
proposed from token *structure* — how many alphanumeric runs, joined how, with
digits where — plus repetition and geometry.  `test_d_open_world.py` parses
every module's AST and fails if any executable string constant is shaped like a
drawing code, and runs the same engine over two sheets whose code sets are
disjoint.

**A pipe is geometry, not a label.**  Physical pipes are built from wall pairs,
dash chains and the graph.  Their identity is the runs they are made of.  A
pipe with no designation is still a pipe, is still measured, and is reported as
unnamed — the count of pipes never depends on the text stage succeeding.

**Nothing is confirmed on one direction of evidence.**  Forward is
designation → leader → the leader's far end → the pipe there.  Backward is
pipe → its neighbourhood → the labels in it → the designation.  `CONFIRMED`
requires both; one direction is `AMBIGUOUS / ONE_DIRECTION_ONLY`; two
candidates with equal support name nothing at all.

**No metres without a scale.**  Scale comes from independent signals — ratio
notes (read across a text item's alternative readings, so one mis-read colon
cannot destroy a sheet) and scale bars.  Agreement gives `CONFIRMED`;
disagreement is `SCALE_CONFLICT`; nothing is `SCALE_UNKNOWN`, and then the
report carries points, not metres.

**Vertical is not invented.**  A riser contributes a length only when two
elevations can be read near it.  One elevation is `VERTICAL_HEIGHT_UNKNOWN`.

**A metre is counted once.**  Reconciliation checks that no two candidates
share a centerline, no run belongs to two pipes and no candidate belongs to two
runs.  It is a gate: failing it makes the analysis `INVALID`.

**Determinism.**  Every collection is sorted by content, every id is a hash of
content, and ties are refusals rather than coin flips.  The harness runs the
analysis over the original, the reversed and two seeded permutations of every
internal collection and requires identical digests.

**Blindness.**  Detection reads exactly one clean PDF.  The loader refuses a
spreadsheet or a file whose name says it carries an answer; annotations are
modelled for conservation but excluded from every geometry stage, and analysing
a sheet carrying a previous take-off gives byte-identical pipes and quantities
to analysing the clean one.

**No false 100 %.**  Coverage is reported as confirmed / ambiguous / unresolved
for designations, pipes, diameters and measurements, and unnamed pipes appear
in the take-off as unnamed rows rather than being dropped.

## What the acceptance run shows

Two generated CAD-like sheets — one lettered entirely as single-stroke geometry
with no text layer at all, one with a real text layer — analysed blind:

```
                       drawing A (1:50)      drawing B (1:100)
PDF objects            230                   240
glyphs                 148 (21 unresolved)   156 (16 unresolved)
text items             30                    30 (4 native)
designations           12 candidates         11 candidates
leaders                6                     5
pipe candidates        7                     5
pipe runs              6 (2 junctions)       5 (1 junction)
physical pipes         4, all named          4, all named
scale                  CONFIRMED 1:50        CONFIRMED 1:100
measurement            4 measurable          4 measurable
reconciliation         OK                    OK
elapsed                3.8 s                 3.7 s
```

Against each sheet's independently generated ground truth — read only after the
blind run, never before it — every quantity agrees exactly:

```
S1-P2-160  160  10.407 m   (truth 10.4069)      KV1-X7   63  17.639 m  (17.6389)
S1-P2-110  110  13.053 m   (truth 13.0528)      VV1-X7   50  14.111 m  (14.1111)
S1-P2-75    75   2.293 m + 2.650 vertical       VVC1-X7  32  10.583 m  (10.5833)
                 (truth 2.2931 + 2.65)          VS1-S13  40   6.703 + 2.700 (6.7028 + 2.7)
```

A 30-times larger sheet built from form XObjects (7 020 objects, 26 370
segments) analyses in 16 s, conserves total length exactly, and reports the
pipes it cannot name as unnamed rather than guessing.

## Honest limitations

* Character recognition of single-stroke CAD lettering matched against the
  base-14 faces is good but not perfect; on the test sheets a few characters
  keep a strong runner-up (`X`/`N`, `3`/`5`).  Every reading carries its
  alternatives, and the scale reader already uses them; the designation stage
  does not yet vote across repeated instances of the same code.
* Single-line pipes are only promoted when independent evidence points at them
  (a leader ending on the stroke).  A sheet that draws all its piping as single
  lines with no leaders will report few pipes — deliberately, rather than
  promoting every stroke.
* Dimension labels are recognised structurally (`DN110`, `Ø110`, `110x50`).  A
  bare number is never a diameter; where the walls are drawn to scale the
  measured bore is used and reported as `MEASURED_WALLS_ONLY` unless a label or
  the designation's own trailing number agrees with it.
* Wall pairing does not yet distinguish a pipe from a building wall of the same
  drawn separation; on architectural backgrounds this is the first thing to
  measure and extend (the roles table is where it belongs).
* Multi-page documents are modelled and searched per page; nothing yet follows
  a system across sheets.
