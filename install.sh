#!/usr/bin/env bash
# Install the generic project files after checking prerequisites and conflicts.
set -euo pipefail

usage() {
    cat <<'USAGE'
Usage: bash install.sh [options]

  --target DIR                 Project to install into (default: this repository)
  --python EXECUTABLE          Python 3.10+ interpreter (default: python3 or python)
  --opencode-command-json JSON OpenCode V1 1.2.0+ executable/interpreter arguments
  --check                      Check prerequisites and planned changes only
  --help                       Show this help

Installs agents, the skill, runner, templates, and a blank local queue.
Existing queue contents are preserved; conflicting files abort installation.
Install Python, the supported OpenCode CLI, and provider authentication separately.
USAGE
}

fail() { printf 'install: %s\n' "$1" >&2; exit 2; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
target=$script_dir
python_command=''
opencode_command='["opencode"]'
check_only=0

while (( $# > 0 )); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --check) check_only=1; shift ;;
        --target|--python|--opencode-command-json)
            (( $# >= 2 )) || fail "Missing value for $1."
            [[ -n "$2" ]] || fail "Empty value for $1."
            case "$1" in
                --target) target=$2 ;;
                --python) python_command=$2 ;;
                --opencode-command-json) opencode_command=$2 ;;
            esac
            shift 2 ;;
        *) fail "Unknown option: $1. Use --help." ;;
    esac
done

if [[ -z "$python_command" ]]; then
    if command -v python3 >/dev/null 2>&1; then python_command=python3
    elif command -v python >/dev/null 2>&1; then python_command=python
    else fail 'Python 3.10+ was not found. Supply --python EXECUTABLE.'
    fi
fi
if [[ "$python_command" == [A-Za-z]:* ]] && command -v cygpath >/dev/null 2>&1; then
    python_command=$(cygpath -u "$python_command")
fi
command -v "$python_command" >/dev/null 2>&1 || fail 'The selected Python interpreter was not found.'

# MSYS would otherwise rewrite paths embedded inside the JSON argument.
python_source=$script_dir
python_target=$target
if command -v cygpath >/dev/null 2>&1; then
    python_platform=$("$python_command" -c 'import os; print(os.name)')
    if [[ "$python_platform" == nt ]]; then
        python_source=$(cygpath -m "$script_dir")
        python_target=$(cygpath -m "$target")
    fi
fi

MSYS2_ARG_CONV_EXCL='*' PYTHONDONTWRITEBYTECODE=1 "$python_command" - "$python_source" "$python_target" "$opencode_command" "$check_only" <<'PY'
import sys

if sys.version_info < (3, 10):
    print("install: Python 3.10 or newer is required.", file=sys.stderr)
    raise SystemExit(2)

import importlib.util
import json
import os
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
source = Path(sys.argv[1]).resolve()
target = Path(sys.argv[2]).expanduser().resolve()
check_only = sys.argv[4] == "1"


def destination(relative):
    path = target / relative
    if not path.resolve().is_relative_to(target):
        raise ValueError("An installation path leaves the target project.")
    current = target
    for part in Path(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Installation paths cannot contain symlinks.")
        if current.exists() and current != path and not current.is_dir():
            raise ValueError("A parent installation path is not a directory.")
    if path.exists() and not path.is_file():
        raise ValueError("A destination file path is not a regular file.")
    return path


def main():
    if target.exists() and not target.is_dir():
        raise ValueError("The target project path is not a directory.")
    prefix = json.loads(sys.argv[3])
    if not isinstance(prefix, list) or not prefix or any(not isinstance(arg, str) for arg in prefix):
        raise ValueError("--opencode-command-json must be a non-empty JSON array of strings.")
    spec = importlib.util.spec_from_file_location("experiment_runner", source / "scripts/experiment_runner.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.reviewer_command(source, prefix)

    files = [
        ".opencode/agents/research-agent.md",
        ".opencode/agents/experiment-reviewer.md",
        ".opencode/agents/paper-reviewer.md",
        ".opencode/agents/doc-reviewer.md",
        ".opencode/skills/research-optimization/SKILL.md",
        "scripts/experiment_runner.py",
        "templates/project-contract.template.md",
        "templates/experiment-plan.template.md",
        "templates/experiment-queue.template.csv",
        "templates/experiment-log.template.csv",
        "templates/review-report.template.md",
        "templates/decision-record.template.md",
    ]
    additions = []
    for name in files:
        original = source / name
        if not original.resolve().is_relative_to(source):
            raise ValueError("A source file leaves the installer repository.")
        data = original.read_bytes()
        path = destination(name)
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError(f"Conflicting destination file: {name}; review it before installing.")
        else:
            additions.append((path, data))

    queue = destination("experiment-queue.csv")
    if not queue.exists():
        additions.append((queue, (source / "templates/experiment-queue.template.csv").read_bytes()))

    ignore = destination(".gitignore")
    old_ignore = ignore.read_bytes() if ignore.exists() else None
    ignore_data = old_ignore if old_ignore is not None else (source / ".gitignore").read_bytes()
    text = ignore_data.decode("utf-8-sig")
    required = [".research/", "experiment-queue.csv", "experiment-log.csv", "__pycache__/", "*.py[cod]", ".env", ".env.*"]
    existing_patterns = {line.strip() for line in text.splitlines()}
    missing = [line for line in required if line not in existing_patterns]
    if missing:
        separator = b"" if not ignore_data or ignore_data.endswith(b"\n") else b"\n"
        ignore_data += separator + ("\n# Auto Research local state\n" + "\n".join(missing) + "\n").encode("utf-8")

    print("Python and OpenCode prerequisites passed.")
    if check_only:
        print(f"Preflight passed: {len(additions)} files to add; existing queue contents will be preserved.")
        return

    target.mkdir(parents=True, exist_ok=True)
    for path, data in additions:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation preserves files added after preflight too.
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())

    if old_ignore is None:
        with ignore.open("xb") as handle:
            handle.write(ignore_data)
    elif ignore_data != old_ignore:
        # Keep the original content and append only missing ignore patterns.
        temp_name = None
        try:
            with tempfile.NamedTemporaryFile(dir=target, prefix=".agent-ignore-", delete=False) as handle:
                temp_name = handle.name
                handle.write(ignore_data)
                handle.flush()
                os.fsync(handle.fileno())
            if ignore.read_bytes() != old_ignore:
                raise ValueError(".gitignore changed during installation; retry after reviewing it.")
            os.replace(temp_name, ignore)
        finally:
            if temp_name and os.path.exists(temp_name):
                os.unlink(temp_name)

    print(f"Installed {len(additions)} new files. Existing matching files and queue entries were preserved.")
    print("Fill experiment-queue.csv with an approved plan before running an experiment.")
    print("Then use your Python interpreter to run scripts/experiment_runner.py run --id <experiment-id>.")


try:
    main()
except Exception as error:
    print(f"install: {error}", file=sys.stderr)
    raise SystemExit(2)
PY
