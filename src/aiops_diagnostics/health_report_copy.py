"""Health-report copy, per language (#539).

The charging health report is the one answer surface that had NO language
handling at all: its summary, its curve names and every stop-reason string were
hard-coded Chinese literals, so every reader — including the consumer entry —
got Chinese regardless of `Accept-Language`.

What is translated here is only what WE author:

- the summary sentence and the curve series names;
- the stop-reason text, from two places: the YKC code table (the upstream sends
  a numeric code, we supply the words) and the four fallbacks used when the
  upstream sends nothing.

What is NOT here, deliberately:

- `order.stopped_reason_content` — the upstream's own text. It is DATA describing
  an HTTP-layer reason; translating it would rewrite what the source reported.
  It is passed through verbatim, and this module never sees it.
- The `code` / `status` / `unit` / `reason_code` fields — contract identifiers
  the client renders (standard-api-contract.md §5.4).
"""

from __future__ import annotations

from aiops_diagnostics.i18n import DEFAULT_LANGUAGE

#: The three summary sentences. Keys mirror the branches in
#: ``health_report.build_minimal_health_report``.
HEALTH_SUMMARY_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "normal": "本次充电健康报告包含 1 项正常指标",
        "attention": "本次充电健康报告有 1 项指标需关注",
        "insufficient": "本次充电健康报告有 1 项指标因数据不足无法评估",
    },
    "en": {
        "normal": "This charging health report contains 1 normal indicator",
        "attention": "This charging health report has 1 indicator that needs attention",
        "insufficient": (
            "This charging health report has 1 indicator that could not be assessed for lack of data"
        ),
    },
    "de": {
        "normal": "Dieser Ladegesundheitsbericht enthält 1 normalen Indikator",
        "attention": "In diesem Ladegesundheitsbericht ist 1 Indikator auffällig",
        "insufficient": (
            "In diesem Ladegesundheitsbericht konnte 1 Indikator mangels Daten nicht bewertet werden"
        ),
    },
    "fr": {
        "normal": "Ce rapport de santé de charge contient 1 indicateur normal",
        "attention": "Ce rapport de santé de charge comporte 1 indicateur à surveiller",
        "insufficient": (
            "Dans ce rapport de santé de charge, 1 indicateur n'a pas pu être évalué faute de données"
        ),
    },
    "es": {
        "normal": "Este informe de salud de carga contiene 1 indicador normal",
        "attention": "Este informe de salud de carga tiene 1 indicador que requiere atención",
        "insufficient": "En este informe de salud de carga, 1 indicador no pudo evaluarse por falta de datos",
    },
    "pt": {
        "normal": "Este relatório de saúde de carregamento contém 1 indicador normal",
        "attention": "Este relatório de saúde de carregamento tem 1 indicador que requer atenção",
        "insufficient": (
            "Neste relatório de saúde de carregamento, 1 indicador não pôde ser avaliado por falta de dados"
        ),
    },
}

#: Curve series display names, keyed by the field name the curve is built from.
HEALTH_CURVE_NAMES: dict[str, dict[str, str]] = {
    "zh": {
        "power": "实际功率",
        "outputCurrent": "输出电流",
        "outputVoltage": "输出电压",
        "temperature": "电池温度",
        "batteryMaxTemperature": "最高温度",
        "batteryMinTemperature": "最低温度",
    },
    "en": {
        "power": "Actual power",
        "outputCurrent": "Output current",
        "outputVoltage": "Output voltage",
        "temperature": "Battery temperature",
        "batteryMaxTemperature": "Maximum temperature",
        "batteryMinTemperature": "Minimum temperature",
    },
    "de": {
        "power": "Ist-Leistung",
        "outputCurrent": "Ausgangsstrom",
        "outputVoltage": "Ausgangsspannung",
        "temperature": "Batterietemperatur",
        "batteryMaxTemperature": "Höchsttemperatur",
        "batteryMinTemperature": "Mindesttemperatur",
    },
    "fr": {
        "power": "Puissance réelle",
        "outputCurrent": "Courant de sortie",
        "outputVoltage": "Tension de sortie",
        "temperature": "Température de la batterie",
        "batteryMaxTemperature": "Température maximale",
        "batteryMinTemperature": "Température minimale",
    },
    "es": {
        "power": "Potencia real",
        "outputCurrent": "Corriente de salida",
        "outputVoltage": "Tensión de salida",
        "temperature": "Temperatura de la batería",
        "batteryMaxTemperature": "Temperatura máxima",
        "batteryMinTemperature": "Temperatura mínima",
    },
    "pt": {
        "power": "Potência real",
        "outputCurrent": "Corrente de saída",
        "outputVoltage": "Tensão de saída",
        "temperature": "Temperatura da bateria",
        "batteryMaxTemperature": "Temperatura máxima",
        "batteryMinTemperature": "Temperatura mínima",
    },
}

#: The four fallbacks used when the upstream supplies no stop-reason text.
HEALTH_STOP_FALLBACK_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "manual_stop": "平台或用户主动停止",
        "package_exhausted": "套餐耗尽",
        "start_failure": "启动失败",
        "unknown_stop_reason": "未提供停止原因",
        "ykc_unknown_code": "YKC 停止码 {code}",
    },
    "en": {
        "manual_stop": "Stopped by the platform or the user",
        "package_exhausted": "Package exhausted",
        "start_failure": "Start-up failed",
        "unknown_stop_reason": "No stop reason provided",
        "ykc_unknown_code": "YKC stop code {code}",
    },
    "de": {
        "manual_stop": "Von der Plattform oder dem Nutzer beendet",
        "package_exhausted": "Tarifpaket aufgebraucht",
        "start_failure": "Start fehlgeschlagen",
        "unknown_stop_reason": "Kein Stoppgrund angegeben",
        "ykc_unknown_code": "YKC-Stoppcode {code}",
    },
    "fr": {
        "manual_stop": "Arrêt par la plateforme ou l'utilisateur",
        "package_exhausted": "Forfait épuisé",
        "start_failure": "Échec du démarrage",
        "unknown_stop_reason": "Aucun motif d'arrêt fourni",
        "ykc_unknown_code": "Code d'arrêt YKC {code}",
    },
    "es": {
        "manual_stop": "Detenido por la plataforma o el usuario",
        "package_exhausted": "Paquete agotado",
        "start_failure": "Fallo de arranque",
        "unknown_stop_reason": "No se indicó el motivo de parada",
        "ykc_unknown_code": "Código de parada YKC {code}",
    },
    "pt": {
        "manual_stop": "Parado pela plataforma ou pelo utilizador",
        "package_exhausted": "Pacote esgotado",
        "start_failure": "Falha no arranque",
        "unknown_stop_reason": "Motivo de paragem não indicado",
        "ykc_unknown_code": "Código de paragem YKC {code}",
    },
}

#: The YKC stop-code table, per language, keyed by numeric code as a string.
#:
#: These are OUR words describing the upstream's codes — the upstream sends only
#: the number — so they are ours to translate. 33 codes × 6 languages.
YKC_STOP_REASON_MESSAGES: dict[str, dict[str, str]] = {
    "zh": {
        "64": "APP 远程停止",
        "65": "SOC 达到 100%",
        "66": "充电电量满足设定条件",
        "67": "充电金额满足设定条件",
        "68": "充电时间满足设定条件",
        "69": "手动停止充电",
        "70": "车端正常主动停止",
        "74": "启动失败：充电桩控制系统故障",
        "75": "启动失败：控制导引断开",
        "76": "启动失败：断路器跳位",
        "77": "启动失败：电表通信中断",
        "78": "启动失败：余额不足",
        "79": "启动失败：充电模块故障",
        "80": "启动失败：急停开入",
        "83": "启动失败：温度异常",
        "85": "启动失败：电子锁异常",
        "87": "启动失败：绝缘异常",
        "88": "启动失败：枪故障",
        "106": "异常中止：系统闭锁",
        "107": "异常中止：导引断开",
        "108": "异常中止：断路器跳位",
        "109": "异常中止：电表通信中断",
        "110": "异常中止：余额不足",
        "113": "异常中止：充电模块故障",
        "114": "异常中止：急停开入",
        "116": "异常中止：温度异常",
        "119": "异常中止：电子锁异常",
        "124": "异常中止：电池组过温",
        "131": "异常中止：充电桩断电",
        "138": "异常中止：设备故障",
        "141": "异常中止：枪故障",
        "142": "异常中止：充电数据异常",
        "144": "未知原因停止",
    },
    "en": {
        "64": "Stopped remotely from the app",
        "65": "SOC reached 100%",
        "66": "Charged energy reached the configured limit",
        "67": "Charged amount reached the configured limit",
        "68": "Charging time reached the configured limit",
        "69": "Charging stopped manually",
        "70": "Stopped normally by the vehicle",
        "74": "Start-up failed: charger control system fault",
        "75": "Start-up failed: control pilot disconnected",
        "76": "Start-up failed: circuit breaker tripped",
        "77": "Start-up failed: meter communication lost",
        "78": "Start-up failed: insufficient balance",
        "79": "Start-up failed: charging module fault",
        "80": "Start-up failed: emergency stop engaged",
        "83": "Start-up failed: abnormal temperature",
        "85": "Start-up failed: electronic lock fault",
        "87": "Start-up failed: insulation fault",
        "88": "Start-up failed: connector fault",
        "106": "Aborted: system interlock",
        "107": "Aborted: pilot disconnected",
        "108": "Aborted: circuit breaker tripped",
        "109": "Aborted: meter communication lost",
        "110": "Aborted: insufficient balance",
        "113": "Aborted: charging module fault",
        "114": "Aborted: emergency stop engaged",
        "116": "Aborted: abnormal temperature",
        "119": "Aborted: electronic lock fault",
        "124": "Aborted: battery pack overtemperature",
        "131": "Aborted: charger power lost",
        "138": "Aborted: equipment fault",
        "141": "Aborted: connector fault",
        "142": "Aborted: abnormal charging data",
        "144": "Stopped for an unknown reason",
    },
    "de": {
        "64": "Aus der App heraus ferngestoppt",
        "65": "SOC hat 100 % erreicht",
        "66": "Geladene Energiemenge hat den eingestellten Wert erreicht",
        "67": "Ladebetrag hat den eingestellten Wert erreicht",
        "68": "Ladezeit hat den eingestellten Wert erreicht",
        "69": "Laden manuell beendet",
        "70": "Vom Fahrzeug normal beendet",
        "74": "Start fehlgeschlagen: Störung der Ladesteuerung",
        "75": "Start fehlgeschlagen: Steuerpilot getrennt",
        "76": "Start fehlgeschlagen: Schutzschalter ausgelöst",
        "77": "Start fehlgeschlagen: Zählerkommunikation unterbrochen",
        "78": "Start fehlgeschlagen: Guthaben nicht ausreichend",
        "79": "Start fehlgeschlagen: Störung des Lademoduls",
        "80": "Start fehlgeschlagen: Not-Aus ausgelöst",
        "83": "Start fehlgeschlagen: Temperatur auffällig",
        "85": "Start fehlgeschlagen: Störung der elektronischen Verriegelung",
        "87": "Start fehlgeschlagen: Isolationsfehler",
        "88": "Start fehlgeschlagen: Störung des Ladesteckers",
        "106": "Abgebrochen: Systemverriegelung",
        "107": "Abgebrochen: Steuerpilot getrennt",
        "108": "Abgebrochen: Schutzschalter ausgelöst",
        "109": "Abgebrochen: Zählerkommunikation unterbrochen",
        "110": "Abgebrochen: Guthaben nicht ausreichend",
        "113": "Abgebrochen: Störung des Lademoduls",
        "114": "Abgebrochen: Not-Aus ausgelöst",
        "116": "Abgebrochen: Temperatur auffällig",
        "119": "Abgebrochen: Störung der elektronischen Verriegelung",
        "124": "Abgebrochen: Überhitzung des Batteriepakets",
        "131": "Abgebrochen: Stromausfall an der Ladestation",
        "138": "Abgebrochen: Gerätestörung",
        "141": "Abgebrochen: Störung des Ladesteckers",
        "142": "Abgebrochen: Ladedaten auffällig",
        "144": "Aus unbekanntem Grund beendet",
    },
    "fr": {
        "64": "Arrêt à distance depuis l'application",
        "65": "SOC atteint 100 %",
        "66": "L'énergie chargée a atteint la limite configurée",
        "67": "Le montant chargé a atteint la limite configurée",
        "68": "La durée de charge a atteint la limite configurée",
        "69": "Charge arrêtée manuellement",
        "70": "Arrêt normal par le véhicule",
        "74": "Échec du démarrage : défaut du système de contrôle de la borne",
        "75": "Échec du démarrage : pilote de contrôle déconnecté",
        "76": "Échec du démarrage : disjoncteur déclenché",
        "77": "Échec du démarrage : communication du compteur perdue",
        "78": "Échec du démarrage : solde insuffisant",
        "79": "Échec du démarrage : défaut du module de charge",
        "80": "Échec du démarrage : arrêt d'urgence enclenché",
        "83": "Échec du démarrage : température anormale",
        "85": "Échec du démarrage : défaut du verrou électronique",
        "87": "Échec du démarrage : défaut d'isolement",
        "88": "Échec du démarrage : défaut du connecteur",
        "106": "Interrompu : verrouillage du système",
        "107": "Interrompu : pilote déconnecté",
        "108": "Interrompu : disjoncteur déclenché",
        "109": "Interrompu : communication du compteur perdue",
        "110": "Interrompu : solde insuffisant",
        "113": "Interrompu : défaut du module de charge",
        "114": "Interrompu : arrêt d'urgence enclenché",
        "116": "Interrompu : température anormale",
        "119": "Interrompu : défaut du verrou électronique",
        "124": "Interrompu : surchauffe du pack de batteries",
        "131": "Interrompu : coupure d'alimentation de la borne",
        "138": "Interrompu : défaut de l'équipement",
        "141": "Interrompu : défaut du connecteur",
        "142": "Interrompu : données de charge anormales",
        "144": "Arrêt pour une raison inconnue",
    },
    "es": {
        "64": "Detenido de forma remota desde la aplicación",
        "65": "SOC alcanzó el 100 %",
        "66": "La energía cargada alcanzó el límite configurado",
        "67": "El importe cargado alcanzó el límite configurado",
        "68": "El tiempo de carga alcanzó el límite configurado",
        "69": "Carga detenida manualmente",
        "70": "Detenido con normalidad por el vehículo",
        "74": "Fallo de arranque: avería del sistema de control del cargador",
        "75": "Fallo de arranque: piloto de control desconectado",
        "76": "Fallo de arranque: interruptor automático disparado",
        "77": "Fallo de arranque: comunicación del contador perdida",
        "78": "Fallo de arranque: saldo insuficiente",
        "79": "Fallo de arranque: avería del módulo de carga",
        "80": "Fallo de arranque: parada de emergencia activada",
        "83": "Fallo de arranque: temperatura anómala",
        "85": "Fallo de arranque: avería del cierre electrónico",
        "87": "Fallo de arranque: fallo de aislamiento",
        "88": "Fallo de arranque: avería del conector",
        "106": "Interrumpido: bloqueo del sistema",
        "107": "Interrumpido: piloto desconectado",
        "108": "Interrumpido: interruptor automático disparado",
        "109": "Interrumpido: comunicación del contador perdida",
        "110": "Interrumpido: saldo insuficiente",
        "113": "Interrumpido: avería del módulo de carga",
        "114": "Interrumpido: parada de emergencia activada",
        "116": "Interrumpido: temperatura anómala",
        "119": "Interrumpido: avería del cierre electrónico",
        "124": "Interrumpido: sobretemperatura del paquete de baterías",
        "131": "Interrumpido: corte de alimentación del cargador",
        "138": "Interrumpido: avería del equipo",
        "141": "Interrumpido: avería del conector",
        "142": "Interrumpido: datos de carga anómalos",
        "144": "Detenido por una razón desconocida",
    },
    "pt": {
        "64": "Parado remotamente a partir da aplicação",
        "65": "SOC atingiu 100 %",
        "66": "A energia carregada atingiu o limite configurado",
        "67": "O montante carregado atingiu o limite configurado",
        "68": "O tempo de carregamento atingiu o limite configurado",
        "69": "Carregamento parado manualmente",
        "70": "Parado normalmente pelo veículo",
        "74": "Falha no arranque: avaria do sistema de controlo do carregador",
        "75": "Falha no arranque: piloto de controlo desligado",
        "76": "Falha no arranque: disjuntor disparado",
        "77": "Falha no arranque: comunicação do contador perdida",
        "78": "Falha no arranque: saldo insuficiente",
        "79": "Falha no arranque: avaria do módulo de carregamento",
        "80": "Falha no arranque: paragem de emergência acionada",
        "83": "Falha no arranque: temperatura anómala",
        "85": "Falha no arranque: avaria do fecho eletrónico",
        "87": "Falha no arranque: falha de isolamento",
        "88": "Falha no arranque: avaria do conector",
        "106": "Interrompido: bloqueio do sistema",
        "107": "Interrompido: piloto desligado",
        "108": "Interrompido: disjuntor disparado",
        "109": "Interrompido: comunicação do contador perdida",
        "110": "Interrompido: saldo insuficiente",
        "113": "Interrompido: avaria do módulo de carregamento",
        "114": "Interrompido: paragem de emergência acionada",
        "116": "Interrompido: temperatura anómala",
        "119": "Interrompido: avaria do fecho eletrónico",
        "124": "Interrompido: sobretemperatura do conjunto de baterias",
        "131": "Interrompido: corte de energia no carregador",
        "138": "Interrompido: avaria do equipamento",
        "141": "Interrompido: avaria do conector",
        "142": "Interrompido: dados de carregamento anómalos",
        "144": "Parado por motivo desconhecido",
    },
}


def _pack(table: dict[str, dict[str, str]], language: str) -> dict[str, str]:
    return table.get(language) or table[DEFAULT_LANGUAGE]


def health_summary(language: str, key: str) -> str:
    """The report's summary sentence for one status branch."""
    pack = _pack(HEALTH_SUMMARY_MESSAGES, language)
    return pack.get(key) or HEALTH_SUMMARY_MESSAGES[DEFAULT_LANGUAGE]["insufficient"]


def curve_name(language: str, field: str) -> str:
    """A curve series display name. An unknown field falls back to the field
    name itself rather than an empty label — a chart legend must say something."""
    return _pack(HEALTH_CURVE_NAMES, language).get(field) or field


def stop_reason_fallback(language: str, classification: str) -> str | None:
    """The fallback text for a classification, or ``None`` when the
    classification has none (most of them do not: the upstream's own text
    usually carries the reason, and we only supply words for the four cases
    where it does not)."""
    pack = _pack(HEALTH_STOP_FALLBACK_MESSAGES, language)
    return pack.get(classification) or HEALTH_STOP_FALLBACK_MESSAGES[DEFAULT_LANGUAGE].get(classification)


def ykc_stop_reason(language: str, code: int, fallback: str) -> str:
    """The description of a YKC stop code. ``fallback`` is the caller's own
    text (upstream content, or the synthesised code label) and is used when the
    code is not in the table — it is never translated."""
    pack = _pack(YKC_STOP_REASON_MESSAGES, language)
    return pack.get(str(code)) or _pack(YKC_STOP_REASON_MESSAGES, DEFAULT_LANGUAGE).get(str(code)) or fallback
