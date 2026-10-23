from unittest.mock import Mock
from langchain_core.messages import AIMessage
from src.agents.runner import MultiSourceAgentRunner
from src.attachments.models import AttachmentRef
from src.services import task_settings
import src.settings as settings

class _FakeRAGBackend:
    name = "fake"
    def retrieve(self,*a,**k): return []

def _ref(i):
    return AttachmentRef(attachment_id=f"att-{i}", asset_id=f"asset-{i}", session_id=1, filename=f"{i}.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document", extension=".docx", size_bytes=10, sha256="a"*64)

def test_compare_answer_is_buffered_until_task_gate(monkeypatch):
    from src.agents import runner as runner_mod
    monkeypatch.setattr(settings,"DOCUMENT_COMPARISON_ENABLED",True)
    monkeypatch.setattr(task_settings,"TASK_PLANNING_ENABLED",True)
    monkeypatch.setattr(task_settings,"TASK_COMPLETION_GATE_ENABLED",True)
    monkeypatch.setattr(task_settings,"TASK_ANSWER_BUFFERING_ENABLED",True)
    monkeypatch.setattr(runner_mod.MultiSourceAgentRunner,"_prefetch_attachment_context",lambda self,**kwargs:"")
    monkeypatch.setattr(runner_mod.MultiSourceAgentRunner,"_prefetch_memory_context",lambda self,**kwargs:"")
    fake_agent=type("F",(),{})()
    fake_agent.stream=lambda *a,**k: iter([(AIMessage(content="比较结论"),{"langgraph_node":"model"})])
    monkeypatch.setattr(runner_mod,"create_chat_model",lambda:object())
    monkeypatch.setattr(runner_mod,"create_deep_agent",lambda **kwargs:fake_agent)
    monkeypatch.setattr(runner_mod,"record_agent",Mock())
    runner=MultiSourceAgentRunner(rag_backend=_FakeRAGBackend(),circuit_service=None)
    deltas=list(runner.stream(query="比较这两份附件，按章节说差异",kb_name="kb_hw",history=[],thread_id="t1",attachments=[_ref(1),_ref(2)],source_scope="attachment_only"))
    print("DELTAS",deltas)
    assert len(deltas)==1
    assert "任务完成状态" in deltas[0]
