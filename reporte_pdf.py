"""
Generador local de Informe Profesional en PDF para el Caso 03:
Comercio Electrónico Jacaranda S. A. — Ventas y devoluciones digitales.

Utiliza ReportLab (100% Python puro, multiplataforma, sin dependencias nativas)
y construye el PDF completamente en memoria sin alterar los archivos CSV originales.
"""

from __future__ import annotations

import io
from typing import List
from xml.sax.saxutils import escape

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from auditoria_motor import NOTA_METODOLOGICA, ResultadoAuditoria
from chatbot_auditor import BASE_CONOCIMIENTO_POLITICAS

# Paleta corporativa sobria
AZUL_MARINO = colors.HexColor("#1B365D")
AZUL_ACERO = colors.HexColor("#2C5282")
AZUL_SUAVE = colors.HexColor("#EBF4FF")
GRIS_OSCURO = colors.HexColor("#2D3748")
GRIS_TEXTO = colors.HexColor("#4A5568")
GRIS_BORDE = colors.HexColor("#CBD5E0")
GRIS_FILA = colors.HexColor("#F7FAFC")
VERDE_OK = colors.HexColor("#276749")
VERDE_BG = colors.HexColor("#F0FFF4")
AMBAR_WARN = colors.HexColor("#975A16")
AMBAR_BG = colors.HexColor("#FFFFF0")
ROJO_ALERTA = colors.HexColor("#9B2C2C")
ROJO_BG = colors.HexColor("#FFF5F5")


def _limpiar_texto_pdf(texto: object) -> str:
    """
    Escapa caracteres XML para Paragraph y convierte símbolos fuera de WinAnsi
    (como '₡' o flechas Unicode) para garantizar renderizado limpio en cualquier SO.
    """
    if texto is None or (isinstance(texto, float) and pd.isna(texto)):
        return "-"
    s = str(texto)
    reemplazos = {
        "₡": "CRC ",
        "→": "->",
        "←": "<-",
        "—": "-",
        "–": "-",
        "•": "-",
        "“": '"',
        "”": '"',
        "‘": "'",
        "’": "'",
        "≤": "<=",
        "≥": ">=",
        "≠": "!=",
    }
    for orig, dest in reemplazos.items():
        s = s.replace(orig, dest)
    return escape(s)


class _CanvasNumerado(canvas.Canvas):
    """Canvas personalizado con encabezado y pie de página 'Página X de Y'."""

    def __init__(self, *args, nombre_lote: str = "", ejecutado_en: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states: List[dict] = []
        self._nombre_lote = nombre_lote
        self._ejecutado_en = ejecutado_en

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total_paginas = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self._dibujar_marco(total_paginas)
            super().showPage()
        super().save()

    def _dibujar_marco(self, total_paginas: int):
        ancho, alto = letter
        margen = 0.65 * inch
        self.saveState()

        # Encabezado discreto a partir de la página 2
        if self._pageNumber > 1:
            self.setStrokeColor(GRIS_BORDE)
            self.setLineWidth(0.6)
            self.line(margen, alto - 0.55 * inch, ancho - margen, alto - 0.55 * inch)
            self.setFont("Helvetica-Bold", 8)
            self.setFillColor(AZUL_MARINO)
            self.drawString(
                margen,
                alto - 0.48 * inch,
                "Comercio Electrónico Jacaranda S. A. | Informe Técnico de Auditoría de Datos",
            )
            self.setFont("Helvetica", 8)
            self.setFillColor(GRIS_TEXTO)
            self.drawRightString(
                ancho - margen,
                alto - 0.48 * inch,
                f"Lote: {self._nombre_lote}",
            )

        # Pie de página en todas las páginas
        self.setStrokeColor(GRIS_BORDE)
        self.setLineWidth(0.6)
        self.line(margen, 0.55 * inch, ancho - margen, 0.55 * inch)
        self.setFont("Helvetica", 7.5)
        self.setFillColor(GRIS_TEXTO)
        self.drawString(
            margen,
            0.38 * inch,
            f"Generado: {self._ejecutado_en} | Uso analítico y formativo (no constituye declaración de fraude confirmado)",
        )
        self.setFont("Helvetica-Bold", 8)
        self.drawRightString(
            ancho - margen,
            0.38 * inch,
            f"Página {self._pageNumber} de {total_paginas}",
        )
        self.restoreState()


def _construir_estilos():
    base = getSampleStyleSheet()
    estilos = {
        "banner_titulo": ParagraphStyle(
            "banner_titulo",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=17,
            leading=21,
            textColor=colors.white,
            alignment=TA_LEFT,
            spaceAfter=4,
        ),
        "banner_sub": ParagraphStyle(
            "banner_sub",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9.5,
            leading=13,
            textColor=colors.HexColor("#E2E8F0"),
            alignment=TA_LEFT,
        ),
        "h1": ParagraphStyle(
            "h1",
            parent=base["Heading1"],
            fontName="Helvetica-Bold",
            fontSize=13,
            leading=16,
            textColor=AZUL_MARINO,
            spaceBefore=14,
            spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=13,
            textColor=AZUL_ACERO,
            spaceBefore=10,
            spaceAfter=4,
        ),
        "cuerpo": ParagraphStyle(
            "cuerpo",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=9,
            leading=13,
            textColor=GRIS_OSCURO,
            alignment=TA_JUSTIFY,
            spaceAfter=6,
        ),
        "bullet": ParagraphStyle(
            "bullet",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.8,
            leading=12.5,
            textColor=GRIS_OSCURO,
            leftIndent=14,
            firstLineIndent=-8,
            spaceAfter=3.5,
        ),
        "callout": ParagraphStyle(
            "callout",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=12,
            textColor=GRIS_OSCURO,
            alignment=TA_JUSTIFY,
        ),
        "th": ParagraphStyle(
            "th",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=7.8,
            leading=10,
            textColor=colors.white,
            alignment=TA_LEFT,
        ),
        "td": ParagraphStyle(
            "td",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.6,
            leading=10,
            textColor=GRIS_OSCURO,
            alignment=TA_LEFT,
        ),
        "td_num": ParagraphStyle(
            "td_num",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.6,
            leading=10,
            textColor=GRIS_OSCURO,
            alignment=TA_RIGHT,
        ),
        "kpi_val": ParagraphStyle(
            "kpi_val",
            parent=base["Normal"],
            fontName="Helvetica-Bold",
            fontSize=14,
            leading=17,
            textColor=AZUL_MARINO,
            alignment=TA_CENTER,
        ),
        "kpi_lbl": ParagraphStyle(
            "kpi_lbl",
            parent=base["Normal"],
            fontName="Helvetica",
            fontSize=7.5,
            leading=9.5,
            textColor=GRIS_TEXTO,
            alignment=TA_CENTER,
        ),
    }
    return estilos


def _crear_tabla_estilizada(
    encabezados: List[str],
    filas: List[List[str]],
    anchos: List[float],
    estilos: dict,
    cols_numericas: Optional[set] = None,
) -> Table:
    cols_numericas = cols_numericas or set()
    data = [[Paragraph(_limpiar_texto_pdf(h), estilos["th"]) for h in encabezados]]
    for fila in filas:
        row_cells = []
        for idx, val in enumerate(fila):
            st = estilos["td_num"] if idx in cols_numericas else estilos["td"]
            row_cells.append(Paragraph(_limpiar_texto_pdf(val), st))
        data.append(row_cells)

    tabla = Table(data, colWidths=anchos, repeatRows=1)
    cmds = [
        ("BACKGROUND", (0, 0), (-1, 0), AZUL_MARINO),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
        ("GRID", (0, 0), (-1, -1), 0.4, GRIS_BORDE),
        ("LINEBELOW", (0, 0), (-1, 0), 1.0, AZUL_ACERO),
    ]
    for r_idx in range(1, len(data)):
        if r_idx % 2 == 0:
            cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx), GRIS_FILA))
    tabla.setStyle(TableStyle(cmds))
    return tabla


def _generar_narrativa_ejecutiva(res: ResultadoAuditoria) -> str:
    reglas_aptas = int(res.etapa_a_aptitud["habilitada_etapa_b"].sum())
    reglas_omitidas = res.etapa_a_aptitud[~res.etapa_a_aptitud["habilitada_etapa_b"]]["regla_id"].tolist()
    n_hall_a = len(res.etapa_a_hallazgos)
    n_crit_a = int((res.etapa_a_hallazgos["severidad"] == "CRITICO").sum()) if n_hall_a > 0 else 0
    n_alertas_b = int(
        res.etapa_b_alertas["categoria"].isin(["ALERTA_EMPRESARIAL", "ALERTA_VERIFICACION_D04"]).sum()
    )
    n_no_eval_b = int((res.etapa_b_alertas["categoria"] == "NO_EVALUABLE_CALIDAD").sum())
    monto_alerta = int(res.etapa_b_resumen_reglas["monto_en_alerta_crc"].sum())
    n_senales = len(res.etapa_b_senales_exploratorias)

    partes = [
        f"El presente informe documenta la evaluación reproducible del lote <b>{_limpiar_texto_pdf(res.nombre_lote)}</b> "
        f"correspondiente al proceso de Ventas y Devoluciones Digitales de Comercio Electrónico Jacaranda S. A., "
        f"ejecutada el {_limpiar_texto_pdf(res.ejecutado_en)} en modo estrictamente de solo lectura."
    ]

    if reglas_aptas == 5:
        partes.append(
            f"En la <b>Etapa A (Calidad, Integridad Referencial y Aptitud)</b>, las 5 reglas de negocio fueron declaradas "
            f"aptas para ejecución, registrándose {n_hall_a} hallazgo(s) técnico(s) de calidad ({n_crit_a} crítico(s))."
        )
    else:
        partes.append(
            f"En la <b>Etapa A</b>, únicamente {reglas_aptas} de 5 reglas cumplieron las condiciones mínimas de aptitud; "
            f"las reglas {', '.join(reglas_omitidas)} fueron omitidas de la Etapa B por insuficiencia de datos o riesgo de "
            f"duplicación de claves, registrándose {n_hall_a} hallazgo(s) de calidad ({n_crit_a} crítico(s))."
        )

    if n_alertas_b == 0 and reglas_aptas == 5:
        partes.append(
            f"En la <b>Etapa B (Reglas Empresariales D-01 a D-05)</b>, no se identificaron alertas empresariales "
            f"sobre las operaciones evaluadas ({n_no_eval_b} registro(s) no evaluable(s) por calidad). "
            f"De forma complementaria, se documentaron {n_senales} señal(es) exploratoria(s) de monitoreo."
        )
    else:
        partes.append(
            f"En la <b>Etapa B</b>, se identificaron <b>{n_alertas_b} alerta(s) empresarial(es)</b> con una exposición "
            f"acumulada de <b>CRC {monto_alerta:,}</b>, adicionalmente a {n_no_eval_b} registro(s) clasificado(s) como no "
            f"evaluable(s) por calidad y {n_senales} señal(es) exploratoria(s). Cada alerta representa un indicio técnico "
            f"que exige corroboración documental externa antes de emitir juicios concluyentes."
        )

    return " ".join(partes)


def exportar_reporte_pdf(res: ResultadoAuditoria) -> bytes:
    """
    Genera un informe técnico profesional en PDF (bytes en memoria).
    """
    buffer = io.BytesIO()
    margen = 0.65 * inch
    ancho_util = letter[0] - 2 * margen

    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=margen,
        rightMargin=margen,
        topMargin=0.75 * inch,
        bottomMargin=0.72 * inch,
        title=f"Informe de Auditoría - {res.nombre_lote}",
        author="Herramienta Local de Auditoría - Caso 03",
    )

    st = _construir_estilos()
    story = []

    # -----------------------------------------------------------------------
    # 1. Portada / Encabezado Ejecutivo
    # -----------------------------------------------------------------------
    banner = Table(
        [
            [
                Paragraph(
                    f"Informe Técnico de Auditoría de Datos<br/>Lote: {_limpiar_texto_pdf(res.nombre_lote)}",
                    st["banner_titulo"],
                )
            ],
            [
                Paragraph(
                    f"Comercio Electrónico Jacaranda S. A. | Caso 03: Ventas y devoluciones digitales | "
                    f"Ejecutado el {_limpiar_texto_pdf(res.ejecutado_en)}",
                    st["banner_sub"],
                )
            ],
        ],
        colWidths=[ancho_util],
    )
    banner.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), AZUL_MARINO),
                ("LEFTPADDING", (0, 0), (-1, -1), 14),
                ("RIGHTPADDING", (0, 0), (-1, -1), 14),
                ("TOPPADDING", (0, 0), (0, 0), 14),
                ("BOTTOMPADDING", (0, 0), (0, 0), 4),
                ("TOPPADDING", (0, 1), (0, 1), 2),
                ("BOTTOMPADDING", (0, 1), (0, 1), 12),
            ]
        )
    )
    story.append(banner)
    story.append(Spacer(1, 10))

    # Aviso metodológico destacado
    aviso_tbl = Table(
        [[Paragraph(_limpiar_texto_pdf(NOTA_METODOLOGICA), st["callout"])]],
        colWidths=[ancho_util],
    )
    aviso_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), AMBAR_BG),
                ("BOX", (0, 0), (-1, -1), 0.8, AMBAR_WARN),
                ("LINEBEFORE", (0, 0), (0, -1), 3.5, AMBAR_WARN),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]
        )
    )
    story.append(aviso_tbl)
    story.append(Spacer(1, 10))

    # Tarjetas KPI
    reglas_aptas = int(res.etapa_a_aptitud["habilitada_etapa_b"].sum())
    n_hall_a = len(res.etapa_a_hallazgos)
    n_alertas_b = int(
        res.etapa_b_alertas["categoria"].isin(["ALERTA_EMPRESARIAL", "ALERTA_VERIFICACION_D04"]).sum()
    )
    n_no_eval_b = int((res.etapa_b_alertas["categoria"] == "NO_EVALUABLE_CALIDAD").sum())
    monto_alerta = int(res.etapa_b_resumen_reglas["monto_en_alerta_crc"].sum())

    kpis = [
        (f"{reglas_aptas} / 5", "Reglas Aptas\n(Etapa A)"),
        (str(n_hall_a), "Hallazgos Calidad\n(Etapa A)"),
        (str(n_alertas_b), "Alertas Reglas\n(Etapa B)"),
        (str(n_no_eval_b), "No Evaluables\n(Calidad)"),
        (f"CRC {monto_alerta:,}", "Monto en Alerta\n(Etapa B)"),
    ]
    kpi_row = [
        [Paragraph(_limpiar_texto_pdf(v), st["kpi_val"]), Paragraph(_limpiar_texto_pdf(lbl), st["kpi_lbl"])]
        for v, lbl in kpis
    ]
    kpi_cards = []
    card_w = ancho_util / 5.0
    for val_p, lbl_p in kpi_row:
        sub = Table([[val_p], [lbl_p]], colWidths=[card_w - 6])
        sub.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), AZUL_SUAVE),
                    ("BOX", (0, 0), (-1, -1), 0.6, GRIS_BORDE),
                    ("TOPPADDING", (0, 0), (-1, 0), 7),
                    ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ]
            )
        )
        kpi_cards.append(sub)
    kpi_grid = Table([kpi_cards], colWidths=[card_w] * 5)
    kpi_grid.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3)]))
    story.append(kpi_grid)
    story.append(Spacer(1, 10))

    # Síntesis ejecutiva
    story.append(Paragraph("1. Resumen Ejecutivo del Análisis", st["h1"]))
    story.append(Paragraph(_generar_narrativa_ejecutiva(res), st["cuerpo"]))

    # -----------------------------------------------------------------------
    # 2. Marco Metodológico y Políticas Evaluadas
    # -----------------------------------------------------------------------
    story.append(Paragraph("2. Marco Metodológico y Políticas de Control", st["h1"]))
    story.append(
        Paragraph(
            "La evaluación aplica un diseño estrictamente bifásico y reproducible sobre copias en memoria de los "
            "cuatro archivos CSV del lote (clientes.csv, ventas.csv, devoluciones.csv y autorizaciones_devolucion.csv). "
            "Primero, la <b>Etapa A</b> verifica la estructura exacta de columnas, tipos de dato, completitud, unicidad de "
            "claves primarias, integridad referencial y cronología técnica, actuando como compuerta de aptitud. "
            "Solo cuando los datos son suficientes y no existe riesgo de duplicación artificial (efecto fan-out), "
            "la <b>Etapa B</b> ejecuta las cinco reglas empresariales derivadas de las políticas D-01 a D-05.",
            st["cuerpo"],
        )
    )

    pol_filas = []
    for cod, info in BASE_CONOCIMIENTO_POLITICAS["politicas"].items():
        pol_filas.append([cod, info["regla_id"], info["condicion_oficial"], info["umbral"]])
    story.append(
        _crear_tabla_estilizada(
            ["Política", "Regla", "Condición Oficial de la Organización", "Criterio / Umbral Verificable"],
            pol_filas,
            [0.65 * inch, 0.65 * inch, 3.25 * inch, 2.65 * inch],
            st,
        )
    )
    story.append(Spacer(1, 8))

    # -----------------------------------------------------------------------
    # 3. Etapa A: Calidad, Integridad Referencial y Aptitud
    # -----------------------------------------------------------------------
    story.append(Paragraph("3. Etapa A · Calidad de Datos, Integridad Referencial y Aptitud", st["h1"]))

    story.append(Paragraph("3.1 Inventario, Integridad de Carga y Trazabilidad (SHA-256)", st["h2"]))
    tab_filas = []
    for _, r in res.etapa_a_resumen_tablas.iterrows():
        tab_filas.append(
            [
                r["archivo"],
                "Sí" if r["cargada"] else "No",
                f"{int(r['filas']):,}",
                str(r["columnas_faltantes"]),
                "-" if pd.isna(r["pk_duplicadas"]) else str(int(r["pk_duplicadas"])),
                "-" if pd.isna(r["errores_tipo"]) else str(int(r["errores_tipo"])),
                str(r["sha256_corto"]),
            ]
        )
    story.append(
        _crear_tabla_estilizada(
            ["Archivo CSV", "Cargado", "Filas", "Cols. Falt.", "PK Dup.", "Err. Tipo", "SHA-256 (12)"],
            tab_filas,
            [1.85 * inch, 0.65 * inch, 0.70 * inch, 0.80 * inch, 0.70 * inch, 0.75 * inch, 1.75 * inch],
            st,
            cols_numericas={2, 3, 4, 5},
        )
    )
    story.append(Spacer(1, 8))

    story.append(Paragraph("3.2 Matriz de Aptitud para Habilitación de la Etapa B", st["h2"]))
    apt_filas = []
    for _, r in res.etapa_a_aptitud.iterrows():
        apt_filas.append(
            [
                r["regla_id"],
                r["nombre_regla"],
                r["estado_aptitud"],
                "Habilitada" if r["habilitada_etapa_b"] else "Omitida",
                r["justificacion_aptitud"],
            ]
        )
    story.append(
        _crear_tabla_estilizada(
            ["Regla", "Descripción", "Aptitud", "Etapa B", "Justificación Técnica"],
            apt_filas,
            [0.60 * inch, 1.85 * inch, 1.15 * inch, 0.75 * inch, 2.85 * inch],
            st,
        )
    )
    story.append(Spacer(1, 8))

    story.append(Paragraph("3.3 Hallazgos de Calidad, Cronología e Integridad Referencial", st["h2"]))
    if res.etapa_a_hallazgos.empty:
        story.append(
            Paragraph(
                "No se detectaron defectos de calidad, nulos críticos, duplicación de claves primarias, "
                "errores de tipo ni rupturas de integridad referencial en la Etapa A.",
                st["cuerpo"],
            )
        )
    else:
        max_a = 80
        hall_filas = []
        for _, r in res.etapa_a_hallazgos.head(max_a).iterrows():
            hall_filas.append(
                [
                    r["severidad"],
                    r["tabla"],
                    r["tipo_control"],
                    r["campo"],
                    str(r["registro_id"]),
                    str(r["cantidad_afectada"]),
                    r["detalle"],
                ]
            )
        story.append(
            _crear_tabla_estilizada(
                ["Sev.", "Tabla", "Control", "Campo", "ID(s)", "Cant.", "Detalle"],
                hall_filas,
                [0.72 * inch, 1.05 * inch, 1.05 * inch, 0.95 * inch, 0.90 * inch, 0.43 * inch, 2.10 * inch],
                st,
                cols_numericas={5},
            )
        )
        if len(res.etapa_a_hallazgos) > max_a:
            story.append(Spacer(1, 4))
            story.append(
                Paragraph(
                    f"Nota: Se muestran los primeros {max_a} hallazgos de {len(res.etapa_a_hallazgos)} totales. "
                    "El listado íntegro está disponible en el archivo exportable CSV/JSON de Etapa A.",
                    st["cuerpo"],
                )
            )

    story.append(PageBreak())

    # -----------------------------------------------------------------------
    # 4. Etapa B: Reglas Empresariales, Alertas y Señales Exploratorias
    # -----------------------------------------------------------------------
    story.append(Paragraph("4. Etapa B · Resultados por Regla Empresarial y Alertas", st["h1"]))
    story.append(
        Paragraph(
            "La tabla siguiente resume el universo evaluado por cada regla habilitada, separando estrictamente "
            "las alertas empresariales (indicios de incumplimiento de política) de los registros no evaluables "
            "por defectos de calidad.",
            st["cuerpo"],
        )
    )

    story.append(Paragraph("4.1 Totales Consolidados por Regla", st["h2"]))
    res_b_filas = []
    for _, r in res.etapa_b_resumen_reglas.iterrows():
        res_b_filas.append(
            [
                r["regla_id"],
                r["estado_ejecucion"],
                f"{int(r['evaluados']):,}",
                f"{int(r['conformes']):,}",
                f"{int(r['alertas_empresariales']):,}",
                f"{int(r['no_evaluables_calidad']):,}",
                f"CRC {int(r['monto_en_alerta_crc']):,}",
            ]
        )
    story.append(
        _crear_tabla_estilizada(
            ["Regla", "Estado Ejecución", "Evaluados", "Conformes", "Alertas", "No Eval.", "Monto en Alerta"],
            res_b_filas,
            [0.62 * inch, 1.98 * inch, 0.78 * inch, 0.78 * inch, 0.68 * inch, 0.68 * inch, 1.68 * inch],
            st,
            cols_numericas={2, 3, 4, 5, 6},
        )
    )
    story.append(Spacer(1, 8))

    story.append(Paragraph("4.2 Detalle Explicable de Alertas y Registros No Evaluables", st["h2"]))
    if res.etapa_b_alertas.empty:
        story.append(
            Paragraph(
                "No se generaron alertas empresariales ni registros no evaluables en las reglas ejecutadas durante la Etapa B.",
                st["cuerpo"],
            )
        )
    else:
        max_b = 100
        al_filas = []
        for _, r in res.etapa_b_alertas.head(max_b).iterrows():
            monto_str = f"CRC {int(r['monto_crc']):,}" if pd.notna(r["monto_crc"]) else "N/D"
            ids_str = f"Dev: {r['devolucion_id']} | Ven: {r['venta_id']} | Cli: {r['cliente_id']}"
            if str(r["autorizacion_id"]) not in ("-", "nan", "None", ""):
                ids_str += f" | Aut: {r['autorizacion_id']}"
            al_filas.append(
                [
                    r["regla_id"],
                    f"{r['categoria']}\n({r['subtipo']})",
                    ids_str,
                    monto_str,
                    r["justificacion"],
                ]
            )
        story.append(
            _crear_tabla_estilizada(
                ["Regla", "Categoría / Subtipo", "Identificadores", "Monto", "Justificación Técnica"],
                al_filas,
                [0.58 * inch, 1.32 * inch, 1.65 * inch, 0.85 * inch, 2.80 * inch],
                st,
                cols_numericas={3},
            )
        )
        if len(res.etapa_b_alertas) > max_b:
            story.append(Spacer(1, 4))
            story.append(
                Paragraph(
                    f"Nota: Se presentan las primeras {max_b} filas de {len(res.etapa_b_alertas)} totales. "
                    "Consulte el CSV/JSON exportable de Etapa B para el detalle completo.",
                    st["cuerpo"],
                )
            )

    story.append(Spacer(1, 8))
    story.append(Paragraph("4.3 Señales Exploratorias Complementarias (Monitoreo)", st["h2"]))
    if res.etapa_b_senales_exploratorias.empty:
        story.append(
            Paragraph(
                "No se detectaron patrones exploratorios de fraccionamiento de devoluciones bajo el umbral de CRC 120,000.",
                st["cuerpo"],
            )
        )
    else:
        story.append(
            Paragraph(
                "Las siguientes ventas agrupan múltiples devoluciones individuales menores a CRC 120,000 cuya suma "
                "acumulada iguala o supera dicho umbral. Se documentan únicamente como señal informativa de monitoreo "
                "y no representan un incumplimiento literal de D-03 si el umbral se aplica por devolución individual.",
                st["cuerpo"],
            )
        )
        sen_filas = []
        for _, r in res.etapa_b_senales_exploratorias.iterrows():
            sen_filas.append(
                [
                    r["venta_id"],
                    r["devoluciones_ids"],
                    str(r["cantidad_devoluciones"]),
                    f"CRC {int(r['suma_crc']):,}",
                    r["detalle"],
                ]
            )
        story.append(
            _crear_tabla_estilizada(
                ["Venta", "Devoluciones", "Cant.", "Suma Acum.", "Observación"],
                sen_filas,
                [0.80 * inch, 1.35 * inch, 0.50 * inch, 0.95 * inch, 3.60 * inch],
                st,
                cols_numericas={2, 3},
            )
        )

    # -----------------------------------------------------------------------
    # 5. Limitaciones Declaradas y Evidencia Externa para Corroboración
    # -----------------------------------------------------------------------
    bloque_final = [
        Paragraph("5. Limitaciones Declaradas y Próximos Pasos de Corroboración", st["h1"]),
        Paragraph(
            "De conformidad con el orden de análisis del Caso 03, ninguna diferencia entre tablas, "
            "hallazgo de calidad o alerta empresarial permite afirmar dolo o fraude confirmado. "
            "Se declaran formalmente las siguientes limitaciones de alcance y suficiencia de datos:",
            st["cuerpo"],
        ),
    ]
    for lim in res.limitaciones_declaradas:
        bloque_final.append(Paragraph(f"- {_limpiar_texto_pdf(lim)}", st["bullet"]))

    bloque_final.append(Spacer(1, 6))
    bloque_final.append(Paragraph("Evidencia Documental Externa Recomendada para Corroboración", st["h2"]))
    for ev in BASE_CONOCIMIENTO_POLITICAS["evidencia_corroboracion"]:
        bloque_final.append(Paragraph(f"- {_limpiar_texto_pdf(ev)}", st["bullet"]))

    story.append(KeepTogether(bloque_final))

    doc.build(
        story,
        canvasmaker=lambda *args, **kwargs: _CanvasNumerado(
            *args,
            nombre_lote=res.nombre_lote,
            ejecutado_en=res.ejecutado_en,
            **kwargs,
        ),
    )
    return buffer.getvalue()
