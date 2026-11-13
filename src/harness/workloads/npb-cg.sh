# Workload adapter: NPB 3.4.3 OpenMP CG (sourced by src/harness/runner.sh; not executable on its own).
# Knob: OMP_WAIT_POLICY = active | passive | default (variable unset: libgomp's own default).
# Params: P_class (default B), P_threads (default 4).
WL_ROOT=/opt/gangsched/workloads/npb
WL_PARSER=npb                  # summarize.py: Mop/s total; Verification must be SUCCESSFUL; compute window = whole run

wl_init() {  # $1 = knob
  case $1 in active|passive|default) ;; *) echo "npb-cg: unknown knob '$1' (active|passive|default)" >&2; return 2 ;; esac
  WL_POLICY=$1
  WL_BIN=$WL_ROOT/cg.${P_class:-B}.x
  WL_COMM=$(basename "$WL_BIN")
  WL_HASH=("$WL_BIN")
  [[ -x $WL_BIN ]]
}

wl() {  # $1 = timeout s; stdout = NPB report
  if [[ $WL_POLICY == default ]]; then
    env -u OMP_WAIT_POLICY OMP_NUM_THREADS="${P_threads:-4}" timeout "$1" taskset -c "$CPUS" "$WL_BIN"
  else
    env OMP_NUM_THREADS="${P_threads:-4}" OMP_WAIT_POLICY="$WL_POLICY" timeout "$1" taskset -c "$CPUS" "$WL_BIN"
  fi
}

wl_meta() {
  echo "command: OMP_NUM_THREADS=${P_threads:-4} OMP_WAIT_POLICY=$WL_POLICY $(basename "$WL_BIN") (taskset -c $CPUS; 'default' = variable unset)"
  echo "flags: $(grep -E '^(FC|FFLAGS)\s*=' $WL_ROOT/make.def | tr '\n' ' ')"
  echo "libgomp: $(ldd "$WL_BIN" | grep gomp | sed 's/ (0x.*//')"
}
