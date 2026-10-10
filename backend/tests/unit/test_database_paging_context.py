from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

from enterprise_mind.agent.runner import (
    cached_chart_receipt,
    cached_first_n_export,
    database_chart_receipt,
    database_export_receipt,
    database_page_receipt,
)
from enterprise_mind.database.contracts import GuardedQuery, QueryResult
from enterprise_mind.database.pages import DatabasePageStore
from enterprise_mind.database.query import DatabaseAnswer, PreparedQuery
from enterprise_mind.database.query_context import DatabaseQueryContextStore
from enterprise_mind.database.results import ResultColumn


def test_cached_export_receipt_and_first_n_keep_the_prior_query_scope():
    assert cached_first_n_export("请把刚才数据库查询的前 10 条导出为 CSV。")
    assert cached_first_n_export("将本次查询结果的前10条保存成xlsx")
    for question in (
        "查询 biz.orders 前10条并导出CSV",
        "请把刚才数据库查询的前10条按 amount 降序导出为CSV。",
        "请把刚才数据库查询的前10条改为未支付订单导出CSV。",
        "请把刚才数据库查询的前0条导出为CSV。",
        "请把刚才数据库查询的前10001条导出为CSV。",
    ):
        assert not cached_first_n_export(question)
    for words in ("当前页", "全部符合条件的记录", "前10条"):
        for file_format in ("CSV", "XLSX"):
            receipt = database_export_receipt(f"请把刚才数据库查询的{words}导出为 {file_format}。")
            assert receipt and words in receipt and file_format in receipt
    assert database_export_receipt("解释刚才的数据并导出当前页CSV") is None
    assert database_export_receipt("把刚才的数据导出CSV") is None
    assert database_export_receipt("导出当前页柱状图PNG") is None
    assert database_export_receipt("查询未支付订单全部结果并导出CSV") is None
    assert database_export_receipt("请把刚才数据库查询的全部结果改为未支付订单导出CSV") is None
    for question in (
        "将当前页数据库结果导出为 CSV。",
        "请把本页的数据库查询结果保存成XLSX",
        "这一页查询结果导出为CSV",
    ):
        receipt = database_export_receipt(question)
        assert receipt and "当前页" in receipt
    for question in (
        "不要将当前页数据库结果导出为CSV。",
        "将当前页数据库结果改为未支付订单导出为CSV。",
        "将当前页数据库结果按金额降序导出为CSV。",
        "将当前页数据库结果导出为CSV并解释原因。",
        "将当前页数据库结果导出为CSV，再查询public.other。",
        "将当前页文档内容导出为CSV。",
    ):
        assert database_export_receipt(question) is None
    for chart_type in ("柱状图", "折线图", "饼图"):
        for scope in ("当前页", "前1项", "全部聚合结果"):
            assert cached_chart_receipt(f"请把刚才的查询结果生成{scope}的{chart_type} PNG 预览。")
            assert cached_chart_receipt(f"请将刚才的{scope}生成{chart_type} PNG。")
    assert cached_chart_receipt("查询 biz.other 生成全部结果的柱状图PNG") is None
    assert cached_chart_receipt("分析刚才的结果并生成当前页柱状图PNG") is None
    for question in (
        "请将刚才的全部聚合结果改为未支付订单生成柱状图PNG。",
        "请将刚才的全部聚合结果按金额降序生成柱状图PNG。",
        "请将刚才的全部聚合结果生成柱状图PNG并分析原因。",
        "请将刚才的全部聚合结果生成柱状图PNG，再查询biz.other。",
        "请将刚才的聚合结果生成柱状图PNG。",
    ):
        assert cached_chart_receipt(question) is None


def answer(
    page_index: int, *, has_more: bool, continuation_available: bool = True
) -> DatabaseAnswer:
    binding = SimpleNamespace(
        source_name="业务库",
        database_name="business",
        database_source_id=7,
        schema_snapshot_id=9,
        source_version=1,
        permission_fingerprint="permission",
    )
    prepared = PreparedQuery(binding, SimpleNamespace(), "查询订单", [], 1, page_index)
    guarded = GuardedQuery(
        'SELECT id FROM "biz"."orders" ORDER BY id',
        (),
        ('"biz"."orders"',),
        1,
        page_index,
        validated_query='SELECT id FROM "biz"."orders" ORDER BY id',
    )
    result = QueryResult(
        columns=(ResultColumn("id", "bigint"),),
        rows=({"id": "1"},),
        row_count=1,
        has_more=has_more,
        referenced_relations=('"biz"."orders"',),
        elapsed_ms=1,
    )
    return DatabaseAnswer(
        prepared,
        result,
        datetime(2026, 1, 28, 9, 30, tzinfo=UTC),
        "明细查询",
        guarded_query=guarded,
        continuation_available=continuation_available,
    )


def test_context_reuses_query_definition_and_page_store_keeps_only_current_page():
    contexts = DatabaseQueryContextStore()
    pages = DatabasePageStore()
    first = answer(0, has_more=True)
    context = contexts.start(owner_user_id=3, conversation_id=4, answer=first)
    page_one = pages.put(
        owner_user_id=3, conversation_id=4, answer=first, query_id=context.query_id
    )

    second = answer(1, has_more=False)
    advanced = contexts.advance(
        context,
        owner_user_id=3,
        conversation_id=4,
        answer=second,
    )
    page_two = pages.put(
        owner_user_id=3, conversation_id=4, answer=second, query_id=advanced.query_id
    )

    assert advanced.query_id == context.query_id
    assert advanced.next_page_index == 2
    assert contexts.latest(owner_user_id=3, conversation_id=4) == advanced
    assert pages.get(page_one.result_id, owner_user_id=3) is None
    assert pages.get(page_two.result_id, owner_user_id=3) == page_two
    assert contexts.get(context.query_id, owner_user_id=8, conversation_id=4) is None
    assert "SELECT id" not in repr(context)
    assert "1" not in repr(page_two)


def test_expired_context_is_not_returned():
    contexts = DatabaseQueryContextStore()
    context = contexts.start(owner_user_id=3, conversation_id=4, answer=answer(0, has_more=True))
    contexts._items[context.query_id] = replace(context, expires_at=0)
    assert contexts.latest(owner_user_id=3, conversation_id=4) is None


def test_result_budget_can_update_has_more_without_changing_query_id():
    contexts = DatabaseQueryContextStore()
    context = contexts.start(owner_user_id=3, conversation_id=4, answer=answer(0, has_more=False))
    updated = contexts.update_has_more(
        context,
        owner_user_id=3,
        conversation_id=4,
        has_more=True,
    )
    assert updated.query_id == context.query_id
    assert updated.next_page_index == context.next_page_index
    assert updated.has_more is True


def test_continuation_is_bound_to_the_current_result_page():
    contexts = DatabaseQueryContextStore()
    pages = DatabasePageStore()
    first = answer(0, has_more=True)
    context = contexts.start(owner_user_id=3, conversation_id=4, answer=first)
    page = pages.put(owner_user_id=3, conversation_id=4, answer=first, query_id=context.query_id)
    bound = contexts.bind_result(
        context,
        owner_user_id=3,
        conversation_id=4,
        result_id=page.result_id,
    )
    assert bound.current_result_id == page.result_id
    advanced = contexts.advance(
        bound,
        owner_user_id=3,
        conversation_id=4,
        answer=answer(1, has_more=False),
    )
    assert advanced.current_result_id is None
    assert pages.get(page.result_id, owner_user_id=3, conversation_id=4) == page


def test_unstable_first_page_keeps_has_more_but_disables_continuation():
    contexts = DatabaseQueryContextStore()
    pages = DatabasePageStore()
    first = answer(0, has_more=True, continuation_available=False)
    context = contexts.start(owner_user_id=3, conversation_id=4, answer=first)
    page = pages.put(owner_user_id=3, conversation_id=4, answer=first, query_id=context.query_id)

    assert page.has_more is True
    assert page.continuation_available is False
    assert context.has_more is True
    assert context.continuation_available is False
    assert first.locator()["continuation_available"] is False


def test_next_page_receipt_uses_only_verified_page_metadata_and_source_handle():
    second = answer(1, has_more=True)
    final = answer(2, has_more=False)

    assert database_page_receipt(second, "source_2") == (
        "数据库查询第 2 页已返回 1 行，has_more=true，"
        "查询时间：2026-01-28 17:30:00（北京时间）。还有下一页。"
        "请在引用来源中查看本页明细及查询条件。[[source_2]]"
    )
    assert "已到最后一页" in database_page_receipt(final, "source_1")


def test_chart_receipt_requires_complete_scope_and_unambiguous_aggregate_columns():
    aggregated = replace(
        answer(0, has_more=False),
        query_kind="aggregate",
        result=QueryResult(
            columns=(ResultColumn("department", "text"), ResultColumn("total_amount", "numeric")),
            rows=({"department": "sales", "total_amount": "780.00"},),
            row_count=1,
            has_more=False,
            referenced_relations=('"biz"."orders"',),
            elapsed_ms=1,
        ),
    )
    question = "按 department 汇总 total_amount，生成全部聚合数据的柱状图 PNG 预览。"
    receipt = database_chart_receipt(aggregated, "source_7", question)
    assert receipt is not None
    assert "1 项聚合数据" in receipt and "has_more=false" in receipt
    assert "2026-01-28 17:30:00（北京时间）" in receipt and "[[source_7]]" in receipt
    assert "后台生成" in receipt and "已生成" not in receipt
    assert database_chart_receipt(aggregated, "source_7", "生成柱状图 PNG") is None
    assert database_chart_receipt(aggregated, "source_7", "导出全部结果的柱状图") is None
    assert database_chart_receipt(aggregated, "source_7", question + "并分析原因") is None
    assert database_chart_receipt(answer(0, has_more=False), "source_7", question) is None
    ambiguous = replace(
        aggregated,
        result=replace(
            aggregated.result,
            columns=(
                ResultColumn("department", "text"),
                ResultColumn("category", "text"),
                ResultColumn("amount", "numeric"),
            ),
        ),
    )
    assert database_chart_receipt(ambiguous, "source_7", "生成全部结果的柱状图 PNG") is None
