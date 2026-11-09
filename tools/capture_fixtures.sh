#!/usr/bin/env bash
# Re-capture the diagnostic fixtures in tests/fixtures/captured/.
#
#   GABLE=/path/to/gable tools/capture_fixtures.sh
#
# Each case is compiled by the real compiler from a fixed directory layout, its
# combined output is captured, and absolute paths are rewritten to /proj so the
# fixtures are stable across machines. Re-run this after upgrading the compiler
# and review the diff: it is the earliest warning that the output contract
# changed.
#
# The snippets and file names below are frozen. Changing them means updating
# tests/test_diagnostics.py too, because the assertions pin line and column
# numbers.

set -u

GABLE=${GABLE:-gable}
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
OUT="$HERE/../tests/fixtures/captured"

if ! command -v "$GABLE" >/dev/null 2>&1 && [ ! -x "$GABLE" ]; then
    echo "error: Gable compiler not found at '$GABLE' (set GABLE=/path/to/gable)" >&2
    exit 1
fi

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/tests/fixtures/src" "$OUT"

SRC="$WORK/tests/fixtures/src"

# --- single-file cases -----------------------------------------------------
cat >"$SRC/immut.gable" <<'EOF'
fn f() -> int {
    let a: i32 = 1;
    a = 2;
    return a;
}
EOF

cat >"$SRC/expr.gable" <<'EOF'
fn main() {
    let a: i32 = 1 + ;
    print(a)
}
EOF

cat >"$SRC/divzero.gable" <<'EOF'
fn main() {
    let total: int = 4 / 0
    print(total)
}
EOF

cat >"$SRC/quote.gable" <<'EOF'
fn value() -> int {

    return "hello"
}
EOF

cat >"$WORK/mismatch.gable" <<'EOF'
fn main() {
    let text: str = "3"
    let n: int = text
    print(n)
}
EOF

cat >"$SRC/importer.gable" <<'EOF'
import "absent.gable";
fn main() {
    print(1)
}
EOF

# --- import cycle: main.gable <-> b.gable -----------------------------------
cat >"$WORK/main.gable" <<'EOF'
import "b.gable"

fn main() {
    print(1)
}
EOF
cat >"$WORK/b.gable" <<'EOF'
import "main.gable"

fn other() {
}
EOF

# --- cross-file: root.gable imports a file with a type error -----------------
cat >"$WORK/root.gable" <<'EOF'
import "badtype.gable"

fn main() {
    print(1)
}
EOF
cat >"$WORK/badtype.gable" <<'EOF'
fn value() -> int {
    let x: str = "nope"
    return x
}
EOF

# Every check is invoked with an ABSOLUTE path, exactly as the language server
# does. The compiler then prints absolute paths for the file under check and for
# imported files, and the sed below makes them all relative to /proj.
capture() {
    local name=$1
    local file=$2
    "$GABLE" check "$file" >"$OUT/$name.txt" 2>&1
    sed -i "s#${WORK}#/proj#g" "$OUT/$name.txt"
    printf 'captured %-16s %s\n' "$name" "$(wc -l <"$OUT/$name.txt") line(s)"
}

capture immut            "$SRC/immut.gable"
capture expr             "$SRC/expr.gable"
capture division_by_zero "$SRC/divzero.gable"
capture tsr_quote        "$SRC/quote.gable"
capture caret_span       "$WORK/mismatch.gable"
capture missing_import   "$SRC/importer.gable"
capture import_cycle     "$WORK/main.gable"
capture cross_file       "$WORK/root.gable"

# --- operational error: the input file does not exist ----------------------
capture input_error      "$SRC/missing.gable"

echo "fixtures written to $OUT"
