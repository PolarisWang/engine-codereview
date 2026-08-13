"""Tests for review conclusion confidence layering (阶段1.5: keep/drop/warn, no human review).

Classifies each finding into:
- keep   : objectively verifiable fact (e.g. a true garbage/accidentally-committed file)
- drop   : objectively false / misreport (finding points at a garbage file and claims a
           code bug that cannot exist there / a clear fabrication)
- warn   : semantic-inference / hedged conclusion (锁/循环边界/健壮性推断, 或用了
           '可能/若/建议确认' 话术) — kept in the report but flagged as 存疑, severity not raised.

Deterministic rules only — no second LLM, no human. Any uncertainty → warn, never误杀.
"""
import code_reviewer as cr


def _f(file, issue="x", suggestion="y", category="architecture", severity="warning"):
    return {"file": file, "issue": issue, "suggestion": suggestion,
            "category": category, "severity": severity}


# ── keep: objectively true facts ────────────────────────────────────────
def test_garbage_file_finding_kept():
    """指向真实误提交垃圾文件(文件名含 '(')且 finding 在讲删除/清理 -> 客观事实, keep."""
    f = _f("mempool/lockingDynamicTlsfAllocator.cpp(40",
           issue="此垃圾文件需 git rm 清理", suggestion="git rm + 忽略",
           category="quality")
    verdict, reason = cr._classify_confidence(f, [], "/tmp")
    assert verdict == "keep", reason


# ── drop: garbage file with a fabricated code-bug claim ─────────────────
def test_garbage_file_with_fake_bug_dropped():
    """finding 指向垃圾文件却硬造一个不存在于其中的代码 bug -> drop."""
    f = _f("mempool/lockingDynamicTlsfAllocator.cpp(41",
           issue="此文件存在锁竞争死锁", suggestion="加锁",
           category="architecture")
    verdict, reason = cr._classify_confidence(f, [], "/tmp")
    assert verdict == "drop", reason


# ── warn: hedged / semantic-inference ───────────────────────────────────
def test_hedged_finding_warn():
    f = _f("x/bigAlloc.cpp", "若 GetBlockSize 无锁且 malloc 持锁可能死锁", "建议确认", "architecture")
    verdict, _ = cr._classify_confidence(f, [], "/tmp")
    assert verdict == "warn"


def test_concrete_architecture_kept():
    """明确架构主张(无猜测话术)不应被过度降级为 warn; 保留(结论是否成立以真代码为准)."""
    f = _f("x/bigAlloc.cpp", "锁策略不一致导致一致性缺陷", "统一锁策略", "architecture")
    verdict, _ = cr._classify_confidence(f, [], "/tmp")
    assert verdict == "keep"


def test_quality_suggestion_warn():
    f = _f("x/a.cpp", "命名风格建议", "改命名", "quality", severity="suggestion")
    verdict, _ = cr._classify_confidence(f, [], "/tmp")
    assert verdict == "warn"


# ── _apply_confidence integration ──────────────────────────────────────
def test_apply_confidence_drops_and_warns_and_keeps():
    findings = [
        _f("garbage/foo.cpp(40", "此文件是垃圾需删除", "rm", "quality"),   # -> keep
        _f("garbage/bar.cpp(41", "此文件死锁", "加锁", "architecture"),    # -> drop
        _f("x/big.cpp", "若 X 无锁可能死锁", "建议确认", "architecture"),  # -> warn
    ]
    diff_info = {"repo_dir": None, "changed_files": ["garbage/foo.cpp(40",
                                                      "garbage/bar.cpp(41", "x/big.cpp"]}
    kept, notes = cr._apply_confidence(findings, diff_info)
    conf = [f["confidence"] for f in kept]
    dropped = notes.get("drop") or []
    # foo -> keep, big -> warn; bar -> drop (removed)
    assert set(conf) == {"keep", "warn"}, conf
    assert sum(1 for c in conf if c == "keep") == 1
    assert sum(1 for c in conf if c == "warn") == 1
    assert len(dropped) == 1 and dropped[0]["file"] == "garbage/bar.cpp(41"
    assert notes.get("warn") == 1


def test_severity_counts_recomputed_after_confidence():
    """counts must reflect final findings (post confidence drop/warn), not pre-filter total."""
    findings = [
        {"file": "garbage/bad.cpp(1", "severity": "critical",
         "issue": "此文件死锁", "suggestion": "加锁", "category": "architecture"},   # garbage+bug -> drop
        {"file": "x/real.cpp", "severity": "warning",
         "issue": "若 realFunc 边界可能越界", "suggestion": "建议确认", "category": "architecture"},  # warn
        {"file": "x/real2.cpp", "severity": "warning",
         "issue": "明确释放遗漏", "suggestion": "补释放", "category": "security"},    # keep
    ]
    diff_info = {"repo_dir": None, "changed_files": ["garbage/bad.cpp(1", "x/real.cpp", "x/real2.cpp"]}
    kept, _ = cr._apply_confidence(findings, diff_info)
    counts = cr._findings_counts(kept)
    # dropped the critical garbage one; kept 1 warning (warn) + 1 warning (keep)
    assert counts == {"critical": 0, "warning": 2, "suggestion": 0}, counts


# ── A-3: 文本冲突判定 ─────────────────────────────────────────────────
def _mk_repo(tmp_path, files):
    import pathlib
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return str(tmp_path)


A3_SRC = """
u64 BigSizeAllocator::GetBlockSize( u64 block ) const {
    CMP_SCOPE_SHARED_LOCK( m_lock );
    return block;
}
void BigSizeAllocator::Other() { const u64 x = GetBlockSize( a ); }
"""


def test_A3_detects_lock_contradiction(tmp_path):
    """finding 断言 `GetBlockSize 无锁`, 但源码 GetBlockSize 内有锁宏 -> 冲突(drop)."""
    import code_reviewer as cr
    rd = _mk_repo(tmp_path, {"_source/mod/bigSizeAllocator.cpp": A3_SRC})
    f = {"file": "_source/mod/bigSizeAllocator.cpp", "severity": "critical",
         "issue": "但 GetBlockSize 未被修改（仍无锁）; 引致一致性缺陷",
         "suggestion": "统一锁", "category": "architecture"}
    verdict, reason = cr._classify_confidence(f, [], rd)
    assert verdict == "drop", reason
    assert "GetBlockSize" in reason


def test_A3_no_conflict_when_same_lock_exists_or_hedged(tmp_path):
    """finding 用 hedge('若可能') 或根本没断言 '无锁' -> 不 A-3 drop(宁 keep/warn)."""
    import code_reviewer as cr
    rd = _mk_repo(tmp_path, {"_source/mod/bigSizeAllocator.cpp": A3_SRC})
    # hedged 表述 -> A-3 不该开枪(那是 warn 层)
    f1 = {"file": "_source/mod/bigSizeAllocator.cpp", "severity": "warning",
          "issue": "若 GetBlockSize 重入同一把锁可能死锁", "suggestion": "建议确认",
          "category": "architecture"}
    assert cr._classify_confidence(f1, [], rd)[0] in ("warn", "keep")
    # 断言的是别的函数(Other 无锁宏) -> 不冲突 -> 不 drop
    f2 = {"file": "_source/mod/bigSizeAllocator.cpp", "severity": "warning",
          "issue": "Other 未被修改（无锁保护）导致并发不一致", "suggestion": "加锁",
          "category": "architecture"}
    v2, _ = cr._classify_confidence(f2, [], rd)
    assert v2 != "drop"


def test_function_body_treats_call_site_as_not_definition(tmp_path):
    """_function_body 必须跳过调用点(Other 里的 GetBlockSize( a )), 只认定义处."""
    import code_reviewer as cr
    rd = _mk_repo(tmp_path, {"_source/mod/bigSizeAllocator.cpp": A3_SRC})
    content = open(tmp_path / "_source/mod/bigSizeAllocator.cpp", encoding="utf-8").read()
    body = cr._function_body(content, "GetBlockSize")
    assert body is not None
    assert "CMP_SCOPE_SHARED_LOCK" in body      # 取的是定义体, 不是调用点所在函数
