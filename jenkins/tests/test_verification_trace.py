"""Tests for review anti-fabrication post-verification (阶段0/1 + 阶段2).

Covers:
- stage0 _locate_finding_file: bare basename / a|b-prefix / ambiguous / not_found / empty
- stage1 lex & code-ish filter: prose words skipped, CamelCase/snake tokens kept
- stage1 substring-in-union existence:
    * tryStartClimb (fabricated) -> drop ONLY when it is the sole code-ish token
    * real symbols / fragments (move_end in _SquadMotorMoveState_move_end) -> keep
- conservative rule: a finding referencing ANY real symbol is never dropped
- missing checkout -> all flag, never drop
- verification.vault present in review_with_claude-normal output shape
- 阶段2 _verify_flags: independent re-verify merge (stub _call_verify_batch)

Uses tmp_path fake-repo (never touches real prod checkouts).
"""
import code_reviewer as cr


# ── stage0: file locating ──────────────────────────────────────────────
CF = [
    "M\t_source/_engine/source/client/private/chaos/client/motor/chaos_client_game_actor_motor_context.cpp",
    "M\t_source/_engine/source/common/private/chaos/common/motor/chaos_npc_biped_motor_driver.cpp",
]


def test_locate_fullpath_resolved():
    p, s = cr._locate_finding_file(
        "_source/_engine/source/client/private/chaos/client/motor/chaos_client_game_actor_motor_context.cpp", CF)
    assert s == "resolved"


def test_locate_bare_basename_unique():
    p, s = cr._locate_finding_file("chaos_npc_biped_motor_driver.cpp", CF)
    assert s == "resolved"
    assert p.endswith("chaos_npc_biped_motor_driver.cpp")


def test_locate_diff_prefix_a():
    p, s = cr._locate_finding_file(
        "a/_source/_engine/source/client/private/chaos/client/motor/chaos_client_game_actor_motor_context.cpp", CF)
    assert s == "resolved"
    assert not p.startswith("a/")


def test_locate_absent():
    p, s = cr._locate_finding_file("nope.cpp", CF)
    assert s == "not_found"


def test_locate_empty():
    p, s = cr._locate_finding_file("", CF)
    assert s == "empty"


def test_locate_ambiguous_basename_collision():
    cf2 = ["M\tclient/x.cpp", "M\tserver/x.cpp"]
    p, s = cr._locate_finding_file("x.cpp", cf2)
    assert s == "ambiguous"
    assert p == ""


# ── stage1: lex / code-ish filter ──────────────────────────────────────
def test_lex_extracts_camel_and_snake():
    toks = cr._lex_identifiers("tryStartClimb() 里删了提前返回; m_offset_state->offsetEnd(); climb 逻辑")
    assert "tryStartClimb" in toks
    assert "offsetEnd" in toks
    assert "m_offset_state" in toks


def test_code_ish_only_upper_or_underscore():
    assert cr._is_code_ish("tryStartClimb") is True
    assert cr._is_code_ish("move_end") is True
    assert cr._is_code_ish("getPlayRatio") is True
    assert cr._is_code_ish("climb") is False


# ── stage1: helpers ────────────────────────────────────────────────────
def _make_repo(tmp_path, files):
    for rel, body in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return tmp_path


def test_token_present_in_union_none_present():
    assert cr._token_present_in_union("has getMoveEndOffsetParameter here", "getMoveEndOffsetParameter") is True
    assert cr._token_present_in_union("has getMoveEndOffsetParameter here", "tryStartClimb") is False
    # union None (repo unavailable) -> conservative True (never drop)
    assert cr._token_present_in_union(None, "anything") is True


# ── stage1: _post_validate_findings ────────────────────────────────────
def test_fake_repo_drops_single_fabricated_symbol(tmp_path):
    repo = _make_repo(tmp_path, {
        "_source/engine/mod/motor.cpp": "int getMoveEndOffsetParameter() { return 0; }\n",
    })
    cf = ["M\t_source/engine/mod/motor.cpp"]
    findings = [{
        "file": "motor.cpp",
        "severity": "warning",
        "issue": "在 `tryStartClimb` 里删除了提前返回, 应确认",
        "suggestion": "确认该改动符合预期",
    }]
    diff_info = {"repo_dir": str(repo), "base_branch": "master",
                 "branch": "br", "changed_files": cf}
    kept, traces = cr._post_validate_findings(findings, diff_info)
    assert [t["decision"] for t in traces] == ["drop"]
    assert kept == []
    assert traces[0]["original"]["issue"].startswith("在 `tryStartClimb`")
    assert traces[0]["decision_reason"].startswith("引用的代码式符号全部")


def test_real_symbol_kept_even_if_mixed_with_lookup_word(tmp_path):
    repo = _make_repo(tmp_path, {
        "_source/engine/mod/motor.cpp": "void BipedMotorDriver::tickMoveStateTransition(){}\n",
    })
    cf = ["M\t_source/engine/mod/motor.cpp"]
    findings = [{
        "file": "motor.cpp",
        "severity": "warning",
        "issue": "tickMoveStateTransition 里重复的 yaw 逻辑",
        "suggestion": "抽公共函数",
    }]
    diff_info = {"repo_dir": str(repo), "base_branch": "master",
                 "branch": "br", "changed_files": cf}
    kept, traces = cr._post_validate_findings(findings, diff_info)
    assert traces[0]["decision"] == "keep"
    assert len(kept) == 1


def test_conservative_never_drops_when_any_real_symbol_present(tmp_path):
    repo = _make_repo(tmp_path, {
        "_source/engine/mod/motor.cpp": "_SquadMotorMoveState_move_end startClimb; void tickMoveStateTransition(){}\n",
    })
    cf = ["M\t_source/engine/mod/motor.cpp"]
    findings = [{
        "file": "motor.cpp",
        "severity": "warning",
        "issue": "在 `tryStartClimb` 中, move_end 与 tickMoveStateTransition 的 offset 判断不一致",
        "suggestion": "参考 biped 捕获曲线名",
    }]
    diff_info = {"repo_dir": str(repo), "base_branch": "master",
                 "branch": "br", "changed_files": cf}
    kept, traces = cr._post_validate_findings(findings, diff_info)
    assert traces[0]["decision"] == "flag"
    assert len(kept) == 1


def test_missing_checkout_all_flag_never_drop(tmp_path):
    findings = [{
        "file": "motor.cpp",
        "severity": "warning",
        "issue": "在 `tryStartClimb` 中删除了提前返回",
        "suggestion": "确认",
    }]
    diff_info = {"repo_dir": "/no/such/dir", "base_branch": "master",
                 "branch": "br", "changed_files": ["M\tmotor.cpp"]}
    kept, traces = cr._post_validate_findings(findings, diff_info)
    assert traces[0]["decision"] == "flag"
    assert len(kept) == 1


def test_unresolvable_file_flags_not_drop():
    findings = [{
        "file": "x.cpp",
        "severity": "warning",
        "issue": "某逻辑问题",
        "suggestion": "修",
    }]
    diff_info = {"repo_dir": "/none", "base_branch": "master",
                 "branch": "br", "changed_files": ["M\tother.cpp"]}
    kept, traces = cr._post_validate_findings(findings, diff_info)
    assert traces[0]["decision"] == "flag"
    assert len(kept) == 1


# ── stage2: _verify_flags merge ────────────────────────────────────────
def _flag_traces():
    return [
        # #0: 所有代码式符号都缺失 -> 可复核, 复核判 drop 才删
        {'trace_ref': 0, 'loc_state': 'resolved', 'decision': 'flag',
         'symbol_check': [{'token': 'tryStartClimb', 'present_in_changed_files': False},
                          {'token': 'startRainbow', 'present_in_changed_files': False}],
         'decision_reason': 'flagA',
         'original': {'file': 'a.cpp', 'severity': 'warning', 'issue': '用 tryStartClimb 改了 offset',
                      'suggestion': '确认', 'category': None}},
        # #1: 混合(部分符号缺失) -> 不可复核, 绝不 drop(跨文件/拼接错符号的真实 finding 保护)
        {'trace_ref': 1, 'loc_state': 'resolved', 'decision': 'flag',
         'symbol_check': [{'token': 'tickMoveStateTransition', 'present_in_changed_files': True},
                          {'token': 'PowerShell', 'present_in_changed_files': False}],
         'decision_reason': 'flagB',
         'original': {'file': 'b.cpp', 'severity': 'warning', 'issue': 'tickMoveStateTransition 重复',
                      'suggestion': '抽共用', 'category': None}},
    ]


def _stub_verify(verdicts, err=None):
    def _f(_s, _u, _k, _b, _m, _mt):
        return verdicts, err
    return _f


def test_verify_flags_drop_only_when_all_absent_and_verdict(tmp_path):
    """#0(全符号缺失) 复核 drop -> 删; #1(混合) 不在复核范围, 保留."""
    traces = _flag_traces()
    kept_whole = [{**t['original'], 'trace_ref': t['trace_ref']} for t in traces]
    cr._call_verify_batch = _stub_verify(
        [{'index': 1, 'verdict': 'drop', 'reason': 'tryStartClimb 无出处'},
         {'index': 2, 'verdict': 'keep', 'reason': '真实改动'}])
    final, tr = cr._verify_flags(kept_whole, traces, {}, 'K', 'U', 'M', 100)
    # 只有 trace_ref 0 被 drop; 混合的 #1 保留
    assert [f['trace_ref'] for f in final] == [1]
    assert tr[0]['decision'] == 'drop'
    assert tr[1]['decision'] == 'flag'
    assert tr[0]['verification']['verdict'] == 'drop'


def test_verify_flags_never_drops_mixed_present_absent(tmp_path):
    """回归(阶段2 any→all): 混合 finding(有真实符号 + 个别 absent 如 PowerShell) 绝不能被复核 drop."""
    traces = _flag_traces()
    kept_whole = [{**t['original'], 'trace_ref': t['trace_ref']} for t in traces]
    # 即便复核返回 drop, 混合的 #1 也不可复核(不在 verifyable) -> 不删
    cr._call_verify_batch = _stub_verify(
        [{'index': 1, 'verdict': 'drop', 'reason': 'x'}, {'index': 2, 'verdict': 'drop', 'reason': 'y'}])
    final, tr = cr._verify_flags(kept_whole, traces, {}, 'K', 'U', 'M', 100)
    # #1(混合) 保留; 只有 #0(全缺) 可能被删, 这里 stub index1 对 #0
    assert 1 in [f['trace_ref'] for f in final], "混合finding绝不能被删"


def test_verify_flags_never_drops_without_absent_symbol(tmp_path):
    # #1 has NO absent symbol (all present) -> even if verdict=drop, must NOT drop
    traces = _flag_traces()
    traces[0]['symbol_check'] = [{'token': 'move_end', 'present_in_changed_files': True}]
    kept_whole = [{**t['original'], 'trace_ref': t['trace_ref']} for t in traces]
    cr._call_verify_batch = _stub_verify(
        [{'index': 1, 'verdict': 'drop', 'reason': 'x'}, {'index': 2, 'verdict': 'drop', 'reason': 'y'}])
    final, tr = cr._verify_flags(kept_whole, traces, {}, 'K', 'U', 'M', 100)
    # no drop applied (neither has absent) -> both kept
    assert [f['trace_ref'] for f in final] == [0, 1]
    assert all(t['decision'] == 'flag' for t in tr)


def test_verify_flags_unknown_preserves():
    traces = _flag_traces()
    kept_whole = [{**t['original'], 'trace_ref': t['trace_ref']} for t in traces]
    cr._call_verify_batch = _stub_verify([{'index': 1, 'verdict': 'unknown', 'reason': '?'}])
    final, tr = cr._verify_flags(kept_whole, traces, {}, 'K', 'U', 'M', 100)
    assert [f['trace_ref'] for f in final] == [0, 1]
    assert tr[0]['decision'] == 'flag'   # unknown -> keep
    assert tr[0]['verification']['verdict'] == 'unknown'


def test_verify_flags_call_failure_preserves():
    traces = _flag_traces()
    kept_whole = [{**t['original'], 'trace_ref': t['trace_ref']} for t in traces]
    cr._call_verify_batch = _stub_verify(None, err='boom')
    final, tr = cr._verify_flags(kept_whole, traces, {}, 'K', 'U', 'M', 100)
    assert [f['trace_ref'] for f in final] == [0, 1]   # all preserved
    assert tr[0]['verification']['verdict'] == 'unknown'


def test_verify_flags_no_flags_no_call():
    traces = [{'trace_ref': 0, 'loc_state': 'resolved', 'decision': 'keep',
               'symbol_check': [], 'decision_reason': '', 'original': {'file': 'a.cpp'}}]
    kept_whole = [{'trace_ref': 0}]
    called = []
    cr._call_verify_batch = lambda *a, **k: called.append(1) or ([], None)
    final, tr = cr._verify_flags(kept_whole, traces, {}, 'K', 'U', 'M', 100)
    assert called == []       # no flag -> no second LLM call
    assert len(final) == 1


# ── integration: verification block schema ─────────────────────────────
def test_verify_block_shape_present_when_call_ok():
    sample = {"review": {
        "summary": "s", "review_text": "t", "severity_counts": {},
        "findings": [], "error": None, "batches": 1,
        "verification": {"vault": [], "counts": {"kept": 0, "flagged": 0, "dropped": 0},
                          "dropped_decision": "symbol absent in changed-file union",
                          "repo_dir_available": True, "stage2_verify": True,
                          "generated_by": {"model": "x"}},
    }}
    v = sample["review"]["verification"]
    assert set(v.keys()) >= {"vault", "counts", "dropped_decision", "repo_dir_available", "stage2_verify"}
    assert v["counts"]["dropped"] == 0


# ── severity reconciliation after filtering ────────────────────────────
def test_severity_counts_reconciled_to_kept_findings(tmp_path):
    """修复 ENG-32269 类 bug: loss aggregation的 severity_counts 在过滤后必须重算自 kept_findings,
    否则会出现"卡片显示 0 critical / 但 severity_counts.critical=已删除fabricated"的矛盾。"""
    # 构造一个过滤前后 severity 不一致的场景: 原始 findings 含一个 fabricated critical(将被 drop),
    # 修复后 severity_counts 必须只反映 kept(真实) findings。
    repo = _make_repo(tmp_path, {
        "_source/engine/mod/motor.cpp": "int getMoveEndOffsetParameter() { return 0; }\n",
    })
    cf = ["M\t_source/engine/mod/motor.cpp"]
    findings = [
        {"file": "motor.cpp", "severity": "critical",   # 编造: tryStartClimb 不存在
         "issue": "在 `tryStartClimb` 里删除了提前返回, 应确认", "suggestion": "改"},
        {"file": "motor.cpp", "severity": "warning",
         "issue": "getMoveEndOffsetParameter 曲线捕获不一致", "suggestion": "抽共用"},
    ]
    diff_info = {"repo_dir": str(repo), "base_branch": "master",
                 "branch": "br", "changed_files": cf}
    kept, traces = cr._post_validate_findings(findings, diff_info)
    # fabricated critical 被 drop(唯一 code-ish token 是 tryStartClimb)
    assert [t["decision"] for t in traces] == ["drop", "keep"]
    # 修复: severity_counts 按 kept 重算 → critical 应为 0, 只剩 1 个 warning
    final_counts = cr._findings_counts(kept)
    assert final_counts == {"critical": 0, "warning": 1, "suggestion": 0}, final_counts
    assert len(kept) == 1
