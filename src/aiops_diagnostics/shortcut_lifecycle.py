"""Platform and tenant shortcut drafts with immutable published versions (#230/#244).

Independent product-entry resource — NOT the Agent ``quick_commands`` model.
Tenant identity is ``(tenant_id, business_entry, code)``; platform defaults use
an internal platform scope. The lifecycle mirrors
agent_lifecycle (draft -> published -> disabled) so operators already know
the semantics, but the store, permission scope, and version snapshots are
separate. Editing a draft never mutates a snapshot an in-flight client may
already be rendering.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from aiops_diagnostics.agent_lifecycle import SAFE_ID
from aiops_diagnostics.i18n import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
from aiops_diagnostics.private_files import ensure_private_directory, protect_private_file

logger = logging.getLogger(__name__)


class ShortcutCopyGap(NamedTuple):
    """One field of one shortcut that lacks a supported language."""

    tenant_id: str
    code: str
    field: str
    language: str


def shortcut_copy_gaps(shortcuts: Iterable[Any]) -> list[ShortcutCopyGap]:
    """Return every supported-language gap across ``shortcuts``.

    The deterministic gate behind the CI coverage check: a shortcut shipping
    with three of six languages is a defect that must fail a check rather than
    reach a user whose buttons are in a language they did not ask for. Kept
    separate from the warning path so the gate and the log agree on what
    counts as a gap.
    """
    gaps: list[ShortcutCopyGap] = []
    for shortcut in shortcuts:
        for language in SUPPORTED_LANGUAGES:
            for field, _fallback in shortcut.missing_translations(language):
                gaps.append(
                    ShortcutCopyGap(
                        tenant_id=shortcut.tenant_id,
                        code=shortcut.code,
                        field=field,
                        language=language,
                    )
                )
    return gaps


def _warn_missing_translation(shortcut: Any, language: str, field: str, fallback: str) -> None:
    """Record a shortcut copy fallback (never silently, never the copy itself).

    Logs identifiers and the fact of the gap. The fallback text is not logged:
    it is not secret, but the log is for finding the gap, not for reading the
    product's copy in a place nobody curates.
    """
    logger.warning(
        "shortcut copy missing translation: code=%s tenant=%s entry=%s field=%s language=%s",
        shortcut.code,
        shortcut.tenant_id,
        shortcut.business_entry,
        field,
        language,
        extra={
            "event": "shortcut_translation_missing",
            "code": shortcut.code,
            "tenant_id": shortcut.tenant_id,
            "business_entry": shortcut.business_entry,
            "field": field,
            "language": language,
            "has_zh_fallback": bool(fallback),
        },
    )


# Promotional Agent/version reference — same shape as ConversationCreateRequest's
# agent_version_key ("agt_<hex>#vN"), so a shortcut target always resolves to
# one immutable published agent version.
_AGENT_VERSION = re.compile(r"^agt_[A-Za-z0-9]{8,64}#v\d{1,6}$")

SHORTCUT_MANAGE_SCOPE = "aiops:shortcuts:manage"
SHORTCUT_INTENTS = frozenset(
    {"knowledge", "casual", "order_issue", "report_fault", "case_exploration", "solution_discovery"}
)
SHORTCUT_STATUSES = frozenset({"draft", "published", "disabled"})
PLATFORM_SCOPE = "platform"
TENANT_SCOPE = "tenant"
PLATFORM_TENANT_ID = "__platform__"
SHORTCUT_SCOPES = frozenset({PLATFORM_SCOPE, TENANT_SCOPE})
PLATFORM_ROLES = frozenset({"ROLE_PLATFORM_ADMIN"})
# Stable codes the frontend may hard-wire behavior to (order picker etc.).
STABLE_CODES = frozenset({"case_exploration", "smart_diagnosis", "report_fault"})

# Product default for the fault-reporting jump action. The path is an
# in-app route, so it is not localized: one path per action, not per language.
REPORT_FAULT_JUMP_PATH = "/charge/pages/faultReport/faultReportList"

# The battery report the chat-page banner navigates to. Same rule as the fault
# report: an in-app route, one path for every language (ADR-0006).
BATTERY_REPORT_JUMP_PATH = "/aiPackage/pages/batteryReport/batteryReport"

# Resource kind — the EXPLICIT discriminator between a chat-page banner and an
# ordinary shortcut button. It is a column, not a `fields_json` convention and
# not "image_url is set", because the serving query has to select a surface
# without parsing JSON, and because deriving the surface from `image_url` would
# make "which endpoint read this row" an implicit input to the shared
# projection. Exactly two kinds; the value is what the row IS, so it is never
# edited after creation.
BUTTON_KIND = "button"
BANNER_KIND = "banner"
SHORTCUT_KINDS = frozenset({BUTTON_KIND, BANNER_KIND})

VIEW_ROLES = frozenset(
    {"ROLE_AGENT_VIEWER", "ROLE_AGENT_ADMIN", "ROLE_AGENT_PUBLISHER", "ROLE_PLATFORM_ADMIN"}
)
EDIT_ROLES = frozenset({"ROLE_AGENT_ADMIN", "ROLE_PLATFORM_ADMIN"})
PUBLISH_ROLES = frozenset({"ROLE_AGENT_PUBLISHER", "ROLE_AGENT_ADMIN", "ROLE_PLATFORM_ADMIN"})


class ShortcutError(RuntimeError):
    """Base class for shortcut lifecycle failures."""

    code = "SHORTCUT_ERROR"


# The initial product-entry shortcuts (#230 initial codes). Copy lives here
# only as the seed default; after creation the rows are ordinary managed
# resources — operators edit/publish/disable them through the API.
#
# Copy covers ALL of SUPPORTED_LANGUAGES. There is no separate label/description
# table to match — i18n.py holds the language INVENTORY and the message catalogs,
# while these seeds ARE the shortcut copy, keyed by the inventory's tags and
# gated by the same coverage checks. The earlier comment pointed at i18n.py for
# tables that do not live there, so replaying it reconstructed a premise that was
# never true.
#
# A two-language seed is not harmless: resolve_language accepts every declared
# language and public() falls back to zh, so a user of any other language
# silently gets Chinese buttons while the response still echoes their language
# (41 live, 2026-09-18).
#
# The operator entry (#427) reuses the SAME codes as consumer: both actions run
# on the published execution paths, so an operator-entry button behaves exactly
# like its consumer twin and needs no second implementation. Only two: the
# fault-report jump action is not the assistant entry's business, so it stays on
# the consumer side. Publishing per tenant+entry remains an operator act — these
# rows seed as drafts like the consumer ones.
#
# The fields that are byte-identical on both entries are therefore shared
# constants rather than two copies: `intent` / `requires_order` / `sort_order` /
# `question_templates` must match the consumer twin, and a second copy drifts
# invisibly (only the button copy differs between entries).
_SHARED_ACTION_FIELDS: dict[str, dict[str, Any]] = {
    "case_exploration": {
        "intent": "case_exploration",
        "requires_order": False,
        "sort_order": 10,
        "question_templates": {
            "zh": "我想看看客户案例",
            "zh-Hant": "我想看看客戶案例",
            "vi": "Tôi muốn xem các trường hợp khách hàng",
            "mn": "Хэрэглэгчийн жишээг үзэхийг хүсэж байна",
            "th": "ฉันอยากดูกรณีตัวอย่างลูกค้า",
            "km": "ខ្ញុំចង់មើលករណីអតិថិជន",
            "en": "I'd like to see customer cases",
            "de": "Ich möchte Kundenfälle sehen",
            "fr": "Je voudrais voir des cas clients",
            "es": "Quiero ver casos de cliente",
            "pt": "Quero ver casos de cliente",
        },
    },
    "smart_diagnosis": {
        "intent": "order_issue",
        "requires_order": True,
        "sort_order": 20,
        "question_templates": {
            "zh": "帮我检测这个订单的充电异常",
            "zh-Hant": "幫我檢測這個訂單的充電異常",
            "vi": "Hãy kiểm tra bất thường sạc của đơn hàng này",
            "mn": "Энэ захиалгын цэнэглэлтийн алдааг шалгаж өгнө үү",
            "th": "ช่วยตรวจสอบความผิดปกติในการชาร์จของคำสั่งซื้อนี้",
            "km": "សូមពិនិត្យភាពមិនប្រក្រតីនៃការសាករបស់ការបញ្ជាទិញនេះ",
            "en": "Diagnose the charging issue of this order",
            "de": "Diagnostizieren Sie den Ladefehler dieses Auftrags",
            "fr": "Diagnostiquez l'anomalie de charge de cette commande",
            "es": "Diagnostique la anomalía de carga de este pedido",
            "pt": "Diagnostique a anomalia de carregamento deste pedido",
        },
    },
}
_BUNDLED_SHORTCUTS: tuple[tuple[str, dict[str, dict[str, Any]]], ...] = (
    (
        "consumer",
        {
            "case_exploration": {
                **_SHARED_ACTION_FIELDS["case_exploration"],
                "labels": {
                    "zh": "客户案例",
                    "zh-Hant": "客戶案例",
                    "vi": "Trường hợp khách hàng",
                    "mn": "Хэрэглэгчийн жишээ",
                    "th": "กรณีตัวอย่างลูกค้า",
                    "km": "ករណីអតិថិជន",
                    "en": "Customer Cases",
                    "de": "Kundenfälle",
                    "fr": "Cas clients",
                    "es": "Casos de cliente",
                    "pt": "Casos de cliente",
                },
                "descriptions": {
                    "zh": "查看不同行业的充电运营标杆案例",
                    "zh-Hant": "檢視不同行業的充電運營標杆案例",
                    "vi": "Xem các trường hợp điển hình vận hành sạc theo ngành",
                    "mn": "Салбар бүрийн цэнэглэлтийн үйл ажиллагааны жишиг жишээг үзэх",
                    "th": "ดูกรณีตัวอย่างการดำเนินงานชาร์จในแต่ละอุตสาหกรรม",
                    "km": "មើលករណីគំរូប្រតិបត្តិការសាកក្នុងឧស្សាហកម្មផ្សេងៗ",
                    "en": "Explore charging-operation benchmark cases by industry",
                    "de": "Benchmark-Fälle aus dem Ladebetrieb verschiedener Branchen",
                    "fr": "Découvrez des cas de référence par secteur d'activité",
                    "es": "Conozca casos de referencia de distintos sectores",
                    "pt": "Conheça casos de referência de vários setores",
                },
            },
            "smart_diagnosis": {
                **_SHARED_ACTION_FIELDS["smart_diagnosis"],
                "labels": {
                    "zh": "智能检测",
                    "zh-Hant": "智慧檢測",
                    "vi": "Kiểm tra thông minh",
                    "mn": "Ухаалаг шалгалт",
                    "th": "ตรวจสอบอัจฉริยะ",
                    "km": "ការពិនិត្យឆ្លាតវៃ",
                    "en": "Smart Diagnosis",
                    "de": "Intelligente Diagnose",
                    "fr": "Diagnostic intelligent",
                    "es": "Diagnóstico inteligente",
                    "pt": "Diagnóstico inteligente",
                },
                "descriptions": {
                    "zh": "选择订单后自动诊断充电异常",
                    "zh-Hant": "選擇訂單後自動診斷充電異常",
                    "vi": "Chọn đơn hàng để tự động chẩn đoán bất thường sạc",
                    "mn": "Захиалга сонгосны дараа цэнэглэлтийн алдааг автоматаар оношилно",
                    "th": "เลือกคำสั่งซื้อแล้วระบบจะวินิจฉัยความผิดปกติในการชาร์จอัตโนมัติ",
                    "km": "ជ្រើសរើសការបញ្ជាទិញ រួចវិនិច្ឆ័យភាពមិនប្រក្រតីនៃការសាកដោយស្វ័យប្រវត្តិ",
                    "en": "Pick an order, then diagnose the charging issue automatically",
                    "de": "Auftrag wählen, Ladeabbrüche werden automatisch diagnostiziert",
                    "fr": "Sélectionnez une commande pour diagnostiquer l'anomalie de charge",
                    "es": "Seleccione un pedido para diagnosticar la anomalía de carga",
                    "pt": "Selecione um pedido para diagnosticar a anomalia de carregamento",
                },
            },
            "report_fault": {
                "intent": "report_fault",
                "requires_order": False,
                "sort_order": 30,
                "labels": {
                    "zh": "故障上报",
                    "zh-Hant": "故障上報",
                    "vi": "Báo lỗi",
                    "mn": "Гэмтэл мэдэгдэх",
                    "th": "แจ้งเหตุขัดข้อง",
                    "km": "រាយការណ៍ការខូច",
                    "en": "Report a Fault",
                    "de": "Störung melden",
                    "fr": "Signaler une panne",
                    "es": "Notificar una avería",
                    "pt": "Comunicar uma avaria",
                },
                "descriptions": {
                    "zh": "描述故障现象，由平台跟进处理",
                    "zh-Hant": "描述故障現象，由平臺跟進處理",
                    "vi": "Mô tả hiện tượng lỗi, nền tảng sẽ theo dõi xử lý",
                    "mn": "Гэмтлийн шинж тэмдгийг тайлбарлана уу, платформ үргэлжлүүлэн шийдвэрлэнэ",
                    "th": "อธิบายอาการขัดข้อง แพลตฟอร์มจะติดตามจัดการให้",
                    "km": "ពិពណ៌នាអំពីអាការៈខូច វេទិកានឹងតាមដានដោះស្រាយ",
                    "en": "Describe the fault and the platform will follow up",
                    "de": "Beschreiben Sie die Störung, die Plattform kümmert sich darum",
                    "fr": "Décrivez la panne, la plateforme prend le relais",
                    "es": "Describa la avería y la plataforma se encargará",
                    "pt": "Descreva a avaria e a plataforma dará seguimento",
                },
                "question_templates": {
                    "zh": "我要上报一个故障",
                    "zh-Hant": "我要上報一個故障",
                    "vi": "Tôi muốn báo một lỗi",
                    "mn": "Би гэмтэл мэдэгдэхийг хүсэж байна",
                    "th": "ฉันต้องการแจ้งเหตุขัดข้อง",
                    "km": "ខ្ញុំចង់រាយការណ៍ការខូច",
                    "en": "I want to report a fault",
                    "de": "Ich möchte eine Störung melden",
                    "fr": "Je souhaite signaler une panne",
                    "es": "Quiero notificar una avería",
                    "pt": "Quero comunicar uma avaria",
                },
                # The product's default fault-reporting entry is the in-app
                # form, not a preset prompt. Kept here as the versioned
                # definition so the path is a repo asset, not a magic string
                # in an ad-hoc migration command.
                "jump_path": REPORT_FAULT_JUMP_PATH,
            },
            # The chat-page banner (#578). Same table, same lifecycle, same
            # copy coverage — but a card, on its own surface, reached through
            # the banner endpoint. `kind` is what puts it there; the button
            # listing never sees it.
            #
            # No `question_templates`: a jump action never reaches the unified
            # assistant entry, so a preset prompt would be dead copy that the
            # coverage gate would then demand in eleven languages.
            #
            # The copy is the product's own, taken from the shipped client
            # bundle (build 21 / 1.2.12) where this banner is already live in
            # all eleven languages — it is NOT invented here. Two caveats,
            # both recorded rather than silently fixed:
            #
            #   * `zh-Hant` is the repo's own derivation of the `zh` authority
            #     (opencc `s2twp`, enforced by tools/derive_zh_hant.py), which
            #     yields 「待生成智慧電池檢查報告」. The client currently ships
            #     「待產生智能電池檢查報告」 instead. Two strings for one banner;
            #     the repo rule wins here, and the divergence is a frontend
            #     question (does the client read this table or its own bundle?).
            #   * GLOSSARY objects to 「待生成」 (it implies a queued,
            #     unpersisted report — see 可生成报告订单). Product owns the
            #     wording, so it is kept as shipped rather than rewritten here.
            "battery_report": {
                "intent": "knowledge",  # inert for a jump action; see the brief
                "requires_order": False,
                "sort_order": 10,
                "labels": {
                    "zh": "你有一份待生成智能电池检查报告",
                    "zh-Hant": "你有一份待生成智慧電池檢查報告",
                    "vi": "Bạn có một báo cáo kiểm tra pin thông minh đang chờ tạo",
                    "mn": "Танд бэлтгэгдэх ухаалаг батерей шалгалтын тайлан байна",
                    "th": "คุณมีรายงานการตรวจสภาพแบตเตอรี่อัจฉริยะรอสร้างอยู่",
                    "km": "អ្នកមានរបាយការណ៍ត្រួតពិនិត្យអាគុយឆ្លាតវៃរង់ចាំការបង្កើត",
                    "en": "You have a smart battery health report ready to generate",
                    "de": "Ein smarter Batterieprüfbericht steht zur Erstellung bereit",
                    "fr": "Vous avez un rapport d'état de batterie prêt à être généré",
                    "es": "Tienes un informe inteligente de salud de batería listo para generar",
                    "pt": "Você tem um relatório inteligente de saúde da bateria pronto para gerar",
                },
                "descriptions": {
                    "zh": "想知道你的电池容量衰减多少？",
                    "zh-Hant": "想知道你的電池容量衰減多少？",
                    "vi": "Bạn muốn biết dung lượng pin đã suy giảm bao nhiêu?",
                    "mn": "Батерейны багтаамж хэр буурсныг мэдэхийг хүсч байна уу?",
                    "th": "ต้องการทราบว่าความจุแบตเตอรี่ของคุณลดลงเท่าไรหรือไม่?",
                    "km": "ចង់ដឹងថាទំហំផ្ទុកអាគុយរបស់អ្នកថយចុះប៉ុន្មានដែរឬទេ?",
                    "en": "Want to check your battery degradation and capacity?",
                    "de": "Möchten Sie wissen, wie viel Kapazität Ihr Akku verloren hat?",
                    "fr": "Vous voulez savoir comment votre batterie s'est dégradée ?",
                    "es": "¿Quieres saber cuánta capacidad ha perdido tu batería?",
                    "pt": "Quer saber quanto a sua bateria degradou?",
                },
                "jump_path": BATTERY_REPORT_JUMP_PATH,
                "kind": BANNER_KIND,
                # The operator's fallback image. Left unset in the seed: the
                # personalized photo comes from the client's own car lookup, and
                # the repo has no business hot-linking a third-party CDN from a
                # seed. Ops sets this when they want a fallback.
                "image_url": None,
            },
        },
    ),
    (
        "operator",
        {
            "case_exploration": {
                **_SHARED_ACTION_FIELDS["case_exploration"],
                "labels": {
                    "zh": "客户案例",
                    "zh-Hant": "客戶案例",
                    "vi": "Trường hợp khách hàng",
                    "mn": "Хэрэглэгчийн жишээ",
                    "th": "กรณีตัวอย่างลูกค้า",
                    "km": "ករណីអតិថិជន",
                    "en": "Customer Cases",
                    "de": "Kundenfälle",
                    "fr": "Cas clients",
                    "es": "Casos de cliente",
                    "pt": "Casos de cliente",
                },
                "descriptions": {
                    "zh": "查看与充电运营相关的标杆案例",
                    "zh-Hant": "檢視與充電運營相關的標杆案例",
                    "vi": "Xem các trường hợp điển hình liên quan đến vận hành sạc",
                    "mn": "Цэнэглэлтийн үйл ажиллагаатай холбоотой жишиг жишээг үзэх",
                    "th": "ดูกรณีตัวอย่างที่เกี่ยวข้องกับการดำเนินงานชาร์จ",
                    "km": "មើលករណីគំរូពាក់ព័ន្ធនឹងប្រតិបត្តិការសាក",
                    "en": "Browse benchmark cases from charging operations",
                    "de": "Benchmark-Fälle aus dem Ladebetrieb ansehen",
                    "fr": "Consultez des cas de référence liés à l'exploitation de la recharge",
                    "es": "Consulte casos de referencia relacionados con la operación de carga",
                    "pt": "Consulte casos de referência relacionados à operação de recarga",
                },
            },
            "smart_diagnosis": {
                **_SHARED_ACTION_FIELDS["smart_diagnosis"],
                "labels": {
                    "zh": "订单检测",
                    "zh-Hant": "訂單檢測",
                    "vi": "Kiểm tra đơn hàng",
                    "mn": "Захиалга шалгах",
                    "th": "ตรวจสอบคำสั่งซื้อ",
                    "km": "ពិនិត្យការបញ្ជាទិញ",
                    "en": "Order Diagnosis",
                    "de": "Auftragsdiagnose",
                    "fr": "Diagnostic de commande",
                    "es": "Diagnóstico de pedido",
                    "pt": "Diagnóstico de pedido",
                },
                "descriptions": {
                    "zh": "选择订单，检测该订单的充电异常",
                    "zh-Hant": "選擇訂單，檢測該訂單的充電異常",
                    "vi": "Chọn đơn hàng và kiểm tra bất thường sạc của đơn đó",
                    "mn": "Захиалга сонгоод тухайн захиалгын цэнэглэлтийн алдааг шалгах",
                    "th": "เลือกคำสั่งซื้อและตรวจสอบความผิดปกติในการชาร์จของคำสั่งซื้อนั้น",
                    "km": "ជ្រើសរើសការបញ្ជាទិញ រួចពិនិត្យភាពមិនប្រក្រតីនៃការសាករបស់វា",
                    "en": "Pick an order and its charging issues are detected",
                    "de": "Auftrag wählen, Ladefehler werden automatisch erkannt",
                    "fr": "Sélectionnez une commande, ses anomalies de charge sont détectées",
                    "es": "Seleccione un pedido y se detectarán sus anomalías de carga",
                    "pt": "Selecione um pedido e as anomalias de carregamento serão detectadas",
                },
            },
        },
    ),
)


class ShortcutNotFound(ShortcutError):
    code = "SHORTCUT_NOT_FOUND"


class ShortcutForbidden(ShortcutError):
    code = "SHORTCUT_FORBIDDEN"


class ShortcutConflict(ShortcutError):
    code = "SHORTCUT_REVISION_CONFLICT"


class ShortcutValidationError(ShortcutError):
    code = "SHORTCUT_VALIDATION_FAILED"


@dataclass(frozen=True, slots=True)
class Shortcut:
    shortcut_id: str
    tenant_id: str
    business_entry: str
    code: str
    intent: str
    requires_order: bool
    sort_order: int
    status: str
    revision: int
    labels: dict[str, str]  # language -> label
    descriptions: dict[str, str]
    question_templates: dict[str, str]
    target_agent_version: str | None  # "agt_xxx#vN" for promotional targets
    # In-app route the client navigates to on click. A non-empty path IS the
    # discriminator: it makes this a jump action, which never reaches the
    # unified assistant entry. None keeps the prompt action behavior.
    jump_path: str | None
    # Which surface this row belongs to. A column rather than a `fields_json`
    # key because the serving query selects a surface, and because deriving it
    # from `image_url` would make the reader an implicit input. Immutable after
    # creation: a row does not become a different kind of resource.
    kind: str
    # Optional fallback image for a banner row (`None` for buttons). Frozen
    # into the publish snapshot like every other served field; the client uses
    # its own car lookup first and falls back here.
    image_url: str | None
    published_version: int | None
    created_by: str
    created_at: str
    updated_at: str
    scope: str = TENANT_SCOPE

    def served_language(self, language: str) -> str:
        """The language this row's copy can actually be served in.

        A row published before a language existed has no copy for it, and
        `_localized_field` falls back to zh. The listing echoes a language, so
        it must echo the one the TEXT is in — reporting the requested tag while
        sending Chinese is the defect this whole workstream exists to remove,
        and it survives in every already-published row unless the listing
        reports the fallback instead of hiding it.
        """
        if language == DEFAULT_LANGUAGE:
            return DEFAULT_LANGUAGE
        # EVERY field that carries copy must have this language, not just one of
        # them: a row with an English label and a Chinese description is not an
        # English row, and reporting `en` would claim the description too. A
        # field that is empty in every language (a jump action has no
        # question_template) does not participate.
        participating = [
            values for values in (self.labels, self.descriptions, self.question_templates) if values
        ]
        if participating and all(values.get(language) for values in participating):
            return language
        return DEFAULT_LANGUAGE

    def public(self, language: str) -> dict[str, Any]:
        """Public listing shape: stable code + localized text for ONE language.

        Falls back to zh for a missing language (repo i18n rule: the catalog
        never returns empty copy). Status is always ``published`` here — the
        listing endpoint only serves published rows (#230 acceptance).

        A missing translation is ALWAYS recorded before the fallback is used.
        The fallback itself is deliberate — returning empty copy would blank a
        user's buttons — but doing it silently is what let a shortcut language
        gap survive a whole release cycle: the request returned 200, the
        ``language`` echoed exactly what was asked for, and only the text was
        wrong. See :meth:`missing_translations`.

        This is the BUTTON surface's shape, and it is byte-for-byte what it
        always was (#588 acceptance: adding a banner must not change the
        button listing). It therefore gained no `kind` and no `image_url`: a
        banner never reaches it, and a button can never carry an image (see
        `_validated_fields`). The banner has its own projection; see
        :meth:`banner`.
        """
        for field, value in self.missing_translations(language):
            _warn_missing_translation(self, language, field, value)
        return {
            "code": self.code,
            # The language THIS row's copy is in — the requested one when the
            # row has it, the authority language when it does not. Per-row and
            # not only per-list: rows are published independently, so one
            # listing can carry a translated row beside an untranslated one.
            "language": self.served_language(language),
            "intent": self.intent,
            "requires_order": self.requires_order,
            "sort_order": self.sort_order,
            "label": self._localized_field(self.labels, language),
            "description": self._localized_field(self.descriptions, language),
            "question_template": self._localized_field(self.question_templates, language),
            "target_agent_version": self.target_agent_version,
            "jump_path": self.jump_path,
        }

    def banner(self, language: str) -> dict[str, Any]:
        """The banner surface's shape for ONE language.

        Deliberately narrower than :meth:`public`: a banner is a jump action,
        so it has no `question_template` (it never reaches the unified
        assistant entry) and no `target_agent_version`. Sending them would
        invite a client to use a field that is meaningless for the card, and
        `question_template` is a product rule, not a trimming convenience.

        `image_url` is the operator's fallback only. The personalized car photo
        is the client's own business — AI-Ops reads no business table and
        returns no order- or vehicle-derived value here.
        """
        return {
            "code": self.code,
            "language": self.served_language(language),
            "label": self._localized_field(self.labels, language),
            "description": self._localized_field(self.descriptions, language),
            "jump_path": self.jump_path,
            "image_url": self.image_url,
        }

    @staticmethod
    def _localized_field(values: dict[str, str], language: str) -> str:
        return values.get(language) or values.get("zh", "")

    def missing_translations(self, language: str) -> list[tuple[str, str]]:
        """Return ``(field, fallback_value)`` for copy this language lacks.

        Empty when the language is fully covered, or when it is the default
        (zh is the authority, so its presence is not a gap). Also used by the
        coverage check, so the warning and the gate agree on what counts.

        A field that is empty in EVERY language does not participate — the same
        rule :meth:`served_language` applies. The case that made this
        necessary: a banner is a jump action, so it carries no
        `question_templates` at all, and counting that as a gap in eleven
        languages would be eleven warnings per request and a CI failure for
        copy that is deliberately absent rather than untranslated.
        """
        if language == DEFAULT_LANGUAGE:
            return []
        missing: list[tuple[str, str]] = []
        for field, values in (
            ("label", self.labels),
            ("description", self.descriptions),
            ("question_template", self.question_templates),
        ):
            if not values:
                continue
            if not values.get(language):
                missing.append((field, values.get(DEFAULT_LANGUAGE, "")))
        return missing

    def to_dict(self) -> dict[str, Any]:
        """Full management shape (admin surface only)."""
        return {
            "shortcut_id": self.shortcut_id,
            "code": self.code,
            "business_entry": self.business_entry,
            "intent": self.intent,
            "requires_order": self.requires_order,
            "sort_order": self.sort_order,
            "status": self.status,
            "revision": self.revision,
            "labels": dict(self.labels),
            "descriptions": dict(self.descriptions),
            "question_templates": dict(self.question_templates),
            "target_agent_version": self.target_agent_version,
            "jump_path": self.jump_path,
            "kind": self.kind,
            "image_url": self.image_url,
            "published_version": self.published_version,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "scope": self.scope,
        }


@dataclass(frozen=True, slots=True)
class ShortcutVersion:
    shortcut_id: str
    tenant_id: str
    version_no: int
    snapshot: dict[str, Any]
    published_by: str
    published_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "shortcut_id": self.shortcut_id,
            "version_no": self.version_no,
            "snapshot": json.loads(json.dumps(self.snapshot, ensure_ascii=False)),
            "published_by": self.published_by,
            "published_at": self.published_at,
        }


class ShortcutStore:
    """SQLite persistence for shortcuts sharing the gateway database file."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        ensure_private_directory(self.path.parent)
        self._initialize()

    def seed_bundled(self, context: Any, manager: ShortcutManager) -> list[Shortcut]:
        """Create the initial seed shortcuts if absent (#230, #588).

        Idempotent: existing rows in this tenant+entry keep their lifecycle
        state (an operator may have disabled them deliberately). The seeded
        rows are drafts — publishing is an explicit operator act per the
        PRD's governance boundary, never automatic.

        The seed is not only buttons: the consumer entry also carries the
        chat-page banner. Which surface a row belongs to is `kind`, taken from
        the seed spec — never inferred from which fields happen to be set.
        """
        del manager
        seeded: list[Shortcut] = []
        tenant_id = context.effective_tenant_id
        for entry, fields in _BUNDLED_SHORTCUTS:
            for code, spec in fields.items():
                if self.find_by_code(tenant_id, entry, code) is not None:
                    continue
                seeded.append(
                    self.create(
                        tenant_id,
                        entry,
                        code,
                        intent=spec["intent"],
                        requires_order=spec["requires_order"],
                        sort_order=spec["sort_order"],
                        labels=spec["labels"],
                        descriptions=spec["descriptions"],
                        question_templates=spec.get("question_templates") or {},
                        target_agent_version=None,
                        jump_path=spec.get("jump_path"),
                        kind=spec.get("kind", BUTTON_KIND),
                        image_url=spec.get("image_url"),
                        created_by=_actor(context),
                    )
                )
        return seeded

    def create(
        self,
        tenant_id: str,
        business_entry: str,
        code: str,
        *,
        intent: str,
        requires_order: bool,
        sort_order: int,
        labels: dict[str, str],
        descriptions: dict[str, str],
        question_templates: dict[str, str],
        target_agent_version: str | None,
        jump_path: str | None = None,
        kind: str = BUTTON_KIND,
        image_url: str | None = None,
        created_by: str,
    ) -> Shortcut:
        shortcut_id = "sct_" + uuid.uuid4().hex
        now = _iso(datetime.now(UTC))
        payload = {
            "labels": labels,
            "descriptions": descriptions,
            "question_templates": question_templates,
            "target_agent_version": target_agent_version,
            "jump_path": jump_path,
        }
        with self._connection(write=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO shortcuts (
                        shortcut_id, tenant_id, business_entry, code, intent,
                        requires_order, sort_order, status, revision, fields_json,
                        published_version, kind, image_url, created_by, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', 1, ?, NULL, ?, ?, ?, ?, ?)
                    """,
                    (
                        shortcut_id,
                        tenant_id,
                        business_entry,
                        code,
                        intent,
                        int(requires_order),
                        sort_order,
                        _json(payload),
                        kind,
                        image_url,
                        created_by,
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ShortcutConflict(
                    f"shortcut code already exists in this tenant and entry: {code}"
                ) from exc
        return self.get(shortcut_id, tenant_id)

    def get(self, shortcut_id: str, tenant_id: str) -> Shortcut:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM shortcuts WHERE shortcut_id = ? AND tenant_id = ?",
                (shortcut_id, tenant_id),
            ).fetchone()
        if row is None:
            raise ShortcutNotFound("shortcut not found")
        return _shortcut_from_row(row)

    def find_by_code(self, tenant_id: str, business_entry: str, code: str) -> Shortcut | None:
        """Locator for tests/imports; the HTTP listing uses list_published."""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE tenant_id = ? AND business_entry = ? AND code = ?
                """,
                (tenant_id, business_entry, code),
            ).fetchone()
        return _shortcut_from_row(row) if row is not None else None

    def list_all(self, tenant_id: str, business_entry: str) -> list[Shortcut]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE tenant_id = ? AND business_entry = ?
                ORDER BY sort_order, code
                """,
                (tenant_id, business_entry),
            ).fetchall()
        return [_shortcut_from_row(row) for row in rows]

    def list_published(self, tenant_id: str, business_entry: str) -> list[Shortcut]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE tenant_id = ? AND business_entry = ? AND status = 'published'
                ORDER BY sort_order, code
                """,
                (tenant_id, business_entry),
            ).fetchall()
        return [_shortcut_from_row(row) for row in rows]

    def list_published_for_entry(self, business_entry: str) -> list[Shortcut]:
        """List published tenant rows for migration/admin inspection only."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM shortcuts
                WHERE business_entry = ? AND status = 'published' AND tenant_id != ?
                ORDER BY business_entry, code, created_at, tenant_id
                """,
                (business_entry, PLATFORM_TENANT_ID),
            ).fetchall()
        return [_shortcut_from_row(row) for row in rows]

    def list_effective(
        self, tenant_id: str, business_entry: str, *, kind: str | None = None
    ) -> list[Shortcut]:
        """Resolve the published platform defaults and tenant rows.

        A published tenant row replaces the platform row with the same code.
        A disabled tenant row suppresses that code; drafts do not affect the
        effective result. The merge is the single seam shared by listing and
        shortcut execution.

        ``kind`` narrows the result to one surface (``button`` / ``banner``).
        The split is enforced HERE rather than in a response filter, so a
        banner row cannot reach the button listing by any route — including
        through the merge, where a tenant override could otherwise change a
        row's surface.
        """
        if tenant_id == PLATFORM_TENANT_ID:
            raise ShortcutValidationError("tenant id is reserved")
        if kind is not None and kind not in SHORTCUT_KINDS:
            raise ShortcutValidationError("kind is invalid")
        platform_rows = self.list_published(PLATFORM_TENANT_ID, business_entry)
        tenant_rows = self.list_all(tenant_id, business_entry)
        effective = {row.code: row for row in platform_rows}
        for row in tenant_rows:
            if row.status == "published":
                effective[row.code] = row
            elif row.status == "disabled":
                effective.pop(row.code, None)
        rows = [row for row in effective.values() if kind is None or row.kind == kind]
        return sorted(rows, key=lambda row: (row.sort_order, row.code))

    def find_effective_by_code(self, tenant_id: str, business_entry: str, code: str) -> Shortcut | None:
        return next((row for row in self.list_effective(tenant_id, business_entry) if row.code == code), None)

    def update(
        self,
        shortcut_id: str,
        tenant_id: str,
        expected_revision: int,
        *,
        intent: str,
        requires_order: bool,
        sort_order: int,
        labels: dict[str, str],
        descriptions: dict[str, str],
        question_templates: dict[str, str],
        target_agent_version: str | None,
        jump_path: str | None = None,
        image_url: str | None = None,
    ) -> Shortcut:
        # `kind` is deliberately NOT a parameter: a row does not become a
        # different kind of resource. Letting an edit move a row between
        # surfaces would make "which endpoint serves this" mutable config,
        # which is the implicit-input shape the explicit column exists to end.
        now = _iso(datetime.now(UTC))
        payload = {
            "labels": labels,
            "descriptions": descriptions,
            "question_templates": question_templates,
            "target_agent_version": target_agent_version,
            "jump_path": jump_path,
        }
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts
                SET intent = ?, requires_order = ?, sort_order = ?, fields_json = ?,
                    image_url = ?, revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'draft' AND revision = ?
                """,
                (
                    intent,
                    int(requires_order),
                    sort_order,
                    _json(payload),
                    image_url,
                    now,
                    shortcut_id,
                    tenant_id,
                    expected_revision,
                ),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def fork_draft(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> Shortcut:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts SET status = 'draft', revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'published' AND revision = ?
                """,
                (now, shortcut_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def publish(
        self,
        shortcut_id: str,
        tenant_id: str,
        expected_revision: int,
        snapshot: dict[str, Any],
        published_by: str,
    ) -> ShortcutVersion:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            row = connection.execute(
                "SELECT status, revision FROM shortcuts WHERE shortcut_id = ? AND tenant_id = ?",
                (shortcut_id, tenant_id),
            ).fetchone()
            if row is None:
                raise ShortcutNotFound("shortcut not found")
            if row["status"] != "draft" or int(row["revision"]) != expected_revision:
                raise ShortcutConflict("shortcut revision or state changed")
            current = connection.execute(
                """
                SELECT COALESCE(MAX(version_no), 0) AS version_no
                FROM shortcut_versions WHERE shortcut_id = ?
                """,
                (shortcut_id,),
            ).fetchone()
            version_no = int(current["version_no"]) + 1
            connection.execute(
                """
                INSERT INTO shortcut_versions
                    (shortcut_id, tenant_id, version_no, snapshot_json, published_by, published_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (shortcut_id, tenant_id, version_no, _json(snapshot), published_by, now),
            )
            connection.execute(
                """
                UPDATE shortcuts
                SET status = 'published', published_version = ?, revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND revision = ?
                """,
                (version_no, now, shortcut_id, tenant_id, expected_revision),
            )
        return ShortcutVersion(shortcut_id, tenant_id, version_no, snapshot, published_by, now)

    def disable(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> Shortcut:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts SET status = 'disabled', revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'published' AND revision = ?
                """,
                (now, shortcut_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def enable(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> Shortcut:
        now = _iso(datetime.now(UTC))
        with self._connection(write=True) as connection:
            updated = connection.execute(
                """
                UPDATE shortcuts SET status = 'published', revision = revision + 1, updated_at = ?
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'disabled' AND revision = ?
                """,
                (now, shortcut_id, tenant_id, expected_revision),
            )
            if updated.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)
        return self.get(shortcut_id, tenant_id)

    def delete_draft(self, shortcut_id: str, tenant_id: str, expected_revision: int) -> None:
        with self._connection(write=True) as connection:
            deleted = connection.execute(
                """
                DELETE FROM shortcuts
                WHERE shortcut_id = ? AND tenant_id = ? AND status = 'draft'
                  AND published_version IS NULL AND revision = ?
                """,
                (shortcut_id, tenant_id, expected_revision),
            )
            if deleted.rowcount != 1:
                self._raise_update_error(connection, shortcut_id, tenant_id, expected_revision)

    def version(self, shortcut_id: str, tenant_id: str, version_no: int) -> ShortcutVersion:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM shortcut_versions
                WHERE shortcut_id = ? AND tenant_id = ? AND version_no = ?
                """,
                (shortcut_id, tenant_id, version_no),
            ).fetchone()
        if row is None:
            raise ShortcutNotFound("shortcut version not found")
        return _version_from_row(row)

    @staticmethod
    def _raise_update_error(
        connection: sqlite3.Connection, shortcut_id: str, tenant_id: str, revision: int
    ) -> None:
        row = connection.execute(
            "SELECT revision FROM shortcuts WHERE shortcut_id = ? AND tenant_id = ?",
            (shortcut_id, tenant_id),
        ).fetchone()
        if row is None:
            raise ShortcutNotFound("shortcut not found")
        raise ShortcutConflict(f"shortcut revision conflict: expected {revision}")

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS shortcuts (
                    shortcut_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    business_entry TEXT NOT NULL,
                    code TEXT NOT NULL,
                    intent TEXT NOT NULL,
                    requires_order INTEGER NOT NULL,
                    sort_order INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    fields_json TEXT NOT NULL,
                    published_version INTEGER,
                    kind TEXT NOT NULL DEFAULT 'button',
                    image_url TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(tenant_id, business_entry, code)
                );
                CREATE INDEX IF NOT EXISTS idx_shortcuts_listing
                    ON shortcuts(tenant_id, business_entry, status, sort_order);
                CREATE TABLE IF NOT EXISTS shortcut_versions (
                    shortcut_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    version_no INTEGER NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    published_by TEXT NOT NULL,
                    published_at TEXT NOT NULL,
                    PRIMARY KEY(shortcut_id, version_no),
                    FOREIGN KEY(shortcut_id) REFERENCES shortcuts(shortcut_id) ON DELETE CASCADE
                );
                """
            )
            # No migration framework in this repo: an existing database gets
            # the columns added in place. Two separate additions because they
            # arrived in the same release but one is NOT NULL, and a NOT NULL
            # column must carry its default for the existing rows to be valid.
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(shortcuts)").fetchall()}
            if "kind" not in columns:
                connection.execute(
                    f"ALTER TABLE shortcuts ADD COLUMN kind TEXT NOT NULL DEFAULT '{BUTTON_KIND}'"
                )
            if "image_url" not in columns:
                connection.execute("ALTER TABLE shortcuts ADD COLUMN image_url TEXT")
        protect_private_file(self.path)

    @contextmanager
    def _connection(self, *, write: bool = False):
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except Exception:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()


class ShortcutManager:
    def __init__(self, store: ShortcutStore) -> None:
        self.store = store

    def create(self, context: Any, payload: dict[str, Any], *, scope: str = TENANT_SCOPE) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        fields = self._validated_fields(payload, for_publish=False)
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        return self.store.create(
            tenant_id,
            _entry(payload.get("business_entry")),
            _code(payload.get("code")),
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
            kind=fields["kind"],
            image_url=fields["image_url"],
            created_by=_actor(context),
        )

    def list(self, context: Any, *, business_entry: str | None, scope: str = TENANT_SCOPE) -> list[Shortcut]:
        """Management listing (all statuses) for the caller's tenant+entry."""
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        return self.store.list_all(tenant_id, _entry(business_entry))

    def list_published(self, context: Any, *, business_entry: str) -> list[Shortcut]:
        """Public listing — authenticated callers, roles NOT required (#230).

        Any caller that passed the endpoint's auth scope may read the entry's
        published shortcuts (they render the product home). Only drafts,
        disabled rows, other tenants, and other entries are withheld.
        """
        entry = (business_entry or "").strip().lower()
        if entry not in {"consumer", "operator"}:
            raise ShortcutValidationError("business_entry is invalid")
        return self.store.list_published(context.effective_tenant_id, entry)

    def list_effective(
        self, context: Any, *, business_entry: str, kind: str | None = None
    ) -> list[Shortcut]:
        entry = (business_entry or "").strip().lower()
        if entry not in {"consumer", "operator"}:
            raise ShortcutValidationError("business_entry is invalid")
        return self.store.list_effective(context.effective_tenant_id, entry, kind=kind)

    def get(self, context: Any, shortcut_id: str, *, scope: str = TENANT_SCOPE) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        return self.store.get(_id(shortcut_id), tenant_id)

    def update(
        self, context: Any, shortcut_id: str, payload: dict[str, Any], *, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        # Absent ``jump_path`` means "leave it alone", not "clear it". The HTTP
        # request model defaults the field to None, so an unchanged edit would
        # otherwise silently demote a jump action to a prompt action — turning
        # the one field that discriminates the two into collateral damage of an
        # unrelated label edit. Explicit null clears it; both spellings go
        # through the single _jump_path validator so there is one normalizer.
        current = self.store.get(_id(shortcut_id), tenant_id)
        fields = self._validated_fields(
            {
                **payload,
                "jump_path": payload.get("jump_path") or current.jump_path,
                # Same "absent means leave it alone" rule as jump_path, and for
                # the same reason: the HTTP model defaults to None, so an
                # unrelated label edit would otherwise wipe the banner's
                # fallback image. `kind` is not in this merge because it is not
                # editable at all — see the store's update().
                "image_url": payload.get("image_url") or current.image_url,
                "kind": current.kind,
            },
            for_publish=False,
        )
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        return self.store.update(
            _id(shortcut_id),
            tenant_id,
            int(payload.get("expected_revision") or 0),
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
            image_url=fields["image_url"],
        )

    def publish(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, PUBLISH_ROLES)
        shortcut = self.store.get(_id(shortcut_id), tenant_id)
        if shortcut.revision != expected_revision or shortcut.status != "draft":
            raise ShortcutConflict("shortcut revision or state changed")
        snapshot = shortcut.to_dict()
        return self.store.publish(
            shortcut.shortcut_id,
            tenant_id,
            expected_revision,
            snapshot,
            _actor(context),
        )

    def fork_draft(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        return self.store.fork_draft(_id(shortcut_id), tenant_id, expected_revision)

    def suppress(self, context: Any, *, business_entry: str, code: str) -> Shortcut:
        """Publish a tenant-only disabled override for a platform action."""
        tenant_id = self._scope_tenant(context, TENANT_SCOPE, PUBLISH_ROLES)
        entry = _entry(business_entry)
        stable_code = _code(code)
        existing = self.store.find_by_code(tenant_id, entry, stable_code)
        if existing is not None:
            if existing.status == "disabled":
                return existing
            if existing.status != "published":
                raise ShortcutConflict("shortcut override is not published")
            return self.store.disable(existing.shortcut_id, tenant_id, existing.revision)
        platform = self.store.find_by_code(PLATFORM_TENANT_ID, entry, stable_code)
        if platform is None or platform.status != "published":
            raise ShortcutNotFound("platform shortcut not found")
        created = self.store.create(
            tenant_id,
            entry,
            stable_code,
            intent=platform.intent,
            requires_order=platform.requires_order,
            sort_order=platform.sort_order,
            labels=platform.labels,
            descriptions=platform.descriptions,
            question_templates=platform.question_templates,
            target_agent_version=None,
            jump_path=platform.jump_path,
            # The override must land on the SAME surface as the row it
            # shadows. Suppressing a banner by creating a button here would
            # both fail to hide the banner and inject a stray button.
            kind=platform.kind,
            image_url=platform.image_url,
            created_by=_actor(context),
        )
        self.publish(context, created.shortcut_id, expected_revision=created.revision)
        published = self.store.get(created.shortcut_id, tenant_id)
        return self.store.disable(published.shortcut_id, tenant_id, published.revision)

    def restore(self, context: Any, *, business_entry: str, code: str) -> Shortcut:
        """Restore the previous tenant override after a tenant suppression."""
        tenant_id = self._scope_tenant(context, TENANT_SCOPE, PUBLISH_ROLES)
        existing = self.store.find_by_code(tenant_id, _entry(business_entry), _code(code))
        if existing is None:
            raise ShortcutNotFound("tenant shortcut override not found")
        if existing.status != "disabled":
            return existing
        return self.store.enable(existing.shortcut_id, tenant_id, existing.revision)

    def rollback(
        self,
        context: Any,
        shortcut_id: str,
        *,
        version_no: int,
        expected_revision: int,
        scope: str = TENANT_SCOPE,
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        if version_no < 1:
            raise ShortcutValidationError("version_no must be positive")
        current = self.store.get(_id(shortcut_id), tenant_id)
        if current.status != "published" or current.revision != expected_revision:
            raise ShortcutConflict("shortcut revision or state changed")
        target = self.store.version(current.shortcut_id, tenant_id, version_no)
        fields = self._validated_fields(target.snapshot, for_publish=False)
        if scope == PLATFORM_SCOPE and fields["target_agent_version"] is not None:
            raise ShortcutValidationError("platform shortcuts cannot bind a tenant agent version")
        draft = self.store.fork_draft(current.shortcut_id, tenant_id, expected_revision)
        updated = self.store.update(
            draft.shortcut_id,
            tenant_id,
            draft.revision,
            intent=fields["intent"],
            requires_order=fields["requires_order"],
            sort_order=fields["sort_order"],
            labels=fields["labels"],
            descriptions=fields["descriptions"],
            question_templates=fields["question_templates"],
            target_agent_version=fields["target_agent_version"],
            jump_path=fields["jump_path"],
            image_url=fields["image_url"],
        )
        return self.publish(context, updated.shortcut_id, expected_revision=updated.revision, scope=scope)

    def disable(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> Shortcut:
        tenant_id = self._scope_tenant(context, scope, PUBLISH_ROLES)
        return self.store.disable(_id(shortcut_id), tenant_id, expected_revision)

    def delete(
        self, context: Any, shortcut_id: str, *, expected_revision: int, scope: str = TENANT_SCOPE
    ) -> None:
        tenant_id = self._scope_tenant(context, scope, EDIT_ROLES)
        self.store.delete_draft(_id(shortcut_id), tenant_id, expected_revision)

    def version(
        self, context: Any, shortcut_id: str, version_no: int, *, scope: str = TENANT_SCOPE
    ) -> ShortcutVersion:
        tenant_id = self._scope_tenant(context, scope, VIEW_ROLES)
        if version_no < 1:
            raise ShortcutValidationError("version_no must be positive")
        return self.store.version(_id(shortcut_id), tenant_id, version_no)

    @staticmethod
    def _scope_tenant(context: Any, scope: str, roles: frozenset[str]) -> str:
        if scope not in SHORTCUT_SCOPES:
            raise ShortcutValidationError("scope is invalid")
        if scope == PLATFORM_SCOPE:
            ShortcutManager._require(context, roles | PLATFORM_ROLES)
            if not frozenset(getattr(context, "roles", ())).intersection(PLATFORM_ROLES):
                raise ShortcutForbidden("platform shortcut access is not permitted")
            return PLATFORM_TENANT_ID
        ShortcutManager._require(context, roles)
        if getattr(context, "effective_tenant_id", "") == PLATFORM_TENANT_ID:
            raise ShortcutValidationError("tenant id is reserved")
        return context.effective_tenant_id

    @staticmethod
    def _require(context: Any, roles: frozenset[str]) -> None:
        effective_tenant = getattr(context, "effective_tenant_id", "")
        effective_roles = frozenset(getattr(context, "roles", ()))
        if not effective_tenant or not effective_roles.intersection(roles):
            raise ShortcutForbidden("shortcut access is not permitted")

    def _validated_fields(self, payload: dict[str, Any], *, for_publish: bool) -> dict[str, Any]:
        del for_publish  # publish-time validation equals create/update: same rules
        intent = str(payload.get("intent") or "")
        if intent not in SHORTCUT_INTENTS:
            raise ShortcutValidationError("intent is invalid")
        requires_order = bool(payload.get("requires_order", False))
        if intent in {"order_issue", "smart_diagnosis"} and not requires_order:
            # smart_diagnosis / any order_issue shortcut must force the picker.
            raise ShortcutValidationError("order-related shortcuts must set requires_order")
        sort_order = payload.get("sort_order", 100)
        if not isinstance(sort_order, int) or not 0 <= sort_order <= 9999:
            raise ShortcutValidationError("sort_order is invalid")
        labels = _localized(payload.get("labels"), "labels", require_zh=True)
        descriptions = _localized(payload.get("descriptions"), "descriptions", require_zh=False)
        question_templates = _localized(
            payload.get("question_templates"), "question_templates", require_zh=False
        )
        jump_path = _jump_path(payload.get("jump_path"))
        image_url = _image_url(payload.get("image_url"))
        kind = str(payload.get("kind") or BUTTON_KIND)
        if kind not in SHORTCUT_KINDS:
            raise ShortcutValidationError("kind is invalid")
        if kind == BANNER_KIND and jump_path is None:
            # A banner IS a jump action: the product's banner navigates to the
            # report page. A banner with no route would render a card that does
            # nothing, and it would be creatable only by misconfiguration.
            raise ShortcutValidationError("a banner must set jump_path")
        if kind == BUTTON_KIND and image_url is not None:
            # One discriminator, not two. A button carrying an image would
            # satisfy the "image ⇒ card" rule a client may still hold, so the
            # row would be served as a button by `kind` and rendered as a card
            # by the client. Rejected rather than silently dropped.
            raise ShortcutValidationError("image_url is only allowed for a banner")
        target = payload.get("target_agent_version")
        if target is not None:
            if not isinstance(target, str) or not _AGENT_VERSION.fullmatch(target):
                raise ShortcutValidationError("target_agent_version is invalid")
            if intent not in {"case_exploration", "solution_discovery"}:
                raise ShortcutValidationError("target_agent_version is only allowed for promotional intents")
        if jump_path is not None and target is not None:
            # A jump action never reaches the agent, so a pin would be dead
            # config with a live side effect: the pin is what marks an agent
            # promotional, so it would keep excluding that agent from
            # customer-agent selection for a response nobody ever fetches.
            #
            # Rejected only for NEW configuration. An old published version that
            # carries both must still be rollback-able, so this is enforced in
            # _reject_new_conflicts rather than here.
            raise ShortcutValidationError("jump_path and target_agent_version are mutually exclusive")
        return {
            "intent": intent,
            "requires_order": requires_order,
            "sort_order": sort_order,
            "labels": labels,
            "descriptions": descriptions,
            "question_templates": question_templates,
            "target_agent_version": target,
            "jump_path": jump_path,
            "kind": kind,
            "image_url": image_url,
        }


def _shortcut_from_row(row: sqlite3.Row) -> Shortcut:
    fields = json.loads(str(row["fields_json"]))
    return Shortcut(
        shortcut_id=str(row["shortcut_id"]),
        tenant_id=str(row["tenant_id"]),
        business_entry=str(row["business_entry"]),
        code=str(row["code"]),
        intent=str(row["intent"]),
        requires_order=bool(row["requires_order"]),
        sort_order=int(row["sort_order"]),
        status=str(row["status"]),
        revision=int(row["revision"]),
        labels={str(k): str(v) for k, v in dict(fields.get("labels") or {}).items()},
        descriptions={str(k): str(v) for k, v in dict(fields.get("descriptions") or {}).items()},
        question_templates={str(k): str(v) for k, v in dict(fields.get("question_templates") or {}).items()},
        target_agent_version=fields.get("target_agent_version"),
        jump_path=fields.get("jump_path") or None,
        kind=str(row["kind"]) if row["kind"] is not None else BUTTON_KIND,
        image_url=str(row["image_url"]) if row["image_url"] is not None else None,
        published_version=int(row["published_version"]) if row["published_version"] is not None else None,
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        scope=PLATFORM_SCOPE if str(row["tenant_id"]) == PLATFORM_TENANT_ID else TENANT_SCOPE,
    )


def _version_from_row(row: sqlite3.Row) -> ShortcutVersion:
    return ShortcutVersion(
        shortcut_id=str(row["shortcut_id"]),
        tenant_id=str(row["tenant_id"]),
        version_no=int(row["version_no"]),
        snapshot=json.loads(str(row["snapshot_json"])),
        published_by=str(row["published_by"]),
        published_at=str(row["published_at"]),
    )


_JUMP_PATH_MAX = 512


def _jump_path(value: Any) -> str | None:
    """Validate the optional in-app route for a jump action.

    Only the format the product states is enforced (must start with ``/``).
    There is deliberately NO route allowlist: the repository holds no
    authoritative H5 route convention, so a whitelist here would copy the
    client's router into the backend and need a backend release per new page.
    Whether the page exists is the client's acceptance scope.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShortcutValidationError("jump_path is invalid")
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith("//") or not candidate.startswith("/") or len(candidate) > _JUMP_PATH_MAX:
        # See the HTTP request model for why "//" is rejected: RFC 3986 §4.2
        # makes a leading "//" a network-path reference (an authority, i.e. a
        # host), not a route, so a client navigator could treat it as a jump
        # to another origin. Duplicated here because this validator is also
        # called directly by the operator runbook, which does not go through
        # the HTTP layer.
        raise ShortcutValidationError(
            f"jump_path must be a '/'-prefixed path (not '//') of at most {_JUMP_PATH_MAX} characters"
        )
    return candidate


_IMAGE_URL_MAX = 512


def _image_url(value: Any) -> str | None:
    """Validate the optional banner fallback image.

    Only ``http(s)`` is accepted. What that rules out is the part that
    matters: a ``data:`` payload is an unbounded blob smuggled through config,
    and ``javascript:`` / ``file:`` are the shapes a client navigator or image
    loader turns into an execution or local-read primitive. This is a config
    field an operator fills in, but it is still an external string reaching a
    client, so it is validated at the boundary rather than trusted.

    A third-party CDN URL is explicitly allowed: the car photos already are
    those, and the availability / licensing consequences are recorded in the
    frontend handoff rather than enforced here.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ShortcutValidationError("image_url is invalid")
    candidate = value.strip()
    if not candidate:
        return None
    if len(candidate) > _IMAGE_URL_MAX or not candidate.lower().startswith(("http://", "https://")):
        raise ShortcutValidationError(
            f"image_url must be an http(s) URL of at most {_IMAGE_URL_MAX} characters"
        )
    return candidate


def _localized(value: Any, name: str, *, require_zh: bool) -> dict[str, str]:
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ShortcutValidationError(f"{name} is invalid")
    result: dict[str, str] = {}
    for language, text in value.items():
        if language not in SUPPORTED_LANGUAGES:
            raise ShortcutValidationError(f"{name} uses an unsupported language")
        if not isinstance(text, str) or not text.strip() or len(text) > 500:
            raise ShortcutValidationError(f"{name} text is invalid")
        result[str(language)] = text
    if require_zh and not result.get("zh", "").strip():
        raise ShortcutValidationError(f"{name} must include zh copy")
    return result


def _entry(value: Any) -> str:
    candidate = str(value or "").strip().lower()
    if candidate not in {"consumer", "operator"}:
        raise ShortcutValidationError("business_entry is invalid")
    return candidate


def _code(value: Any) -> str:
    candidate = str(value or "").strip()
    if not SAFE_ID.fullmatch(candidate) or len(candidate) > 64:
        raise ShortcutValidationError("code is invalid")
    return candidate


def _id(value: str) -> str:
    candidate = (value or "").strip()
    if not SAFE_ID.fullmatch(candidate):
        raise ShortcutValidationError("shortcut_id is invalid")
    return candidate


def _actor(context: Any) -> str:
    caller = getattr(context, "caller", None)
    return str(getattr(caller, "b_user_id", "shortcut-admin"))[:128]


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()
