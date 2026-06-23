from app.llm.base import Message
from app.llm.providers.openai import OpenAIProvider


class FakeCompletions:
    def create(self, **kwargs):
        class Msg:
            content = "openai reply"

        class Choice:
            message = Msg()

        class Resp:
            choices = [Choice()]
        self.kwargs = kwargs
        return Resp()


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self, **kwargs):
        self.chat = FakeChat()


def test_openai_chat_uses_client(monkeypatch):
    monkeypatch.setattr("app.llm.providers.openai.OpenAI", FakeClient)
    p = OpenAIProvider(api_key="sk-test", model="gpt-4o-mini")
    out = p.chat([Message(role="user", content="hi")])
    assert out == "openai reply"
