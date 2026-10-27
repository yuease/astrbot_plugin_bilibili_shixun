"""Public errors and safe diagnostic logging."""

from pathlib import Path
from traceback import extract_tb

MESSAGES = {
    "INVALID_ARGUMENT": "参数无效，请检查字段、日期和数值范围。",
    "ACCESS_DENIED": "此操作仅允许获准的私聊用户使用；群聊禁用，请检查插件访问设置。",
    "USER_LIMIT": "个人档案数量已达管理员配置上限，请联系管理员。",
    "DASHBOARD_DENIED": "需要已认证且获准访问此插件的 Dashboard 管理员。",
    "NOT_CONFIGURED": "请先在订阅来源页面保存至少一个有效来源。",
    "SETTINGS_CONFLICT": "设置已变化，请刷新页面后再保存。",
    "TASK_BUSY": "采集、临时搜索或简报任务正在运行，请完成后再操作。",
    "REQUEST_CONFLICT": "同一请求编号不能用于不同操作或参数。",
    "REQUEST_REQUIRED": "缺少有效请求编号；重试时请沿用原编号。",
    "BODY_TOO_LARGE": "提交内容超过 128 KiB，请缩短兴趣描述或减少配置条目。",
    "NOT_FOUND": "未找到这条记录或可下载的正式报告。",
    "PLUGIN_CLOSED": "插件正在关闭，请等待重载完成。",
    "RUNTIME_BUSY": "此数据目录已被另一个插件实例占用。",
    "MESSAGE_READ_LIMIT": "本条消息已达到八次不同读取的上限。",
    "INTERNAL_ERROR": "操作未完成，请凭请求编号查看插件日志。",
    "BILI_LOGIN_REQUIRED": "请先在自己的 QQ 私聊发送 /B站登录 并扫码确认。",
    "BILI_QQ_PRIVATE_REQUIRED": "请在自己的 QQ 私聊中进行 B 站登录。",
    "BILI_OFFICIAL_C2C_REQUIRED": "QQ官方机器人请使用与机器人的单聊（C2C）；暂不支持频道私信或群聊登录。",
    "BILI_OTHER_OWNER": "当前账号或登录操作绑定了另一位用户，请由原绑定者操作。",
    "BILI_QR_FAILED": "未完成 B 站扫码登录，请稍后重试。",
    "BILI_LOGIN_BUSY": "正在等待扫码登录，请完成或等待二维码过期后再采集。",
    "BILI_PROACTIVE_UNAVAILABLE": "此 QQ 适配器不支持当前主动推送方式，请在私聊中手动获取二维码。",
    "BILI_DELIVERY_FAILED": "二维码未能送达绑定的 QQ 私聊，请检查机器人连接。",
    "BILI_SESSION_INVALID": "本地 B 站登录记录不可读取，请检查插件日志。",
    "IMAGE_NOT_FOUND": "此条目没有可发送的内容图片。",
    "IMAGE_METADATA_UNKNOWN": "当前未解析到可发送图片，尚不能确认原帖没有图片；请刷新来源后重试。",
    "TEMP_SEARCH_LIMIT": "本条消息已达到三次不同临时联网搜索的上限，请缩小范围或在新消息中继续。",
    "TEMP_SEARCH_EXPIRED": "临时搜索结果已超过24小时保留期，请在新消息中重新搜索。",
    "IMAGE_URL_DENIED": "图片地址不属于允许的 B 站内容图片地址。",
    "IMAGE_TOO_LARGE": "图片超过下载、像素或发送大小限制。",
    "IMAGE_INVALID": "图片格式不可读取或不受支持。",
    "IMAGE_DOWNLOAD_FAILED": "图片下载或处理失败，请稍后再试。",
    "IMAGE_DELIVERY_UNCERTAIN": "图片发送失败或结果未确认；为避免重复，已停止本次发送，请先检查聊天记录。",
}

QR_REASONS = {
    "invalid_json",
    "invalid_response",
    "api_rejected",
    "invalid_data",
    "invalid_url",
    "unsupported_url",
    "invalid_key",
}


class PluginError(Exception):
    def __init__(self, code, *, status=400, reason=None, api_code=None):
        self.code = code
        self.message = MESSAGES.get(code, MESSAGES["INTERNAL_ERROR"])
        self.status = status
        self.reason = reason if reason in QR_REASONS else None
        self.api_code = api_code if type(api_code) is int else None
        super().__init__(self.message)

    def as_dict(self):
        return {"error_code": self.code, "error_message": self.message}


def log_failure(logger, stage, request_id, exc):
    # Exception messages/tracebacks can contain credentials or source content.
    if isinstance(exc, PluginError):
        logger.error(
            "B站拾讯 stage=%s request=%s exception=%s code=%s reason=%s api_code=%s",
            stage,
            request_id,
            type(exc).__name__,
            exc.code if exc.code in MESSAGES else "UNKNOWN",
            exc.reason,
            exc.api_code,
        )
    else:
        locations = " > ".join(
            f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
            for frame in extract_tb(exc.__traceback__)[-5:]
        )
        logger.error(
            "B站拾讯 stage=%s request=%s exception=%s locations=%s",
            stage,
            request_id,
            type(exc).__name__,
            locations,
        )
