#!/bin/bash
# One pytest process per test file.
#
# Combining Seamless test files into a single pytest run produces spurious
# failures: process-global cache and refholder state carries between files, and
# a genuine failure in one file manufactures audit failures in the next
# (§15 A1).  conftest.py resets that state per test, which is necessary but not
# sufficient — worker processes, the shutdown registry and module-level caches
# in seamless-core are per-process.  So the suite is run the way the rest of the
# project runs its tests: one file, one process.
#
# Usage:
#   ./run-tests.sh                 # everything
#   ./run-tests.sh -m "not slow"   # skip the sleeping bodies
#   ./run-tests.sh -m a1           # only what phase A1 must make green
#
# This suite is expected to be RED against the current implementation: it
# describes the contract of phase A0, not the behaviour of the code.  See
# README.md for the file-by-file expectation.

cd "$(dirname "$0")" || exit 1

for i in node-transition/test_*.py quiescence-barrier/test_*.py latency/test_*.py correctness/test_*.py; do
    echo "$i"
    pytest -s "$i" "$@"
    echo "DONE $i"
done
