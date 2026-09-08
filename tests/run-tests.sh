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
# Requires the `seamless1` environment (`conda activate seamless1`), which puts
# the sibling repositories — seamless, seamless-core, seamless-transformer,
# seamless-config, seamless-database, seamless-signature, seamless-workflow —
# on the path.  Without seamless-signature the compiled tests skip rather than
# fail.
#
# Usage:
#   ./run-tests.sh                 # everything
#   ./run-tests.sh -m "not slow"   # skip the sleeping bodies
#   ./run-tests.sh -m a1           # only what phase A1 must make green
#   ./run-tests.sh -m now          # the regression net, which must always be green
#
# Both the original and A0 contract suites are required to pass after A5.

cd "$(dirname "$0")" || exit 1

status=0
for i in test_*.py \
         node-transition/test_*.py \
         quiescence-barrier/test_*.py \
         latency/test_*.py \
         correctness/test_*.py; do
    echo "$i"
    python -m pytest -s "$i" "$@" || status=1
    echo "DONE $i"
done
exit "$status"
