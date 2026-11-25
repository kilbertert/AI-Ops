"""开场白与预设问题的**版本化内容制品** + 运行时按语言派生（#585 / PRD #577）。

## 为什么这三类字段不能留在 Dify

Dify **没有逐语言的内容变体**：`opening_statement` / `suggested_questions` 各是
**一个字段一个值**，没有按语言分支的形状。而这三类都是**用户可见**的：

* 开场白 —— 用户进到会话页看到的第一句话；
* 预设问题 —— 用户点一下就能发起的那几个问题；
* 快捷动作标签 —— 按钮上写的那几个字（这一类的**权威本来就在我们侧**，见
  ``shortcut_lifecycle`` 的 seed；这里不重复实现，只把三者列为同一类）。

把它们的权威放在 Dify，等于让"新增一门语言"依赖外部产品的迭代，并且会把智能体绑死在
一门语言上。所以**其余语言**的权威落在我们的**版本化制品**里（本模块的 ``AGENT_STARTERS``）。

**2026-10-14 更新（PR-5 的 #585 反转）**：``opening_statement`` **现在被消费**，但只覆盖
``zh`` —— Dify 存中文母版、我们派生。所以现在这条边界的形状是：
- **开场白**：``zh`` 取运营在 Dify 里写的那一份（已发布快照），**非 zh 取本制品**；
  载荷带 ``opening_source``（``dify``/``artifact``）让"哪几门语言还停在制品上"可观测。
- **预设问题**（``suggested_questions``）：仍**只**取本制品，本制品是唯一权威。
- ``opening_questions`` / ``quick_commands`` 两个元组字段与本节无关（它们是清单/库中值，
  不是 Dify 文案），拉取路径从不写它们。

为什么只反转了一半：Dify 一个字段只有一个值，所以"运营改开场白"在结构上只可能改到中文
那一份；非中文必须是**写下来的文案**（运行时没有翻译机制）。这是「Dify 存中文母版」这个
决定的直接后果，不是遗漏。

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

from aiops_diagnostics.common.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES


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


OPERATOR_MAIN_AGENT_ID = "operator_main"
OPERATOR_STARTERS_VERSION = "2026.11.25"
OPERATOR_ALLOWED_EXTENSIONS: tuple[str, ...] = ("jpg", "jpeg", "png", "webp", "pdf", "doc", "docx")
OPERATOR_MAX_ATTACHMENTS = 5
OPERATOR_MAX_IMAGE_BYTES = 10 * 1024 * 1024
OPERATOR_MAX_DOCUMENT_BYTES = 20 * 1024 * 1024


class UnknownAssistantAgentError(KeyError):
    """请求传入了未注册或未启用的智能体标识。"""


_OPERATOR_AGENT_NAMES: dict[str, str] = {
    "zh": "充电运营智能助手",
    "zh-Hant": "充電營運智慧助手",
    "en": "Charging Ops Assistant",
    "de": "Ladebetriebs-Assistent",
    "fr": "Assistant d'exploitation de recharge",
    "es": "Asistente de operación de carga",
    "pt": "Assistente de operação de recarga",
    "vi": "Trợ lý vận hành sạc",
    "mn": "Цэнэглэлтийн ашиглалтын туслах",
    "th": "ผู้ช่วยปฏิบัติการชาร์จ",
    "km": "ជំនួយការប្រតិបត្តិការសាក",
}

_OPERATOR_AGENT_DESCRIPTIONS: dict[str, str] = {
    "zh": "面向充电桩后台的只读运营与故障排查主智能体，支持订单核验、设备离线分析及工单资料上下文。",
    "zh-Hant": "面向充電樁後台的唯讀營運與故障排查主智能體，支援訂單核驗、設備離線分析及工單資料上下文。",
    "en": (
        "Read-only charging operations assistant for order checks, device outage analysis, "
        "and troubleshooting attachments."
    ),
    "de": "Schreibgeschützter Betriebsassistent für Bestellprüfung, Geräteausfälle und Anhänge.",
    "fr": "Assistant en lecture seule pour la vérification des commandes, pannes et pièces jointes.",
    "es": "Asistente de solo lectura para pedidos, fallos de equipos y documentos adjuntos.",
    "pt": "Assistente somente leitura para pedidos, falhas de equipamentos e anexos.",
    "vi": "Trợ lý chỉ đọc hỗ trợ kiểm tra đơn hàng, thiết bị ngoại tuyến và tài liệu sự cố.",
    "mn": "Захиалга, төхөөрөмжийн саатал болон хавсралт шинжлэх зөвхөн унших туслах.",
    "th": "ผู้ช่วยแบบอ่านอย่างเดียวสำหรับตรวจสอบคำสั่งซื้อ อุปกรณ์ออฟไลน์ และเอกสารประกอบ",
    "km": "ជំនួយការអានតែប៉ុណ្ណោះសម្រាប់ពិនិត្យការបញ្ជាទិញ ឧបករណ៍អុហ្វឡាញ និងឯកសារភ្ជាប់។",
}


@dataclass(frozen=True, slots=True)
class OperatorStarters:
    """后台管理端（``operator``）AI 抽屉的欢迎语、预设问题与免责声明制品。"""

    version: str
    greeting: dict[str, str]
    supporting_text: dict[str, str]
    suggested: dict[str, tuple[tuple[str, str], ...]]
    disclaimer: dict[str, str]

    def greeting_for(self, language: str) -> str:
        return self.greeting.get(language) or self.greeting[DEFAULT_LANGUAGE]

    def supporting_text_for(self, language: str) -> str:
        return self.supporting_text.get(language) or self.supporting_text[DEFAULT_LANGUAGE]

    def suggested_for(self, language: str) -> tuple[tuple[str, str], ...]:
        return self.suggested.get(language) or self.suggested[DEFAULT_LANGUAGE]

    def disclaimer_for(self, language: str) -> str:
        return self.disclaimer.get(language) or self.disclaimer[DEFAULT_LANGUAGE]

    def public(self, language: str, *, agent_id: str = OPERATOR_MAIN_AGENT_ID) -> dict[str, Any]:
        greeting = self.greeting_for(language)
        return {
            "agent_id": agent_id,
            "language": language,
            "starters_version": self.version,
            "greeting": greeting,
            "supporting_text": self.supporting_text_for(language),
            "opening": greeting,
            "suggested_questions": [
                {"id": item_id, "text": text} for item_id, text in self.suggested_for(language)
            ],
            "disclaimer": self.disclaimer_for(language),
        }

    def missing_translations(self, language: str) -> tuple[str, ...]:
        gaps: list[str] = []
        if not self.greeting.get(language):
            gaps.append("greeting")
        if not self.supporting_text.get(language):
            gaps.append("supporting_text")
        if not self.suggested.get(language):
            gaps.append("suggested_questions")
        if not self.disclaimer.get(language):
            gaps.append("disclaimer")
        return tuple(gaps)


OPERATOR_STARTERS = OperatorStarters(
    version=OPERATOR_STARTERS_VERSION,
    greeting={
        "zh": "你好，有什么可以帮你？",
        "zh-Hant": "你好，有什麼可以幫你？",
        "en": "Hi, how can I help you today?",
        "de": "Hallo, wie kann ich Ihnen heute helfen?",
        "fr": "Bonjour, comment puis-je vous aider ?",
        "es": "Hola, ¿en qué puedo ayudarte hoy?",
        "pt": "Olá, como posso ajudar hoje?",
        "vi": "Xin chào, tôi có thể hỗ trợ gì cho bạn?",
        "mn": "Сайн байна уу, танд юугаар туслах вэ?",
        "th": "สวัสดี มีอะไรให้ช่วยตรวจสอบไหม?",
        "km": "សួស្តី តើខ្ញុំអាចជួយអ្វីបានខ្លះ?",
    },
    supporting_text={
        "zh": "你可以直接描述订单、设备或站点问题，上传照片/PDF/Word 排查资料，或点选下方预设问题开始。",
        "zh-Hant": "你可以直接描述訂單、設備或站點問題，上傳照片/PDF/Word 排查資料，或點選下方預設問題開始。",
        "en": (
            "Describe an order, charger, or station issue, upload photos/PDF/Word files, "
            "or choose a suggested question below."
        ),
        "de": (
            "Beschreiben Sie ein Bestell-, Geräte- oder Stationsproblem, laden Sie Dateien hoch "
            "oder wählen Sie eine Frage unten."
        ),
        "fr": (
            "Décrivez un problème de commande, borne ou station, importez des fichiers "
            "ou choisissez une question ci-dessous."
        ),
        "es": (
            "Describe un problema de pedido, cargador o estación, sube archivos "
            "o elige una pregunta sugerida abajo."
        ),
        "pt": (
            "Descreva um problema de pedido, carregador ou estação, envie arquivos "
            "ou escolha uma pergunta abaixo."
        ),
        "vi": (
            "Mô tả sự cố đơn hàng, trụ sạc hoặc trạm, tải lên ảnh/PDF/Word "
            "hoặc chọn một câu hỏi gợi ý bên dưới."
        ),
        "mn": (
            "Захиалга, цэнэглэгч эсвэл станцын асуудлыг бичиж, зураг/PDF/Word хавсаргах "
            "эсвэл доорх асуултаас сонгоно уу."
        ),
        "th": "อธิบายปัญหาคำสั่งซื้อ อุปกรณ์ หรือสถานี อัปโหลดรูป/PDF/Word หรือเลือกคำถามแนะนำด้านล่าง",
        "km": ("ពិពណ៌នាបញ្ហាការបញ្ជាទិញ ឧបករណ៍ ឬស្ថានីយ ផ្ទុកឡើងរូបភាព/PDF/Word ឬជ្រើសរើសសំណួរខាងក្រោម។"),
    },
    suggested={
        "zh": (
            ("op_q_01", "帮我排查近 7 天离线最频繁的充电桩"),
            ("op_q_02", "如何核验一笔充电订单是否存在费用异常？"),
            ("op_q_03", "列出我当前权限下可查看的充电站点"),
            ("op_q_04", "结合我上传的现场资料分析这笔异常订单的停止原因"),
        ),
        "zh-Hant": (
            ("op_q_01", "幫我排查近 7 天離線最頻繁的充電樁"),
            ("op_q_02", "如何核驗一筆充電訂單是否存在費用異常？"),
            ("op_q_03", "列出我目前權限下可查看的充電站點"),
            ("op_q_04", "結合我上傳的現場資料分析這筆異常訂單的停止原因"),
        ),
        "en": (
            ("op_q_01", "Find chargers with the most offline events in the past 7 days"),
            ("op_q_02", "How do I verify whether a charging order has a billing discrepancy?"),
            ("op_q_03", "List charging stations visible to my current account"),
            ("op_q_04", "Analyze why this abnormal order stopped using my uploaded files"),
        ),
        "de": (
            ("op_q_01", "Ladestationen mit den häufigsten Ausfällen in den letzten 7 Tagen prüfen"),
            ("op_q_02", "Wie prüfe ich eine Ladebestellung auf Abrechnungsabweichungen?"),
            ("op_q_03", "Alle für mein Konto sichtbaren Ladestationen auflisten"),
            ("op_q_04", "Stoppgrund dieser auffälligen Bestellung anhand hochgeladener Dateien analysieren"),
        ),
        "fr": (
            ("op_q_01", "Identifier les bornes les plus souvent hors ligne sur les 7 derniers jours"),
            ("op_q_02", "Comment vérifier une anomalie de facturation sur une commande ?"),
            ("op_q_03", "Lister les stations visibles pour mon compte"),
            ("op_q_04", "Analyser la cause d'arrêt de cette commande avec mes fichiers joints"),
        ),
        "es": (
            ("op_q_01", "Revisar los cargadores con más desconexiones en los últimos 7 días"),
            ("op_q_02", "¿Cómo verificar si un pedido de carga tiene anomalías de cobro?"),
            ("op_q_03", "Listar las estaciones de carga visibles para mi cuenta"),
            ("op_q_04", "Analizar la causa de detención del pedido con los archivos subidos"),
        ),
        "pt": (
            ("op_q_01", "Verificar carregadores com mais quedas nos últimos 7 dias"),
            ("op_q_02", "Como verificar se um pedido de recarga tem divergência de valor?"),
            ("op_q_03", "Listar estações de recarga visíveis para minha conta"),
            ("op_q_04", "Analisar o motivo da parada do pedido usando os arquivos enviados"),
        ),
        "vi": (
            ("op_q_01", "Kiểm tra các trụ sạc ngoại tuyến nhiều nhất trong 7 ngày qua"),
            ("op_q_02", "Làm sao đối chiếu bất thường chi phí của một đơn sạc?"),
            ("op_q_03", "Liệt kê các trạm sạc thuộc phạm vi tài khoản hiện tại"),
            ("op_q_04", "Phân tích nguyên nhân dừng đơn sạc bất thường dựa trên tài liệu đã tải lên"),
        ),
        "mn": (
            ("op_q_01", "Сүүлийн 7 хоногт хамгийн их салсан цэнэглэгчийг шалгах"),
            ("op_q_02", "Цэнэглэлтийн захиалгын төлбөрийн зөрүүг хэрхэн шалгах вэ?"),
            ("op_q_03", "Миний эрхэд харагдах цэнэглэх станцуудыг жагсаах"),
            ("op_q_04", "Хавсаргасан баримтад тулгуурлан захиалга зогссон шалтгааныг шинжлэх"),
        ),
        "th": (
            ("op_q_01", "ตรวจสอบแท่นชาร์จที่ออฟไลน์บ่อยที่สุดใน 7 วันที่ผ่านมา"),
            ("op_q_02", "จะตรวจสอบความผิดปกติของค่าบริการคำสั่งซื้อชาร์จได้อย่างไร?"),
            ("op_q_03", "แสดงรายการสถานีชาร์จที่บัญชีของฉันมองเห็น"),
            ("op_q_04", "วิเคราะห์สาเหตุการหยุดชาร์จผิดปกติจากเอกสารที่อัปโหลด"),
        ),
        "km": (
            ("op_q_01", "ពិនិត្យឧបករណ៍សាកដែលអុហ្វឡាញញឹកញាប់បំផុតក្នុង 7 ថ្ងៃចុងក្រោយ"),
            ("op_q_02", "តើផ្ទៀងផ្ទាត់ភាពខុសប្រក្រតីនៃថ្លៃការបញ្ជាទិញសាកដោយរបៀបណា?"),
            ("op_q_03", "បង្ហាញបញ្ជីស្ថានីយសាកដែលគណនីខ្ញុំអាចមើលឃើញ"),
            ("op_q_04", "វិភាគមូលហេតុឈប់ខុសប្រក្រតីដោយផ្អែកលើឯកសារដែលបានផ្ទុកឡើង"),
        ),
    },
    disclaimer={
        "zh": "AI 生成内容仅供参考，关键业务与财务结论请以原始记录为准。",
        "zh-Hant": "AI 生成內容僅供參考，關鍵業務與財務結論請以原始記錄為準。",
        "en": (
            "AI-generated responses are for reference only. "
            "Verify critical operational and billing records directly."
        ),
        "de": (
            "KI-generierte Antworten dienen nur zur Orientierung. "
            "Kritische Daten bitte anhand der Originaldaten prüfen."
        ),
        "fr": (
            "Les réponses générées par l'IA sont fournies à titre indicatif. "
            "Vérifiez les données critiques à la source."
        ),
        "es": (
            "Las respuestas de IA son solo de referencia. "
            "Verifica los registros críticos en la fuente original."
        ),
        "pt": "As respostas de IA são apenas referência. Verifique registros críticos na fonte original.",
        "vi": (
            "Nội dung do AI tạo chỉ mang tính tham khảo. "
            "Vui lòng đối chiếu dữ liệu gốc cho kết luận quan trọng."
        ),
        "mn": (
            "AI-ийн хариулт нь зөвхөн лавлагааны зориулалттай. "
            "Чухал дүгнэлтийг эх бүртгэлээр баталгаажуулна уу."
        ),
        "th": "คำตอบจาก AI ใช้เพื่ออ้างอิงเท่านั้น โปรดตรวจสอบบันทึกต้นฉบับสำหรับข้อสรุปสำคัญ",
        "km": "ចម្លើយដែលបង្កើតដោយ AI គ្រាន់តែជាឯកសារយោងប៉ុណ្ណោះ សូមផ្ទៀងផ្ទាត់ជាមួយកំណត់ត្រាដើម។",
    },
)


def resolve_operator_agent_id(agent_id: str | None) -> str:
    """校验后台主智能体标识；省略返回默认 ``operator_main``，未知值抛错。"""
    candidate = (agent_id or "").strip()
    if not candidate:
        return OPERATOR_MAIN_AGENT_ID
    if candidate != OPERATOR_MAIN_AGENT_ID:
        raise UnknownAssistantAgentError(candidate)
    return candidate


def operator_agent_brief(language: str, agent_id: str = OPERATOR_MAIN_AGENT_ID) -> dict[str, str]:
    resolved = agent_id if agent_id == OPERATOR_MAIN_AGENT_ID else OPERATOR_MAIN_AGENT_ID
    name = _OPERATOR_AGENT_NAMES.get(language) or _OPERATOR_AGENT_NAMES[DEFAULT_LANGUAGE]
    return {
        "agent_id": resolved,
        "name": name,
        "subtitle": "Main Agent",
    }


def operator_agent_summary(language: str, agent_id: str = OPERATOR_MAIN_AGENT_ID) -> dict[str, Any]:
    resolved = resolve_operator_agent_id(agent_id)
    brief = operator_agent_brief(language, resolved)
    description = _OPERATOR_AGENT_DESCRIPTIONS.get(language) or _OPERATOR_AGENT_DESCRIPTIONS[DEFAULT_LANGUAGE]
    return {
        **brief,
        "description": description,
        "avatar_url": None,
        "is_default": True,
        "capabilities": {
            "streaming": True,
            "node_path": True,
            "max_attachments": OPERATOR_MAX_ATTACHMENTS,
            "max_image_bytes": OPERATOR_MAX_IMAGE_BYTES,
            "max_document_bytes": OPERATOR_MAX_DOCUMENT_BYTES,
            "allowed_extensions": list(OPERATOR_ALLOWED_EXTENSIONS),
        },
    }


def list_operator_agents(language: str) -> dict[str, Any]:
    return {
        "type": "assistant_agent_list",
        "default_agent_id": OPERATOR_MAIN_AGENT_ID,
        "agents": [operator_agent_summary(language, OPERATOR_MAIN_AGENT_ID)],
    }


def operator_starter_gaps(starters: OperatorStarters = OPERATOR_STARTERS) -> tuple[str, ...]:
    return tuple(language for language in SUPPORTED_LANGUAGES if starters.missing_translations(language))
