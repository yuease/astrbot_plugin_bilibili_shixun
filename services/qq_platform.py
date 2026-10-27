"""QQ delivery policy over AstrBot's public message components."""

QQ_OFFICIAL = frozenset({"qq_official", "qq_official_webhook"})
QQ_ADAPTERS = QQ_OFFICIAL | {"aiocqhttp"}


def official_c2c(event):
    if event.get_platform_name() not in QQ_OFFICIAL:
        return True
    from botpy.message import C2CMessage

    # Channel DMs are not QQ C2C and use different upload and recipient APIs.
    return isinstance(getattr(event.message_obj, "raw_message", None), C2CMessage)


def qq_message(adapter, text, image=None):
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image, Plain

    chain = MessageChain(chain=[Plain(text)] + ([Image.fromBytes(image)] if image else []))
    if adapter in QQ_OFFICIAL:
        # Do not require the bot to have native Markdown permissions.
        chain.use_markdown(False)
    return chain


def qq_plain_result(event, text):
    result = event.plain_result(text)
    if event.get_platform_name() in QQ_OFFICIAL:
        result.use_markdown(False)
    return result
