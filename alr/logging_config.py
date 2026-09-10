"""Structured logging — section 16 wants OpenTelemetry-grade observability.
This MVP ships JSON-line logging to stdout (the universal lowest common
denominator for log aggregators — Datadog, CloudWatch, Loki, etc. all
ingest this fine) rather than a full OTel SDK dependency. Swap
`configure_logging` for real OTel instrumentation when you have a
collector to point it at; every call site already goes through the
`logging` module so the swap is one file.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any, Dict


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": round(time.time(), 3),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if hasattr(record, "trace_id"):
            payload["trace_id"] = record.trace_id  # type: ignore[attr-defined]
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger("alr")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"alr.{name}")
