from types import SimpleNamespace

import pytest

from enterprise_mind.agent.runner import (
    _chart_columns,
    requested_chart_request,
    requested_content_draft,
    requested_database_export_intent,
    requested_database_export_limit,
    requested_database_export_scope,
    requested_document_translation,
    requested_draft_update,
    requested_file_format,
    requested_file_format_missing,
)
from enterprise_mind.api.errors import APIError
from enterprise_mind.api.generation_schemas import DatabaseExportTaskCreate
from enterprise_mind.database.query import explicit_chart_query
from enterprise_mind.generation.service import (
    _derived_chart_statistic,
    _validate_chart_point_budget,
)


def test_agent_only_creates_files_for_explicit_formats() -> None:
    assert requested_file_format("生成 Word 报告") == "docx"
    assert requested_file_format("把刚才的回答导出成 PDF") == "pdf"
    assert requested_file_format("请整理成 Markdown") == "md"
    assert requested_file_format("导出当前页为 Excel") == "xlsx"
    assert requested_file_format("导出全部订单为 CSV") == "csv"
    assert requested_file_format("总结这份文档") is None
    assert requested_file_format("不要导出 PDF") is None
    assert requested_file_format_missing("请把这份内容导出成文件")
    assert not requested_file_format_missing("写一份操作报告")


def test_literal_chart_query_does_not_guess_table_metric_or_other_intents():
    question = "查询 biz.orders，按 department 分组汇总 SUM(amount)，生成全部柱状图 PNG。"
    assert explicit_chart_query(question) == ("biz", "orders", "department", "amount")
    for ambiguous in (
        "查询订单，按部门汇总金额，生成柱状图 PNG。",
        question + "并参考知识库中的文档",
        question + "并分析原因",
        question + "以及 other.orders",
        question + "最近的记录",
        question.replace("SUM(amount)", "某个指标"),
    ):
        assert explicit_chart_query(ambiguous) is None


def test_enterprise_document_translation_requires_current_sources() -> None:
    assert requested_document_translation("请翻译《采购说明.pdf》的第三章并总结")
    assert requested_document_translation("Translate the contract.docx document into English")
    assert not requested_document_translation("请翻译下面这段文字：Hello, world.")
    assert not requested_document_translation("请总结采购说明.pdf 的第三章")


def test_database_export_scope_requires_explicit_range() -> None:
    assert requested_database_export_scope("导出当前页为 Excel", "xlsx") == "current_page"
    assert requested_database_export_scope("导出全部订单为 XLSX", "xlsx") == "all_matching"
    assert requested_database_export_scope("导出前十条为 Excel", "xlsx") == "first_n"
    assert requested_database_export_scope("导出全部订单为 CSV", "csv") == "all_matching"
    assert requested_database_export_scope("导出当前页查询结果为 CSV", "csv") == "current_page"
    assert requested_database_export_scope("导出全部报告为 Excel", "xlsx") is None
    assert requested_database_export_limit("导出前十条为 Excel") == 10
    assert requested_database_export_limit("导出前 25 条为 Excel") == 25
    assert requested_database_export_scope("导出查询结果为 Excel", "xlsx") is None
    assert requested_database_export_intent("导出查询结果为 Excel", "xlsx")
    assert not requested_database_export_intent("把这份报告导出为 Excel", "xlsx")
    assert not requested_database_export_intent("把这份报告导出为 CSV", "csv")


@pytest.mark.parametrize(
    "question",
    [
        "请回答问题，不生成文件。",
        "起草一份报告，不生成 DOCX/PDF/XLSX 文件。",
        "请回答问题，不要生成文件。",
        "请回答问题，无需导出文档。",
        "请回答问题，不需要保存为文件。",
        "请回答问题，别自动生成 PDF 文件。",
        "请回答问题，禁止导出 Excel 文件。",
    ],
)
def test_negated_file_requests_do_not_create_files_or_require_a_format(question: str) -> None:
    assert requested_file_format(question) is None
    assert not requested_file_format_missing(question)


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("不要导出 PDF，请生成 Word 文件。", "docx"),
        ("请生成 Word 文件，不要导出 PDF。", "docx"),
        ("不生成 PDF，而是生成 Excel 文件。", "xlsx"),
        ("不要保存文件。请总结 PDF 文档。", None),
    ],
)
def test_file_request_negation_is_local_to_the_action(question: str, expected: str | None):
    assert requested_file_format(question) == expected


def test_positive_unspecified_file_request_still_requires_a_format() -> None:
    assert requested_file_format_missing("不要导出 PDF，请把结果保存成文件。")


def test_chart_request_requires_explicit_format_and_scope_unless_preview() -> None:
    request = requested_chart_request("预览当前页柱状图")
    assert request is not None
    assert request["chart_format"] == "png"
    assert not request["chart_format_missing"]
    assert request["chart_scope"] == "current_page"
    request = requested_chart_request("导出全部结果为柱状图")
    assert request is not None
    assert request["chart_format_missing"]
    assert request["chart_scope"] == "all_aggregated"
    request = requested_chart_request("导出柱状图 PNG")
    assert request is not None and request["chart_scope_missing"]
    request = requested_chart_request("生成柱状图")
    assert request is not None and request["chart_format"] == "png"
    request = requested_chart_request("导出前 51 项柱状图 PNG")
    assert request is not None
    assert request["chart_budget_exceeded"] is True
    assert request["chart_budget_limit"] == 50
    request = requested_chart_request("导出前 20 项饼图 CSV")
    assert request is not None and request["chart_budget_exceeded"] is False


def test_chart_columns_do_not_choose_first_column_when_ambiguous() -> None:
    answer = SimpleNamespace(
        result=SimpleNamespace(
            columns=(
                SimpleNamespace(name="department", data_type="text"),
                SimpleNamespace(name="region", data_type="text"),
                SimpleNamespace(name="amount", data_type="numeric"),
            )
        )
    )
    request = requested_chart_request("导出当前页柱状图 PNG")
    assert request is not None
    assert _chart_columns(answer, request) is None


def test_chart_statistic_is_derived_from_guarded_query_semantics() -> None:
    aggregate = SimpleNamespace(
        query_kind="aggregate",
        guarded_query=SimpleNamespace(
            validated_query=(
                "SELECT department, SUM(amount) AS amount FROM public.orders GROUP BY department"
            )
        ),
    )
    assert _derived_chart_statistic(aggregate, "department", "amount") == (
        "SUM(amount)；按 department 分组"
    )

    aliased = SimpleNamespace(
        query_kind="aggregate",
        guarded_query=SimpleNamespace(
            validated_query=(
                "SELECT category, SUM(amount) AS total_amount, COUNT(*) AS order_count "
                "FROM business.phase10_orders GROUP BY category"
            )
        ),
    )
    assert _derived_chart_statistic(aliased, "category", "total_amount") == (
        "SUM(amount)；按 category 分组"
    )
    assert _derived_chart_statistic(aliased, "category", "order_count") == (
        "COUNT(*)；按 category 分组"
    )
    assert _derived_chart_statistic(aliased, "category", "unknown_alias") == (
        "服务端聚合结果（指标 unknown_alias）；按 category 分组"
    )
    qualified = SimpleNamespace(
        query_kind="aggregate",
        guarded_query=SimpleNamespace(
            validated_query=(
                "SELECT o.department, SUM(o.amount) AS total_amount, "
                'COUNT(DISTINCT o.id) AS order_count FROM "biz"."orders" o GROUP BY o.department'
            )
        ),
    )
    assert _derived_chart_statistic(qualified, "department", "total_amount") == (
        "SUM(amount)；按 department 分组"
    )
    assert _derived_chart_statistic(qualified, "department", "order_count") == (
        "COUNT(DISTINCT id)；按 department 分组"
    )


def test_chart_top_n_budget_is_rejected_before_task_creation() -> None:
    with pytest.raises(APIError, match="QUERY_RESULT_TOO_LARGE"):
        _validate_chart_point_budget("bar", "top_n", 51)
    with pytest.raises(APIError, match="QUERY_RESULT_TOO_LARGE"):
        _validate_chart_point_budget("pie", "top_n", 21)
    _validate_chart_point_budget("line", "top_n", 100)

    detail = SimpleNamespace(
        query_kind="detail",
        guarded_query=SimpleNamespace(
            validated_query="SELECT department, amount FROM public.orders"
        ),
    )
    assert "未聚合" in _derived_chart_statistic(detail, "department", "amount")


def test_direct_first_n_export_requires_a_semantic_limit_assertion() -> None:
    body = DatabaseExportTaskCreate(
        query_id="q" * 16,
        scope="first_n",
        row_limit=10,
    )
    assert body.row_limit == 10
    assert body.format == "xlsx"
    csv_body = DatabaseExportTaskCreate(query_id="q" * 16, scope="all_matching", format="csv")
    assert csv_body.format == "csv"


def test_report_request_creates_a_working_draft_without_a_file() -> None:
    assert requested_content_draft("写一份操作报告")
    assert requested_content_draft("生成一份操作报告")
    assert not requested_content_draft("总结这份文档")
    assert requested_draft_update("修改刚才的报告并导出 PDF")
    assert not requested_draft_update("导出全部订单为 XLSX")
