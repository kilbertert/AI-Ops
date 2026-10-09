"""开场白与预设问题的**版本化内容制品** + 运行时按语言派生（#585 / PRD #577）。

## 为什么这三类字段不能留在 Dify

Dify **没有逐语言的内容变体**：`opening_statement` / `suggested_questions` 各是
**一个字段一个值**，没有按语言分支的形状。而这三类都是**用户可见**的：

* 开场白 —— 用户进到会话页看到的第一句话；
* 预设问题 —— 用户点一下就能发起的那几个问题；
* 快捷动作标签 —— 按钮上写的那几个字（这一类的**权威本来就在我们侧**，见
  ``shortcut_lifecycle`` 的 seed；这里不重复实现，只把三者列为同一类）。

把它们的权威放在 Dify，等于让"新增一门语言"依赖外部产品的迭代，并且会把智能体绑死在
一门语言上。所以权威落在我们的**版本化制品**里（本模块的 ``AGENT_STARTERS``），
Dify 侧对应字段**不被消费** —— 这一点与 #580 的"不从 DSL 取"是同一条边界的两个方向。

## 派生：一份中文权威母版 → 每门语言

这里的每一门语言都是**写下来的文案**，不是机器翻译的产物：短句（按钮、引导语）机器
翻译的质量不足以直接给用户看，而这一版的量很小。因此"派生"的含义是**按语言取用**，
而不是运行时翻译。等到某门语言真的要加进来时，加的是这一张表里的一行 ——
**不改 Dify 里的任何配置**（这正是 #585 的判据之一）。

## 语言清单来自 `i18n.LANGUAGES`，且**覆盖是判据**

`SUPPORTED_LANGUAGES` 是唯一清单；某个语言缺条目会被 :func:`starter_gaps` 报出来 ——
一份只有两门语言的表不是"还没补完"，而是**用户会看到中文**（``resolve_language`` 接受
所有已声明语言，而取不到就回退 zh）。这与 `shortcut_lifecycle` 的语言覆盖门是同一条。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES


@dataclass(frozen=True, slots=True)
class AgentStarters:
    """一个智能体面向用户的三类文案，按语言索引（zh 为权威母版）。"""

    #: 版本号进对外载荷，让"用户看到的是哪一版文案"可追溯。
    version: str
    #: 开场白：``{language: text}``
    opening: dict[str, str]
    #: 预设问题：``{language: [question, ...]}``
    suggested: dict[str, tuple[str, ...]]

    def opening_for(self, language: str) -> str:
        """这门语言的开场白；缺该语言时回退 zh（``DEFAULT_LANGUAGE`` 是权威）。"""
        return self.opening.get(language) or self.opening[DEFAULT_LANGUAGE]

    def suggested_for(self, language: str) -> tuple[str, ...]:
        return self.suggested.get(language) or self.suggested[DEFAULT_LANGUAGE]

    def public(self, language: str) -> dict[str, Any]:
        """面向客户端的载荷。语言是**入参**，不是请求里读出来的 —— 调用方负责解析它。"""
        return {
            "language": language,
            "starters_version": self.version,
            "opening": self.opening_for(language),
            "suggested_questions": list(self.suggested_for(language)),
        }

    def missing_translations(self, language: str) -> tuple[str, ...]:
        """这门语言缺哪些字段。空元组 = 完整。"""
        gaps: list[str] = []
        if not self.opening.get(language):
            gaps.append("opening")
        if not self.suggested.get(language):
            gaps.append("suggested_questions")
        return tuple(gaps)


#: 制品的唯一权威。改文案 = 改这里 + 抬 ``version``；**不改 Dify**。
STARTERS_VERSION = "2026.10.09"

AGENT_STARTERS = AgentStarters(
    version=STARTERS_VERSION,
    opening={
        "zh": "您好，我是智能客服小趋。充电、订单或车辆的问题都可以问我。",
        "zh-Hant": "您好，我是智慧客服小趨。充電、訂單或車輛的問題都可以問我。",
        "en": "Hi, I'm Xiaoqu, the assistant. Ask me about charging, orders, or your vehicle.",
        "de": "Hallo, ich bin Xiaoqu, Ihr Assistent. Fragen Sie mich zu Laden, Bestellungen oder Fahrzeug.",
        "fr": (
            "Bonjour, je suis Xiaoqu, votre assistant. Posez-moi vos questions "
            "sur la recharge, les commandes ou votre véhicule."
        ),
        "es": "Hola, soy Xiaoqu, el asistente. Pregúntame sobre carga, pedidos o tu vehículo.",
        "pt": "Olá, sou o Xiaoqu, o assistente. Pergunte sobre carregamento, pedidos ou seu veículo.",
        "vi": "Xin chào, tôi là Xiaoqu, trợ lý của bạn. Hãy hỏi tôi về sạc, đơn hàng hoặc xe của bạn.",
        "mn": (
            "Сайн байна уу, би Xiaoqu туслах байна. "
            "Цэнэглэлт, захиалга, тээврийн хэрэгслийн талаар асуугаарай."
        ),
        "th": "สวัสดี ฉันคือเสี่ยวชวี ผู้ช่วยของคุณ สอบถามเรื่องการชาร์จ คำสั่งซื้อ หรือรถของคุณได้เลย",
        "km": ("សូមស្វាគមន៍ ខ្ញុំគឺ Xiaoqu ជាជំនួយការរបស់អ្នក។ សួរខ្ញុំអំពីការសាក ការបញ្ជាទិញ ឬរថយន្តរបស់អ្នក។"),
    },
    suggested={
        "zh": ("充电桩无法启动怎么办？", "订单扣费为什么和预期不同？", "怎么查充电记录？"),
        "zh-Hant": ("充電樁無法啟動怎麼辦？", "訂單扣費為什麼和預期不同？", "怎麼查充電記錄？"),
        "en": (
            "What if the charger won't start?",
            "Why does the order cost differ from what I expected?",
            "How do I check my charging history?",
        ),
        "de": (
            "Was tun, wenn die Ladestation nicht startet?",
            "Warum weicht der Bestellbetrag von meiner Erwartung ab?",
            "Wie sehe ich meinen Ladeverlauf?",
        ),
        "fr": (
            "Que faire si la borne ne démarre pas ?",
            "Pourquoi le montant de la commande diffère-t-il de mes attentes ?",
            "Comment consulter mon historique de recharge ?",
        ),
        "es": (
            "¿Qué hago si el cargador no arranca?",
            "¿Por qué el importe del pedido difiere de lo que esperaba?",
            "¿Cómo consulto mi historial de carga?",
        ),
        "pt": (
            "O que fazer se o carregador não iniciar?",
            "Por que o valor do pedido difere do que eu esperava?",
            "Como consulto meu histórico de carregamento?",
        ),
        "vi": (
            "Làm gì khi trụ sạc không khởi động?",
            "Vì sao phí đơn hàng khác với dự kiến?",
            "Làm sao xem lịch sử sạc?",
        ),
        "mn": (
            "Цэнэглэгч ажиллахгүй бол яах вэ?",
            "Захиалгын төлбөр яагаад хүлээснээс өөр байна вэ?",
            "Цэнэглэлтийн түүхээ хэрхэн харах вэ?",
        ),
        "th": (
            "ถ้าแท่นชาร์จไม่เริ่มทำงานทำอย่างไร?",
            "ทำไมค่าสั่งซื้อจึงต่างจากที่คาดไว้?",
            "ดูประวัติการชาร์จได้อย่างไร?",
        ),
        "km": (
            "បើស្ថានីយសាកមិនចាប់ផ្តើម ធ្វើដូចម្តេច?",
            "ហេតុអ្វីបានជាថ្លៃបញ្ជាទិញខុសពីការរំពឹង?",
            "មើលប្រវត្តិការសាកដូចម្តេច?",
        ),
    },
)


def starter_gaps(starters: AgentStarters = AGENT_STARTERS) -> tuple[str, ...]:
    """制品对**支持语言清单**的覆盖缺口，形如 ``"vi"`` / ``"th"``。

    判据的用法是"空元组才算完整"：一条非空结果意味着某门语言会**拿到中文**
    （``resolve_language`` 接受所有已声明语言，取不到就回退）。这是缺陷不是进度。
    """
    return tuple(language for language in SUPPORTED_LANGUAGES if starters.missing_translations(language))
