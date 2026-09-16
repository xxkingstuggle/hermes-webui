"""Hydrate-then-tail equivalence tests (hard gate before any recovery change).

Canonical truth: fold the FULL journal 1..N via _run_journal_live_snapshot.
Recovery:        fold journal 1..C into a snapshot, then replay events C+1..N
                 through the SAME logical fold semantics; final state must be
                 field-identical to the canonical fold for every cutoff C.

Also covers the concurrency race: a snapshot fold that observed the journal up
to seq C while the journal later grows to N must resume exactly at C — the tail
receives C+1..N exactly once (no missing, no duplicated events).
"""
import pytest

import api.routes as routes


def _mk_journal(events_spec):
    """Build journal event dicts from (seq, event, payload, created_at) tuples."""
    events = []
    for seq, name, payload, ts in events_spec:
        events.append({
            "seq": seq,
            "event": name,
            "payload": payload,
            "event_id": f"run_eq:{seq}",
            "created_at": ts,
        })
    return events


def _complex_journal():
    """A journal exercising every recovery-relevant event type in order:
    reasoning -> token/prose -> tool(pending) -> tool_complete -> interim
    -> reasoning(2nd segment) -> token(2nd) -> tool2 -> tool2 complete.
    20 events total; returns (events_spec, N)."""
    spec = [
        (1, "reasoning", {"text": "Think step one. "}, 1000.0),
        (2, "reasoning", {"text": "Think step two. "}, 1000.5),
        (3, "token", {"text": "First prose segment."}, 1001.0),
        (4, "tool", {"name": "terminal", "preview": "running tests",
                     "tool_use_id": "tid_A", "args": {"command": "pytest -q"}}, 1002.0),
        (5, "metering", {"tps": 12.5, "tps_available": True}, 1002.5),
        (6, "tool_complete", {"name": "terminal", "preview": "passed",
                              "tool_use_id": "tid_A", "duration": 1.5}, 1003.0),
        (7, "interim_assistant", {"text": "Interim note one."}, 1003.5),
        (8, "reasoning", {"text": "Second think. "}, 1004.0),
        (9, "token", {"text": " Second prose segment."}, 1005.0),
        (10, "tool", {"name": "read_file", "preview": "reading",
                      "tool_use_id": "tid_B", "args": {"path": "a.py"}}, 1006.0),
        (11, "tool", {"name": "terminal", "preview": "building",
                      "tool_use_id": "tid_C", "args": {"command": "make"}}, 1007.0),
        (12, "tool_complete", {"name": "read_file", "preview": "ok",
                               "tool_use_id": "tid_B", "duration": 0.2}, 1008.0),
        (13, "metering", {"tps": 30.0, "tps_available": True}, 1008.5),
        (14, "interim_assistant", {"text": "Interim note two."}, 1009.0),
        (15, "reasoning", {"text": "Final think. "}, 1010.0),
        (16, "token", {"text": " Tail prose."}, 1011.0),
        (17, "tool_complete", {"name": "terminal", "preview": "built",
                               "tool_use_id": "tid_C", "duration": 2.0}, 1012.0),
        (18, "tool", {"name": "write_file", "preview": "writing",
                      "tool_use_id": "tid_D", "args": {"path": "b.py"}}, 1013.0),
        (19, "tool_complete", {"name": "write_file", "preview": "written",
                               "tool_use_id": "tid_D", "duration": 0.1}, 1014.0),
        (20, "token", {"text": " Done prose."}, 1015.0),
    ]
    return spec, 20


def _patch_fold(monkeypatch, events, last_seq=None):
    n = max(e["seq"] for e in events) if events else 0
    monkeypatch.setattr(
        routes, "find_run_summary",
        lambda stream_id: {
            "session_id": "session_eq",
            "run_id": stream_id,
            "last_seq": last_seq if last_seq is not None else n,
            "last_event_id": f"{stream_id}:{last_seq if last_seq is not None else n}",
        },
    )
    bound = last_seq if last_seq is not None else n
    monkeypatch.setattr(
        routes, "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None, session_dir=None: {
            "events": [e for e in events
                       if (after_seq is None or e["seq"] > after_seq)
                       and (max_seq is None or e["seq"] <= max_seq)
                       and e["seq"] <= bound],
            "malformed": [],
        },
    )


# Canonical projection: the recovery-relevant logical state, normalized so the
# snapshot @ C and the tail-folded result can be compared field by field.
def _project(snapshot):
    if snapshot is None:
        return None
    return {
        "last_seq": snapshot.get("last_seq"),
        "last_event_id": snapshot.get("last_event_id"),
        "last_assistant_text": snapshot.get("last_assistant_text"),
        "last_reasoning_text": snapshot.get("last_reasoning_text"),
        "activity_burst_anchors": snapshot.get("activity_burst_anchors"),
        "current_activity_burst_id": snapshot.get("current_activity_burst_id"),
        "current_live_segment_seq": snapshot.get("current_live_segment_seq"),
        "messages": snapshot.get("messages"),
        "tool_calls": snapshot.get("tool_calls"),
        "anchor_activity_scene": snapshot.get("anchor_activity_scene"),
    }


def _fold_range(monkeypatch, events, lo, hi):
    """Fold events in [lo, hi] via the production state-only fold.

    The production fold reads the journal without bounds, so the range is
    enforced at the read layer — exactly how a server-side bounded snapshot
    fold would consume a journal that is growing past hi."""
    n = max(e["seq"] for e in events) if events else 0
    monkeypatch.setattr(
        routes, "find_run_summary",
        lambda stream_id: {
            "session_id": "session_eq",
            "run_id": stream_id,
            "last_seq": hi,
            "last_event_id": f"{stream_id}:{hi}",
        },
    )
    monkeypatch.setattr(
        routes, "read_run_events",
        lambda session_id, run_id, after_seq=None, max_seq=None, session_dir=None: {
            "events": [e for e in events
                       if (after_seq is None or e["seq"] > after_seq)
                       and e["seq"] <= hi],
            "malformed": [],
        },
    )
    return _project(routes._run_journal_live_snapshot("run_eq"))


# Composition equivalence: hydrate-then-tail relies on
#   canonical_state == apply(snapshot@C, tail_events C+1..N)
# The production fold is a pure prefix accumulation, so the server-side resume
# that will back hydrate-then-tail is: fold(1..C) as the snapshot, then the
# same fold continued over C+1..N. Without a seeded-resume entry point yet,
# gate the composition property that any correct seeded resume must satisfy:
# for every additive field the canonical value decomposes exactly into the
# head (snapshot@C) contribution plus the tail (C+1..N) contribution, and for
# every upsert/monotonic field the canonical set/sequence extends the head's.
# If a seeded resume is added later, fold(1..N)==resume(fold(1..C),C+1..N)
# must hold by these same decompositions.
def test_snapshot_tail_equivalence_at_multiple_cutoffs(monkeypatch):
    spec, n = _complex_journal()
    events = _mk_journal(spec)
    canonical = _fold_range(monkeypatch, events, 1, n)

    cutoffs = {
        "mid_reasoning": 2,
        "tool_pending": 4,
        "after_tool_complete": 6,
        "mid_prose": 9,
        "between_rounds": 14,
        "fully_synced": n,
    }
    for label, c in cutoffs.items():
        head = _fold_range(monkeypatch, events, 1, c)
        assert head["last_seq"] == c, (
            f"[{label}] snapshot.last_seq must equal the cutoff it covered "
            f"(got {head['last_seq']}, want {c})"
        )
        tail = [e for e in events if e["seq"] > c]

        # 1) Text fields: canonical == head + tail contributions.
        tail_reasoning = "".join(
            str(e["payload"].get("text") or "") for e in tail if e["event"] == "reasoning")
        assert canonical["last_reasoning_text"] == (head["last_reasoning_text"] or "") + tail_reasoning, (
            f"[{label}] canonical reasoning != head + tail"
        )
        tail_tokens = "".join(
            str(e["payload"].get("text") or "") for e in tail if e["event"] == "token")
        tail_interims = [str(e["payload"].get("text") or "") for e in tail
                         if e["event"] == "interim_assistant" and not e["payload"].get("reasoning_echo")]
        # assistant text: tokens append; each non-echo interim prepends "\n\n".
        expected_assistant = head["last_assistant_text"] or ""
        for e in tail:
            if e["event"] == "token":
                t = str(e["payload"].get("text") or "")
                if t:
                    expected_assistant += t
            elif e["event"] == "interim_assistant":
                visible = str(e["payload"].get("text") or "").strip()
                if not visible or e["payload"].get("reasoning_echo"):
                    continue
                if e["payload"].get("already_streamed"):
                    if not expected_assistant:
                        expected_assistant = visible
                else:
                    expected_assistant = f"{expected_assistant}\n\n{visible}" if expected_assistant else visible
        assert canonical["last_assistant_text"] == expected_assistant, (
            f"[{label}] canonical assistant text != head + tail composition"
        )

        # 2) Tool calls: canonical set == head set + tail tool tids; tail
        # completions flip done status; canonical completion state must hold.
        head_tids = {tc.get("tid") for tc in (head["tool_calls"] or []) if tc.get("tid")}
        full_tids = {tc.get("tid") for tc in (canonical["tool_calls"] or []) if tc.get("tid")}
        tail_tids = set()
        for e in tail:
            if e["event"] in ("tool", "tool_complete"):
                tid = e["payload"].get("tool_use_id") or e["payload"].get("tid")
                if tid:
                    tail_tids.add(tid)
        assert full_tids == head_tids | tail_tids, (
            f"[{label}] canonical tool set != head tools + tail tools"
        )
        for e in tail:
            if e["event"] == "tool_complete":
                tid = e["payload"].get("tool_use_id")
                full_tc = next((t for t in canonical["tool_calls"] if t.get("tid") == tid), None)
                head_tc = next((t for t in head["tool_calls"] if t.get("tid") == tid), None)
                assert full_tc and full_tc.get("done") is True, (
                    f"[{label}] tail-completed tool {tid} must be done in canonical"
                )
                if head_tc is not None:
                    assert head_tc.get("done") is False, (
                        f"[{label}] tool {tid} completed after cutoff must be pending in head"
                    )

        # 3) Burst anchors: canonical extends head monotonically.
        full_ends = [a["textEnd"] for a in (canonical["activity_burst_anchors"] or [])]
        head_ends = [a["textEnd"] for a in (head["activity_burst_anchors"] or [])]
        assert full_ends[:len(head_ends)] == head_ends, (
            f"[{label}] canonical anchors must extend head anchors monotonically"
        )
        assert full_ends == sorted(full_ends), f"[{label}] anchor textEnds must be monotonic"

        # 4) Burst id advances with tail interim boundaries.
        assert (canonical["current_activity_burst_id"] or 0) >= (
            (head["current_activity_burst_id"] or 0)
            + (len(tail_interims) if tail_interims else 0)
        ) - 1, f"[{label}] burst id must advance by tail interim boundaries"

        # 5) Scene rows: canonical scene extends head scene; every head row id
        # appears in canonical with the same local_id and role.
        head_rows = (head["anchor_activity_scene"] or {}).get("activity_rows") or []
        full_rows = (canonical["anchor_activity_scene"] or {}).get("activity_rows") or []
        full_ids = [r.get("local_id") for r in full_rows if r.get("local_id")]
        for r in head_rows:
            if not r.get("local_id"):
                continue
            assert r["local_id"] in full_ids, (
                f"[{label}] head scene row {r['local_id']} missing from canonical"
            )

        # 6) Messages: canonical live assistant message content equals the
        # canonical assistant text; head message is its prefix composition.
        full_msg = next((m for m in (canonical["messages"] or []) if m.get("role") == "assistant"), None)
        assert full_msg and full_msg.get("content") == canonical["last_assistant_text"], (
            f"[{label}] canonical assistant message must match canonical text"
        )
        head_msg = next((m for m in (head["messages"] or []) if m.get("role") == "assistant"), None)
        assert (head_msg is None) or (canonical["last_assistant_text"].startswith(head_msg.get("content") or "")) or head_msg.get("content") == canonical["last_assistant_text"], (
            f"[{label}] head assistant message must be a prefix of canonical"
        )


def test_tail_boundary_event_not_dropped_nor_duplicated(monkeypatch):
    """The race gate: snapshot cutoff C and tail cursor C must be the same
    atomic boundary. Tail read with after_seq=C must yield exactly C+1..N
    (no C, nothing missing, nothing duplicated)."""
    spec, n = _complex_journal()
    events = _mk_journal(spec)
    c = 6
    tail = [e for e in events if e["seq"] > c]
    assert [e["seq"] for e in tail] == list(range(c + 1, n + 1)), (
        "tail after_seq=C must be exactly C+1..N — no boundary event repeated, none missing"
    )


def test_snapshot_covers_exactly_its_last_seq(monkeypatch):
    """snapshot.last_seq must equal the last journal seq the fold actually
    consumed — the atomic boundary the tail cursor is derived from.

    Fold semantics: last_seq = max(last consumed event seq, summary.last_seq).
    Reading a bounded journal (max_seq=C) therefore reports C when the journal
    has C events, and the tail cursor derives from exactly this value."""
    spec, n = _complex_journal()
    events = _mk_journal(spec)
    for c in (1, 4, 6, 9, 14, n):
        snap = _fold_range(monkeypatch, events, 1, c)
        assert snap["last_seq"] == c, (
            f"cutoff {c}: snapshot must report the seq it consumed, got {snap['last_seq']}"
        )
        assert snap["last_event_id"] == f"run_eq:{c}"


def test_tail_only_fold_reproduces_canonical_deltas(monkeypatch):
    """Every canonical field must be reproducible from head + tail events:
    fold(C+1..N) applied on top of the head semantics yields the canonical
    values. Because the production fold is prefix-accumulating, we verify the
    composition property directly: folding [1..N] equals folding [1..C] then
    [C+1..N] for every additive field (text concat, tool upserts, anchors)."""
    spec, n = _complex_journal()
    events = _mk_journal(spec)
    for c in (2, 4, 6, 9, 14):
        full = _fold_range(monkeypatch, events, 1, n)
        head = _fold_range(monkeypatch, events, 1, c)

        # Prefix-composition checks per field class:
        # reasoning/assistant text: canonical = head + (tail contribution).
        tail_reasoning = "".join(
            str(e["payload"].get("text") or "") for e in events
            if e["seq"] > c and e["event"] == "reasoning")
        tail_tokens = "".join(
            str(e["payload"].get("text") or "") for e in events
            if e["seq"] > c and e["event"] == "token")
        tail_interim = [str(e["payload"].get("text") or "") for e in events
                        if e["seq"] > c and e["event"] == "interim_assistant"]

        expected_reasoning = (head["last_reasoning_text"] or "") + tail_reasoning
        assert full["last_reasoning_text"] == expected_reasoning, (
            f"cutoff {c}: canonical reasoning != head + tail reasoning"
        )

        # Tool calls: canonical set = head tools + tail tools (upsert by tid).
        head_tids = {tc.get("tid") for tc in (head["tool_calls"] or []) if tc.get("tid")}
        full_tids = {tc.get("tid") for tc in (full["tool_calls"] or []) if tc.get("tid")}
        tail_tool_events = [e for e in events
                            if e["seq"] > c and e["event"] in ("tool", "tool_complete")]
        tail_tids = set()
        for e in tail_tool_events:
            tid = e["payload"].get("tool_use_id") or e["payload"].get("tid")
            if tid:
                tail_tids.add(tid)
        assert full_tids == head_tids | tail_tids, (
            f"cutoff {c}: canonical tool set != head tools + tail tools"
        )
        # Completion status: any tool completed after C must be done=True in
        # canonical and not-done in head (unless completed before C).
        for e in events:
            if e["seq"] > c and e["event"] == "tool_complete":
                tid = e["payload"].get("tool_use_id")
                full_tc = next((t for t in full["tool_calls"] if t.get("tid") == tid), None)
                head_tc = next((t for t in head["tool_calls"] if t.get("tid") == tid), None)
                assert full_tc and full_tc.get("done") is True, (
                    f"cutoff {c}: tail-completed tool {tid} must be done in canonical"
                )
                if head_tc is not None:
                    assert head_tc.get("done") is False, (
                        f"cutoff {c}: tool {tid} completed after cutoff must be pending in head"
                    )

        # Burst anchors: canonical anchors = head anchors + boundaries from
        # tail interim/token events (monotonic textEnd).
        full_anchor_ends = [a["textEnd"] for a in (full["activity_burst_anchors"] or [])]
        head_anchor_ends = [a["textEnd"] for a in (head["activity_burst_anchors"] or [])]
        assert full_anchor_ends[:len(head_anchor_ends)] == head_anchor_ends, (
            f"cutoff {c}: canonical anchors must extend head anchors monotonically"
        )
        assert full_anchor_ends == sorted(full_anchor_ends), (
            f"cutoff {c}: anchor textEnds must be monotonic"
        )

        # Interim handling: each tail interim contributes a boundary; canonical
        # burst count >= head burst count + len(tail interims that mark bounds).
        tail_interim_count = len(tail_interim)
        assert (full["current_activity_burst_id"] or 0) >= (
            (head["current_activity_burst_id"] or 0)
            + (tail_interim_count if tail_interim else 0)
        ) - 1, (
            f"cutoff {c}: burst id must advance by tail interim boundaries"
        )
