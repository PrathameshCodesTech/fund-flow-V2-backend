from dataclasses import dataclass


class ExtractionError(ValueError):
    pass


@dataclass
class ExtractedRecord:
    raw_data: dict
    normalized_data: dict
    document_type: str
    confidence_score: float
    validation_errors: list[str]


@dataclass
class ExtractionResult:
    raw_data: dict
    records: list[ExtractedRecord]
    extractor: str

