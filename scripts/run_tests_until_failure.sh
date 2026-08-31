#!/usr/bin/env bash
#
# Run the full suite repeatedly and keep the output of the first failure.
#
# A flake was observed once in roughly twenty full runs and could not be
# identified, because only the failure count was on screen — the test name and
# traceback were gone by the time anyone looked. This script exists so that
# does not happen twice: every run is captured with `-v`, passing runs are
# discarded, and the first failing run is kept whole.
#
# Usage:
#   scripts/run_tests_until_failure.sh [runs]      # default 30
#
# On failure it prints the saved path and exits non-zero. Attach that file to
# the issue; it contains the test name, the traceback, and the order tests ran
# in, which is what identifying an order-dependent flake needs.

set -uo pipefail

runs="${1:-30}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root" || exit 1

out_dir="${TMPDIR:-/tmp}/ai-brainstorm-flake"
mkdir -p "$out_dir"

echo "Running the suite up to ${runs} times. Output of the first failure is kept."
echo "Working directory: ${repo_root}"
echo

for i in $(seq 1 "$runs"); do
    log="${out_dir}/run-$(date +%Y%m%d-%H%M%S)-${i}.log"
    if python3 -m unittest discover -s tests -v >"$log" 2>&1; then
        printf 'run %3d/%s: OK\n' "$i" "$runs"
        rm -f "$log"
    else
        printf 'run %3d/%s: FAILED\n' "$i" "$runs"
        echo
        echo "Saved: ${log}"
        echo
        echo "--- failing tests ---"
        grep -E '^(FAIL|ERROR):' "$log" || echo "(no FAIL/ERROR lines; check the log)"
        echo
        echo "--- summary ---"
        tail -n 5 "$log"
        exit 1
    fi
done

echo
echo "No failure in ${runs} runs. Nothing kept."
