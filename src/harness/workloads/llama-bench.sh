# Workload adapter: llama.cpp llama-bench (sourced by src/harness/runner.sh; not executable on its own).
# Knob: omp (GGML_OPENMP=ON build, libgomp barrier) | noomp (GGML_OPENMP=OFF build, llama.cpp's spin barrier).
# Params (from the spec, exported by the runner as P_<name>): P_n_gen (default 512), P_threads (default 4).
# Builds are copies of the exploratory ones (src/env/guest/workloads-setup.sh). Their RUNPATH still points at
# the explore tree, so LD_LIBRARY_PATH pins the copied libraries (it takes precedence over RUNPATH).
WL_ROOT=/opt/gangsched/workloads/llama.cpp
WL_MODEL=/opt/gangsched/workloads/models/qwen2.5-1.5b-instruct-q4_k_m.gguf
WL_PARSER=llama-bench          # summarize.py: generation tok/s = avg_ts of the n_gen test; compute window = sum(samples_ns)

wl_init() {  # $1 = knob
  case $1 in omp|noomp) ;; *) echo "llama-bench: unknown knob '$1' (omp|noomp)" >&2; return 2 ;; esac
  WL_LIB=$WL_ROOT/build-$1/bin
  WL_BIN=$WL_LIB/llama-bench
  WL_COMM=llama-bench          # every thread inherits the process name
  WL_HASH=("$WL_BIN" "$WL_LIB"/lib*.so.*.* "$WL_LIB"/libllama-bench-impl.so "$WL_MODEL")
  [[ -x $WL_BIN && -f $WL_MODEL ]]
}

wl() {  # $1 = timeout s; stdout = llama-bench JSON
  LD_LIBRARY_PATH=$WL_LIB timeout "$1" taskset -c "$CPUS" "$WL_BIN" -m "$WL_MODEL" \
    -t "${P_threads:-4}" -p 0 -n "${P_n_gen:-512}" -r 1 -o json
}

wl_meta() {
  echo "llama.cpp commit=$(cat $WL_ROOT/COMMIT)"
  echo "command: llama-bench -m $(basename "$WL_MODEL") -t ${P_threads:-4} -p 0 -n ${P_n_gen:-512} -r 1 -o json (taskset -c $CPUS)"
  echo "libraries (LD_LIBRARY_PATH=$WL_LIB):"
  LD_LIBRARY_PATH=$WL_LIB ldd "$WL_BIN" | grep -E 'ggml|llama|gomp' | sed 's/ (0x.*//'
}
