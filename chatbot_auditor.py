"""
Asistente de Auditoría (Chatbot) basado exclusivamente en:
1. Las políticas oficiales del Caso 03 (Comercio Electrónico Jacaranda S. A., D-01 a D-05).
2. Los resultados obtenidos en la Etapa A y Etapa B del lote analizado.

Modos de operación:
- Modo Local Determinista (por defecto, sin API y 100% reproducible/offline).
- Modo LLM Opcional (Gemini API o Ollama local vía HTTP estándar), con inyección
  estricta del contexto del lote y prohibición expresa de inventar datos o
  declarar "fraude confirmado".
"""

from __future__ import annotations

import json
import re
import unicodedata
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

import pandas as pd

from auditoria_motor import (
    NOTA_METODOLOGICA,
    UMBRAL_AUTORIZACION_CRC,
    ResultadoAuditoria,
)

# ---------------------------------------------------------------------------
# Base de conocimiento oficial de políticas y límites del escenario
# ---------------------------------------------------------------------------

BASE_CONOCIMIENTO_POLITICAS: Dict[str, Any] = {
    "empresa": "Comercio Electrónico Jacaranda S. A. (entorno didáctico ficticio)",
    "mision": (
        "Determinar si las devoluciones pagadas corresponden a ventas válidas, "
        "montos autorizables y cuentas de reembolso consistentes; investigar "
        "devoluciones excesivas o no justificadas."
    ),
    "flujo_proceso": [
        "1. Clientes registra a las personas o entidades compradoras y su cuenta de reembolso de referencia (clientes.csv).",
        "2. Ventas conserva el importe de la operación, el canal y su estado final (ventas.csv).",
        "3. Atención al Cliente tramita solicitudes de devolución parcial o total; puede existir más de una devolución legítima para una venta (devoluciones.csv).",
        "4. Los reembolsos de CRC 120,000 o más requieren aprobación previa de supervisión; Tesorería ejecuta el reembolso (autorizaciones_devolucion.csv).",
        "5. Auditoría verifica primero calidad e integridad relacional (Etapa A) y luego aplica las condiciones D-01 a D-05 (Etapa B) antes de concluir si existe un problema.",
    ],
    "politicas": {
        "D-01": {
            "regla_id": "R-D01",
            "titulo": "Integridad y titularidad venta-cliente en reembolsos pagados",
            "condicion_oficial": (
                "Un reembolso PAGADO debe vincularse a una venta y cliente existentes, "
                "con correspondencia del identificador de cliente entre ambas fuentes."
            ),
            "umbral": "0 incumplimientos (100% de coincidencia en devoluciones PAGADA/PAGADO).",
            "campos": "devoluciones(devolucion_id, venta_id, cliente_id, estado), ventas(venta_id, cliente_id), clientes(cliente_id).",
            "calidad_vs_alerta": (
                "Claves nulas/vacías o PK duplicadas son problemas de calidad (Etapa A); "
                "claves bien formadas que apuntan a ventas/clientes inexistentes o a clientes "
                "distintos entre venta y devolución generan alerta empresarial (Etapa B)."
            ),
            "insuficiencia": (
                "No es posible saber con los 4 CSV si una venta ausente pertenece a un periodo "
                "anterior al corte de extracción o si dos cliente_id pertenecen al mismo grupo corporativo."
            ),
        },
        "D-02": {
            "regla_id": "R-D02",
            "titulo": "Techo acumulado de reembolsos pagados por venta",
            "condicion_oficial": (
                "La suma de todos los reembolsos PAGADOS de una misma venta no puede superar "
                "el importe original vendido. Varias devoluciones parciales sí son posibles."
            ),
            "umbral": "SUM(monto_crc) <= monto_total_crc (exceso máximo permitido: CRC 0).",
            "campos": "devoluciones(devolucion_id, venta_id, monto_crc, estado), ventas(venta_id, monto_total_crc).",
            "calidad_vs_alerta": (
                "Uniones sin agregación previa (efecto fan-out), devolucion_id duplicados o "
                "montos <= 0/no numéricos son defectos de calidad; una suma acumulada real "
                "superior a monto_total_crc es una alerta empresarial."
            ),
            "insuficiencia": (
                "No existen líneas de detalle de factura (productos individuales), fletes ni "
                "notas de crédito contables para validar si varias devoluciones parciales "
                "corresponden a artículos distintos o pagos repetidos."
            ),
        },
        "D-03": {
            "regla_id": "R-D03",
            "titulo": "Autorización previa y segregación de funciones",
            "condicion_oficial": (
                "Toda devolución de CRC 120,000 o más requiere autorización APROBADA, "
                "anterior al procesamiento y otorgada por un usuario diferente del ejecutor."
            ),
            "umbral": "monto_crc >= 120,000 (inclusivo); estado='APROBADA'; aprobado_en < procesado_en; aprobador_id != ejecutor_id.",
            "campos": "devoluciones(devolucion_id, monto_crc, procesado_en, ejecutor_id, estado), autorizaciones_devolucion(autorizacion_id, devolucion_id, aprobador_id, aprobado_en, estado).",
            "calidad_vs_alerta": (
                "Fechas/IDs nulos, formatos inválidos o pequeños desfases de reloj (ej. aprobado_en "
                "unos minutos antes de solicitado_en) son hallazgos de calidad; ausencia de autorización, "
                "estado distinto de APROBADA, aprobación posterior/igual al procesamiento o autoaprobación "
                "son alertas empresariales."
            ),
            "insuficiencia": (
                "No existe catálogo de roles/personal que certifique que aprobador_id es supervisor real "
                "ni que dos códigos no pertenezcan a la misma persona física; tampoco existe campo de "
                "monto_autorizado en autorizaciones_devolucion.csv."
            ),
        },
        "D-04": {
            "regla_id": "R-D04",
            "titulo": "Consistencia entre cuenta destino y cuenta registrada del cliente",
            "condicion_oficial": (
                "Los reembolsos deben dirigirse a la cuenta registrada del cliente; "
                "las diferencias deben verificarse antes de considerarse irregulares."
            ),
            "umbral": "100% de coincidencia entre devoluciones.cuenta_destino y clientes.cuenta_reembolso.",
            "campos": "devoluciones(devolucion_id, cliente_id, cuenta_destino, estado), clientes(cliente_id, cuenta_reembolso).",
            "calidad_vs_alerta": (
                "Cuentas nulas o diferencias únicamente de espacios/guiones/mayúsculas se clasifican "
                "en calidad; cuentas funcionalmente distintas generan una alerta de verificación obligatoria "
                "(no irregularidad confirmada)."
            ),
            "insuficiencia": (
                "clientes.csv solo guarda la cuenta vigente al corte sin historial de cambios ni "
                "estados bancarios; un cambio legítimo de cuenta puede producir una diferencia."
            ),
        },
        "D-05": {
            "regla_id": "R-D05",
            "titulo": "Sustento en venta con estado ENTREGADA",
            "condicion_oficial": (
                "En el escenario simplificado, solo ventas ENTREGADA sustentan devoluciones PAGADAS. "
                "Una venta ANULADA con reembolso pagado amerita corroborar la secuencia y posible reversión."
            ),
            "umbral": "0 devoluciones pagadas sobre ventas con estado distinto de ENTREGADA.",
            "campos": "devoluciones(devolucion_id, venta_id, estado, solicitado_en, procesado_en), ventas(venta_id, estado, vendido_en).",
            "calidad_vs_alerta": (
                "ventas.estado nulo o desfase en la sincronización de estados es un problema de calidad; "
                "una devolución pagada sobre venta ANULADA u otro estado no entregado es una alerta empresarial."
            ),
            "insuficiencia": (
                "ventas.csv no incluye fecha de entrega, fecha/motivo de anulación, estado de cobro en "
                "pasarela de pagos, guías de transporte ni actas de ingreso físico a bodega."
            ),
        },
    },
    "limites_escenario": [
        "No se incluyen guías de transporte ni comprobantes logísticos de entrega.",
        "No se incluyen registros de devoluciones físicas ni reingreso a inventario.",
        "No se incluyen estados bancarios ni comprobantes de acreditación o reversión.",
        "No se incluye histórico de cambios de cuenta bancaria ni bitácora de roles de usuarios.",
        "Una diferencia entre registros, ausencia de aprobación o señal estadística es un indicio que requiere corroboración; nunca puede clasificarse automáticamente como 'fraude confirmado'.",
    ],
    "evidencia_corroboracion": [
        "Estados bancarios y comprobantes de transferencia/SINPE que acrediten el destinatario real y la fecha efectiva.",
        "Guías de transporte, confirmaciones de entrega y actas de reingreso físico de mercadería a bodega.",
        "Bitácoras del sistema de clientes (solicitudes firmadas y fecha de cambio de cuenta de reembolso).",
        "Matriz de roles y perfiles de usuario (confirmación de facultades de supervisión y segregación real).",
        "Tickets de Atención al Cliente, notas de crédito contables y comprobantes de anulación/reversión en pasarela de pagos.",
    ],
}


# ---------------------------------------------------------------------------
# Utilidades de búsqueda y normalización de consultas
# ---------------------------------------------------------------------------

def _quitar_acentos(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _extraer_ids_consulta(pregunta: str) -> List[str]:
    # Detecta IDs con prefijos típicos del caso (DV, VT, CLI, AD, SV, AT)
    patron = re.compile(r"\b(?:DV|VT|CLI|AD|SV|AT)[0-9A-Z_-]*\d+\b", re.IGNORECASE)
    encontrados = patron.findall(pregunta)
    # Mantener orden sin duplicados
    vistos = []
    for item in encontrados:
        u = item.upper()
        if u not in vistos:
            vistos.append(u)
    return vistos


def _buscar_expediente_id(res: ResultadoAuditoria, reg_id: str) -> str:
    reg_up = reg_id.upper()
    lineas: List[str] = [f"### Expediente del identificador `{reg_up}` en el lote `{res.nombre_lote}`"]
    encontrado_algo = False

    # 1. Buscar en datos cargados (solo lectura)
    if res.tablas_consulta:
        for tabla, df in res.tablas_consulta.items():
            if df is None or df.empty:
                continue
            cols_id = [
                c for c in df.columns
                if c.endswith("_id") or c in ("cuenta_reembolso", "cuenta_destino")
            ]
            mask = pd.Series(False, index=df.index)
            for c in cols_id:
                mask = mask | (df[c].astype(str).str.strip().str.upper() == reg_up)
            coinc = df[mask]
            if not coinc.empty:
                encontrado_algo = True
                lineas.append(f"**Registros en `{tabla}.csv` ({len(coinc)} coincidencia(s)):**")
                for _, row in coinc.head(5).iterrows():
                    pares = ", ".join(f"`{k}`={row[k]}" for k in df.columns)
                    lineas.append(f"- {pares}")
                if len(coinc) > 5:
                    lineas.append(f"- _(... y {len(coinc) - 5} registro(s) adicionales)_")

    # 2. Buscar en Etapa A (hallazgos de calidad)
    if not res.etapa_a_hallazgos.empty:
        m_a = (
            res.etapa_a_hallazgos["registro_id"].astype(str).str.upper().str.contains(reg_up, na=False)
            | res.etapa_a_hallazgos["detalle"].astype(str).str.upper().str.contains(reg_up, na=False)
        )
        ha = res.etapa_a_hallazgos[m_a]
        if not ha.empty:
            encontrado_algo = True
            lineas.append(f"**Hallazgos de Etapa A (calidad/integridad) asociados ({len(ha)}):**")
            for _, row in ha.iterrows():
                lineas.append(
                    f"- [{row['severidad']}] `{row['tipo_control']}` en `{row['tabla']}.{row['campo']}`: {row['detalle']}"
                )

    # 3. Buscar en Etapa B (alertas empresariales y no evaluables)
    if not res.etapa_b_alertas.empty:
        m_b = (
            res.etapa_b_alertas["devolucion_id"].astype(str).str.upper().str.contains(reg_up, na=False)
            | res.etapa_b_alertas["venta_id"].astype(str).str.upper().str.contains(reg_up, na=False)
            | res.etapa_b_alertas["cliente_id"].astype(str).str.upper().str.contains(reg_up, na=False)
            | res.etapa_b_alertas["autorizacion_id"].astype(str).str.upper().str.contains(reg_up, na=False)
        )
        hb = res.etapa_b_alertas[m_b]
        if not hb.empty:
            encontrado_algo = True
            lineas.append(f"**Alertas / registros no evaluables de Etapa B asociados ({len(hb)}):**")
            for _, row in hb.iterrows():
                lineas.append(
                    f"- `{row['regla_id']}` ({row['categoria']} / {row['subtipo']}): {row['justificacion']} "
                    f"_[Estado: {row['estado_investigacion']}]_"
                )

    # 4. Buscar en señales exploratorias
    if not res.etapa_b_senales_exploratorias.empty:
        m_s = (
            res.etapa_b_senales_exploratorias["venta_id"].astype(str).str.upper().str.contains(reg_up, na=False)
            | res.etapa_b_senales_exploratorias["devoluciones_ids"].astype(str).str.upper().str.contains(reg_up, na=False)
        )
        hs = res.etapa_b_senales_exploratorias[m_s]
        if not hs.empty:
            encontrado_algo = True
            lineas.append(f"**Señales exploratorias asociadas ({len(hs)}):**")
            for _, row in hs.iterrows():
                lineas.append(f"- `{row['tipo_senal']}` ({row['venta_id']}): {row['detalle']}")

    if not encontrado_algo:
        lineas.append(
            f"No se encontraron registros ni hallazgos asociados al identificador `{reg_up}` en el lote cargado."
        )
    elif res.etapa_a_hallazgos.empty or not any("Hallazgos de Etapa A" in l for l in lineas):
        if not any("Alertas / registros" in l for l in lineas):
            lineas.append(
                "**Estado en controles:** Sin alertas empresariales en Etapa B ni defectos de calidad asociados a este ID."
            )

    return "\n".join(lineas)


def _formatear_resumen_ejecutivo(res: ResultadoAuditoria) -> str:
    reglas_aptas = int(res.etapa_a_aptitud["habilitada_etapa_b"].sum())
    n_hall_a = len(res.etapa_a_hallazgos)
    n_alertas_b = int(
        res.etapa_b_alertas["categoria"].isin(["ALERTA_EMPRESARIAL", "ALERTA_VERIFICACION_D04"]).sum()
    )
    n_no_eval_b = int((res.etapa_b_alertas["categoria"] == "NO_EVALUABLE_CALIDAD").sum())
    monto_alerta = int(res.etapa_b_resumen_reglas["monto_en_alerta_crc"].sum())
    n_senales = len(res.etapa_b_senales_exploratorias)

    lineas = [
        f"### Resumen ejecutivo del lote `{res.nombre_lote}`",
        f"- **Fecha de ejecución:** {res.ejecutado_en}",
        f"- **Etapa A (Calidad e Integridad):** {reglas_aptas}/5 reglas declaradas aptas; {n_hall_a} hallazgo(s) de calidad registrados.",
        f"- **Etapa B (Reglas Empresariales):** {n_alertas_b} alerta(s) empresarial(es), {n_no_eval_b} registro(s) no evaluable(s) por calidad, y monto acumulado en alerta de **CRC {monto_alerta:,}**.",
        f"- **Señales exploratorias complementarias:** {n_senales} caso(s) de monitoreo (no violan la política literal).",
        "",
        "**Detalle por regla en Etapa B:**",
    ]
    for _, r in res.etapa_b_resumen_reglas.iterrows():
        lineas.append(
            f"- **{r['regla_id']}** ({r['estado_ejecucion']}): evaluados={r['evaluados']}, "
            f"conformes={r['conformes']}, alertas={r['alertas_empresariales']}, "
            f"no_evaluables={r['no_evaluables_calidad']}, monto_alerta=CRC {int(r['monto_en_alerta_crc']):,}."
        )
    lineas.append("")
    lineas.append(f"_{NOTA_METODOLOGICA}_")
    return "\n".join(lineas)


def _formatear_respuesta_politica(res: ResultadoAuditoria, codigo_pol: str) -> str:
    pol = BASE_CONOCIMIENTO_POLITICAS["politicas"][codigo_pol]
    regla_id = pol["regla_id"]
    apt_row = res.etapa_a_aptitud[res.etapa_a_aptitud["regla_id"] == regla_id].iloc[0]
    res_row = res.etapa_b_resumen_reglas[res.etapa_b_resumen_reglas["regla_id"] == regla_id].iloc[0]
    alertas_regla = res.etapa_b_alertas[res.etapa_b_alertas["regla_id"] == regla_id]

    lineas = [
        f"### Política `{codigo_pol}` / Regla `{regla_id}` — {pol['titulo']}",
        f"- **Condición oficial:** {pol['condicion_oficial']}",
        f"- **Umbral verificable:** {pol['umbral']}",
        f"- **Campos utilizados:** {pol['campos']}",
        f"- **Calidad vs. Alerta empresarial:** {pol['calidad_vs_alerta']}",
        "",
        f"**Resultado en el lote `{res.nombre_lote}`:**",
        f"- **Aptitud (Etapa A):** `{apt_row['estado_aptitud']}` — {apt_row['justificacion_aptitud']}",
        f"- **Ejecución (Etapa B):** `{res_row['estado_ejecucion']}` | Evaluados: {res_row['evaluados']} | "
        f"Conformes: {res_row['conformes']} | Alertas empresariales: {res_row['alertas_empresariales']} | "
        f"No evaluables: {res_row['no_evaluables_calidad']} | Monto en alerta: CRC {int(res_row['monto_en_alerta_crc']):,}.",
    ]
    if not alertas_regla.empty:
        lineas.append(f"- **Primeras alertas/hallazgos detectados ({len(alertas_regla)} total):**")
        for _, a in alertas_regla.head(5).iterrows():
            lineas.append(
                f"  - `{a['devolucion_id']}` (venta `{a['venta_id']}`, cliente `{a['cliente_id']}`): {a['justificacion']}"
            )
        if len(alertas_regla) > 5:
            lineas.append(f"  - _(... y {len(alertas_regla) - 5} registro(s) más; consulte la tabla de Etapa B o el informe PDF)._")
    else:
        if res_row["estado_ejecucion"] == "EJECUTADA":
            lineas.append("- **Alertas registradas:** Ninguna alerta ni registro no evaluable para esta regla.")
    lineas.append(f"- **Límite de evidencia de esta política:** {pol['insuficiencia']}")
    return "\n".join(lineas)


# ---------------------------------------------------------------------------
# Respuesta determinista local (100% offline y reproducible)
# ---------------------------------------------------------------------------

def responder_local_determinista(pregunta: str, res: ResultadoAuditoria) -> str:
    p_raw = pregunta.strip()
    if not p_raw:
        return "Escribe una pregunta sobre el lote analizado, las políticas D-01 a D-05 o algún identificador (por ejemplo, `DV00055` o `VT00191`)."

    p = _quitar_acentos(p_raw)

    # 1. Pregunta sobre fraude confirmado / culpabilidad / dolo
    if any(k in p for k in ("fraude confirmado", "hay fraude", "es fraude", "confirmar fraude", "culpable", "dolo", "robo")):
        n_alertas = int(
            res.etapa_b_alertas["categoria"].isin(["ALERTA_EMPRESARIAL", "ALERTA_VERIFICACION_D04"]).sum()
        )
        return (
            "### Criterio metodológico sobre 'fraude confirmado'\n"
            "De acuerdo con las políticas del Caso 03, **esta herramienta y el análisis de los cuatro CSV NO permiten declarar 'fraude confirmado'** "
            "a partir de alertas de reglas, ausencias de autorización o señales estadísticas sin investigación adicional.\n\n"
            f"- En el lote `{res.nombre_lote}` se detectaron **{n_alertas} alerta(s) empresarial(es)** y "
            f"**{len(res.etapa_a_hallazgos)} hallazgo(s) de calidad (Etapa A)**.\n"
            "- Cada alerta debe tratarse como un **indicio explicable que requiere corroboración** con pruebas externas:\n"
            + "\n".join(f"  - {ev}" for ev in BASE_CONOCIMIENTO_POLITICAS["evidencia_corroboracion"])
        )

    # 2. Consulta específica de uno o varios IDs de registros
    ids_detectados = _extraer_ids_consulta(p_raw)
    if ids_detectados:
        bloques = [_buscar_expediente_id(res, rid) for rid in ids_detectados[:4]]
        return "\n\n---\n\n".join(bloques)

    # 3. Consulta por política específica D-01 a D-05 (o R-D01 a R-D05)
    codigos_mencionados = []
    for num in ("1", "2", "3", "4", "5"):
        if re.search(rf"\b(?:r-)?d[-_ ]?0?{num}\b", p):
            codigos_mencionados.append(f"D-0{num}")
    if not codigos_mencionados:
        if any(k in p for k in ("autorizacion", "120000", "120.000", "120,000", "segregacion", "aprobador", "ejecutor")):
            codigos_mencionados.append("D-03")
        if any(k in p for k in ("cuenta destino", "cuenta registrada", "cuenta de reembolso", "cuenta bancaria")):
            codigos_mencionados.append("D-04")
        if any(k in p for k in ("anulada", "entregada", "estado de la venta", "reversion")):
            codigos_mencionados.append("D-05")
        if any(k in p for k in ("exceso", "techo", "suma de reembolsos", "superar el importe", "parciales")):
            codigos_mencionados.append("D-02")
        if any(k in p for k in ("titularidad", "cliente no coincide", "venta inexistente")):
            codigos_mencionados.append("D-01")

    if codigos_mencionados:
        bloques = [_formatear_respuesta_politica(res, c) for c in dict.fromkeys(codigos_mencionados)]
        return "\n\n---\n\n".join(bloques)

    # 4. Preguntas sobre señales exploratorias / fraccionamiento
    if any(k in p for k in ("fraccionamiento", "splitting", "exploratoria", "senales")):
        df_s = res.etapa_b_senales_exploratorias
        if df_s.empty:
            return (
                f"En el lote `{res.nombre_lote}` **no se detectaron señales exploratorias** de posible fraccionamiento "
                f"(múltiples devoluciones individuales < CRC {UMBRAL_AUTORIZACION_CRC:,} cuya suma por venta sea >= CRC {UMBRAL_AUTORIZACION_CRC:,})."
            )
        lineas = [
            f"### Señales exploratorias en el lote `{res.nombre_lote}` ({len(df_s)} caso(s))",
            "Estas señales **no constituyen incumplimiento literal de D-03** si el umbral se aplica por devolución individual, pero ameritan revisión de monitoreo:",
        ]
        for _, r in df_s.iterrows():
            lineas.append(
                f"- Venta `{r['venta_id']}` (devoluciones `{r['devoluciones_ids']}`): "
                f"{r['cantidad_devoluciones']} devoluciones que suman **CRC {int(r['suma_crc']):,}** — {r['detalle']}"
            )
        return "\n".join(lineas)

    # 5. Preguntas sobre Etapa A: calidad, integridad referencial, cronología, aptitud
    if any(k in p for k in ("etapa a", "calidad", "integridad", "aptitud", "cronolog", "duplicad", "nulo", "esquema", "sha256", "hash")):
        lineas = [
            f"### Diagnóstico de Etapa A (Calidad, Integridad y Aptitud) — `{res.nombre_lote}`",
            "**Resumen de tablas cargadas:**",
        ]
        for _, r in res.etapa_a_resumen_tablas.iterrows():
            lineas.append(
                f"- `{r['archivo']}`: cargada={r['cargada']}, filas={r['filas']}, "
                f"cols_faltantes={r['columnas_faltantes']}, pk_duplicadas={r['pk_duplicadas']}, "
                f"errores_tipo={r['errores_tipo']}, sha256={r['sha256_corto']}"
            )
        lineas.append("\n**Matriz de aptitud hacia Etapa B:**")
        for _, a in res.etapa_a_aptitud.iterrows():
            lineas.append(f"- `{a['regla_id']}`: **{a['estado_aptitud']}** — {a['justificacion_aptitud']}")
        if res.etapa_a_hallazgos.empty:
            lineas.append("\n**Hallazgos de calidad:** No se registraron defectos de calidad ni integridad referencial.")
        else:
            lineas.append(f"\n**Hallazgos de calidad registrados ({len(res.etapa_a_hallazgos)}):**")
            for _, h in res.etapa_a_hallazgos.head(10).iterrows():
                lineas.append(
                    f"- [{h['severidad']}] `{h['tipo_control']}` en `{h['tabla']}.{h['campo']}` "
                    f"(ID: `{h['registro_id']}`): {h['detalle']}"
                )
            if len(res.etapa_a_hallazgos) > 10:
                lineas.append(f"- _(... y {len(res.etapa_a_hallazgos) - 10} hallazgo(s) adicionales)._")
        return "\n".join(lineas)

    # 6. Preguntas sobre alertas de Etapa B
    if any(k in p for k in ("etapa b", "alerta", "incumplimiento", "problema", "anomalia", "hallazgo", "monto en alerta")):
        df_al = res.etapa_b_alertas
        if df_al.empty:
            return (
                f"### Etapa B — Alertas del lote `{res.nombre_lote}`\n"
                "No se generaron alertas empresariales ni registros no evaluables en las reglas ejecutadas.\n\n"
                + _formatear_resumen_ejecutivo(res)
            )
        emp = df_al[df_al["categoria"].isin(["ALERTA_EMPRESARIAL", "ALERTA_VERIFICACION_D04"])]
        noev = df_al[df_al["categoria"] == "NO_EVALUABLE_CALIDAD"]
        lineas = [
            f"### Etapa B — Alertas y registros no evaluables en `{res.nombre_lote}`",
            f"- **Alertas empresariales / verificación:** {len(emp)}",
            f"- **Registros no evaluables por calidad:** {len(noev)}",
            "",
        ]
        for _, a in df_al.head(12).iterrows():
            m_txt = f"CRC {int(a['monto_crc']):,}" if pd.notna(a["monto_crc"]) else "N/D"
            lineas.append(
                f"- `{a['regla_id']}` [{a['categoria']}] | Dev: `{a['devolucion_id']}` | "
                f"Venta: `{a['venta_id']}` | Cliente: `{a['cliente_id']}` | Monto: {m_txt} — {a['justificacion']}"
            )
        if len(df_al) > 12:
            lineas.append(f"- _(... y {len(df_al) - 12} registro(s) adicionales disponibles en el reporte exportable)._")
        return "\n".join(lineas)

    # 7. Limitaciones, evidencia adicional o suficiencia de datos
    if any(k in p for k in ("limitacion", "insuficiente", "evidencia", "documento", "corroborar", "bancario", "guia", "bodega", "siguiente paso", "recomendacion")):
        lineas = [
            "### Limitaciones declaradas y evidencia externa requerida",
            "**Límites del lote y del escenario:**",
            *[f"- {l}" for l in res.limitaciones_declaradas],
            "",
            "**Documentación adicional recomendada para corroborar indicios:**",
            *[f"- {e}" for e in BASE_CONOCIMIENTO_POLITICAS["evidencia_corroboracion"]],
        ]
        return "\n".join(lineas)

    # 8. Preguntas metodológicas del caso (fan-out, devolución parcial, flujo, políticas generales)
    if any(k in p for k in ("fan-out", "fan out", "duplicar", "duplicacion", "union", "join")):
        return (
            "### Prevención de duplicación artificial al unir `ventas.csv` y `devoluciones.csv`\n"
            "Dado que una venta puede tener múltiples devoluciones parciales (cardinalidad `1:N`), unir directamente "
            "`ventas.csv` con `devoluciones.csv` repite `monto_total_crc` por cada devolución. "
            "Para evitar inflar las ventas, el motor:\n"
            "1. Verifica primero en **Etapa A** la unicidad de las claves primarias (`venta_id`, `devolucion_id`, `cliente_id`, `autorizacion_id`).\n"
            "2. En **R-D02**, filtra las devoluciones pagadas, **agrupa y suma por `venta_id` antes de cruzar** (`groupby -> sum -> left join`).\n"
            "3. En **R-D03**, agrupa las autorizaciones por `devolucion_id` para tolerar múltiples registros sin duplicar devoluciones."
        )

    if any(k in p for k in ("flujo", "proceso", "politica", "reglas", "criterio", "todas las reglas")):
        lineas = [
            "### Marco de proceso y políticas de Comercio Electrónico Jacaranda S. A.",
            "**Flujo operativo:**",
            *[f"- {paso}" for paso in BASE_CONOCIMIENTO_POLITICAS["flujo_proceso"]],
            "",
            "**Políticas de monitoreo (D-01 a D-05):**",
        ]
        for cod, info in BASE_CONOCIMIENTO_POLITICAS["politicas"].items():
            lineas.append(f"- **{cod} ({info['regla_id']}):** {info['condicion_oficial']}")
        return "\n".join(lineas)

    # 9. Resumen general del lote
    if any(k in p for k in ("resumen", "resultado", "conclu", "estado", "lote", "cuantas", "total", "hola", "ayuda")):
        return _formatear_resumen_ejecutivo(res)

    # 10. Respuesta por defecto: declara alcance y guía preguntas soportadas
    return (
        f"Mi conocimiento está restringido exclusivamente a las **políticas D-01 a D-05** del Caso 03 y a los "
        f"**resultados obtenidos en el lote `{res.nombre_lote}`**. Si la consulta requiere información externa "
        "(como estados bancarios, guías de transporte, devoluciones físicas o roles de empleados), los datos cargados son insuficientes.\n\n"
        "Puedes preguntarme, por ejemplo:\n"
        "- _\"Dame un resumen ejecutivo del lote\"_\n"
        "- _\"¿Qué encontró la Etapa A de calidad e integridad?\"_\n"
        "- _\"Explícame la política D-03 y sus resultados en este lote\"_\n"
        "- _\"¿Qué alertas salieron en la Etapa B?\"_\n"
        "- _\"¿Qué pasa con el registro DV00055 o VT00191?\"_\n"
        "- _\"¿Esto confirma fraude o qué evidencia adicional se necesita?\"_"
    )


# ---------------------------------------------------------------------------
# Modo LLM Opcional (Gemini API o Ollama local, usando solo librería estándar)
# ---------------------------------------------------------------------------

def construir_contexto_llm(res: ResultadoAuditoria, pregunta: str) -> str:
    ids = _extraer_ids_consulta(pregunta)
    expedientes = [_buscar_expediente_id(res, rid) for rid in ids[:3]] if ids else []

    contexto = {
        "politicas_oficiales": BASE_CONOCIMIENTO_POLITICAS,
        "lote_actual": {
            "nombre_lote": res.nombre_lote,
            "ejecutado_en": res.ejecutado_en,
            "etapa_a_resumen_tablas": res.etapa_a_resumen_tablas.to_dict(orient="records"),
            "etapa_a_aptitud": res.etapa_a_aptitud.to_dict(orient="records"),
            "etapa_a_hallazgos_muestra": res.etapa_a_hallazgos.head(40).to_dict(orient="records"),
            "etapa_a_total_hallazgos": len(res.etapa_a_hallazgos),
            "etapa_b_resumen_reglas": res.etapa_b_resumen_reglas.to_dict(orient="records"),
            "etapa_b_alertas_muestra": res.etapa_b_alertas.head(50).to_dict(orient="records"),
            "etapa_b_total_alertas": len(res.etapa_b_alertas),
            "etapa_b_senales_exploratorias": res.etapa_b_senales_exploratorias.head(30).to_dict(orient="records"),
            "limitaciones_declaradas": res.limitaciones_declaradas,
        },
        "expedientes_ids_consultados": expedientes,
    }
    return json.dumps(contexto, ensure_ascii=False, indent=2)


SYSTEM_PROMPT_LLM = (
    "Eres un analista de datos y auditor en formación asistiendo sobre el Caso 03 "
    "(Comercio Electrónico Jacaranda S. A.). Responde SIEMPRE en español, de forma clara, "
    "rigurosa y verificable, basándote ÚNICAMENTE en el contexto JSON proporcionado "
    "(políticas D-01 a D-05 y resultados reales de Etapa A y Etapa B del lote actual). "
    "REGLAS ESTRICTAS: "
    "1) Nunca inventes registros, montos, aprobaciones ni documentos. "
    "2) Nunca clasifiques ningún hallazgo como 'fraude confirmado'; toda alerta es un indicio que requiere corroboración externa. "
    "3) Si una pregunta requiere información que no existe en los cuatro CSV ni en las políticas, declara explícitamente que los datos son insuficientes. "
    "4) Cita siempre identificadores reales (regla_id, devolucion_id, venta_id, cliente_id, autorizacion_id) cuando correspondan."
)


def responder_con_llm(
    pregunta: str,
    res: ResultadoAuditoria,
    proveedor: str,
    historial: List[Dict[str, str]],
    api_key: str = "",
    modelo: str = "",
    ollama_url: str = "http://localhost:11434",
) -> str:
    contexto_json = construir_contexto_llm(res, pregunta)
    hist_reciente = historial[-6:]
    hist_txt = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in hist_reciente)

    prompt_usuario = (
        f"CONTEXTO OFICIAL (POLÍTICAS + RESULTADOS DEL LOTE):\n{contexto_json}\n\n"
        f"HISTORIAL RECIENTE:\n{hist_txt}\n\n"
        f"PREGUNTA ACTUAL DEL AUDITOR:\n{pregunta}"
    )

    try:
        if proveedor == "gemini":
            if not api_key.strip():
                return "Error de configuración: ingresa una API Key de Gemini o cambia al **Modo Local Determinista (sin API)**."
            mod = modelo.strip() or "gemini-2.5-flash"
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{mod}:generateContent?key={api_key.strip()}"
            body = {
                "system_instruction": {"parts": [{"text": SYSTEM_PROMPT_LLM}]},
                "contents": [{"role": "user", "parts": [{"text": prompt_usuario}]}],
                "generationConfig": {"temperature": 0.1},
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["candidates"][0]["content"]["parts"][0]["text"]

        if proveedor == "ollama":
            mod = modelo.strip() or "llama3.1"
            url = ollama_url.rstrip("/") + "/api/chat"
            body = {
                "model": mod,
                "stream": False,
                "options": {"temperature": 0.1},
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT_LLM},
                    {"role": "user", "content": prompt_usuario},
                ],
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["message"]["content"]

        return responder_local_determinista(pregunta, res)

    except urllib.error.HTTPError as exc:
        return (
            f"No fue posible consultar el proveedor LLM (`HTTP {exc.code}`). "
            "Se muestra la respuesta del **Modo Local Determinista** como respaldo:\n\n"
            + responder_local_determinista(pregunta, res)
        )
    except Exception as exc:
        return (
            f"No fue posible conectar con el proveedor LLM (`{exc}`). "
            "Se muestra la respuesta del **Modo Local Determinista** como respaldo:\n\n"
            + responder_local_determinista(pregunta, res)
        )
