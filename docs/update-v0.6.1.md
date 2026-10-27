# v0.6.1：空结果与查询状态修复

2026-10-27。在独立插件目录修复，不修改原 bilibili-shixun 项目、运行数据、账号凭据或其他插件。安装后需要重载插件，确认启动日志为 v0.6.1；沿用原运行目录。

## 行为变化

- B 站匿名搜索实测零命中时会省略 result，并返回 numResults=0、numPages=0、in_black_key=0。此结构现在被接受为成功空结果；缺少计数、非零计数、黑名单标志、错误码、显式错误类型 result 仍报告解析失败。长期关键词使用相同校验，避免把不明响应误当空结果。
- time_range.period 新增 last_3_days，按服务端计算截至请求时点的过去72小时，跨夏令时仍为72小时。临时搜索以任务开始时点固定范围，状态读取不移动窗口。today 保持原语义；显式 start 包含、end 不包含，日期结束值为该日零点。工具说明引导模型使用周期，不能保证模型永远不传错参数。
- 失败搜索明确提示已经结束、无需继续 status 等待；运行中才提示有限等待。保留 status 幂等读取能力，不因重复读取触发网络。模型是否遵循说明仍需真实对话复测。
- get_settings 的 data_status、has_retained_results 仅汇总启用的长期来源，data_status_scope=enabled_long_term_sources。source_status 保持原逐来源诊断，并添加 temporary_source_status 供独立查看临时结果及失败；临时失败不再使健康的长期来源变为 old_snapshot_after_failure。

## 验证及边界

81项离线回归，涵盖合法空结果经过真实 SDK 包装进入临时/长期采集、异常结构拒绝、失败终态提示、健康长期来源与临时失败隔离、72小时及夏令时边界。另用修复后的 search_public 实际匿名查询两次：不存在的组合词返回0条、remote_has_more=false；二阶堂希罗返回20条、remote_has_more=true。未读取保存的登录凭据，未写入生产候选池或调用付费模型。

语音延迟由用户的其他插件处理，本轮不改语音插件或配置。没有部署服务器或发送QQ消息，线上对话行为仍需安装新版后复测。
