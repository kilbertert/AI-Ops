"""使用真实TCP/SSE，验证Chatflow消息与运行的完成边界。"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from aiops_diagnostics.dify_chat_workflow import DifyWorkflowError
from aiops_diagnostics.dify_chatflow import DifyChatflowClient

IDS = [f"00000000-0000-0000-0000-{n:012}" for n in range(1, 6)]
WORKFLOW, RUN, TASK, CONVERSATION, MESSAGE = IDS


def event(kind, **extra):
    return {
        "event": kind,
        "workflow_run_id": RUN,
        "task_id": TASK,
        "conversation_id": CONVERSATION,
        "message_id": MESSAGE,
        **extra,
    }


def transcript():
    return [
        event("workflow_started", data={"id": RUN, "workflow_id": WORKFLOW}),
        event("message", answer="请先核对枪号。"),
        event("workflow_finished", data={"id": RUN, "workflow_id": WORKFLOW, "status": "succeeded"}),
        event("message_end", metadata={"retriever_resources": []}),
    ]


@pytest.fixture
def server():
    state = {"events": transcript(), "requests": []}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append((self.path, body, self.headers.get("Authorization")))
            self.send_response(200)
            if self.path.endswith("/stop"):
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"result":"success"}')
            else:
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for item in state["events"]:
                        self.wfile.write(
                            b"data: " + json.dumps(item, ensure_ascii=False).encode() + b"\r\n\r\n"
                        )
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    yield DifyChatflowClient(f"http://127.0.0.1:{http.server_port}", timeout_seconds=5), state
    http.shutdown()
    http.server_close()


def run(client, **overrides):
    args = {
        "key": "private-key",
        "workflow_id": WORKFLOW,
        "user": "scope-a",
        "query": "怎么开始？",
        "inputs": {"language": "zh"},
        "on_started": lambda *args: None,
    }
    return client.run(**{**args, **overrides})


def test_real_sse_requires_both_completion_events_and_preserves_identity(server):
    client, state = server
    started = []
    reply = run(client, on_started=lambda *args: started.append(args))
    assert (reply.workflow_run_id, reply.task_id, reply.conversation_id, reply.message_id) == tuple(IDS[1:])
    assert started == [tuple(IDS[1:])]
    assert reply.answer == "请先核对枪号。"
    run(client, conversation_id=reply.conversation_id, query="然后呢？")
    path, body, auth = state["requests"][-1]
    assert path == "/v1/chat-messages" and auth == "Bearer private-key"
    assert body["conversation_id"] == CONVERSATION and body["workflow_id"] == WORKFLOW
    assert body["query"] == "然后呢？" and body["user"] == "scope-a"
    assert "private-key" not in json.dumps(body)
    assert client.stop(key="private-key", user="scope-a", task_id=TASK)
    assert state["requests"][-1][:2] == (f"/v1/chat-messages/{TASK}/stop", {"user": "scope-a"})


@pytest.mark.parametrize(
    "bad",
    [
        "truncated",
        "no_workflow_finish",
        "failed",
        "wrong_workflow",
        "wrong_message",
        "wrong_conversation",
        "missing_message_identity",
        "duplicate_start",
        "error_after_text",
        "no_text",
        "missing_finish_identity",
    ],
)
def test_partial_wrong_or_failed_runs_never_return_an_answer(server, bad):
    client, state = server
    events = state["events"]
    if bad == "truncated":
        events.pop()
    elif bad == "no_workflow_finish":
        events.pop(2)
    elif bad == "failed":
        events[2]["data"]["status"] = "failed"
    elif bad == "wrong_workflow":
        events[2]["data"]["workflow_id"] = MESSAGE
    elif bad == "wrong_message":
        events[1]["message_id"] = WORKFLOW
    elif bad == "wrong_conversation":
        events[1]["conversation_id"] = WORKFLOW
    elif bad == "missing_message_identity":
        del events[1]["message_id"]
    elif bad == "duplicate_start":
        events.insert(1, events[0])
    elif bad == "error_after_text":
        events.insert(2, event("error"))
    elif bad == "missing_finish_identity":
        del events[2]["task_id"]
    elif bad == "no_text":
        events[1]["answer"] = ""
    with pytest.raises(DifyWorkflowError):
        run(client)


def test_existing_conversation_must_be_the_returned_conversation(server):
    client, _ = server
    with pytest.raises(DifyWorkflowError):
        run(client, conversation_id=WORKFLOW)


def test_message_replacement_does_not_append_unmoderated_text(server):
    client, state = server
    state["events"].insert(2, event("message_replace", answer="请联系现场人员。"))
    assert run(client).answer == "请联系现场人员。"


def test_real_dify_sends_message_end_before_workflow_finish(server):
    client, state = server
    events = state["events"]
    events[2], events[3] = events[3], events[2]
    del events[1]["workflow_run_id"]
    del events[2]["workflow_run_id"]
    assert run(client).answer == "请先核对枪号。"


def test_text_after_message_end_is_not_a_complete_answer(server):
    client, state = server
    events = state["events"]
    events[2], events[3] = events[3], events[2]
    events.insert(3, event("message", answer="late text"))
    with pytest.raises(DifyWorkflowError):
        run(client)
