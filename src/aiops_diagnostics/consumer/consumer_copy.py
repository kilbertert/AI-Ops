"""C 端答复的租户文案包：措辞可发布，数字与占位符不可改。

为什么需要这一层：C 端订单状态/账单的正文**不是模型写的**
（`consumer_knowledge.final_answer` 在存在 live 快照时直接用快照渲染），
所以改 Dify chatflow 的提示词改不到它。产品想要"用配置调整说话方式"，
于是把**措辞**从代码里搬出来，做成按租户发布、按版本钉住的内容。

三条边界（ADR-0183）：

* **只能改字，不能改结构**：占位符集合必须与内置文案逐字相同 —— 少了 `{order_no}`
  就等于把订单号从答复里删掉，那种"改措辞"是破坏可核对性。
* **数字仍由代码给**：文案里的金额来自本次取证到的快照，不是文案作者写的。
* **按版本复现**：答复在生成时钉住所用文案的版本（写进 run context），
  回读用同一版渲染。否则发布一次文案就会让在途答复对不上，回读变成 503。
"""

from __future__ import annotations

from string import Formatter
from typing import Any

from aiops_diagnostics.consumer.consumer_plain import WORDS

#: 一条文案的上限。文案是短句与列表项，不是段落；放宽到几千字会让一条错误配置
#: 直接把答复撑成没人读的长文，而发布校验是唯一能拦住它的地方。
MAX_COPY_CHARS = 400

#: 可以被文案包覆盖的语种。
#:
#: 只有 `zh`／`en` 的正文是**逐句组合**出来的（`consumer_plain` 的小键词表），
#: 所以"改一句"能精确对应到一个键。其余九种（含 `zh-Hant`）的正文仍是
#: `consumer_language.json` 里的**整段模板**：改一句就得重写整段，
#: 而整段还带着占位符与免责句，用 400 字的短文案上限去管它只会逼人写错。
#: 把这九种迁成组合式是独立的一片活（ADR-0183「代价」），在那之前**明确拒绝**，
#: 不做"发布了但悄悄不生效"这种更糟的事。
COMPOSED_LANGUAGES = ("zh", "en")


class CopyBundleInvalid(ValueError):
    """文案包不合法。错误文本要能直接告诉发布者改哪一条。"""


def _fields(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name is not None}


def bundled(language: str) -> dict[str, str]:
    """内置文案（代码里的默认）。未知语种按 `consumer_plain` 的既有降级取英文。"""
    if language in WORDS:
        return WORDS[language]
    return WORDS["zh-Hant"] if language.startswith("zh") else WORDS["en"]


def validate(bundle: Any) -> dict[str, dict[str, str]]:
    """校验并归一化一份文案包。

    接受形状：``{"languages": {lang: {key: text}}}``。只允许**覆盖内置键**，
    不允许新增键 —— 新增键说明发布者以为自己在改一个不存在的地方，静默忽略它
    会让人以为改动生效了。
    """
    if not isinstance(bundle, dict) or set(bundle) != {"languages"}:
        raise CopyBundleInvalid("文案包必须且只能有 languages 一个顶层字段")
    languages = bundle["languages"]
    if not isinstance(languages, dict) or not languages:
        raise CopyBundleInvalid("languages 必须是非空对象")
    parsed: dict[str, dict[str, str]] = {}
    for language, entries in languages.items():
        if language not in COMPOSED_LANGUAGES:
            raise CopyBundleInvalid(
                f"{language} 的正文还是整段模板，暂时不能改文案；"
                f"当前可改的语种：{', '.join(COMPOSED_LANGUAGES)}"
            )
        if not isinstance(entries, dict) or not entries:
            raise CopyBundleInvalid(f"{language} 必须是非空对象")
        default = bundled(language)
        clean: dict[str, str] = {}
        for key, text in entries.items():
            if key not in default:
                raise CopyBundleInvalid(f"{language} 没有这个文案键：{key}")
            if not isinstance(text, str) or not text.strip():
                raise CopyBundleInvalid(f"{language}.{key} 不能为空")
            if len(text) > MAX_COPY_CHARS:
                raise CopyBundleInvalid(f"{language}.{key} 超过 {MAX_COPY_CHARS} 字（实际 {len(text)}）")
            missing = _fields(default[key]) - _fields(text)
            extra = _fields(text) - _fields(default[key])
            if missing or extra:
                raise CopyBundleInvalid(
                    f"{language}.{key} 的占位符必须与内置文案一致："
                    f"缺 {sorted(missing) or '无'}，多 {sorted(extra) or '无'}"
                )
            clean[key] = text
        parsed[language] = clean
    return parsed


def pack(language: str, overrides: dict[str, dict[str, str]] | None) -> dict[str, str]:
    """内置文案 + 该语种的覆盖项。没覆盖的语种拿到的就是内置文案本身。"""
    base = dict(bundled(language))
    if overrides:
        base.update(overrides.get(language, {}))
    return base


def resolve(
    store: Any,
    tenant_id: str,
    *,
    version: int | None,
    language: str,
) -> dict[str, str]:
    """按**钉住的版本**取词表。`version` 为 0/None 表示"没发布过/历史答复"→ 内置文案。

    为什么按版本而不是按"当前最新"：答复在生成时把版本写进了 run context，
    回读要用同一版复现。若按最新版渲染，运营发布一次文案就会让在途答复对不上，
    回读变成 503 —— 那是把"改措辞"变成"掉线"。

    版本行查不到时退回内置文案而不是抛错：那一行是只增不改的台账，理论上不会缺；
    真缺了，给用户一份能读的内置文案比让这条答复失败更有用。
    """
    if not version:
        return bundled(language)
    _, bundle = store.consumer_copy(tenant_id, int(version))
    return pack(language, (bundle or {}).get("languages"))
