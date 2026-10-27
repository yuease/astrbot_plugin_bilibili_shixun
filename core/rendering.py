import re


def escape(value):
    return re.sub(r"([\\`*_{}\[\]()<>#+.!|~-])", r"\\\1", str(value).replace("\n", " "))


def markdown(state: dict) -> str:
    articles = {a["id"]: a for a in state["articles"]}
    lines = [
        "# 个人信息简报",
        "",
        f"生成时间：{state['created']}",
        "",
        "依据范围：B 站标题、正文／简介、作者、发布时间、视频时长与标签；图片仅展示，未识别图片、观看视频或读取字幕。推荐理由是模型判断，内容未经独立核实。",
        "",
    ]
    if state.get("failures"):
        lines += ["本次采集不完整：", *[f"- {escape(f)}" for f in state["failures"]], ""]
    picks = state["selection"]["picks"]
    if not picks:
        lines += ["本批输入中没有选出合适内容。这不代表平台没有相关更新。", ""]
    for i, pick in enumerate(picks, 1):
        a = articles[pick["id"]]
        title = escape(a["title"])
        duration = (
            "不适用"
            if a.get("content_type", "video") != "video"
            else str(a["duration_seconds"]) + " 秒"
            if a.get("duration_seconds") is not None
            else "未知"
        )
        lines += [
            f"## {i}. {title}",
            "",
            f"来源：<{a['url']}>",
            f"订阅溯源：{escape('；'.join(s['label'] for s in a.get('sources', []))) or '旧记录未提供'}",
            "",
            f"作者：{escape(a['author'])}；发布时间：{escape(a['published'])}；内容类型：{escape(a.get('content_type', 'video'))}",
            "",
            f"内容摘要：{escape(pick['summary'])}",
            "",
            f"推荐理由：{escape(pick['reason'])}",
            "",
            f"类型：{'探索' if pick.get('lane') == 'explore' else '常规'}；时长：{duration}；标签：{escape(', '.join(a.get('tags', []))) or '未知'}",
            "",
            f"建议动作：{escape(pick['action'])}",
            "",
        ]
        for context in a.get("dynamic_contexts", []):
            lines += [
                f"动态上下文：{escape(context.get('author', '未知'))}：{escape(context.get('text', ''))}；原文：<{context['url']}>",
                "",
            ]
    return "\n".join(lines)
