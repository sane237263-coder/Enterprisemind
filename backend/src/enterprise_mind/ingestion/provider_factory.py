"""Media adapter selection uses the declared protocol, never a model or vendor label."""

from enterprise_mind.ingestion.media_providers import HTTPOCR, OCRProvider
from enterprise_mind.ingestion.tencent_ocr import TencentOCR
from enterprise_mind.rag.config import OCRProviderConfig, TencentOCRConfig


def create_ocr_provider(config: OCRProviderConfig | TencentOCRConfig | None) -> OCRProvider | None:
    if config is None:
        return None
    if isinstance(config, TencentOCRConfig):
        return TencentOCR(config)
    return HTTPOCR(config)
