#!/usr/bin/env bash
# Full acceptance run.  Every line of the FINAL REPORT's verdict table comes
# from this script; nothing in it is asserted by hand.
set -uo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
OUT=artifacts/acceptance
mkdir -p "$OUT"
status=0
step() {
  local name="$1"; shift
  if "$@" > "$OUT/$name.log" 2>&1; then
    echo "PASS  $name"
  else
    echo "FAIL  $name  (see $OUT/$name.log)"
    status=1
  fi
}

step build_python           $PY -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('src/python', quiet=2) else 1)"
step typecheck_typescript   npx tsc -p tsconfig.json --noEmit
step build_typescript       npm run build
step tests_python           $PY -m pytest tests/python -q
step tests_typescript       node --test dist/test/domain.test.js dist/test/queue.test.js dist/test/service.test.js
step fixtures               $PY -m tests.fixtures.make_drawings artifacts/fixtures
PYTHONPATH=src/python RUN_BLIND_TEST=1 RUN_FORENSICS=1 \
  step blind_run_drawing_a  $PY -m vvs_pipe.cli analyse artifacts/fixtures/drawing_a_clean.pdf --out "$OUT/drawing_a" --forensics
PYTHONPATH=src/python RUN_BLIND_TEST=1 RUN_FORENSICS=1 \
  step blind_run_drawing_b  $PY -m vvs_pipe.cli analyse artifacts/fixtures/drawing_b_clean.pdf --out "$OUT/drawing_b" --forensics
PYTHONPATH=src/python POST_TEST_COMPARE=1 \
  step post_test_a          $PY -m vvs_pipe.cli analyse artifacts/fixtures/drawing_a_clean.pdf --out "$OUT/drawing_a" --ground-truth artifacts/fixtures/drawing_a_truth.json
PYTHONPATH=src/python POST_TEST_COMPARE=1 \
  step post_test_b          $PY -m vvs_pipe.cli analyse artifacts/fixtures/drawing_b_clean.pdf --out "$OUT/drawing_b" --ground-truth artifacts/fixtures/drawing_b_truth.json
exit $status
