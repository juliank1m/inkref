#!/bin/sh
# Every check. Uses venv when present (the PDF checks need PyMuPDF), stdlib otherwise.
# The cross-check builds the Swift engine and compares it against Python; it skips itself
# when there is no Swift toolchain.
set -e
cd "$(dirname "$0")"
PY=python3
[ -x venv/bin/python ] && PY=venv/bin/python

echo "== goodnotes ==";  $PY tests/test_goodnotes.py  | tail -1
echo "== ink ==";        $PY tests/test_ink.py        | tail -1
echo "== layout ==";     $PY tests/test_layout.py     | tail -1
echo "== beautify ==";   $PY tests/test_beautify.py   | tail -1
echo "== ai ==";         $PY tests/test_ai.py         | tail -1
echo "== pdf ==";        $PY tests/test_pdf.py        | tail -1
echo "== recognize ==";  $PY tests/test_recognize.py  | tail -1
echo "== collide ==";    $PY tests/test_collide.py    | tail -1
echo "== flow ==";       $PY tests/test_flow.py       | tail -1
echo "== swift/python cross-check ==";  $PY tests/test_crosscheck.py | tail -1
