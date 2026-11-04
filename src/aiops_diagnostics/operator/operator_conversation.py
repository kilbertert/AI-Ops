"""CPMS对象工具共享的拒绝类型和原话参数定位；不承担意图分类。"""


class OperatorRefused(RuntimeError):
    pass


SCENARIO = "operator_conversation"  # 已冻结历史运行的类型。
SITE_MAX_SHOWN = 10


def question_span(value: str, question: str) -> str:
    """只忽略空白和大小写匹配，返回用户原文；保留地名内部空格。"""
    needle = "".join(value.split()).casefold()
    if not needle:
        return ""
    positions = [i for i, char in enumerate(question) if not char.isspace() for _ in char.casefold()]
    folded = "".join(char for char in question if not char.isspace()).casefold()
    start = folded.find(needle)
    if start < 0:
        return ""
    return question[positions[start] : positions[start + len(needle) - 1] + 1]
