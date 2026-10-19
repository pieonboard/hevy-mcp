# fmt: off
from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_REDACTED = "[REDACTED]"
_SECRET_KEYS = {
    "authorization",
    "accesstoken",
    "refreshtoken",
    "auth20token",
    "clientsecret",
    "password",
    "cookie",
    "setcookie",
    "apikey",
    "xapikey",
}
_KEY_PATTERN = (
    r"authorization|access[-_]?token|refresh[-_]?token|auth2\.0[-_]?token|"
    r"client[-_]?secret|password|cookie|set[-_]?cookie|api[-_]?key|x[-_]?api[-_]?key"
)
_QUOTED_KEY_VALUE = re.compile(
    rf"(?is)(?<![\w.-])(?P<prefix>[\"']?(?:{_KEY_PATTERN})[\"']?\s*[:=]\s*)"
    rf"(?P<quote>[\"'])(?P<value>.*?)(?P=quote)"
)
_HEADER_VALUE = re.compile(
    r"(?im)(?<![\w.-])(?P<prefix>(?:authorization|cookie|set[-_]?cookie)\s*[:=]\s*)"
    r"[^\r\n]*"
)
_UNQUOTED_KEY_VALUE = re.compile(
    rf"(?i)(?<![\w.-])(?P<prefix>(?:{_KEY_PATTERN})\s*[:=]\s*)[^\"'\s,;}}]+"
)
_BEARER = re.compile(r"(?i)(authorization\s*[:=]\s*)?bearer\s+[^\s,;\"'}]+")


def _sensitive(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(key).strip().lower())
    return normalized in _SECRET_KEYS


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if not parts.scheme or not parts.query:
        return value
    query = [
        (key, _REDACTED if _sensitive(key) else item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def redact_data(value: Any) -> Any:
    """Recursively copy structured values while removing credential material."""
    if isinstance(value, Mapping):
        return {
            key: _REDACTED if _sensitive(key) else redact_data(item) for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, str):
        return redact(_redact_url(value), parse_json=False)
    return value


def redact(value: object, *, parse_json: bool = True) -> str:
    text = str(value)
    if parse_json:
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            pass
        else:
            return json.dumps(redact_data(parsed), ensure_ascii=False)
    text = _redact_url(text)
    text = _QUOTED_KEY_VALUE.sub(
        lambda match: f"{match.group('prefix')}{match.group('quote')}{_REDACTED}{match.group('quote')}",
        text,
    )
    text = _HEADER_VALUE.sub(lambda match: f"{match.group('prefix')}{_REDACTED}", text)
    text = _BEARER.sub(lambda match: f"{match.group(1) or ''}Bearer {_REDACTED}", text)
    return _UNQUOTED_KEY_VALUE.sub(lambda match: f"{match.group('prefix')}{_REDACTED}", text)


class RedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True

# fmt: on


class SafeRedactionFilter(RedactionFilter):
    """Contain external formatting errors without changing copied redaction logic."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return super().filter(record)
        except Exception:
            record.msg = "external_log"
            record.args = ()
            return True


class SafeJSONFormatter(logging.Formatter):
    """Never render arbitrary messages, arguments, stack text or exception bodies."""

    def format(self, record: logging.LogRecord) -> str:
        event: dict[str, object] = {
            "event": "external_log",
            "level": logging.getLevelName(record.levelno),
        }
        if record.name == "hevy_mcp.runtime":
            correlation = getattr(record, "correlation_id", None)
            if isinstance(correlation, str) and re.fullmatch(r"[0-9a-f]{32}", correlation):
                event["correlation_id"] = correlation
                for key in ("tool", "phase", "code", "status"):
                    value = getattr(record, key, None)
                    if isinstance(value, str) and re.fullmatch(r"[A-Za-z_]{1,64}", value):
                        event[key] = value
        if record.exc_info and record.exc_info[0]:
            event["exception_class"] = record.exc_info[0].__name__
        return json.dumps(event, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    if level.upper() not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}:
        raise ValueError("Invalid log level")
    handler = logging.StreamHandler()
    handler.addFilter(SafeRedactionFilter())
    handler.setFormatter(SafeJSONFormatter())
    logging.basicConfig(level=level.upper(), handlers=[handler], force=True)
    prefixes = (
        "fastmcp",
        "mcp",
        "httpx",
        "httpcore",
        "httpx2",
        "httpcore2",
        "playwright",
        "hevy_unofficial",
    )
    names = set(prefixes) | set(logging.root.manager.loggerDict)
    for name in names:
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            logger = logging.getLogger(name)
            for previous in logger.handlers[:]:
                logger.removeHandler(previous)
                previous.close()
            logger.propagate = True
            logger.setLevel(logging.NOTSET)
