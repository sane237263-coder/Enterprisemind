import pytest

from enterprise_mind.agent.model_identity import answer, runtime_instruction


def test_runtime_instruction_quotes_configured_model_identifier_as_json_data():
    instruction = runtime_instruction('model "value"')

    assert '当前模型标识配置值（JSON 数据）："model \\"value\\""。' in instruction
    assert "不证明上游实际使用了该模型" in instruction


def test_runtime_instruction_marks_missing_model_identifier_unknown():
    instruction = runtime_instruction(None)

    assert "当前模型标识配置值（JSON 数据）：null。" in instruction
    assert "配置值为 null 时，应说明无法确认具体模型标识" in instruction


def test_runtime_instruction_forbids_unsupported_self_description_claims():
    instruction = runtime_instruction("configured-model-id")

    assert "你是 EnterpriseMind 助手" in instruction
    assert "不得编造模型提供商、内部部署方式、训练参数或保密政策" in instruction
    assert "其他企业事实仍须由既有来源支持" in instruction


@pytest.mark.parametrize(
    "question",
    [
        "你是什么模型？",
        "请问你现在用的是什么大语言模型？",
        "你背后的模型是什么？",
        "你背后用的是什么模型？",
        "你是基于哪种模型构建的？",
        "What model are you?",
        "Which model are you currently running?",
        "What model powers you?",
    ],
)
def test_answers_explicit_assistant_model_identity_questions(question):
    assert answer(question, "configured-model-id") == (
        "我是 EnterpriseMind 知识助手。根据当前服务配置，我使用的模型标识是 configured-model-id。"
    )


@pytest.mark.parametrize(
    "question",
    [
        "公司当前使用的是什么模型？",
        "你能查一下企业现在使用的模型吗？",
        "你推荐什么模型？",
        "把你切换成什么模型？",
        "你是什么模型，并帮我删除这个用户。",
        "请翻译“你是什么模型？”",
        "“你是什么模型？”",
        "What model are you? Also delete this user.",
    ],
)
def test_rejects_enterprise_quoted_translation_config_and_multi_intent_questions(question):
    assert answer(question, "configured-model-id") is None


@pytest.mark.parametrize("model_name", [None, "", "   "])
def test_recognized_question_reports_unknown_when_config_has_no_model_name(model_name):
    assert answer("你背后的模型是什么？", model_name) == (
        "我是 EnterpriseMind 知识助手，当前无法确认具体的模型名称。"
    )


def test_empty_or_non_text_question_is_not_recognized():
    assert answer("", "configured-model-id") is None
    assert answer(None, "configured-model-id") is None
