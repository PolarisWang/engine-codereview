"""Tests for _send_review_segments (方案A): multi-segment review result must not overwrite
the first card (update-reply PATCHs whole card), so only seg[0] goes to the card and trailing
segs are appended as new messages.
"""
import orchestrate as O


def test_single_segment_only_updates_card(monkeypatch):
    """普通 review(1 段): 只 update-reply 一次, 不追加."""
    calls = []
    monkeypatch.setattr(O, "_run_py", lambda *a, **k: calls.append(a) or (0, "ok", ""))
    rc = O._send_review_segments("card1", ["完整 review"], "aid", "sec", "chat", "k1", "J-1", "P")
    assert rc == 0
    assert len(calls) == 1
    assert "update-reply" in calls[0][1]   # 只有 update-reply, 没有 reply-message
    assert all("reply-message" not in c[1] for c in calls)


def test_multi_segment_card_head_plus_append(monkeypatch):
    """超长 review(3 段): seg0 上卡(update-reply), seg1/seg2 追加(reply-message), 不互覆."""
    calls = []
    monkeypatch.setattr(O, "_run_py", lambda *a, **k: calls.append(a) or (0, "ok", ""))
    rc = O._send_review_segments("card1", ["HEAD", "MID", "TAIL"], "aid", "sec", "chat", "k1", "J-1", "P")
    assert rc == 0
    assert len(calls) == 3
    # _run_py("feishu_notifier.py", [argv...]) -> a[1] 是 argv
    argv0 = calls[0][1]
    assert "update-reply" in argv0
    assert "reply-message" in calls[1][1]
    assert "reply-message" in calls[2][1]
    # 每段内容完整(不互覆) -> 三条消息分别携带 HEAD/MID/TAIL
    import base64
    def body(argv):
        b = argv[-1]   # --message-base64 的值
        return base64.b64decode(b.encode()).decode()
    bodies = [body(c[1]) for c in calls]
    assert bodies[0] == "HEAD"
    assert bodies[1] == "MID"
    assert bodies[2] == "TAIL"


def test_empty_segments_no_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(O, "_run_py", lambda *a, **k: calls.append(a) or (0, "", ""))
    rc = O._send_review_segments("card1", [], "aid", "sec", "chat", "k1", "J-1", "P")
    assert rc == 0
    assert calls == []


def test_failed_append_returns_nonzero(monkeypatch):
    """后续段追加失败 -> 返回值不为 0."""
    def flaky(*a, **k):
        if a[1] and a[1][0] == "reply-message":
            return 1, "", "boom"
        return 0, "", ""
    monkeypatch.setattr(O, "_run_py", flaky)
    rc = O._send_review_segments("card1", ["HEAD", "MID"], "aid", "sec", "chat", "k1", "J-1", "P")
    assert rc != 0
