"""Versioned local rules. Mark evidence; never delete content or infer author blocks."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Rule:
    id: str
    version: int
    label: str
    description: str
    match: object


RULES = {}


def register(rule):
    if rule.id in RULES:
        raise ValueError("rule already registered")
    RULES[rule.id] = rule
    return rule


def lottery(article):
    # Require a participation instruction AND an explicit draw/prize commitment.
    # Mentions, tutorials, warnings, results and quoted criticism alone do not qualify.
    if not article.get("is_dynamic"):
        return None
    text = article.get("body_text") or article.get("excerpt", "")
    if re.search(
        r"(?:抽奖(?:教程|原理|算法|骗局|结果)|开奖结果|开奖公告|中奖名单|获奖名单|(?:讲解|分析|揭露|不要参与|谨防|反对).{0,8}抽奖)",
        article.get("title", ""),
    ):
        return None
    participation = re.search(
        r"(?:关注.{0,30}(?:并|后|且).{0,12}转发|转发(?:本条|这条|此条|本)动态|评论(?:区)?.{0,12}(?:参与|抽取)|(?:转发|评论).{0,8}即可参与|(?:关注|三连).{0,100}转发动态.{0,100}即可参与)",
        text,
        re.S,
    )
    leading = (
        (article.get("repost_text") or text[: participation.start()]) if participation else text
    )
    if re.search(
        r"(?:讲解|分析|示例|教程|算法|骗局|风险|不要参与|谨防|反对|举报|揭露|开奖结果|中奖名单|抽奖结果|开奖公告|获奖名单)",
        leading[:160],
    ):
        return None
    commitment = re.search(
        r"(?:我们将.{0,12}(?:随机)?抽(?:选|取)|(?:随机)?抽(?:选|取).{0,12}(?:位|名).{0,20}(?:送|奖)|送出(?:以下)?奖品|(?:本次活动|本活动|将于).{0,30}开奖|有机会获得.{1,30})",
        text,
    )
    if participation and commitment:
        return "包含明确参与动作及抽选／奖品承诺"
    draw = re.search(r"(?:随机抽取|抽取|抽选)", text) if participation else None
    if draw:
        prizes = text[draw.end() :]
        listing = re.search(
            r"(?:^|\s|[；;。])(?:\d{1,2}[、.．)）]\s*)?[^：:；;\n]{2,70}[：:]\s*[1-9]\d*\s*(?:位|名|人)",
            prizes,
        )
        listing = listing or re.search(
            r"(?:^|\s|[；;。])(?:\d{1,2}[、.．)）]\s*)?[1-9]\d*\s*(?:位|名|人)\s*[：:]\s*[^：:；;\n]{2,70}",
            prizes,
        )
        if listing:
            return "包含明确参与要求、抽选承诺及列明人数和奖品的列表"
    return None


register(
    Rule(
        "lottery",
        1,
        "过滤抽奖动态",
        "仅标记同时具有参与要求与抽选／奖品承诺的动态，不按“抽奖”两字排除。",
        lottery,
    )
)


def catalog():
    return [
        dict(id=r.id, version=r.version, label=r.label, description=r.description)
        for r in RULES.values()
    ]


def marks(article):
    result = []
    for rule in RULES.values():
        reason = rule.match(article)
        if reason:
            result.append(dict(id=rule.id, version=rule.version, reason=reason))
    return result


def block_reason(article, settings):
    for block in settings.blocked_ups:
        if block.scope == "dynamics" and not article.get("is_dynamic"):
            continue
        if article.get("author_uid") == block.uid or (
            block.scope == "all" and article.get("original_author_uid") == block.uid
        ):
            return dict(
                id="manual_up",
                version=1,
                reason="用户明确屏蔽该作者的"
                + ("动态" if block.scope == "dynamics" else "全部内容"),
            )
    return None


def advertisement(article):
    title = article.get("title", "")
    text = article.get("body_text") or article.get("excerpt", "")
    leading = title + " " + (article.get("repost_text") or text[:180])
    if re.search(
        r"(?:新闻|报道|研究|分析|测评|教程|技术|源码|广告分析|广告识别|反广告|不要购买|不要点击|不要下单|不要领券|不要搜|不要嗖|不要复制|不要copy|别搜|别嗖|切勿复制|切勿搜索|别买|不必购买|不需要购买|拒绝购买|切勿购买|谨防|骗局|避坑|不是广告|不推荐购买|不建议购买|(?:没有|未给出|未提供).{0,8}(?:价格|促销信息))",
        leading,
        re.I,
    ):
        return None
    action = re.search(
        r"(?:限时(?:特惠|促销|优惠)|立即(?:抢购|下单)|现在下单|点击.{0,8}(?:购买|下单|领券)|下单(?:立减|返现)|领券.{0,8}(?:购买|下单|立减)|点击链接.{0,10}领取)",
        text,
    )
    purchase = re.search(r"(?:购买|下单|领券|优惠券|折扣码|口令|到手价|返现|抢购)", text)
    # Only classify a short, leading sales pitch. Long substantive posts with a
    # promotional footer remain intact; this rule neither rewrites nor splits them.
    if len(text) > 600 or action and action.start() > 180:
        return None
    merchants = (
        "taobao.com",
        "tb.cn",
        "tmall.com",
        "meituan.com",
        "zhuanzhuan.com",
        "jd.com",
        "pdd.com",
    )
    merchant = False
    for url in re.findall(r'https?://[^\s<>"\u3000]+', text, re.I):
        try:
            host = (urlsplit(url).hostname or "").casefold()
        except ValueError:
            continue
        if any(host == domain or host.endswith("." + domain) for domain in merchants):
            merchant = True
            break
    # A compact coupon/price pitch with an explicit merchant destination, not brand-name detection.
    if action and purchase and merchant:
        return "同时包含明确促销动作、购买／领券引导和商家链接"
    promo_text = title + "\n" + text
    brand = re.search(r"(?:淘宝闪购|美团|美団)", promo_text)
    search_code = re.search(r"(?:搜|嗖)(?:索)?\s*[:：]?\s*[【（(\[]?\s*\d{4,12}(?!\d)", promo_text)
    coupon = re.search(
        r"(?:领券|红包|满减|神券|\d+\s*[-－]\s*\d+.{0,8}券|\d+(?:\.\d+)?\s*(?:分(?:钱)?|元|块钱).{0,8}(?:外卖|到手)|(?:低至|仅需|只需|到手价)\s*\d+)",
        promo_text,
    )
    if brand and search_code and coupon:
        return "包含指定外卖品牌、搜索数字密令指令及领券／红包／低价信息"
    taobao_order = re.search(
        r"(?:复制|copy)\s*(?:本段话|这段话|本段文字)\s*打开\s*手淘\s*下单", promo_text, re.I
    )
    token = re.search(
        r"(?:\d*[（(][A-Za-z0-9]{6,40}[)）](?:://)?|[￥¥][A-Za-z0-9]{6,40}[￥¥])", promo_text
    )
    offer = re.search(
        r"(?:bug价|优惠|特价|促销|限时|低至|仅需|到手价|满减|立减|抢购|包邮|\d+(?:\.\d+)?\s*(?:元|块|一箱|一盒|一件))",
        promo_text,
        re.I,
    )
    if taobao_order and token and offer:
        return "包含复制口令打开手淘下单的指令、口令串及价格／促销信息"
    return None


register(
    Rule(
        "advertisement",
        1,
        "过滤明显促销",
        "识别商家链接促销、指定外卖品牌搜索数字密令及手淘下单口令组合；新闻、教程与否定语境保守保留。",
        advertisement,
    )
)


def enabled_rule_hits(article, settings):
    enabled = {
        (r.id, r.version)
        for r in settings.rules
        if r.enabled and r.id in RULES and r.version == RULES[r.id].version
    }
    return [h for h in marks(article) if (h["id"], h["version"]) in enabled]


def allows(article, settings):
    return not block_reason(article, settings) and not enabled_rule_hits(article, settings)
