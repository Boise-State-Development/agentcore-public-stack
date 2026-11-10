"""Non-Bedrock providers get documents as text, not raw file parts.

The document-cap projection used to be installed for ``ModelProvider.BEDROCK``
only, so an OpenAI, Mantle, Bedrock-Responses or Gemini turn got every
document block as Strands formats it for that wire: a base64 file part whose
MIME type comes from ``mimetypes`` on the format. ``.docx`` has no entry there,
so it went as ``application/octet-stream``, and Chat Completions' file part
reads PDFs only (unified-attachment-delivery §1.3).

Hermetic on purpose: dev has GPT models on Bedrock Responses, but no OpenAI or
Gemini key, so the request each Strands formatter would send is the only thing
all four providers can be checked against. Each test drives the real model's
``stream`` through the factory's wrapping and stops at the formatter.
"""

import asyncio
import io
import zipfile
from unittest.mock import MagicMock, patch

import pytest
from strands.models import OpenAIResponsesModel
from strands.models.gemini import GeminiModel
from strands.models.openai import OpenAIModel

from agents.main_agent.core.model_config import ModelConfig, ModelProvider


def _docx(text: str) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buf.getvalue()


PDF = b"%PDF-1.4\n%fake\n"


def _history() -> list:
    return [
        {
            "role": "user",
            "content": [
                {"document": {"format": "docx", "name": "Plan", "source": {"bytes": _docx("budget is 42")}}},
                {"document": {"format": "txt", "name": "schema sql", "source": {"bytes": b"SELECT 1;"}}},
                {"document": {"format": "pdf", "name": "Report", "source": {"bytes": PDF}}},
                {"image": {"format": "png", "source": {"bytes": b"\x89PNG\r\n"}}},
                {"text": "Summarize."},
            ],
        }
    ]


def _models():
    return {
        ModelProvider.OPENAI: (
            "_create_openai_model",
            lambda: OpenAIModel(client_args={"api_key": "sk-test"}, model_id="gpt-4o"),
            "format_request",
        ),
        ModelProvider.MANTLE: (
            "_create_mantle_model",
            lambda: OpenAIModel(client_args={"api_key": "sk-test"}, model_id="openai.gpt-oss-120b"),
            "format_request",
        ),
        ModelProvider.BEDROCK_RESPONSES: (
            "_create_bedrock_responses_model",
            lambda: OpenAIResponsesModel(client_args={"api_key": "sk-test"}, model_id="us.openai.gpt-5.6"),
            "_format_request",
        ),
        ModelProvider.GEMINI: (
            "_create_gemini_model",
            lambda: GeminiModel(client_args={"api_key": "g-test"}, model_id="gemini-pro"),
            "_format_request",
        ),
    }


def _formatted_request(provider: ModelProvider):
    from agents.main_agent.core.agent_factory import AgentFactory

    builder_name, build, formatter = _models()[provider]
    with patch.object(AgentFactory, builder_name, staticmethod(lambda _cfg: build())), patch(
        "agents.main_agent.core.agent_factory.Agent"
    ) as agent_cls:
        AgentFactory.create_agent(
            model_config=ModelConfig(model_id="m", provider=provider, caching_enabled=False),
            system_prompt="s",
            tools=[],
            session_manager=MagicMock(),
        )
    model = agent_cls.call_args.kwargs["model"]

    captured = {}
    original = getattr(model, formatter)

    def spy(messages, *args, **kwargs):
        captured["messages"] = messages
        captured["request"] = original(messages, *args, **kwargs)
        raise RuntimeError("stop before the network")

    setattr(model, formatter, spy)

    async def drain():
        async for _ in model.stream(_history(), None, "s"):
            pass

    with pytest.raises(RuntimeError, match="stop before the network"):
        asyncio.run(drain())
    return captured


@pytest.mark.parametrize(
    "provider",
    [ModelProvider.OPENAI, ModelProvider.MANTLE, ModelProvider.BEDROCK_RESPONSES, ModelProvider.GEMINI],
)
def test_non_pdf_documents_reach_non_bedrock_models_as_text(provider):
    captured = _formatted_request(provider)
    content = captured["messages"][0]["content"]

    documents = [b["document"]["format"] for b in content if "document" in b]
    assert documents == ["pdf"], "only the PDF stays a native document block"
    texts = [b["text"] for b in content if "text" in b]
    assert any(t.startswith('<document name="Plan"') and "budget is 42" in t for t in texts)
    assert any(t.startswith('<document name="schema sql"') and "SELECT 1;" in t for t in texts)
    assert any("image" in b for b in content), "images keep their native part"

    # And the provider's own formatter accepted the projected request.
    assert "octet-stream" not in str(captured["request"])


def test_the_shared_history_is_never_mutated():
    history = _history()
    before = [list(b) for b in history[0]["content"]]
    from apis.shared.models.document_cap import UNCAPPED, project_document_cap

    project_document_cap(history, UNCAPPED, as_text=True)
    assert [list(b) for b in history[0]["content"]] == before
