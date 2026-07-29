import json

import pytest

from app.pipeline import style_learner as L
from app.pipeline import styles as S


class FakeStore:
    def __init__(self):
        self.data = {}

    def get_setting(self, key, default=None):
        return self.data.get(key, default)

    def set_setting(self, key, value):
        self.data[key] = value

    def delete_setting(self, key):
        self.data.pop(key, None)


class Provider:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def chat(self, messages, **_kwargs):
        self.calls.append(messages)
        return json.dumps(self.result, ensure_ascii=False)


def _samples():
    return [
        {"title": f"样例 {index}", "content": ("短句和真实体验。" * 30)}
        for index in range(1, 4)
    ]


def test_learn_style_extracts_structure_and_saves_examples():
    provider = Provider({
        "suggested_name": "亲切种草风",
        "description": "亲切、短句、体验导向",
        "tone": "亲切口语化",
        "sentence_structure": "短句",
        "opening_hook": "问题开头",
        "paragraph_layout": "多短段",
        "rhetoric": "对比",
        "title_pattern": "数字标题",
        "ending_pattern": "引导互动",
        "system_prompt": "你是一位亲切的生活方式编辑。",
        "style_guidance": "使用亲切口语和短句，多写真实体验，结尾引导互动。",
    })
    store = FakeStore()
    learned = L.learn_style(store, provider, _samples())
    assert learned.origin == "learned"
    assert learned.name == "亲切种草风"
    assert len(learned.examples) == 2
    assert len(learned.learned_from) == 3
    assert "{content}" in learned.instruction
    assert S.get_style(learned.id, store).analysis["tone"] == "亲切口语化"
    assert "只分析写法" in provider.calls[0][0].content


@pytest.mark.parametrize("count", [2, 11])
def test_learn_style_requires_three_to_ten_samples(count):
    with pytest.raises(ValueError, match="3-10"):
        L.collect_samples(_samples()[:1] * count)


def test_collect_samples_rejects_private_urls():
    samples = _samples()
    samples[0] = {"url": "http://127.0.0.1/private"}
    with pytest.raises(ValueError, match="内网"):
        L.collect_samples(samples)
