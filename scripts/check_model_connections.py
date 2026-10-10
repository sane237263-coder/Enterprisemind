"""Explicit live probes using synthetic content only; no application DB or Worker is started."""

import argparse
import asyncio
import io
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from langchain_core.messages import HumanMessage
from PIL import Image, ImageDraw, ImageFont

from enterprise_mind.agent.provider import ChatModel
from enterprise_mind.agent.tools import retrieval_tool
from enterprise_mind.ingestion.media_providers import HTTPVision
from enterprise_mind.ingestion.provider_factory import create_ocr_provider
from enterprise_mind.rag.config import (
    EmbeddingConfig,
    EndpointConfig,
    LLMConfig,
    OCRProviderConfig,
    ProvidersConfig,
    TencentOCRConfig,
    VisionProviderConfig,
)
from enterprise_mind.rag.providers import HTTPEmbeddings, JSONProvider, ProviderFailure


def sample_image() -> bytes:
    image = Image.new("RGB", (900, 700), "white")
    draw, font = ImageDraw.Draw(image), ImageFont.load_default(size=28)
    draw.text((50, 50), "EnterpriseMind connection check", font=font, fill="black")
    for x in (50, 450, 850):
        draw.line((x, 160, x, 520), fill="black", width=3)
    for y in (160, 280, 400, 520):
        draw.line((50, y, 850, y), fill="black", width=3)
    for x, y, text in [
        (80, 200, "Item"),
        (480, 200, "Requirement"),
        (80, 320, "Travel claim"),
        (480, 320, "Within 30 days"),
        (80, 440, "Leave request"),
        (480, 440, "3 days in advance"),
    ]:
        draw.text((x, y), text, font=font, fill="black")
    data = io.BytesIO()
    image.save(data, format="PNG")
    return data.getvalue()


async def probe(role: str, values: dict[str, Any]) -> dict[str, Any]:
    if role == "llm":
        model = ChatModel(LLMConfig.model_validate(values))

        async def never_execute(body: Any) -> None:
            raise AssertionError("Probe planning must never execute tools")

        try:
            general = await model.plan([HumanMessage("请写一句简短欢迎词。")], [], "")
            knowledge = await model.plan(
                [HumanMessage("公司的出差报销期限是几天？")],
                [retrieval_tool(never_execute)],
                "",
            )
            if (
                general.intent_kind != "general"
                or not knowledge.requires_enterprise_facts
                or not any(step.tool_name == "knowledge_retrieve" for step in knowledge.tools)
            ):
                raise ValueError("unexpected planning")
            answer = "".join(
                [part async for part in model.answer([HumanMessage("只回复：连接成功")])]
            )
            summary = await model.summarize(
                [HumanMessage("刚才讨论了如何上传文件，下一步准备上传。")]
            )
            if not answer.strip() or not summary.strip():
                raise ValueError("empty output")
            return {
                "planning": "general_and_retrieval",
                "stream_characters": len(answer),
                "summary_characters": len(summary),
            }
        finally:
            await model.close()
    if role == "embedding":
        provider = HTTPEmbeddings(EmbeddingConfig.model_validate(values))
        vectors = await provider.embed(
            [
                "报销须在出差结束后30天内提交。",
                "年假需提前3天申请。",
                "出差报销期限是多少？",
            ]
        )
        return {
            "vectors": len(vectors),
            "dimensions": len(vectors[0]),
            "local_tokens": provider.counter.count("报销须在出差结束后30天内提交。"),
        }
    if role == "rerank":
        # A connectivity probe does not invent a production threshold or score bounds.
        if values.get("protocol") != "jina_compatible":
            raise ValueError("unsupported probe protocol")
        config = EndpointConfig.model_validate(
            {key: values[key] for key in EndpointConfig.model_fields if key in values}
        )
        provider = JSONProvider(config, values["secret_file_env"])
        raw = await provider.request(
            "/rerank",
            {
                "model": values["model"],
                "query": "出差报销期限是多少天？",
                "documents": [
                    "报销须在出差结束后30天内提交。",
                    "年假需至少提前3天申请。",
                    "会议室在二楼。",
                ],
                "top_n": 3,
                "return_documents": False,
            },
        )
        rows = raw["results"]
        if (
            len(rows) != 3
            or any(type(row["index"]) is not int for row in rows)
            or sorted(row["index"] for row in rows) != [0, 1, 2]
        ):
            raise ValueError("invalid indices")
        scores = [row["relevance_score"] for row in sorted(rows, key=lambda row: row["index"])]
        if any(
            type(score) not in (int, float) or not math.isfinite(score) for score in scores
        ) or scores[0] <= max(scores[1:]):
            raise ValueError("invalid relevance scores")
        return {"candidate_scores": scores, "threshold_calibrated": False}
    data = sample_image()
    if role == "vision":
        result = await HTTPVision(VisionProviderConfig.model_validate(values)).describe(
            data, 900, 700
        )
        return {
            "image_type": result.image_type,
            "description_characters": len(result.description),
        }
    config_type = (
        TencentOCRConfig if values.get("protocol") == "tencent_tc3_ocr" else OCRProviderConfig
    )
    ocr = create_ocr_provider(config_type.model_validate(values))
    assert ocr is not None
    result = await ocr.recognize(data, 900, 700)
    if not result.blocks or "30" not in " ".join(block.text for block in result.blocks):
        raise ValueError("synthetic text not recognized")
    if values.get("table_action") and not any(
        len(table.rows) == 3 and len(table.rows[0]) == 2 for table in result.tables
    ):
        raise ValueError("synthetic table not recognized")
    return {
        "blocks": len(result.blocks),
        "tables": len(result.tables),
        "table_shapes": [[len(table.rows), len(table.rows[0])] for table in result.tables],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/providers.example.yaml"))
    parser.add_argument(
        "--only",
        nargs="+",
        choices=["llm", "embedding", "rerank", "ocr", "vision"],
        default=["llm", "embedding", "rerank", "ocr", "vision"],
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config_file = args.config.resolve()
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    origins = ProvidersConfig(allowed_origins=raw["allowed_origins"]).allowed_origins
    report: dict[str, Any] = {
        "time_utc": datetime.now(UTC).isoformat(),
        "synthetic_content_only": True,
        "results": {},
    }
    for role in args.only:
        try:
            values = dict(raw[role])
            endpoint = urlsplit(values["base_url"])
            if (
                f"{endpoint.scheme}://{endpoint.netloc}" not in origins
                or endpoint.username
                or endpoint.password
                or endpoint.query
                or endpoint.fragment
            ):
                raise ValueError("unapproved endpoint")
            if values.get("tokenizer_file"):
                values["tokenizer_file"] = str(
                    (config_file.parent / values["tokenizer_file"]).resolve()
                )
            # Probe retries are disabled to avoid repeated paid OCR calls or busy-model polling.
            values["max_retries"] = 0
            details = await probe(role, values)
            result = {"status": "passed", **details}
        except ProviderFailure as error:
            result = {"status": "failed", "code": str(error)}
        except Exception as error:
            result = {"status": "failed", "code": type(error).__name__}
        report["results"][role] = result
        print(json.dumps({role: result}, ensure_ascii=True), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return int(any(item["status"] != "passed" for item in report["results"].values()))


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
