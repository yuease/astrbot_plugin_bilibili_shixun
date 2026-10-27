"""Allowlisted diagnostics. Never serialize exception messages, inputs or responses."""

import json
import re
import socket
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from urllib.error import HTTPError, URLError

import httpx
from pydantic import ValidationError

MESSAGES = {
    "SOURCE_ACCOUNT_MISMATCH": "关注来源 UID 与扫码登录账号不同，请使用当前登录账号的 UID。",
    "SOURCE_ACCOUNT_CHANGED": "登录账号已变化，请重新读取设置后再采集。",
    "SOURCE_RATE_LIMIT": "B 站限制了当前请求；请降低频率或稍后重试，不会因此重新扫码。",
    "SOURCE_DEBUG_LOGGING": "B 站库的请求调试日志已开启，为避免记录 Cookie，本次没有采集。",
    "SOURCE_CONNECTION": "B 站连接失败，请检查服务器网络。",
    "SOURCE_TIMEOUT": "来源采集超时，可稍后重试。",
    "SOURCE_HTTP": "B 站接口请求未成功，请稍后重试或检查插件日志。",
    "SOURCE_FORMAT": "来源返回的内容不是预期的数据结构，请检查路由或来源服务。",
    "SOURCE_PARSE": "来源数据解析失败，请检查来源返回格式。",
    "SOURCE_AUTH_REQUIRED": "尚未登录 B 站，请在 QQ 管理员私聊发送 /B站登录。",
    "SOURCE_AUTH_EXPIRED": "B 站登录已失效，请在 QQ 管理员私聊重新扫码。",
    "SOURCE_BUDGET": "本轮采集请求或时间预算已用完，该来源尚未请求；已有结果已保留。",
    "TASK_BUSY": "同类任务已在本机服务中运行，请稍候。",
    "SOURCE_UNKNOWN": "来源采集失败，现有证据不足以确定原因。",
    "ALL_SOURCES_FAILED": "全部来源采集失败，本次未调用模型。请查看各来源的错误说明。",
    "MODEL_CONFIG_MISSING": "模型配置缺失，请检查AstrBot 模型配置。",
    "MODEL_CONFIG_INVALID": "模型配置未通过检查，请检查AstrBot 模型配置。",
    "MODEL_AUTH": "模型服务鉴权失败，请检查提供商、密钥和端点配置。",
    "MODEL_PERMISSION": "模型服务拒绝访问，请检查账号或模型访问权限；不能据此断言风控原因。",
    "MODEL_RATE_LIMIT": "模型服务限流，请稍后重试或检查提供商额度。",
    "MODEL_CONNECTION": "模型连接失败，候选已保留，请检查模型端点与网络。",
    "MODEL_TIMEOUT": "模型请求超时，候选已保留，可稍后重试。",
    "MODEL_SERVER": "模型服务端错误，候选已保留，可稍后重试。",
    "MODEL_HTTP": "模型服务返回 HTTP 错误，候选已保留，请检查提供商配置和服务状态。",
    "MODEL_PARSE": "模型结构化输出解析失败，候选已保留，本次未生成简报。",
    "MODEL_FIELDS": "模型结果字段校验未通过，候选已保留，本次未生成简报。",
    "MODEL_UNKNOWN_IDS": "模型结果校验未通过：返回了未知条目。候选已保留，本次未生成简报。",
    "MODEL_DUPLICATE_IDS": "模型结果校验未通过：返回了重复条目。候选已保留，本次未生成简报。",
    "MODEL_SHORTLIST_LIMIT": "模型结果校验未通过：短名单数量超限。候选已保留，本次未生成简报。",
    "MODEL_UNKNOWN": "模型筛选未完成，现有证据不足以确定原因。候选已保留，请查看运行信息。",
    "PERSISTENCE_FAILED": "数据库保存失败，本次未确认生成正式简报，请检查存储后查看运行记录。",
    "EXPORT_FAILED": "运行文件导出失败，请检查目录权限和磁盘空间。",
    "RECOVERY_FAILED": "历史文件恢复失败，本轮尚未开始采集，请检查目录权限和磁盘空间。",
    "UNCLASSIFIED": "运行未完成，现有证据不足以确定具体原因，请查看已知阶段和错误类型。",
    "LEGACY_UNKNOWN": "旧记录未保存详细原因，无法确定具体失败原因。",
    "PROCESS_INTERRUPTED": "运行进程已中断，本次没有登记正式推荐。可检查已保留候选后重试。",
}


def diagnostic(code, details=None):
    if code not in MESSAGES:
        code = "UNCLASSIFIED"
    result = dict(error_code=code, error_message=MESSAGES[code])
    if details:
        result["error_details"] = sanitize_details(details) if isinstance(details, dict) else {}
    return result


def safe_usage(value):
    if not isinstance(value, dict):
        return None
    result = {
        k: v
        for k, v in value.items()
        if k in ("input_tokens", "output_tokens", "total_tokens") and type(v) is int and v >= 0
    }
    for key in ("input_token_details", "output_token_details"):
        if isinstance(value.get(key), dict):
            result[key] = {
                k: v
                for k, v in value[key].items()
                if k in ("audio", "image", "text", "cache_creation", "cache_read", "reasoning")
                and type(v) is int
                and v >= 0
            }
    return result


class DiagnosedError(ValueError):
    def __init__(self, code, details=None):
        self.diagnosis = diagnostic(code, details)
        super().__init__(self.diagnosis["error_message"])


class ModelConfigurationError(DiagnosedError):
    pass


def safe_type(exc):
    name = type(exc).__name__
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"


def validation_details(exc):
    # Extra-field names and dictionary keys may themselves contain private input.
    fields = {
        "picks",
        "id",
        "summary",
        "reason",
        "action",
        "base_score",
        "eligible_regular",
        "eligible_explore",
        "exploration_reason",
        "evidence",
        "quote",
        "span_id",
        "item_ref",
        "evidence_ref",
    }
    errors = []
    for err in exc.errors(include_input=False, include_context=False, include_url=False)[:30]:
        path = [
            part if isinstance(part, int) or part in fields else "<unknown_field>"
            for part in err["loc"]
        ]
        kind = err["type"]
        if not re.fullmatch(r"[a-z_]{1,60}", kind):
            kind = "validation_error"
        errors.append(dict(path=path, type=kind))
    return dict(validation_errors=errors)


def parsing_diagnostic(exc):
    # Only inspect typed causes. Parser exception messages may contain the entire output.
    current, seen = exc, set()
    for _ in range(6):
        if not isinstance(current, BaseException) or id(current) in seen:
            break
        seen.add(id(current))
        if isinstance(current, ValidationError):
            return diagnostic("MODEL_FIELDS", validation_details(current))
        if isinstance(current, json.JSONDecodeError):
            return diagnostic(
                "MODEL_PARSE",
                dict(parse_reason="invalid_json", line=current.lineno, column=current.colno),
            )
        current = current.__cause__ or current.__context__
    return diagnostic("MODEL_PARSE", dict(parse_reason="structured_output_unreadable"))


def source_diagnostic(exc):
    # SourceError already holds a sanitized diagnosis; imported lazily to avoid a module cycle.
    from .source_types import SourceError

    if isinstance(exc, (SourceError, DiagnosedError)):
        return exc.diagnosis
    reason = exc.reason if isinstance(exc, URLError) else exc
    if isinstance(reason, (TimeoutError, socket.timeout, httpx.TimeoutException)):
        return diagnostic("SOURCE_TIMEOUT")
    if isinstance(exc, HTTPError):
        return diagnostic("SOURCE_HTTP", dict(http_status=int(exc.code)))
    if isinstance(exc, (ET.ParseError, ValidationError)):
        return diagnostic("SOURCE_PARSE")
    if isinstance(exc, URLError) or isinstance(reason, (ConnectionError, OSError)):
        return diagnostic("SOURCE_CONNECTION")
    return diagnostic("SOURCE_UNKNOWN")


def classify(exc, phase):
    if isinstance(exc, DiagnosedError):
        return exc.diagnosis
    if phase == "model_config":
        return diagnostic("MODEL_CONFIG_INVALID")
    if phase in ("model_request", "model_parse", "model_fields", "model_validate"):
        if isinstance(exc, ValidationError):
            return diagnostic("MODEL_FIELDS", validation_details(exc))
        if isinstance(exc, json.JSONDecodeError):
            return parsing_diagnostic(exc)
        if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
            return diagnostic("MODEL_TIMEOUT")
        if isinstance(exc, (httpx.ConnectError, ConnectionError)):
            return diagnostic("MODEL_CONNECTION")
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            code = {401: "MODEL_AUTH", 403: "MODEL_PERMISSION", 429: "MODEL_RATE_LIMIT"}.get(status)
            if code is None:
                code = "MODEL_SERVER" if 500 <= status <= 599 else "MODEL_HTTP"
            return diagnostic(code, dict(http_status=status))
        return diagnostic("MODEL_PARSE" if phase == "model_parse" else "MODEL_UNKNOWN")
    if isinstance(exc, sqlite3.Error) or phase == "persist":
        return diagnostic("PERSISTENCE_FAILED")
    if phase == "export":
        return diagnostic("EXPORT_FAILED")
    if phase == "recover":
        return diagnostic("RECOVERY_FAILED")
    return diagnostic("UNCLASSIFIED")


class RunFailure(RuntimeError):
    def __init__(self, run_id, diagnosis):
        self.run_id, self.diagnosis = run_id, diagnosis
        super().__init__(diagnosis["error_message"])


class RunValueFailure(RunFailure, ValueError):
    pass


class RunIOFailure(RunFailure, OSError):
    pass


def run_failure(exc, run_id, diagnosis):
    cls = (
        RunIOFailure
        if isinstance(exc, OSError)
        else RunValueFailure
        if isinstance(exc, ValueError)
        else RunFailure
    )
    return cls(run_id, diagnosis)


def public_diagnostic(data):
    """Legacy histories must not be reinterpreted from class names or old free text."""
    code = data.get("error_code")
    if code not in MESSAGES:
        if data.get("error_type") or data.get("failed_stage"):
            return diagnostic("LEGACY_UNKNOWN")
        return {}
    result = diagnostic(code)
    # These fields are generated by our allowlisted code, never by a model/provider.
    if isinstance(data.get("error_details"), dict):
        result["error_details"] = sanitize_details(data["error_details"])
    if code == "EXPORT_FAILED" and data.get("export_recovered"):
        result["error_message"] = "本次文件导出曾失败，现已从数据库恢复，无需重新生成。"
    elif code == "EXPORT_FAILED" and data.get("briefing_saved"):
        result["error_message"] = (
            "简报已保存到数据库，文件导出失败。请运行 recover 恢复文件，无需重新生成。"
        )
    elif code == "EXPORT_FAILED" and data.get("data_saved"):
        result["error_message"] = "运行数据已保存到数据库，文件导出失败。请运行 recover 恢复文件。"
    elif code == "PERSISTENCE_FAILED" and data.get("briefing_saved"):
        result["error_message"] = (
            "简报已保存到数据库，但后续运行记录保存异常。请检查存储并查看本次简报，无需重新生成。"
        )
    return result


def source_public(data):
    result = {
        k: data[k]
        for k in ("kind", "key", "label", "status", "count", "batches", "stop_reason")
        if k in data
    }
    if data.get("status") not in ("ok", "empty", "pending"):
        result.update(public_diagnostic(data) or diagnostic("LEGACY_UNKNOWN"))
    return result


def sanitize_details(details):
    numeric = {
        "actual_count",
        "allowed_count",
        "unknown_id_count",
        "duplicate_count",
        "http_status",
        "line",
        "column",
        "actual_chars",
        "allowed_chars",
        "unknown_item_ref_count",
        "unknown_evidence_ref_count",
        "cross_item_ref_count",
    }
    result = {
        k: v for k, v in details.items() if k in numeric and type(v) is int and 0 <= v <= 10**9
    }
    if details.get("phase") in (
        "input_pack",
        "selection_count",
        "selection_chars",
        "response_pack",
    ):
        result["phase"] = details["phase"]
    if details.get("parse_reason") in (
        "invalid_json",
        "structured_output_unreadable",
        "missing_parsed_output",
    ):
        result["parse_reason"] = details["parse_reason"]
    if isinstance(details.get("validation_errors"), list):
        fields = {
            "picks",
            "id",
            "summary",
            "reason",
            "action",
            "base_score",
            "eligible_regular",
            "eligible_explore",
            "exploration_reason",
            "evidence",
            "item_ref",
            "evidence_ref",
            "<unknown_field>",
        }
        result["validation_errors"] = [
            dict(
                path=[
                    v if type(v) is int or isinstance(v, str) and v in fields else "<unknown_field>"
                    for v in (e.get("path", []) if isinstance(e.get("path"), list) else [])[:12]
                ],
                type=e["type"]
                if isinstance(e.get("type"), str) and re.fullmatch("[a-z_]{1,60}", e["type"])
                else "validation_error",
            )
            for e in details["validation_errors"][:30]
            if isinstance(e, dict)
        ]
    return result


@contextmanager
def safe_model_logs():
    """Domain code never changes the host application's logging configuration."""
    yield
