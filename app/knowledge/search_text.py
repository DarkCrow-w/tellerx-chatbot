"""查询规范化、主体识别和分词；不访问数据库或模型。"""

import re
import unicodedata

EXACT_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,12}-\d{2,}(?:-[A-Za-z0-9]+)*)",
    re.IGNORECASE,
)
ACRONYM = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,12}\d{2,})(?![A-Za-z0-9])")
ASCII_TOKEN = re.compile(r"[a-z0-9][a-z0-9_.:/-]*", re.IGNORECASE)
CJK_SPAN = re.compile(r"[\u3400-\u9fff]+")
ZH_ENTITY = re.compile(r"([\u3400-\u9fff]{2,20})(?:业务|系统|项目|模块)")
ZH_QUERY_SUBJECT = re.compile(
    r"^([\u3400-\u9fff]{2,20}?)\s*(?:当前|的|在|受|使用|由|如果|若|最新|发生|执行|需要)"
)
EN_ENTITY = re.compile(
    r"\b(?i:for|about)\s+([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,4})"
    r"(?=\s*[,\.?（(]|\s*$)"
)
EN_RELATED_SUBJECT = re.compile(
    r"\b(?i:knowledge|information|documents?|details?|content)\s+"
    r"(?:(?i:is)\s+)?(?i:related|relevant)\s+(?i:to)\s+"
    r"([A-Z][A-Za-z0-9-]*(?:\s+[A-Z][A-Za-z0-9-]*){0,4})(?=\s*[,\.?]|\s*$)"
)
CONTROLLED_ALIAS = re.compile(
    r"(?im)^(?:Chinese business name\s*/\s*中文业务称谓|"
    r"English operational name\s*/\s*英文运行称谓)\s*:\s*([^\r\n]+?)\s*$"
)
ZH_QUOTED_SUBJECT = re.compile(
    r"[\"“”「」『』]([\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9·_.\-/ ]{1,39}?)[\"“”「」『』]"
)
ZH_FOCUS_SPAN = r"([\u3400-\u9fffA-Za-z0-9][\u3400-\u9fffA-Za-z0-9·_.\-/ ]{1,39}?)"
ZH_RELATED_SUBJECT = re.compile(
    rf"^(?:与|和|跟|关于|有关|有关于|针对|围绕)\s*{ZH_FOCUS_SPAN}\s*"
    r"(?:相关|有关|关联|方面)(?:的)?"
)
ZH_ABOUT_SUBJECT = re.compile(
    rf"^(?:关于|有关|有关于|对于|针对|围绕)\s*{ZH_FOCUS_SPAN}"
    r"(?=\s*(?:，|,|的|都有哪些|有哪些|是什么|$))"
)
ZH_TOPIC_FIRST = re.compile(
    rf"^{ZH_FOCUS_SPAN}\s*[，,]\s*(?:请)?(?:介绍|说明|总结|概括|整理|列出|讲讲|说说)"
)
ZH_KNOWLEDGE_SUBJECT = re.compile(
    rf"^{ZH_FOCUS_SPAN}\s*(?:相关|有关|关联|方面)?(?:的)?(?:具体)?"
    r"(?:知识|信息|内容|资料|文档|规则|情况|详情|介绍|说明)"
)
ZH_DEFINITION_SUBJECT = re.compile(
    rf"^{ZH_FOCUS_SPAN}\s*(?:是什么|有哪些|都有哪些|是做什么的|怎么理解|什么意思|"
    r"如何定义|怎么样|有什么作用|如何使用)"
)
ZH_BARE_BLOCKERS = (
    "当前",
    "最新",
    "发生",
    "执行",
    "需要",
    "使用",
    "如果",
    "怎么办",
    "如何",
    "为什么",
    "什么",
    "多少",
    "哪个",
    "比较",
    "区别",
    "是否",
    "门槛",
    "接口",
    "审批",
    "超时",
)
ZH_DEFINITION_TAIL = re.compile(
    r"(?:的)?(?:当前|最新|正式)?(?:中文控制规则|控制规则|治理责任人|业务负责人|审批角色|"
    r"审批阈值|失败队列|接口路径|策略|规则|门槛|阈值|接口|状态|责任人|负责人|超时)$"
)
ZH_POLITE_PREFIXES = (
    re.compile(r"^(?:请问|请|麻烦(?:你)?|烦请|劳驾|能否|可否|你能(?:否)?|可以(?:请)?|是否可以)\s*"),
    re.compile(
        r"^(?:(?:你)?帮我|为我|给我|我想(?:要|知道|了解)?(?:一下|下)?|"
        r"想(?:要|知道|了解)?(?:一下|下)?)\s*"
    ),
    re.compile(r"^(?:从)?(?:这个)?知识库(?:里|中)?\s*"),
    re.compile(
        r"^(?:列出|整理|汇总|查找|查询|查|搜索|介绍|说明|总结|概括|了解|知道|告诉我|"
        r"讲讲|说说|看看)(?:一下|下)?\s*"
    ),
)


def normalize_query(value: str) -> str:
    """规范化用户输入，同时保留大小写敏感的实体信号。"""

    return " ".join(unicodedata.normalize("NFKC", value).split())


def _strip_polite_prefixes(query: str) -> str:
    """移除中文请求套话，但保留真正的业务短语。"""

    value = query.strip(" \t\r\n，,。.!！?？；;：:")
    changed = True
    while changed and value:
        changed = False
        for pattern in ZH_POLITE_PREFIXES:
            stripped = pattern.sub("", value, count=1).strip()
            if stripped != value:
                value = stripped
                changed = True
    return value


def _structured_subject(query: str) -> str | None:
    """按优先级提取带明确句式的中文业务主题。"""

    for pattern in (
        ZH_RELATED_SUBJECT,
        ZH_ABOUT_SUBJECT,
        ZH_TOPIC_FIRST,
        ZH_DEFINITION_SUBJECT,
        ZH_KNOWLEDGE_SUBJECT,
    ):
        match = pattern.search(query)
        if match is None:
            continue
        subject = match.group(1)
        if pattern is ZH_DEFINITION_SUBJECT:
            subject = ZH_DEFINITION_TAIL.sub("", subject).strip()
        return subject
    return None


def _bare_subject(query: str) -> str | None:
    """仅在短语足够短且不含疑问词时接受裸实体问法。"""

    bare = query.removesuffix("吗").removesuffix("呢").removesuffix("吧").strip()
    has_allowed_characters = re.fullmatch(r"[\u3400-\u9fffA-Za-z0-9·_.\-/ ]+", bare)
    if (
        2 <= len(bare) <= 20
        and not any(term in bare for term in ZH_BARE_BLOCKERS)
        and has_allowed_characters
    ):
        return bare
    return None


def _clean_subject(value: str) -> str | None:
    """清理主题边界字符，并排除 ID、缩写和过长文本。"""

    subject = value.strip(" \t\r\n，,。.!！?？；;：:\"'“”「」『』")
    if EXACT_IDENTIFIER.search(subject) or ACRONYM.search(subject):
        return None
    for suffix in ("业务", "系统", "项目", "模块"):
        if subject.endswith(suffix) and len(subject) > len(suffix):
            subject = subject[: -len(suffix)]
            break
    return subject if 2 <= len(subject) <= 40 else None


def _query_subject_signals(query: str) -> list[str]:
    """从常见中英文问法中提取高置信业务主题。"""

    normalized = normalize_query(query)
    cleaned = _strip_polite_prefixes(normalized)
    values = [match.group(1) for match in ZH_QUOTED_SUBJECT.finditer(normalized)]
    # Possessive questions must stop at the subject, before the requested facts.
    possessive = re.match(r"^([\u3400-\u9fffA-Za-z0-9_. -]{2,30}?)的", cleaned)
    subject = (
        possessive.group(1)
        if possessive
        else (_structured_subject(cleaned) or _bare_subject(cleaned))
    )
    if subject is not None:
        values.append(subject)
    else:
        values.extend(match.group(1) for match in ZH_QUERY_SUBJECT.finditer(cleaned))
    values.extend(match.group(1) for match in EN_ENTITY.finditer(normalized))
    values.extend(match.group(1) for match in EN_RELATED_SUBJECT.finditer(normalized))

    cleaned_values = [subject for value in values if (subject := _clean_subject(value))]
    return list(dict.fromkeys(cleaned_values))


def _lexical_signals(query: str) -> list[str]:
    """无需模型调用，确定性提取稳定标识和业务实体。"""

    normalized = normalize_query(query)
    values = [*EXACT_IDENTIFIER.findall(normalized), *ACRONYM.findall(normalized)]
    values.extend(match.group(1) for match in ZH_ENTITY.finditer(normalized))
    values.extend(_query_subject_signals(normalized))
    return list(dict.fromkeys(value.strip() for value in values if value.strip()))


def normalize_search_text(value: str) -> str:
    """生成适合索引比较的 NFKC、大小写不敏感文本。"""

    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def lexical_tokens(value: str, *, limit: int = 512) -> list[str]:
    """为 PostgreSQL 全文检索生成确定性的中英文混合 Token。

    PostgreSQL 内置解析器不会切分中文业务名，因此应用侧保存英文单词、重叠的
    中文二元组以及较短的完整中文短语，无需服务端分词扩展也能区分内部术语。
    """

    normalized = normalize_search_text(value)
    tokens: list[str] = []
    tokens.extend(ASCII_TOKEN.findall(normalized))
    for span in CJK_SPAN.findall(normalized):
        if 2 <= len(span) <= 24:
            tokens.append(span)
        if len(span) == 1:
            tokens.append(span)
        else:
            tokens.extend(span[index : index + 2] for index in range(len(span) - 1))
    return list(dict.fromkeys(token for token in tokens if token))[:limit]
