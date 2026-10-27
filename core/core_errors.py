"""Stable safe failures for Python/CLI clients; never expose supplier messages."""

from .errors import MESSAGES, sanitize_details

MESSAGES.update(
    {
        "SUBSCRIPTION_KEYWORDS_REQUIRED": "请先添加长期关键词，再开启关键词来源。",
        "SUBSCRIPTION_NOT_FOUND": "未找到这个UP订阅，请先在页面添加对应UID。",
        "INVALID_ARGUMENT": "接口参数未通过校验。",
        "QUERY_TERMS_REQUIRED": "查询必须由宿主提供至少一个有效 terms 关键词；不会从 question 自动生成关键词。",
        "PROFILE_SCOPE_DENIED": "请求不属于宿主授权的当前档案。",
        "ITEM_NOT_AVAILABLE": "该条目在当前档案、来源范围或规则下不可读取。",
        "SETTINGS_CONFLICT": "设置版本已变化，请重新读取后再修改。",
        "SETTINGS_INVALID": "修改不符合现有订阅数量或配置约束。",
        "SETTINGS_BUSY": "采集、生成或设置提交正在进行，请稍后重试。",
        "SETTINGS_IO": "设置保存暂未完成；使用相同请求编号重试或重新读取以恢复。",
        "SETTINGS_RECOVERY_CONFLICT": "设置文件已被其他修改覆盖，未自动覆盖外部修改。",
        "PREFERENCE_CHANGE_NOT_FOUND": "找不到属于当前档案的设置变更。",
        "PREFERENCE_UNDO_CONFLICT": "同一字段已有后续修改，或撤销会违反当前配置约束；未覆盖当前设置。",
        "IDEMPOTENCY_CONFLICT": "同一请求编号不能用于不同参数。",
        "QUERY_CONTEXT_CHANGED": "来源、规则、相关数据或证据策略已改变；请使用新请求编号查询，不会自动重复模型请求。",
        "QUERY_TIMEOUT": "查询等待超时；请保留并复用请求编号，避免重复模型请求。",
        "QUERY_OUTCOME_UNKNOWN": "原查询进程已退出，模型请求结果无法确认；此请求编号不会自动重新调用模型。",
        "PAGINATION_CHANGED": "数据或覆盖状态已变化，请从第一页重新检索。",
        "OUTPUT_BUDGET": "查询输入或返回内容超过配置的长度预算，请缩小范围或调整宿主预算。",
        "QUERY_FAILED": "查询未完成，现有证据不足以确定具体原因。",
        "SOURCE_TYPE_UNSUPPORTED": "不支持该来源类型；先查看来源状态。临时联网搜索请调用search_bilibili。",
        "CORE_STORAGE_FAILED": "查询或设置存储暂不可用；请保留请求编号，恢复后重试。",
        "MODEL_QUOTE_MISMATCH": "模型证据片段不属于对应已提供文字，本次证据未采用。",
        "MODEL_OUTPUT_BUDGET": "模型结果超过本次允许的数量或长度预算。",
    }
)


class CoreError(Exception):
    def __init__(self, code, details=None):
        self.code = code if code in MESSAGES else "QUERY_FAILED"
        self.message = MESSAGES[self.code]
        self.details = sanitize_details(details) if isinstance(details, dict) else {}
        super().__init__(self.message)

    def as_dict(self):
        return dict(error_code=self.code, error_message=self.message, error_details=self.details)
