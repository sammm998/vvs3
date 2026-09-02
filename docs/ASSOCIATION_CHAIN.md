# The association chain

The engine identifies a pipe the way the drawing does, and in one order only:

```
vector glyphs
  -> a complete designation, with its DN
    -> the vector leader the draughtsman drew
      -> that leader's endpoint
        -> geometry on a layer that carries pipework  (the "FE" layer)
          -> the run there, and so the physical pipe
```

Nothing else binds a label to a pipe.  In particular **proximity does not**:
distance is still measured and published, and it is never used.

## What was wrong

The association stage offered two routes to a binding.  One was the chain
above.  The other - "inline" - bound a label to a run because it sat close to
it, with orientation and size as corroboration, and it was a complete argument
in its own right.  On a plan sheet everything sits close to a line, so that
route promoted dates, `ENL. PM-1`, `ENL. PM-2`, drawing cross-references and
duct-schedule strings into pipe designations, and it could equally bind a real
code to the wrong pipe of two running side by side.

Three further defects starved the chain of the evidence it needed:

| Defect | Effect |
| --- | --- |
| A leader had to be a **single two-point object** | Real CAD leaders are a shoulder and a slant, drawn as a polyline or as several objects.  Most leaders on a sheet were therefore invisible, and the association stage fell back on the inline route. |
| The leader endpoint only had to land **near a run** | A tip landing on a wall, a door swing or a hatch counted the same as one landing on pipework.  There was no layer test at all. |
| `[] 0` - a PDF saying **solid** - was counted as a dash | The reference-line rule is "long, dashed and unconnected".  With the first term always true, whole layers of solid pipework scored as centre lines and were deleted before pipe detection saw them.  A drawing that puts its pipes on their own layer lost them entirely. |

## What it does now

**Leaders are traced** (`association/leaders.py`).  From a label, through
however many objects the export split the leader into, following angle
continuity, stopping at a fork, at a right-angle turn and at a heavier pen.
Two lines touching a label equally are not a leader: the drawing is then not
stating anything unambiguous.

**Endpoints are verified** (`association/attachment.py`).  The set of
pipe-carrying layers is *discovered* - layers ranked by how much accepted pipe
centerline they produced - and the tip must land on geometry of such a layer.
No layer name appears anywhere in the engine; a file with no layers reports the
gate as inactive rather than passing everything as though it had checked.

**Only the chain confirms** (`association/associate.py`).  Proximity produces a
*hint*, counted and published under `proximityHintsNotUsed`.  Propagation along
a width-matched connection still spreads a designation through one physical
pipe - that is topology, not proximity - and a propagated run is never
CONFIRMED.

**Promotion follows the chain** (`designations/promote.py`).  A text becomes a
CONFIRMED_DESIGNATION only when an association carrying `leaderTraced` reached
a physical pipe.  Alignment is published as evidence and confirms nothing.

## What every run reports

```
chain page 0   designations 8 -> with DN 7 -> vector leaders 4 -> verified attachments 3
               physical pipes 4, designated 3; pipe layers active W50-VVS-FE-S3;
               proximity hints not used 4
```

and, for every confirmed designation, the whole chain:

```json
{"glyphIds": ["gly_…", …], "designation": "S3-R8-110", "diameterMm": 110.0,
 "leaderId": "leader_…", "leaderObjectIds": ["obj_…", "obj_…"],
 "leaderTip": [430.0, 596.0], "feObjectId": "obj_…", "feLayer": "W50-VVS-FE-S3",
 "pipeRunId": "run_…", "physicalPipeId": "pp_…"}
```

Everything that did not complete the chain records the stage it stopped at
(`leader`, `attachment`, `physical_pipe`, `designation_dn`) and its reason, and
`--debug-crops` renders a local crop of each one with an `index.json` beside
them.

## Overlays

The two overlays answer different questions and are no longer mixed:

* `marked.pdf` - what was **measured**.  It highlights each physical pipe's own
  centerline, in place, on the `Pipe geometry` layer.  It draws no line between
  a label and a pipe.
* `debug.pdf` - **why**.  Traced leaders (`Leaders`), verified endpoints and
  chain captions (`Association`), glyphs, designations, runs, nodes, panels and
  rejected candidates, each on its own optional-content layer, so any of them
  can be switched off in a viewer.

## Running it

```bash
PYTHONPATH=src/python python -m vvs_pipe.cli analyse CLEAN.pdf \
    --out artifacts/run --forensics --debug-crops

# freeze the clean-only result before anything is compared to it
PYTHONPATH=src/python python scripts/freeze_clean_run.py freeze CLEAN.pdf --out artifacts/frozen
PYTHONPATH=src/python python scripts/freeze_clean_run.py check  CLEAN.pdf --out artifacts/frozen

# structural comparison against a previous pipeline's counts on the same sheet
python scripts/chain_regression_report.py artifacts/run --reference 77 77 68 64
```

The four reference numbers live in that script's command line.  The engine
never imports it, no threshold is derived from them, and
`tests/python/test_q_blind_leakage.py` keeps the detector's import closure
clear of anything that could read a result.

## The fixture that proves the refusals

`tests/fixtures/make_hard_drawing.py` builds a sheet carrying the production
failure modes: an inline label half a point from a pipe, a code-shaped note
beside another, a date, a duct-schedule string, a title block, callouts whose
leaders are two objects each, and building fabric drawn as parallel pairs at a
*pipe-like* separation on its own layer.  Blind, the engine confirms the three
labels whose leaders reach pipe geometry and refuses every other string,
including the one sitting on the pipe:

```
designations 8 -> with DN 7 -> vector leaders 4 -> verified attachments 3
pipe layers active: W50-VVS-FE-S3
refused: S3-X9-50 (leader ends on fabric), S3-R8-160 (inline, no leader),
         ENL. PM-2, 2024-04-19, B2-SDLV94NN4 1000X150, title block
```

## The production sheet, W501A0011

Blind, clean-only, frozen before any comparison:

```
designations reconstructed : 142      (109 of them code-shaped instances)
... carrying a DN          :  82
vector leaders traced      : 116
verified FE attachments    :  64
physical pipes             : 303  (23 designated)
pipe layers (discovered)   : the V-…-FE-… layers, from the geometry, never by name
```

Against the previous pipeline's counts on the same sheet - 77 / 77 / 68 / 64,
passed on the command line and never visible to the detector - the chain now
ends where it did: **64 verified attachments against 64**.  The earlier stages
count differently rather than better: "designations" here is every label the
role stage proposes, including the sheet's notes, and "leaders" is counted per
text line, so a code and the level written under it each report one.

Four defects were found by running it, each fixed and each measurable:

| What was wrong | Effect on the sheet |
| --- | --- |
| Pipe detection ran **before** leader tracing and consumed the thin annotation strokes as pipe geometry | 148 leader strokes were gone before the tracer looked; leaders are now traced first and withheld from detection |
| The leader was traced from the **designation's own box** | The leaders on this sheet leave from the rule under the *level* line, ~9 pt below; tracing now works from the label block |
| The tracer **chose one** stroke and refused when two left a label | 38 code labels were refused outright; every plausible line is now traced and the *chain* decides which is a leader |
| Nothing distinguished a leader from a pipe **running beside a label** | A pipe drawn with a similar pen was read as its own label's leader; a line that stays near *and parallel* to the run it would attach to is that pipe, not a leader |

What is still short, stated plainly: **23 of 303 physical pipes carry a
designation**.  That is not an association failure - 64 labels did attach - but
a geometry one: 303 physical pipes on a sheet that draws far fewer means the
dash chains are not being joined into whole pipes, so each verified attachment
names a fragment.  That is the next thing to fix, and it is quantity work.

## The second sheet, W501A0012

The same run on W501A0012 reports `designations 2 -> DN 1 -> leaders 0 ->
attachments 0`.  The chain is not at fault: the glyph stage resolved **none** of
that sheet's 467 glyph candidates, so there is no text for a chain to start
from.  Its facit lists `KV1-X31-16` at 3.9 m + 7.0 m and `S3-R8-75` at 3.1 m +
3.6 m; the engine currently produces nothing to compare against them.  Reading
that sheet's lettering is a separate defect from this one.


## Counting pipework, not linework

The chain fixed *identification*.  Running it on W501A0011 then exposed the
other half plainly: the sheet produced **303 physical pipes and 1 395 m** of
take-off, on a drawing whose drainage is a few hundred metres.  Most of those
"pipes" were the architectural background - hatching, wall pairs, a structural
grid - because two parallel strokes at a pipe-like separation are
indistinguishable from a pipe by geometry alone.  On a plan sheet there is
always a second line at a pipe-like distance from any first one.

What separates them is, again, the drawing's own statement.  A verified leader
landing on a layer says *that layer carries pipework*:

* a layer is attested when several verified leaders land on it - three at
  least, and a real share of them, so one mis-traced leader cannot promote the
  architectural background;
* only geometry whose bore the drawing draws attests a layer - a wall pair or a
  dash chain.  A bare unpaired stroke is the weakest verdict the detector
  makes, and a leader landing near one is not evidence about a whole layer;
* a physical pipe is counted when **most of its length** stands on attested
  layers, not when it merely touches one, so a run of fabric that meets a pipe
  does not inherit its standing;
* a drawing that declares no layers leaves the gate open: nothing is withheld.

Everything else is still extracted, measured and published - as
`unattributedGeometry`, and drawn on the marked drawing in grey on its own
switchable layer.  Nothing is deleted to make a number look better.

On W501A0011 that takes the take-off from 1 395 m over 303 "pipes" to **487 m
over 80**, with 221 pipes and 56 531 pt of linework reported as unattributed.
The 420 m of that total which no leader named is published as an unnamed row
rather than hidden: it is pipework the drawing draws and does not label
anywhere the engine could read.
