#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
launcher=$script_dir/bin/dart-lsp
tmp_root=$(mktemp -d "${TMPDIR:-/tmp}/dart-lsp-test.XXXXXX")
test_path=$tmp_root/path
fvm_cache=$tmp_root/fvm
fake_home=$tmp_root/home
mkdir -p "$test_path" "$fake_home"
ln -s "$(command -v sed)" "$test_path/sed"

cleanup() {
  [ -d "$tmp_root" ] || return 0
  find "$tmp_root" -depth ! -type d -exec rm -f {} \;
  find "$tmp_root" -depth -type d -exec rmdir {} \; 2>/dev/null || :
}
trap cleanup EXIT
trap 'cleanup; exit 130' INT
trap 'cleanup; exit 143' TERM

make_dart() {
  dart_path=$1
  mkdir -p "$(dirname "$dart_path")"
  printf '%s\n' '#!/bin/sh' 'if [ -n "${DART_LSP_CAPTURE:-}" ]; then printf "%s\\n" "$@" > "$DART_LSP_CAPTURE"; fi' > "$dart_path"
  chmod +x "$dart_path"
}

run_print() {
  run_dir=$1
  run_cache=${2:-$fvm_cache}
  if (cd "$run_dir" && HOME="$fake_home" FVM_CACHE_PATH="$run_cache" \
    DART_LSP_PRINT=1 PATH="$test_path" "$launcher") > "$tmp_root/stdout" 2> "$tmp_root/stderr"; then
    run_status=0
  else
    run_status=$?
  fi
  run_stdout=$(cat "$tmp_root/stdout")
  run_stderr=$(cat "$tmp_root/stderr")
}

assert_eq() {
  if [ "$1" != "$2" ]; then
    printf 'FAIL: %s\n  expected: <%s>\n  actual:   <%s>\n' "$3" "$2" "$1" >&2
    exit 1
  fi
}

assert_contains() {
  case $1 in
    *"$2"*) ;;
    *) printf 'FAIL: %s\n  expected substring: <%s>\n  actual: <%s>\n' "$3" "$2" "$1" >&2; exit 1 ;;
  esac
}

project=$tmp_root/project
mkdir -p "$project/lib/src"
printf '%s\n' '{' '  "flutter": "3.44.9"' '}' > "$project/.fvmrc"
pinned=$fvm_cache/versions/3.44.9/bin/cache/dart-sdk/bin/dart
default_dart=$fvm_cache/default/bin/cache/dart-sdk/bin/dart
make_dart "$pinned"
make_dart "$default_dart"

run_print "$project"
assert_eq "$run_status" 0 'pinned SDK exit status when default exists'
assert_eq "$run_stdout" "$pinned" 'pinned SDK takes precedence over default SDK'
assert_eq "$run_stderr" '' 'pinned SDK stderr'
printf '%s\n' 'PASS: (g) pinned SDK takes precedence over default'

run_print "$project/lib/src"
assert_eq "$run_status" 0 'parent .fvmrc exit status'
assert_eq "$run_stdout" "$pinned" 'parent .fvmrc SDK selection'
printf '%s\n' 'PASS: parent .fvmrc discovery'
capture_file=$tmp_root/lsp-arguments
if (cd "$project" && HOME="$fake_home" FVM_CACHE_PATH="$fvm_cache" \
  DART_LSP_CAPTURE="$capture_file" PATH="$test_path" "$launcher") > "$tmp_root/stdout" 2> "$tmp_root/stderr"; then
  run_status=0
else
  run_status=$?
fi
assert_eq "$run_status" 0 'LSP launch exit status'
assert_eq "$(cat "$tmp_root/stdout")" '' 'LSP launch stdout is reserved'
assert_eq "$(cat "$tmp_root/stderr")" '' 'LSP launch stderr'
assert_eq "$(cat "$capture_file")" "$(printf '%s\n' 'language-server' '--protocol=lsp')" 'LSP command arguments'
printf '%s\n' 'PASS: LSP launch uses protocol arguments without stdout noise'

missing_project=$tmp_root/missing-pinned-project
mkdir -p "$missing_project"
printf '%s\n' '{ "flutter": "9.9.9" }' > "$missing_project/.fvmrc"
make_dart "$default_dart"
run_print "$missing_project"
assert_eq "$run_status" 0 'missing pinned SDK fallback exit status'
assert_eq "$run_stdout" "$default_dart" 'missing pinned SDK fallback selection'
assert_contains "$run_stderr" 'pinned Flutter 9.9.9 SDK is not installed; falling back to' 'missing pinned SDK warning'
printf '%s\n' 'PASS: missing pinned SDK warns and falls back'

plain_project=$tmp_root/plain-project
mkdir -p "$plain_project"
run_print "$plain_project"
assert_eq "$run_status" 0 'default SDK exit status'
assert_eq "$run_stdout" "$default_dart" 'default SDK selection without .fvmrc'
assert_eq "$run_stderr" '' 'default SDK stderr'
printf '%s\n' 'PASS: default SDK without .fvmrc'

config_project=$tmp_root/config-project
mkdir -p "$config_project/.fvm"
printf '%s\n' '{' '  "flutterSdkVersion": "3.22.1"' '}' > "$config_project/.fvm/fvm_config.json"
config_dart=$fvm_cache/versions/3.22.1/bin/cache/dart-sdk/bin/dart
make_dart "$config_dart"
run_print "$config_project"
assert_eq "$run_status" 0 'fvm_config.json SDK exit status'
assert_eq "$run_stdout" "$config_dart" 'fvm_config.json SDK selection'
printf '%s\n' 'PASS: .fvm/fvm_config.json SDK'
path_cache=$tmp_root/path-only-fvm-cache
path_project=$tmp_root/path-project
mkdir -p "$path_project"
path_dart=$test_path/dart
make_dart "$path_dart"
run_print "$path_project" "$path_cache"
assert_eq "$run_status" 0 'PATH Dart SDK exit status'
assert_eq "$run_stdout" "$path_dart" 'PATH Dart SDK is selected as an absolute path'
assert_eq "$run_stderr" '' 'PATH Dart SDK stderr'
printf '%s\n' 'PASS: (h) PATH Dart SDK fallback'

invalid_project=$tmp_root/invalid-pinned-project
mkdir -p "$invalid_project"
printf '%s\n' '{}' > "$invalid_project/.fvmrc"
run_print "$invalid_project"
assert_eq "$run_status" 0 'empty pinned version fallback exit status'
assert_eq "$run_stdout" "$default_dart" 'empty pinned version falls back to default SDK'
assert_contains "$run_stderr" 'invalid-pinned-project/.fvmrc' 'empty pinned version warning includes file path'
assert_contains "$run_stderr" 'falling back' 'empty pinned version warning explains fallback'
printf '%s\n' 'PASS: (i) empty pinned version warns and falls back'

empty_project=$tmp_root/empty-project
mkdir -p "$empty_project"
empty_path=$tmp_root/empty-path
mkdir -p "$empty_path"
empty_cache=$tmp_root/no-fvm-cache
if (cd "$empty_project" && HOME="$fake_home" FVM_CACHE_PATH="$empty_cache" \
  DART_LSP_PRINT=1 PATH="$empty_path" "$launcher") > "$tmp_root/stdout" 2> "$tmp_root/stderr"; then
  run_status=0
else
  run_status=$?
fi
run_stdout=$(cat "$tmp_root/stdout")
run_stderr=$(cat "$tmp_root/stderr")
assert_eq "$run_status" 127 'missing SDK exit status'
assert_eq "$run_stdout" '' 'missing SDK stdout'
assert_contains "$run_stderr" 'no executable Dart SDK found' 'missing SDK error'
printf '%s\n' 'PASS: missing SDK exits 127'

printf '%s\n' 'ALL PASS'
