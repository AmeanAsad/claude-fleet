"""Unit tests for the prime-agent backend.

Covers the JSONL→dashboard message converter and the paginated reader using
real prime-agent 0.7.2 session-entry shapes (captured from live sessions).
The daemon-driving paths (ensure_session/send/stop/wake) are covered by the
manual E2E plan in TESTING.md — they need a live prime-agent daemon.
"""

import json

import pytest

from cfleet.prime_backend import prime_entry_to_message, read_prime_messages


@pytest.fixture
def session_file(tmp_path):
    entries = [
        {"type": "session", "version": 3, "id": "abc", "timestamp": "t0", "cwd": "/x"},
        {"type": "model_change", "id": "m1", "parentId": None, "provider": "moonshotai", "modelId": "kimi-k3"},
        {"type": "message", "id": "u1", "parentId": None, "timestamp": "t1",
         "message": {"role": "user", "content": [{"type": "text", "text": "build me a thing"}]}},
        {"type": "message", "id": "a1", "parentId": "u1", "timestamp": "t2",
         "message": {"role": "assistant",
                     "content": [
                         {"type": "thinking", "thinking": "let me think"},
                         {"type": "text", "text": "Building now."},
                         {"type": "toolCall", "id": "ipython_0", "name": "ipython",
                          "arguments": {"code": "print(1)"}},
                     ],
                     "model": "kimi-k3",
                     "usage": {"input": 100, "output": 50, "totalTokens": 150,
                               "cost": {"total": 0.001}}}},
        {"type": "message", "id": "tr1", "parentId": "a1", "timestamp": "t3",
         "message": {"role": "toolResult", "toolCallId": "ipython_0", "toolName": "ipython",
                     "content": [{"type": "text", "text": "1\n"}], "isError": False}},
        {"type": "agent_status", "id": "s1", "parentId": "tr1", "status": {"summary": ""}},
        {"type": "custom_message", "customType": "agent_message",
         "content": "Agent-to-agent message received.\nSource: agent_message\nFrom: client x\n\ncheck the build",
         "display": True,
         "details": {"id": "agentmsg_1", "message": "check the build",
                     "from": {"clientId": "x"}, "target": {}},
         "id": "c1", "parentId": "s1", "timestamp": "t4"},
        {"type": "message", "id": "a2", "parentId": "c1", "timestamp": "t5",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "build is green"}],
                     "model": "kimi-k3"}},
    ]
    p = tmp_path / "session.jsonl"
    with open(p, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return str(p)


class TestConverter:
    def test_skips_metadata_entries(self):
        assert prime_entry_to_message({"type": "session"}) is None
        assert prime_entry_to_message({"type": "model_change"}) is None
        assert prime_entry_to_message({"type": "agent_status", "status": {}}) is None
        assert prime_entry_to_message({"type": "custom_message", "customType": "other"}) is None

    def test_user_message(self, session_file):
        m = prime_entry_to_message({"type": "message", "timestamp": "t1", "message": {
            "role": "user", "content": [{"type": "text", "text": "hi"}]}})
        assert m["type"] == "UserPrompt"
        assert m["role"] == "user"
        assert m["content"] == [{"type": "TextBlock", "text": "hi"}]

    def test_assistant_blocks_and_usage(self, session_file):
        with open(session_file) as f:
            entries = [json.loads(l) for l in f if l.strip()]
        m = prime_entry_to_message(entries[3])
        assert m["type"] == "AssistantMessage"
        assert [b["type"] for b in m["content"]] == ["ThinkingBlock", "TextBlock", "ToolUseBlock"]
        tool = m["content"][2]
        assert tool["tool_name"] == "ipython"
        assert tool["tool_input"] == {"code": "print(1)"}
        assert m["usage"] == {"input_tokens": 100, "output_tokens": 50}
        assert m["total_cost_usd"] == pytest.approx(0.001)
        assert m["model"] == "kimi-k3"

    def test_tool_result_mirrors_claude_shape(self, session_file):
        with open(session_file) as f:
            entries = [json.loads(l) for l in f if l.strip()]
        m = prime_entry_to_message(entries[4])
        assert m["role"] == "user"
        block = m["content"][0]
        assert block["type"] == "ToolResultBlock"
        assert block["content"] == "1\n"
        assert block["is_error"] is False

    def test_send_delivered_prompt_uses_details_message(self, session_file):
        with open(session_file) as f:
            entries = [json.loads(l) for l in f if l.strip()]
        m = prime_entry_to_message(entries[6])
        assert m["type"] == "UserPrompt"
        assert m["content"][0]["text"] == "check the build"

    def test_send_delivered_prompt_fallback_strips_envelope(self):
        m = prime_entry_to_message({
            "type": "custom_message", "customType": "agent_message",
            "content": "Agent-to-agent message received.\nSource: agent_message\n\nreal prompt here",
            "details": {}, "timestamp": "t",
        })
        assert m["content"][0]["text"] == "real prompt here"

    def test_empty_assistant_skipped(self):
        assert prime_entry_to_message({"type": "message", "message": {
            "role": "assistant", "content": []}}) is None


class TestReader:
    def test_tail_read_counts_displayable_only(self, session_file):
        r = read_prime_messages(session_file, limit=200)
        assert r["total"] == 5  # metadata + agent_status excluded
        assert r["has_more"] is False
        assert r["messages"][-1]["content"][0]["text"] == "build is green"

    def test_tail_read_limit(self, session_file):
        r = read_prime_messages(session_file, limit=2)
        assert len(r["messages"]) == 2
        assert r["total"] == 5
        assert r["head"] == 3
        assert r["has_more"] is True

    def test_before_window(self, session_file):
        r = read_prime_messages(session_file, before=2, limit=2)
        assert [m["content"][0]["type"] for m in r["messages"]] == ["TextBlock", "ThinkingBlock"]
        assert r["head"] == 0
        assert r["has_more"] is False

    def test_missing_file(self, tmp_path):
        r = read_prime_messages(str(tmp_path / "nope.jsonl"))
        assert r == {"messages": [], "total": 0, "head": 0, "has_more": False}
