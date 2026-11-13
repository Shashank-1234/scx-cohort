#!/usr/bin/env bash
# Copy (never move) the real-workload builds from /opt/gangsched/explore/ (the record of the exploratory
# runs) to /opt/gangsched/workloads/ for the shared runner, and write SHA256SUMS there.
# Idempotent: existing copies are kept. Run in guest as root:  RUNS_DIR=<dir> bash run.sh workloads-setup
# The llama.cpp builds carry an absolute RUNPATH into the explore tree, so the runner's adapter sets
# LD_LIBRARY_PATH to the copied lib dir; this script checks that ldd then resolves into workloads/.
set -euo pipefail
X=/opt/gangsched/explore; W=/opt/gangsched/workloads
MODEL=qwen2.5-1.5b-instruct-q4_k_m.gguf
NPBX=$X/npb/NPB3.4.3/NPB3.4-OMP
mkdir -p "$W/llama.cpp" "$W/models" "$W/npb"

echo "== llama.cpp builds"
for v in omp noomp; do
  if [[ -d $W/llama.cpp/build-$v/bin ]]; then echo "build-$v: present, kept"; else
    mkdir -p "$W/llama.cpp/build-$v"
    cp -a "$X/llama.cpp/build-$v/bin" "$W/llama.cpp/build-$v/"
    cp -a "$X/llama.cpp/build-$v/CMakeCache.txt" "$W/llama.cpp/build-$v/"
    echo "build-$v: copied"
  fi
done
git -C "$X/llama.cpp" rev-parse HEAD > "$W/llama.cpp/COMMIT"
echo "commit: $(cat "$W/llama.cpp/COMMIT")"

echo "== model"
if [[ -f $W/models/$MODEL ]]; then echo "present, kept"; else cp -a "$X/models/$MODEL" "$W/models/"; echo copied; fi

echo "== NPB CG"
for c in A B; do
  if [[ -x $W/npb/cg.$c.x ]]; then echo "cg.$c.x: present, kept"
  elif [[ -x $NPBX/bin/cg.$c.x ]]; then cp -a "$NPBX/bin/cg.$c.x" "$W/npb/"; echo "cg.$c.x: copied"
  else
    echo "cg.$c.x: building (NPB 3.4.3, -O3 -fopenmp)"
    make -C "$NPBX" cg CLASS=$c > "$W/npb/build-cg-$c.log" 2>&1 < /dev/null
    cp -a "$NPBX/bin/cg.$c.x" "$W/npb/"
  fi
done
cp -a "$NPBX/config/make.def" "$W/npb/make.def"

echo "== library resolution with the adapter's LD_LIBRARY_PATH (must all be under $W)"
bad=0
for v in omp noomp; do
  L=$W/llama.cpp/build-$v/bin
  LD_LIBRARY_PATH=$L ldd "$L/llama-bench" | grep -E 'ggml|llama|gomp' | sed "s/^/build-$v: /"
  if LD_LIBRARY_PATH=$L ldd "$L/llama-bench" | grep -E 'ggml|llama' | grep -v "=> $W/"; then bad=1; fi
done
[[ $bad == 0 ]] && echo "OK: every llama/ggml library resolves into $W" || { echo "FAIL: a library resolves outside $W"; exit 1; }

echo "== copies identical to the originals"
for v in omp noomp; do diff -r "$X/llama.cpp/build-$v/bin" "$W/llama.cpp/build-$v/bin" > /dev/null && echo "build-$v: identical"; done
cmp -s "$X/models/$MODEL" "$W/models/$MODEL" && echo "model: identical"
for c in A B; do cmp -s "$NPBX/bin/cg.$c.x" "$W/npb/cg.$c.x" && echo "cg.$c.x: identical"; done

echo "== SHA256SUMS"
( cd "$W" && find . -type f \( -name llama-bench -o -name '*.so*' -o -name '*.gguf' -o -name 'cg.*.x' \) | sort | xargs sha256sum ) > "$W/SHA256SUMS"
cat "$W/SHA256SUMS"
echo "== disk: $(du -sh "$W" | cut -f1)"
