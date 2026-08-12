"""Tests for review anti-fabrication (#1 prompt + #3 0-change diff filter).

#3: files that appear in changed_files but have NO real `+`/`-` content line (only EOL /
context / path headers) must be dropped before reaching the model — otherwise the model
tends to invent findings on them (root cause of the fabricated 'tryClimb()' finding on an
unchanged climbing block).
"""
import code_reviewer as cr

REAL_DIFF = """diff --git a/a/motor.cpp b/a/motor.cpp
--- a/a/motor.cpp
+++ b/a/motor.cpp
@@ -1,3 +1,3 @@
-old climbing
+new climbing
 check_before(x)
+added_line
"""
EOL_ONLY_DIFF = """diff --git a/b/only_eol.cpp b/b/only_eol.cpp
--- a/b/only_eol.cpp
+++ b/b/only_eol.cpp
@@ -1,1 +1,1 @@
 context line
\\ No newline at end of file
"""
NO_CHANGE_DIFF = """diff --git a/c/only_header.cpp b/c/only_header.cpp
--- a/c/only_header.cpp
+++ b/c/only_header.cpp
@@ -1,1 +1,1 @@
 (empty diff)
"""


def test_real_change_detected():
    assert cr._diff_block_has_real_changes(REAL_DIFF) is True


def test_eol_only_not_a_change():
    assert cr._diff_block_has_real_changes(EOL_ONLY_DIFF) is False


def test_sanitizer_drops_no_change_block():
    blocks = cr._split_diff_by_files(REAL_DIFF + EOL_ONLY_DIFF)
    kept, dropped = cr._sanitize_diff_blocks(blocks)
    assert len(kept) == 1
    assert any("motor.cpp" in b for b in kept)
    assert any("only_eol.cpp" in p for p in dropped)
    assert not any("motor.cpp" in p for p in dropped)


def test_prompt_contains_anti_fabrication():
    # #1: the review instructions now forbid inventing symbols / reviewing unchanged code
    assert "禁止编造" in cr.DEFAULT_REVIEW_INSTRUCTIONS
    assert "不存在的函数" in cr.DEFAULT_REVIEW_INSTRUCTIONS
    assert "`+`/`-`" in cr.DEFAULT_REVIEW_INSTRUCTIONS
