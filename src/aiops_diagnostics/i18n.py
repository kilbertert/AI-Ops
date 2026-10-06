"""Accept-Language resolution for the assistant/FAQ surface (L1/#201).

Pure header parsing: no framework, storage, or auth dependency. The resolved
language is presentation metadata only — it must never widen permissions,
change routing semantics, or reach the incident manifest (#200 decision).
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LanguageSpec:
    """One language, with the facts a consumer actually reads.

    Only the facts something READS live here. An earlier draft of this carried
    four capability booleans (rendering / prompting / routing / guarding); three
    of them were read by nobody, so they were declarations that could be — and
    were — wrong without anything failing. A flag nothing consumes is worse than
    no flag: it reads as a settled decision while encoding nothing. The
    capabilities come back one at a time, together with the consumer that reads
    them.

    What remains is what is genuinely needed today:

    - the tag and its prompt-usable name (the old ``LANGUAGE_NAMES`` table);
    - ``is_chinese``, which the leak guard's language set is derived from.

    ``is_chinese`` is a property of the LANGUAGE, never derived from "is it the
    default". Those coincided while the default was the only Chinese language; a
    second Chinese script (``zh-Hant``) breaks the identity, and the guard would
    then judge every legitimate Traditional answer a Chinese leak.
    """

    tag: str
    name: str  # English display name, used in model prompts
    is_chinese: bool


# THE inventory. Adding a language is one entry here plus whatever its data
# needs — not a sweep for hand-copied tuples. Every derived constant below is
# computed from this list, and a consumer that enumerates languages itself is a
# defect this list exists to prevent.
LANGUAGES: tuple[LanguageSpec, ...] = (
    LanguageSpec("zh", "Simplified Chinese", is_chinese=True),
    LanguageSpec("en", "English", is_chinese=False),
    LanguageSpec("de", "German", is_chinese=False),
    LanguageSpec("fr", "French", is_chinese=False),
    LanguageSpec("es", "Spanish", is_chinese=False),
    LanguageSpec("pt", "Portuguese", is_chinese=False),
    LanguageSpec("vi", "Vietnamese", is_chinese=False),
    LanguageSpec("mn", "Mongolian", is_chinese=False),
    LanguageSpec("th", "Thai", is_chinese=False),
    LanguageSpec("km", "Khmer", is_chinese=False),
)

# Must stay aligned with the i18n catalog shipped in faq_catalog.json (#200).
SUPPORTED_LANGUAGES: tuple[str, ...] = tuple(spec.tag for spec in LANGUAGES)

# The default is the language a request falls back to when nothing is
# acceptable, and the authority whose text the catalogs are written in. It is
# also Chinese — but that is a consequence, not the definition: see
# ``is_chinese`` above for why the two must not be conflated.
DEFAULT_LANGUAGE = "zh"

_BY_TAG = {spec.tag: spec for spec in LANGUAGES}


def language_spec(language: str) -> LanguageSpec | None:
    """The declared spec for ``language``, or ``None`` when it is unsupported."""
    return _BY_TAG.get(language)


def non_chinese_languages(specs: tuple[LanguageSpec, ...]) -> frozenset[str]:
    """The languages whose answers the Chinese-leak guard judges.

    Derived from the DECLARED ``is_chinese`` property, never from "is it the
    default" (#527, ADR-0007's D-6).

    A function rather than an inline comprehension so the RULE can be tested on
    an inventory that distinguishes the two readings. With today's languages it
    cannot be: `zh` is both the only Chinese language and the default, so
    "declared Chinese" and "supported minus default" produce the same set and a
    test over the real list would pass either way. The distinguishing case is a
    second Chinese script — `zh-Hant` is not the default, so the old expression
    would call it non-Chinese and the guard would replace every legitimate
    Traditional answer with the fallback copy.
    """
    return frozenset(spec.tag for spec in specs if not spec.is_chinese)


# Languages whose answers must not contain Chinese. Derived from the declared
# property, never from "is it the default" — see `non_chinese_languages`.
NON_CHINESE_LANGUAGES = non_chinese_languages(LANGUAGES)

# CJK ideographs and CJK punctuation — what a stored Chinese value looks like
# when it is copied into an answer meant for a reader of another language.
#
# Covers the CJK punctuation blocks (U+3000-303F) and the fullwidth forms
# (U+FF00-FFEF). The fullwidth block also holds fullwidth LATIN letters, so a
# non-Chinese answer using them as a typographic choice would be flagged — an
# accepted, deliberate edge: fullwidth punctuation in an English answer means
# the model is writing through a Chinese input method, and Chinese is what
# follows. The cost of the false positive is one retry; the cost of the false
# negative was a customer reading a language they could not.
CJK_TEXT = re.compile(r"[㐀-䶿一-鿿　-〿＀-￯]")


# A Chinese name glossed beside its Latin form — `TrendPower (趋势智能)`. The
# Chinese IS the proper noun, so it is an identifier, and dropping it would make
# the answer unciteable to a reader who knows the company by that name.
#
# Deliberately narrow. It matches ONLY a parenthesised Chinese run that directly
# follows Latin text, because that is the shape a gloss has. A bare Chinese name
# (`特来电`) is NOT exempt: telling a cited proper noun apart from a sentence
# needs semantics a pattern does not have, and guessing would reopen the hole
# this guard exists to close. The cost of the narrow rule is a rare withhold
# that an operator sees in the log; the cost of a broad one is Chinese read by a
# customer.
_NAME_GLOSS = re.compile(r"(?<=[A-Za-z0-9])\s*[\(（]\s*[㐀-䶿一-鿿]{1,12}\s*[\)）]")


def _without_name_glosses(text: str) -> str:
    """Remove `(中文名)` glosses that follow a Latin token.

    A gloss is an identifier written twice, not prose: `TrendPower (趋势智能)`
    names the company in both scripts the reader might know it by. Removing it
    before the CJK scan is what keeps the guard from treating a proper noun as
    a leak. See ``_NAME_GLOSS`` for why the rule stays narrow.
    """
    return _NAME_GLOSS.sub("", text)


def chinese_leak(text: str) -> str:
    """Return the distinct Chinese characters in ``text`` (empty when clean).

    Answers for a non-Chinese language may not contain Chinese at all — source
    data is Chinese, so a copied-out value reads as garbage to the reader. This
    is the single shared judgement behind every answer surface's guard; it lives
    here, beside the language tables, so a surface cannot quietly reimplement it.

    Proper nouns glossed in parentheses are removed before judgement; everything
    else is judged as-is.

    Scope: this proves the answer did not LEAK Chinese. It says nothing about
    whether the translation is correct — that is not decidable by pattern.
    """
    return "".join(sorted(set(CJK_TEXT.findall(_without_name_glosses(text)))))


# Human-readable names used inside model prompts (qa/diagnosis, #204).
LANGUAGE_NAMES: dict[str, str] = {spec.tag: spec.name for spec in LANGUAGES}

# Presentation copy for the qa retrieval fallback per language (#204).
# zh is authoritative: a missing language falls back to the zh strings.
QA_FALLBACK_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "not_found": "知识库中没有找到与当前问题直接相关的资料，暂时无法提供有依据的回答。",
        "unavailable": "当前知识库暂时不可用，本次回答无法基于知识库确认，请稍后重试。",
        "generation_failed": "本次回答没能完成生成，请稍后重试。",
        "limited": "本次检索未能完成，请稍后重试或换个问法。",
    },
    "en": {
        "not_found": (
            "No directly relevant material was found in the knowledge base; "
            "no evidence-backed answer is available."
        ),
        "unavailable": (
            "The knowledge base is temporarily unavailable and the answer "
            "could not be verified. Please retry later."
        ),
        "generation_failed": ("This answer could not be generated. Please try again later."),
        "limited": "This lookup could not be completed. Please try again or rephrase your question.",
    },
    "de": {
        "not_found": (
            "In der Wissensbasis fand sich kein passendes Material; "
            "eine belegte Antwort ist derzeit nicht möglich."
        ),
        "unavailable": (
            "Die Wissensbasis ist vorübergehend nicht erreichbar; "
            "die Antwort blieb unverifiziert. Bitte später erneut versuchen."
        ),
        "generation_failed": ("Diese Antwort konnte nicht erzeugt werden. Bitte später erneut versuchen."),
        "limited": "Diese Suche konnte nicht abgeschlossen werden. Bitte später erneut versuchen.",
    },
    "fr": {
        "not_found": (
            "Aucun document pertinent n'a été trouvé dans la base de connaissances ; "
            "pas de réponse étayée pour l'instant."
        ),
        "unavailable": (
            "La base de connaissances est momentanément indisponible ; "
            "la réponse n'a pu y être vérifiée. Réessayez plus tard."
        ),
        "generation_failed": ("Cette réponse n'a pas pu être générée. Veuillez réessayer plus tard."),
        "limited": "Cette recherche n'a pas abouti. Réessayez plus tard ou reformulez la question.",
    },
    "es": {
        "not_found": (
            "No se halló material pertinente en la base de conocimiento; "
            "no hay respuesta con respaldo por ahora."
        ),
        "unavailable": (
            "La base de conocimiento no está disponible y la respuesta "
            "no pudo verificarse. Inténtelo más tarde."
        ),
        "generation_failed": ("No se pudo generar esta respuesta. Inténtalo de nuevo más tarde."),
        "limited": "No se pudo completar la búsqueda. Inténtelo de nuevo o formule la pregunta otra vez.",
    },
    "pt": {
        "not_found": (
            "Nada de pertinente foi encontrado na base de conhecimento; "
            "não há resposta fundamentada neste momento."
        ),
        "unavailable": (
            "A base de conhecimento está indisponível e a resposta "
            "não pôde ser verificada. Tente novamente mais tarde."
        ),
        "generation_failed": ("Não foi possível gerar esta resposta. Tente novamente mais tarde."),
        "limited": "Não foi possível concluir a busca. Tente novamente ou reformule a sua pergunta.",
    },
    "vi": {
        "not_found": (
            "Không tìm thấy tài liệu nào thực sự liên quan trong kho kiến thức; "
            "hiện chưa có câu trả lời dựa trên bằng chứng."
        ),
        "unavailable": (
            "Kho kiến thức tạm thời không khả dụng và câu trả lời không thể được xác minh. "
            "Vui lòng thử lại sau."
        ),
        "generation_failed": "Không thể tạo câu trả lời này. Vui lòng thử lại sau.",
        "limited": "Không thể hoàn tất tra cứu này. Vui lòng thử lại hoặc diễn đạt lại câu hỏi.",
    },
    "mn": {
        "not_found": (
            "Мэдлэгийн санд энэ асуултад шууд хамаатай материал олдсонгүй; "
            "нотолгоонд суурилсан хариулт өгөх боломжгүй байна."
        ),
        "unavailable": (
            "Мэдлэгийн сан түр хугацаанд боломжгүй байгаа тул хариултыг баталгаажуулж чадсангүй. "
            "Дараа дахин оролдоно уу."
        ),
        "generation_failed": "Энэ хариултыг үүсгэж чадсангүй. Дараа дахин оролдоно уу.",
        "limited": "Энэ хайлтыг дуусгаж чадсангүй. Дараа дахин оролдох эсвэл асуултаа өөрөөр тавина уу.",
    },
    "th": {
        "not_found": "ไม่พบเอกสารที่เกี่ยวข้องโดยตรงในฐานความรู้ จึงยังไม่มีคำตอบที่อ้างอิงหลักฐานได้",
        "unavailable": "ฐานความรู้ไม่พร้อมใช้งานชั่วคราว และคำตอบนี้ไม่สามารถตรวจสอบได้ กรุณาลองใหม่ภายหลัง",
        "generation_failed": "ไม่สามารถสร้างคำตอบนี้ได้ กรุณาลองใหม่ภายหลัง",
        "limited": "การค้นหานี้ไม่สำเร็จ กรุณาลองใหม่หรือเปลี่ยนวิธีถาม",
    },
    "km": {
        "not_found": ("រកមិនឃើញឯកសារពាក់ព័ន្ធដោយផ្ទាល់នៅក្នុងមូលដ្ឋានចំណេះដឹងទេ ដូច្នេះមិនអាចផ្តល់ចម្លើយដែលមានភស្តុតាងបានឡើយ"),
        "unavailable": (
            "មូលដ្ឋានចំណេះដឹងមិនអាចប្រើប្រាស់បានបណ្តោះអាសន្ន ហើយចម្លើយនេះមិនអាចផ្ទៀងផ្ទាត់បានទេ។ សូមព្យាយាមម្តងទៀតនៅពេលក្រោយ"
        ),
        "generation_failed": "មិនអាចបង្កើតចម្លើយនេះបានទេ។ សូមព្យាយាមម្តងទៀតនៅពេលក្រោយ",
        "limited": "ការស្វែងរកនេះមិនបានសម្រេចទេ។ សូមព្យាយាមម្តងទៀត ឬសួរបែបផ្សេង",
    },
}

# Honest empty promotional card when the pinned agent/KB has no match (#231).
#
# Every table below covers ALL of SUPPORTED_LANGUAGES. A missing language is not
# harmless: resolve_language accepts de/fr/es/pt, so a two-language table makes
# the service claim a language it then answers in Chinese — the exact mismatch
# this module exists to prevent (41 live, 2026-09-18).
PROMO_EMPTY_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "case_exploration": "当前没有可用的客户案例，未检索到匹配的宣传资料。",
        "solution_discovery": "当前没有可用的行业方案，未检索到匹配的宣传资料。",
    },
    "en": {
        "case_exploration": "No matching customer case is available in the promotional library.",
        "solution_discovery": "No matching industry solution is available in the promotional library.",
    },
    "de": {
        "case_exploration": "In der Werbematerial-Bibliothek ist kein passender Kundenfall verfügbar.",
        "solution_discovery": "In der Werbematerial-Bibliothek ist keine passende Branchenlösung verfügbar.",
    },
    "fr": {
        "case_exploration": "Aucun cas client correspondant n'est disponible dans la bibliothèque.",
        "solution_discovery": (
            "Aucune solution sectorielle correspondante n'est disponible dans la bibliothèque."
        ),
    },
    "es": {
        "case_exploration": "No hay ningún caso de cliente coincidente en la biblioteca promocional.",
        "solution_discovery": "No hay ninguna solución sectorial coincidente en la biblioteca promocional.",
    },
    "pt": {
        "case_exploration": "Não há nenhum caso de cliente correspondente na biblioteca promocional.",
        "solution_discovery": "Não há nenhuma solução setorial correspondente na biblioteca promocional.",
    },
    "vi": {
        "case_exploration": "Không có khách hàng điển hình nào phù hợp trong thư viện quảng bá.",
        "solution_discovery": "Không có giải pháp ngành nào phù hợp trong thư viện quảng bá.",
    },
    "mn": {
        "case_exploration": "Сурталчилгааны санд тохирох хэрэглэгчийн жишээ байхгүй.",
        "solution_discovery": "Сурталчилгааны санд тохирох салбарын шийдэл байхгүй.",
    },
    "th": {
        "case_exploration": "ไม่มีกรณีลูกค้าที่ตรงกันในคลังสื่อประชาสัมพันธ์",
        "solution_discovery": "ไม่มีโซลูชันอุตสาหกรรมที่ตรงกันในคลังสื่อประชาสัมพันธ์",
    },
    "km": {
        "case_exploration": "មិនមានករណីអតិថិជនដែលត្រូវគ្នានៅក្នុងបណ្ណាល័យផ្សព្វផ្សាយទេ",
        "solution_discovery": "មិនមានដំណោះស្រាយឧស្សាហកម្មដែលត្រូវគ្នានៅក្នុងបណ្ណាល័យផ្សព្វផ្សាយទេ",
    },
}

# The promotional surface could not be produced. Deliberately NOT the messages
# above: those assert "the library holds nothing matching this", which is a
# claim about content. When nothing was searched — no resolvable target, no
# search capability, a dependency failure, or an unreachable model — we know
# nothing about the content, so claiming an empty library would be a false
# statement to the user (and a misleading one to whoever debugs it later).
#
# Wording stays subsystem-neutral on purpose: the same copy covers a search
# outage and a model outage, and naming "检索" for a model failure would be a
# fresh inaccuracy of exactly the kind this table exists to prevent.
PROMO_UNAVAILABLE_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "case_exploration": "客户案例服务暂时不可用，请稍后重试。",
        "solution_discovery": "行业方案服务暂时不可用，请稍后重试。",
    },
    "en": {
        "case_exploration": "Customer cases are temporarily unavailable. Please try again later.",
        "solution_discovery": "Industry solutions are temporarily unavailable. Please try again later.",
    },
    "de": {
        "case_exploration": "Kundenfälle sind vorübergehend nicht verfügbar. Bitte später erneut versuchen.",
        "solution_discovery": (
            "Branchenlösungen sind vorübergehend nicht verfügbar. Bitte später erneut versuchen."
        ),
    },
    "fr": {
        "case_exploration": "Les cas clients sont momentanément indisponibles. Veuillez réessayer plus tard.",
        "solution_discovery": (
            "Les solutions sectorielles sont momentanément indisponibles. Veuillez réessayer plus tard."
        ),
    },
    "es": {
        "case_exploration": (
            "Los casos de cliente no están disponibles temporalmente. Inténtelo de nuevo más tarde."
        ),
        "solution_discovery": (
            "Las soluciones sectoriales no están disponibles temporalmente. Inténtelo de nuevo más tarde."
        ),
    },
    "pt": {
        "case_exploration": (
            "Os casos de cliente estão temporariamente indisponíveis. Tente novamente mais tarde."
        ),
        "solution_discovery": (
            "As soluções setoriais estão temporariamente indisponíveis. Tente novamente mais tarde."
        ),
    },
    "vi": {
        "case_exploration": "Dịch vụ khách hàng điển hình tạm thời không khả dụng. Vui lòng thử lại sau.",
        "solution_discovery": "Dịch vụ giải pháp ngành tạm thời không khả dụng. Vui lòng thử lại sau.",
    },
    "mn": {
        "case_exploration": "Хэрэглэгчийн жишээний үйлчилгээ түр боломжгүй байна. Дараа дахин оролдоно уу.",
        "solution_discovery": "Салбарын шийдлийн үйлчилгээ түр боломжгүй байна. Дараа дахин оролдоно уу.",
    },
    "th": {
        "case_exploration": "บริการกรณีลูกค้าไม่พร้อมใช้งานชั่วคราว กรุณาลองใหม่ภายหลัง",
        "solution_discovery": "บริการโซลูชันอุตสาหกรรมไม่พร้อมใช้งานชั่วคราว กรุณาลองใหม่ภายหลัง",
    },
    "km": {
        "case_exploration": "សេវាករណីអតិថិជនមិនអាចប្រើប្រាស់បានបណ្តោះអាសន្ន។ សូមព្យាយាមម្តងទៀតនៅពេលក្រោយ",
        "solution_discovery": "សេវាដំណោះស្រាយឧស្សាហកម្មមិនអាចប្រើប្រាស់បានបណ្តោះអាសន្ន។ សូមព្យាយាមម្តងទៀតនៅពេលក្រោយ",
    },
}


# Sync clarification replies. These are USER-VISIBLE and rendered directly by
# the client (frontend brief D.3: "渲染 message"), so they must follow
# Accept-Language like every other user-facing string. They were hardcoded in
# Chinese while the response still echoed `language: en` — the one user-visible
# surface that silently ignored the request language (41 live, 2026-09-18).
#
# Keys are the missing context the reply asks for; the jump-action guard uses
# "wrong_entry" because nothing is missing there — the client used the wrong
# surface.
CLARIFICATION_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "order_no": "请先选择需要检测的订单后，我才能继续处理。",
        "context": "请补充订单或设备等必要信息后，我才能继续处理。",
        "wrong_entry": "请点击页面上的快捷按钮进入对应页面。",
    },
    "en": {
        "order_no": "Please select the order you want checked before I can continue.",
        "context": "Please provide the order or device details before I can continue.",
        "wrong_entry": "Please use the shortcut button on the page to open the relevant screen.",
    },
    "de": {
        "order_no": "Bitte wählen Sie zuerst den zu prüfenden Auftrag aus, damit ich fortfahren kann.",
        "context": "Bitte geben Sie die Auftrags- oder Gerätedaten an, damit ich fortfahren kann.",
        "wrong_entry": "Bitte öffnen Sie die entsprechende Seite über die Schaltfläche auf der Seite.",
    },
    "fr": {
        "order_no": "Veuillez d'abord sélectionner la commande à vérifier pour que je puisse continuer.",
        "context": (
            "Veuillez fournir les informations de commande ou d'appareil pour que je puisse continuer."
        ),
        "wrong_entry": "Veuillez utiliser le bouton de la page pour ouvrir l'écran correspondant.",
    },
    "es": {
        "order_no": "Seleccione primero el pedido que desea revisar para que yo pueda continuar.",
        "context": "Facilite los datos del pedido o del dispositivo para que yo pueda continuar.",
        "wrong_entry": "Utilice el botón de la página para abrir la pantalla correspondiente.",
    },
    "pt": {
        "order_no": "Selecione primeiro o pedido que deseja verificar para que eu possa continuar.",
        "context": "Forneça os dados do pedido ou do dispositivo para que eu possa continuar.",
        "wrong_entry": "Utilize o botão da página para abrir o ecrã correspondente.",
    },
    "vi": {
        "order_no": "Vui lòng chọn đơn hàng cần kiểm tra trước khi tôi tiếp tục.",
        "context": "Vui lòng cung cấp thông tin đơn hàng hoặc thiết bị để tôi tiếp tục.",
        "wrong_entry": "Vui lòng dùng nút trên trang để mở màn hình tương ứng.",
    },
    "mn": {
        "order_no": "Үргэлжлүүлэхийн тулд эхлээд шалгах захиалгаа сонгоно уу.",
        "context": "Үргэлжлүүлэхийн тулд захиалга эсвэл төхөөрөмжийн мэдээллийг өгнө үү.",
        "wrong_entry": "Холбогдох хуудсыг хуудасны товчоор нээнэ үү.",
    },
    "th": {
        "order_no": "กรุณาเลือกคำสั่งซื้อที่ต้องการตรวจสอบก่อน เพื่อให้ฉันดำเนินการต่อได้",
        "context": "กรุณาให้ข้อมูลคำสั่งซื้อหรืออุปกรณ์ เพื่อให้ฉันดำเนินการต่อได้",
        "wrong_entry": "กรุณาใช้ปุ่มบนหน้าเพื่อเปิดหน้าจอที่เกี่ยวข้อง",
    },
    "km": {
        "order_no": "សូមជ្រើសរើសការបញ្ជាទិញដែលត្រូវពិនិត្យជាមុនសិន ដើម្បីឱ្យខ្ញុំបន្តបាន",
        "context": "សូមផ្តល់ព័ត៌មានការបញ្ជាទិញ ឬឧបករណ៍ ដើម្បីឱ្យខ្ញុំបន្តបាន",
        "wrong_entry": "សូមប្រើប៊ូតុងនៅលើទំព័រ ដើម្បីបើកអេក្រង់ដែលពាក់ព័ន្ធ",
    },
}

CLARIFICATION_FALLBACK_KEY = "context"

# The zero-order assistant's closing hint. It is emitted by the MODEL (the
# workspace contract tells it to end with this line), so it is not a message
# the server appends — but the line itself must follow the request language.
# It used to be a Chinese literal the contract ordered the model to reproduce
# "exactly", which made a Chinese sentence mandatory on every non-Chinese
# answer that took the order-hint branch: the plan was Chinese text inside a
# German answer, and the output guard then rejected the whole answer.
ZERO_ORDER_REMINDER_MESSAGES: dict[str, str] = {
    "zh": "提供订单号可获得更精确的结果哦。",
    "en": "Providing an order number gives you a more precise result.",
    "de": "Mit einer Auftragsnummer erhalten Sie ein präziseres Ergebnis.",
    "fr": "Indiquer un numéro de commande permet un résultat plus précis.",
    "es": "Indicar un número de pedido permite un resultado más preciso.",
    "pt": "Indicar um número de pedido permite um resultado mais preciso.",
    "vi": "Cung cấp số đơn hàng sẽ cho kết quả chính xác hơn.",
    "mn": "Захиалгын дугаараа оруулбал илүү нарийвчилсан үр дүн гарна.",
    "th": "ระบุหมายเลขคำสั่งซื้อจะได้ผลลัพธ์ที่แม่นยำยิ่งขึ้น",
    "km": "ការផ្តល់លេខការបញ្ជាទិញនឹងផ្តល់លទ្ធផលត្រឹមត្រូវជាង",
}


def zero_order_reminder(language: str) -> str:
    """The closing hint the zero-order answer must end with, in ``language``."""
    return ZERO_ORDER_REMINDER_MESSAGES.get(language) or ZERO_ORDER_REMINDER_MESSAGES[DEFAULT_LANGUAGE]


# A diagnosis that did NOT produce a conclusion, stated for the USER.
#
# When a run blocks, the engine's reason is harness internals — "the tool-call
# budget was exceeded", "the model returned invalid structured output", plus
# about twenty validator error strings naming evidence IDs and contract rules.
# Those are read by an engineer looking at the run record; a 管家端 user reading
# them learns nothing about their own order and reads a language they may not
# have asked for.
#
# So the user-facing surface says only WHICH KIND of ending this was, from a
# closed set, and the run record keeps the precise reason (`_finish_blocked`
# already records it as the `diagnosis_blocked` event). This is the same split
# the codebase already uses for a QA failure: "the record is read by an
# engineer and this response is read by a customer".
#
# Deliberately NOT a sentence about the data source. A blocked run is not proof
# the knowledge base or the order data was unavailable — the run failed its own
# contract. Claiming otherwise is the false-statement failure this project has
# already paid for (ADR-0007).
DIAGNOSIS_FAILURE_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "incomplete": "诊断未能形成结论，已记录运行详情供人工排查。",
        "insufficient_evidence": "现有证据不足以形成诊断结论，已记录运行详情供人工排查。",
        "out_of_scope": "该订单不在当前授权范围内，无法继续诊断。",
    },
    "en": {
        "incomplete": "The diagnosis could not reach a conclusion; run details were recorded for review.",
        "insufficient_evidence": (
            "The available evidence was not sufficient for a conclusion; "
            "run details were recorded for review."
        ),
        "out_of_scope": "This order is outside the authorized scope, so the diagnosis cannot continue.",
    },
    "de": {
        "incomplete": (
            "Die Diagnose konnte keine Schlussfolgerung erreichen; "
            "Laufdetails wurden zur Prüfung festgehalten."
        ),
        "insufficient_evidence": (
            "Die vorhandenen Belege reichten nicht für eine Schlussfolgerung; "
            "Laufdetails wurden zur Prüfung festgehalten."
        ),
        "out_of_scope": (
            "Dieser Auftrag liegt außerhalb des Berechtigungsbereichs; "
            "die Diagnose kann nicht fortgesetzt werden."
        ),
    },
    "fr": {
        "incomplete": (
            "Le diagnostic n'a pas pu aboutir ; les détails de l'exécution ont été enregistrés pour examen."
        ),
        "insufficient_evidence": (
            "Les éléments disponibles ne suffisaient pas pour conclure ; "
            "les détails de l'exécution ont été enregistrés pour examen."
        ),
        "out_of_scope": (
            "Cette commande est hors du périmètre autorisé ; le diagnostic ne peut pas se poursuivre."
        ),
    },
    "es": {
        "incomplete": (
            "El diagnóstico no pudo llegar a una conclusión; "
            "se registraron los detalles de la ejecución para su revisión."
        ),
        "insufficient_evidence": (
            "Las pruebas disponibles no bastaron para concluir; "
            "se registraron los detalles de la ejecución para su revisión."
        ),
        "out_of_scope": ("Este pedido está fuera del ámbito autorizado; el diagnóstico no puede continuar."),
    },
    "pt": {
        "incomplete": (
            "O diagnóstico não conseguiu chegar a uma conclusão; "
            "os detalhes da execução foram registados para análise."
        ),
        "insufficient_evidence": (
            "Os elementos disponíveis não foram suficientes para concluir; "
            "os detalhes da execução foram registados para análise."
        ),
        "out_of_scope": ("Este pedido está fora do âmbito autorizado; o diagnóstico não pode continuar."),
    },
    "vi": {
        "incomplete": "Chẩn đoán không đưa ra được kết luận; chi tiết đã được ghi lại để rà soát.",
        "insufficient_evidence": (
            "Bằng chứng hiện có chưa đủ để kết luận; chi tiết đã được ghi lại để rà soát."
        ),
        "out_of_scope": "Đơn hàng này nằm ngoài phạm vi được phép, không thể tiếp tục chẩn đoán.",
    },
    "mn": {
        "incomplete": "Оношилгоо дүгнэлт гаргаж чадсангүй; дэлгэрэнгүйг хянан шалгахаар бүртгэсэн.",
        "insufficient_evidence": (
            "Байгаа нотолгоо дүгнэлт гаргахад хүрэлцэхгүй байна; дэлгэрэнгүйг хянан шалгахаар бүртгэсэн."
        ),
        "out_of_scope": "Энэ захиалга зөвшөөрөгдсөн хүрээнээс гадуур тул оношилгоог үргэлжлүүлэх боломжгүй.",
    },
    "th": {
        "incomplete": "การวินิจฉัยไม่สามารถสรุปผลได้ บันทึกรายละเอียดไว้ให้ตรวจสอบแล้ว",
        "insufficient_evidence": "หลักฐานที่มีไม่เพียงพอต่อการสรุปผล บันทึกรายละเอียดไว้ให้ตรวจสอบแล้ว",
        "out_of_scope": "คำสั่งซื้อนี้อยู่นอกขอบเขตที่ได้รับอนุญาต จึงวินิจฉัยต่อไม่ได้",
    },
    "km": {
        "incomplete": "ការវិនិច្ឆ័យមិនអាចដល់សេចក្តីសន្និដ្ឋានបានទេ ព័ត៌មានលម្អិតត្រូវបានកត់ត្រាសម្រាប់ពិនិត្យ",
        "insufficient_evidence": "ភស្តុតាងដែលមានមិនគ្រប់គ្រាន់សម្រាប់សេចក្តីសន្និដ្ឋានទេ ព័ត៌មានលម្អិតត្រូវបានកត់ត្រាសម្រាប់ពិនិត្យ",
        "out_of_scope": "ការបញ្ជាទិញនេះស្ថិតនៅក្រៅវិសាលភាពដែលបានអនុញ្ញាត មិនអាចបន្តវិនិច្ឆ័យបានទេ",
    },
}


def diagnosis_failure_message(language: str, key: str) -> str:
    """Bounded, localized text for a diagnosis that produced no conclusion."""
    pack = DIAGNOSIS_FAILURE_MESSAGES.get(language) or DIAGNOSIS_FAILURE_MESSAGES[DEFAULT_LANGUAGE]
    return pack.get(key) or DIAGNOSIS_FAILURE_MESSAGES[DEFAULT_LANGUAGE]["incomplete"]


# The `error.message` a diagnosis job returns, chosen by its CODE.
#
# The stored `error_message` is an engineer's note (`_internal_error_message`):
# an upstream exception string, or a restart notice. It is not a user-facing
# sentence and must not be handed to a 管家端 user — `standard-api-contract.md`
# already promises this surface carries no internal run information. So the
# response carries a bounded sentence picked by the code, and the record keeps
# the precise reason. Same split as the QA failure path.
#
# `code` stays English and non-localized so a client can branch on it.
DIAGNOSIS_ERROR_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "out_of_scope": "该订单不在当前授权范围内。",
        "failed": "本次诊断未能完成，请稍后重试。",
        "expired": "本次诊断已超时，请重新发起。",
        "interrupted": "服务重启导致本次诊断中断，请重新发起。",
        "generic": "本次诊断未能完成。",
    },
    "en": {
        "out_of_scope": "This order is outside the authorized scope.",
        "failed": "This diagnosis could not be completed. Please try again later.",
        "expired": "This diagnosis timed out. Please start a new one.",
        "interrupted": "A service restart interrupted this diagnosis. Please start a new one.",
        "generic": "This diagnosis could not be completed.",
    },
    "de": {
        "out_of_scope": "Dieser Auftrag liegt außerhalb des Berechtigungsbereichs.",
        "failed": "Diese Diagnose konnte nicht abgeschlossen werden. Bitte später erneut versuchen.",
        "expired": "Diese Diagnose hat das Zeitlimit überschritten. Bitte neu starten.",
        "interrupted": "Ein Dienstneustart hat diese Diagnose unterbrochen. Bitte neu starten.",
        "generic": "Diese Diagnose konnte nicht abgeschlossen werden.",
    },
    "fr": {
        "out_of_scope": "Cette commande est hors du périmètre autorisé.",
        "failed": "Ce diagnostic n'a pas pu aboutir. Veuillez réessayer plus tard.",
        "expired": "Ce diagnostic a expiré. Veuillez en lancer un nouveau.",
        "interrupted": "Un redémarrage du service a interrompu ce diagnostic. Veuillez en lancer un nouveau.",
        "generic": "Ce diagnostic n'a pas pu aboutir.",
    },
    "es": {
        "out_of_scope": "Este pedido está fuera del ámbito autorizado.",
        "failed": "Este diagnóstico no pudo completarse. Inténtelo de nuevo más tarde.",
        "expired": "Este diagnóstico ha caducado. Inicie uno nuevo.",
        "interrupted": "Un reinicio del servicio interrumpió este diagnóstico. Inicie uno nuevo.",
        "generic": "Este diagnóstico no pudo completarse.",
    },
    "pt": {
        "out_of_scope": "Este pedido está fora do âmbito autorizado.",
        "failed": "Este diagnóstico não foi concluído. Tente novamente mais tarde.",
        "expired": "Este diagnóstico expirou. Inicie um novo.",
        "interrupted": "O reinício do serviço interrompeu este diagnóstico. Inicie um novo.",
        "generic": "Este diagnóstico não foi concluído.",
    },
    "vi": {
        "out_of_scope": "Đơn hàng này nằm ngoài phạm vi được phép.",
        "failed": "Chẩn đoán này không hoàn tất được. Vui lòng thử lại sau.",
        "expired": "Chẩn đoán này đã quá hạn. Vui lòng bắt đầu lại.",
        "interrupted": "Khởi động lại dịch vụ đã ngắt chẩn đoán này. Vui lòng bắt đầu lại.",
        "generic": "Chẩn đoán này không hoàn tất được.",
    },
    "mn": {
        "out_of_scope": "Энэ захиалга зөвшөөрөгдсөн хүрээнээс гадуур байна.",
        "failed": "Энэ оношилгоо дуусгаж чадсангүй. Дараа дахин оролдоно уу.",
        "expired": "Энэ оношилгооны хугацаа хэтэрсэн. Дахин эхлүүлнэ үү.",
        "interrupted": "Үйлчилгээ дахин эхэлснээс энэ оношилгоо тасарсан. Дахин эхлүүлнэ үү.",
        "generic": "Энэ оношилгоо дуусгаж чадсангүй.",
    },
    "th": {
        "out_of_scope": "คำสั่งซื้อนี้อยู่นอกขอบเขตที่ได้รับอนุญาต",
        "failed": "การวินิจฉัยนี้ไม่สำเร็จ กรุณาลองใหม่ภายหลัง",
        "expired": "การวินิจฉัยนี้หมดเวลาแล้ว กรุณาเริ่มใหม่",
        "interrupted": "การรีสตาร์ตบริการทำให้การวินิจฉัยนี้ถูกขัดจังหวะ กรุณาเริ่มใหม่",
        "generic": "การวินิจฉัยนี้ไม่สำเร็จ",
    },
    "km": {
        "out_of_scope": "ការបញ្ជាទិញនេះស្ថិតនៅក្រៅវិសាលភាពដែលបានអនុញ្ញាត",
        "failed": "ការវិនិច្ឆ័យនេះមិនបានសម្រេចទេ។ សូមព្យាយាមម្តងទៀតនៅពេលក្រោយ",
        "expired": "ការវិនិច្ឆ័យនេះផុតកំណត់ហើយ។ សូមចាប់ផ្តើមថ្មី",
        "interrupted": "ការចាប់ផ្តើមសេវាឡើងវិញបានផ្អាកការវិនិច្ឆ័យនេះ។ សូមចាប់ផ្តើមថ្មី",
        "generic": "ការវិនិច្ឆ័យនេះមិនបានសម្រេចទេ",
    },
}


# Error code -> message key. A code not listed here gets "generic": this map is
# deliberately total-with-a-default rather than an exhaustive enumeration, so a
# NEW code added later cannot make the surface fall back to the raw stored text.
_DIAGNOSIS_ERROR_KEYS: dict[str, str] = {
    "DIAGNOSIS_ORDER_OUT_OF_SCOPE": "out_of_scope",
    "DIAGNOSIS_DEADLINE": "expired",
    "DIAGNOSIS_INTERRUPTED_BY_RESTART": "interrupted",
    "DIAGNOSIS_FAILED": "failed",
    "DIAGNOSIS_BLOCKED": "failed",
}


def diagnosis_error_message(language: str, error_code: str | None) -> str:
    """The bounded, localized `error.message` for a diagnosis failure."""
    pack = DIAGNOSIS_ERROR_MESSAGES.get(language) or DIAGNOSIS_ERROR_MESSAGES[DEFAULT_LANGUAGE]
    return pack[_DIAGNOSIS_ERROR_KEYS.get(error_code or "", "generic")]


def clarification_message(language: str, key: str) -> str:
    """Localized clarification text, falling back to zh then to a safe default.

    A missing key must never yield an empty message: the client renders this
    string verbatim, so an empty one would leave the user with a blank reply.
    """
    pack = CLARIFICATION_MESSAGES.get(language) or CLARIFICATION_MESSAGES[DEFAULT_LANGUAGE]
    resolved = pack.get(key) or CLARIFICATION_MESSAGES[DEFAULT_LANGUAGE].get(key)
    if resolved:
        return resolved
    return CLARIFICATION_MESSAGES[DEFAULT_LANGUAGE][CLARIFICATION_FALLBACK_KEY]


def language_name(language: str) -> str:
    """English display name of a supported language for prompt injection."""
    return LANGUAGE_NAMES.get(language, LANGUAGE_NAMES[DEFAULT_LANGUAGE])


def resolve_language(accept_language: str | None) -> str:
    """Resolve one supported language tag from an ``Accept-Language`` header.

    RFC 7231 list semantics: entries split on ``,``; the first param ``q=``
    sets the weight (default 1.0). Region (and script) subtags fold onto the
    base tag (``en-US`` → ``en``, ``zh-Hans-CN`` → ``zh``). The highest-q
    supported base tag wins; equal q keeps the first occurrence. `*`,
    unsupported tags, malformed weights, and q=0 entries are ignored — a
    header with no acceptable entry falls back to :data:`DEFAULT_LANGUAGE`.
    """
    if not accept_language or not accept_language.strip():
        return DEFAULT_LANGUAGE
    best: tuple[float, str] | None = None
    for entry in accept_language.split(","):
        parts = [part.strip() for part in entry.split(";")]
        tag = parts[0].lower()
        if not tag or tag == "*":
            continue
        weight = 1.0
        malformed = False
        for param in parts[1:]:
            if param.startswith("q="):
                try:
                    weight = float(param[2:])
                except ValueError:
                    malformed = True
                    break
                if not 0.0 <= weight <= 1.0:
                    malformed = True
                    break
        if malformed or weight <= 0.0:
            continue
        base = tag.split("-", 1)[0]
        if base not in SUPPORTED_LANGUAGES:
            continue
        if best is None or weight > best[0]:
            best = (weight, base)
    return best[1] if best else DEFAULT_LANGUAGE
