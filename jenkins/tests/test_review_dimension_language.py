"""Regression tests for P2: language-aware, non-mandatory review dimensions.

Previously `_load_skill_review_instructions()` force-injected the first 14 `##/###`
headings of EVERY reference guide (including the Web/JS "performance" guide with its
`内存管理`/`常见内存泄漏` sections, and `python`) with mandatory "至少覆盖" semantics.
For a C++ game engine that produced off-topic memory/LOD noise and forced the model
to write findings unrelated to the diff.

Fix:
- dimensions are "供参考" (reference-only): only check when the diff actually
  touches the topic, never fabricate findings to fill dimensions;
- language-aware: engine/game -> C++ guide only (no Python / front-end memory
  dimensions); only web-type repos pull in the performance guide.
"""
import code_reviewer as cr


def test_engine_review_has_no_web_memory_dimension():
    p = cr._load_skill_review_instructions("engine") or ""
    assert "内存管理" not in p       # web/JS memory section no longer forced on C++
    assert "常见内存泄漏" not in p
    assert "【Python】" not in p     # no Python dimension on a C++ engine


def test_engine_review_keeps_cpp_dimensions():
    p = cr._load_skill_review_instructions("engine") or ""
    assert "C++" in p
    assert "Ownership and RAII" in p      # the C++ ownership/RAII dims remain relevant


def test_engine_review_is_reference_not_mandatory():
    p = cr._load_skill_review_instructions("engine") or ""
    assert "至少覆盖" not in p          # mandatory semantics removed
    assert "供参考" in p                # reference-only semantics present
    assert "编造" in p                  # explicit anti-fabrication instruction


def test_web_review_keeps_performance_dimension():
    p = cr._load_skill_review_instructions("game_web") or ""
    assert "性能(Performance)" in p
    assert "内存管理" in p              # only web-type repos get the perf/memory guide


def test_default_no_repo_still_sane():
    # No repo_type (default) -> C++-leaning, no mandatory fabrication, no Python.
    p = cr._load_skill_review_instructions() or ""
    assert "至少覆盖" not in p
    assert "【Python】" not in p
