# Design

How each stage works, and why it is built the way it is.  Every threshold named
here lives in a dataclass at the top of its module so it can be reviewed in one
place, and every one of them is a *physical* or *typographic* quantity, never a
value chosen to make a particular drawing come out right.

## 0. Canonical representation (`canonical.py`)

Everything that can be influenced by input order passes through here.

* **Quantisation.** Coordinates are quantised to 1e-4 pt before they are hashed
  or compared, so a value that arrived by a different summation order collapses
  to the same canonical number.  `-0.0` is normalised to `0.0`.
* **Total ordering.** Every entity exposes a `canonical_key()` built from
  geometry and evidence only.  An open polyline's key is the lexicographically
  smaller of the forward and reversed point sequences, because a run and its
  reverse are the same physical pipe.
* **Content-addressed ids.** `obj_…`, `run_…`, `pp_…` are SHA-256 prefixes of
  the canonical key, so a permuted extraction produces identical ids.  Nothing
  is numbered by a counter.
* **Digests.** Canonical JSON (sorted keys, fixed float format) hashed with
  SHA-256 is the determinism contract the tests compare.

## 1. Forensics (`pdf_forensics/`)

A pure description of the file, written before any detection: SHA-256, size,
page geometry, rotation, media/crop boxes, counts of drawings, lines, curves,
rectangles, strokes and fills, colour/width/dash/layer/font histograms,
annotations, embedded images, clip paths.

## 2. Vector extraction (`vector_extraction/`)

* **Annotations are deleted from the in-memory document before any path is
  read.** PyMuPDF's `get_drawings()` walks annotation appearance streams as
  well as page content, so previous manual take-off stored as `/Annots` would
  otherwise enter the geometry.  The file on disk is untouched; the number of
  drawings that disappear is reported.  A second rule drops content-stream
  geometry that exactly matches a markup annotation's declared vertices, which
  covers flattened markup.
* **Deterministic curve flattening** with a fixed chord tolerance and a fixed
  recursion budget, so the polyline is bit-identical everywhere.
* **Degenerate contours are not "closed".** A path closed back onto itself with
  no enclosed area is a stroke, not a symbol.

## 3. Glyph reconstruction (`glyph/`)

CAD exports routinely have no text layer at all - the reference drawing has
zero text objects and 823 line segments, every character among them.

1. **Fragment selection.** Small stroked objects; a *single straight* stroke
   longer than twice the local text height is a leader or a symbol stroke, not
   part of a letter.  Without that rule a leader bridges a label to the
   geometry it points at and destroys the blob boundaries.
2. **Blobbing.** Adaptive proximity clustering into words; the blob's principal
   axis (closed-form, no eigensolver) gives the text rotation.
3. **Projection-profile segmentation.** Fragments are projected onto the blob's
   axis and split wherever coverage is empty - exact for non-touching CAD
   fonts, with no per-character knowledge.
4. **Robust metrics.** The cap height is re-estimated from the *distribution*
   of glyph heights and the baseline from where the full-height characters
   sit, so one stray mark beside a label cannot shift every glyph's features.
5. **Features.** Each glyph is normalised into a 48x48 raster with the aspect
   ratio preserved, thinned (Zhang-Suen, implemented here so there is no
   opencv-contrib dependency), and described by: symmetric chamfer distance,
   dilated-skeleton Jaccard distance, enclosed holes, endpoint and junction
   counts of a *pruned* skeleton (pruned proportionally to stroke thickness -
   thinning a filled outline always produces spurs), aspect ratio, and height
   and baseline offset relative to the line.
6. **Prototypes.** Characters are rendered at run time from the PDF base-14
   fonts.  The bank contains *characters*, not codes.
7. **Alphabet assignment.** A drawing uses one typeface, so glyphs are
   clustered by shape and each cluster is decided once from evidence pooled
   over all its instances; the clusters are then matched to the alphabet by a
   globally optimal *one-to-one* assignment.  If the S-shaped and 5-shaped
   clusters both prefer `5`, the assignment gives `5` to whichever fits it
   better and the other falls to its next best character.  Exact ties are
   frequent, so what is minimised is evidence-weighted *regret* - otherwise the
   solver would break a tie arbitrarily with hundreds of glyphs riding on it.
   A cluster whose best cost exceeds the reject threshold stays unassigned and
   its glyphs are reported `UNRESOLVED_GLYPH`.
8. **Lone marks** (a scale-bar label, but also a riser circle) are matched
   against the resolved clusters afterwards, so a symbol resembling nothing in
   the drawing's own alphabet never consumes a character real text needs.

Where the producer did leave a text layer, native spans are authoritative and
are merged with the co-located reconstruction, with the agreement recorded.

## 4. Designation discovery (`designations/`)

No list of codes exists anywhere.  A string is scored for each role from:

* **token structure** - runs of letters/digits joined by separators; "code-like"
  means two or more alphanumeric runs, at least one containing a digit;
* **panels** - axis-aligned closed rectangles big enough to be a panel and far
  too small to be the sheet border, holding several pieces of text.  A panel
  containing a `1:N` ratio is a title block; one containing repeated code-like
  strings is a legend.  Nothing about *where* a panel sits is assumed;
* **leaders** - a stroke starting at readable text and ending away from it;
* **local geometry and repetition**.

The same code in the legend and on the pipe is therefore separated by evidence,
not by position.

## 5. Pipe detection (`pipes/`)

Geometry the text stages consumed is gone; so is the sheet frame, anything
inside a panel, and closed contours (a pipe in plan is two independent strokes -
a closed contour is a symbol, hatch, panel or scale bar).  Two strokes pair into
a pipe when they are parallel, overlap along their shared axis, sit within a
plausible separation, and agree on weight, colour and dash pattern.  A stroke
never pairs with itself, which is what stops a letter M pairing its own limbs
into a phantom pipe.  Pairs are consumed in a canonical order - best evidence
first, then by their own geometry - so equal-scoring pairs are ordered by where
they are, not by which stroke was parsed first.

The centerline is the midline of the mutually overlapping part; the width is the
perpendicular separation.  A width is never mistaken for a length.

## 6. Topology (`topology/`)

* **Corner healing.** Offsetting two walls round a mitre and taking their
  midline leaves each centerline short by about half the pipe width.  The true
  corner is where the two axes cross, so that intersection is used - not the
  midpoint between the short ends, which would lose real length.
* **Tee splitting.** An end landing on another candidate's interior splits it at
  the foot of the perpendicular.
* **Node merging** by centroid of the endpoint cluster.
* **Runs** are chained by *mutual best continuation*: A continues into B only if
  B is A's best continuation and A is B's.  That makes chaining a stable pairing
  independent of visit order, and it stops a run at a genuine ambiguity rather
  than picking one.
* **PhysicalPipe** merges runs sharing designation and size that touch end to
  end.  The merge is a partition of the run set, so double counting is prevented
  structurally, and the reconciliation re-checks the invariant on the way out.

## 7. Association (`association/`)

Evidence per candidate pipe: leader hit (tolerance grows with the drawn width,
because a leader points at the wall), proximity, orientation agreement, and size
consistency.  Orientation and size are *corroborating*: where unavailable they
stay neutral rather than counting against an association, since a label set
perpendicular to its pipe is ordinary practice.  A leader landing on the pipe is
direct evidence and stands on its own.

If the top two candidates are within the tie epsilon the result is `AMBIGUOUS`
and no quantity comes from it.  Unlabelled runs inherit a neighbour's
designation only across a width-compatible connection and only when exactly one
designation reaches them first.

## 8. Dimensions, scale, verticals, measurement

* **Dimension.** The label's trailing numeric run and the measured wall
  separation are reconciled.  Agreeing, the nominal figure is kept; disagreeing,
  the *measurement* wins and `DIMENSION_CONFLICT` is recorded.
* **Scale.** A `1:N` ratio found anywhere in the sheet's text, cross-checked
  against a scale bar (a row of congruent adjacent cells with numeric labels).
  Disagreement beyond tolerance gives `SCALE_AMBIGUOUS`; neither source gives
  `SCALE_UNKNOWN` - never a default.
* **Verticals.** A riser is a symbol at a run end.  *Two distinct elevations*
  give a length; one or none gives `VERTICAL_HEIGHT_UNKNOWN` and downgrades the
  pipe that carries it.  There is no default storey height anywhere.
* **Measurement** multiplies centerline length by metres-per-point; with no
  scale there are no metres, only points.

## 9. Complexity

| Stage | Complexity |
| --- | --- |
| Extraction | O(path items) |
| Glyph blobbing | O(n) grid buckets + pairs inside them |
| Glyph clustering | O(n^2) *vectorised* signature comparisons + O(matches) shape measures |
| Alphabet assignment | O(k^3) Hungarian, k = distinct shapes (a few dozen) |
| Pipe pairing | grid-indexed candidate pairs |
| Graph build | grid-indexed endpoint clustering |
| Run chaining | O(edges x node degree) |
| Physical merge / propagation | grid-bucketed endpoint pairs |

Measured: 4 305 vector objects and 2 636 glyphs analyse in ~18 s
(`scripts/stress_probe.py`).

## 10. Determinism

Nothing uses insertion order, map iteration order, object identity or array
position as semantics.  `canonical_sort` raises if two distinct items collapse
to the same key.  The only randomness in the repository is the seeded
permutation generator the determinism tests use.
