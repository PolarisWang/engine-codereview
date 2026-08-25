"""Clean up review-bot scratch files during `stop`.

Two scoped passes:

1. ``{skill_dir}/_tmp/`` — files directly in that dir whose suffix matches
   a known scratch extension (``.json``, ``.diff``, ``.stat``).
2. ``{skill_dir}/scripts/`` — per-spawn artifact-writeback scratch
   scripts agents drop here (``_write_artifact_*.py``). Topic agents
   sometimes Write a one-shot helper next to the real scripts and
   never clean it up; we sweep them on stop.

Both passes are intentionally narrow: no recursion, no wildcards beyond
the explicit prefix match, no touching files with other names. Safer
than ``rm -f``.
"""
import pathlib
import sys


TMP_SUFFIXES = {".json", ".diff", ".stat"}
ARTIFACT_SCRIPT_PREFIX = "_write_artifact_"
ARTIFACT_SCRIPT_SUFFIX = ".py"


def _clean_tmp_dir(tmp_dir):
    if not tmp_dir.is_dir():
        print(f"no _tmp directory at {tmp_dir}")
        return
    removed = 0
    kept = []
    for entry in tmp_dir.iterdir():
        if not entry.is_file():
            continue
        if entry.suffix in TMP_SUFFIXES:
            entry.unlink()
            removed += 1
        else:
            kept.append(entry.name)
    print(f"removed {removed} scratch files from {tmp_dir}")
    if kept:
        print(f"kept (unknown suffix): {', '.join(kept)}")


def _clean_artifact_scripts(scripts_dir):
    if not scripts_dir.is_dir():
        return
    removed = []
    for entry in scripts_dir.iterdir():
        if not entry.is_file():
            continue
        name = entry.name
        if (name.startswith(ARTIFACT_SCRIPT_PREFIX)
                and name.endswith(ARTIFACT_SCRIPT_SUFFIX)):
            entry.unlink()
            removed.append(name)
    if removed:
        print(f"removed {len(removed)} artifact-writeback scratch scripts "
              f"from {scripts_dir}: {', '.join(removed)}")
    else:
        print(f"no artifact-writeback scratch in {scripts_dir}")


def main():
    skill_dir = pathlib.Path(__file__).resolve().parent.parent
    _clean_tmp_dir(skill_dir / "_tmp")
    _clean_artifact_scripts(skill_dir / "scripts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
