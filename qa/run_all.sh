#!/bin/sh
# Runs every suite. A crash is a FAILURE, not a silent zero — an earlier version of this
# loop reported "0 fail" while a whole file threw before its first assertion.
set -u

# sweep.js and feed.js need a real quotes.json. Fetch one if it is absent, rather than
# letting two suites crash — the runner reports a crash as failure, but a developer who
# has never fetched the feed reads it as a broken test rather than a missing input.
Q="${QA_QUOTES:-/tmp/q.json}"
if [ ! -f "$Q" ]; then
  echo "  fetching quotes feed -> $Q"
  curl -fsS -o "$Q" \
    https://raw.githubusercontent.com/Asaf-12344321/risk-sizer/data/quotes.json \
    || { echo "  COULD NOT FETCH $Q — sweep and feed will fail"; }
fi

tot=0; fail=0; crashed=""
for s in invariants sweep edge golden defects ux feed custom_edge; do
  out=$(node "$s.js" 2>&1); rc=$?
  p=$(printf '%s' "$out" | grep -cE '^PASS'); q=$(printf '%s' "$out" | grep -c '^FAIL  ')
  if [ "$rc" -ne 0 ] && [ "$q" -eq 0 ]; then
    crashed="$crashed $s"; printf "  %-11s CRASHED\n" "$s"
    printf '%s\n' "$out" | tail -4 | sed 's/^/       /'
  else
    printf "  %-11s %3d pass  %d fail\n" "$s" "$p" "$q"
    [ "$q" -ne 0 ] && printf '%s\n' "$out" | grep '^FAIL  ' | sed 's/^/       /'
  fi
  tot=$((tot+p)); fail=$((fail+q))
done
python3 parity.py > /tmp/parity.json 2>/dev/null
out=$(node parity.js 2>&1); rc=$?
p=$(printf '%s' "$out" | grep -cE '^PASS'); q=$(printf '%s' "$out" | grep -c '^FAIL  ')
if [ "$rc" -ne 0 ] && [ "$q" -eq 0 ]; then crashed="$crashed parity"; printf "  %-11s CRASHED\n" parity
else printf "  %-11s %3d pass  %d fail\n" parity "$p" "$q"; fi
tot=$((tot+p)); fail=$((fail+q))
echo "  ---------------------"
printf "  TOTAL       %3d pass  %d fail\n" "$tot" "$fail"
if [ -n "$crashed" ]; then echo "  CRASHED SUITES:$crashed"; exit 1; fi
[ "$fail" -eq 0 ] || exit 1
