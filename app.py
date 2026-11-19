"""
Interfaz local en Streamlit para la auditoría reproducible de múltiples lotes CSV
(Caso 03: Comercio Electrónico Jacaranda S. A.).

Mejoras v3.1:
- Soluciona el bucle infinito de recargas (ping-pong) entre el selector lateral
  y el "Cambio rápido de lote" usando callbacks sincronizados (`on_change` /
  `on_click`) sin `st.rerun()` innecesarios.
- Cambio de lote instantáneo: índice ligero en disco (`_indice_lotes.json`),
  caché en memoria por lote y generación bajo demanda del PDF y exportables.
- Mantiene solo lectura sobre todos los archivos CSV originales.

Ejecución local:
    streamlit run app.py
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import pandas as pd
import streamlit as st

from almacen_lotes import (
    AlmacenLotes,
    extraer_fuentes_de_archivos_subidos,
    extraer_fuentes_de_carpeta,
)
from auditoria_motor import (
    ESQUEMA_TABLAS,
    NOTA_METODOLOGICA,
    ResultadoAuditoria,
    analizar_lote,
    exportar_reporte_json,
    exportar_reporte_markdown,
)
from chatbot_auditor import responder_con_llm, responder_local_determinista
from reporte_pdf import exportar_reporte_pdf

st.set_page_config(
    page_title="Auditoría de Ventas y Devoluciones Digitales",
    layout="wide",
)

almacen = AlmacenLotes(directorio="lotes_guardados")


# ---------------------------------------------------------------------------
# Helpers de estado y callbacks sincronizados (sin bucles de st.rerun)
# ---------------------------------------------------------------------------

def _mensaje_bienvenida_chat(res: ResultadoAuditoria) -> List[Dict[str, str]]:
    return [
        {
            "role": "assistant",
            "content": (
                f"Análisis del lote **`{res.nombre_lote}`** listo y guardado. "
                "Puedes consultarme sobre las políticas **D-01 a D-05**, hallazgos de **Etapa A**, "
                "alertas de **Etapa B**, señales exploratorias, limitaciones o cualquier ID específico "
                "(por ejemplo, `DV...`, `VT...`, `CLI...`, `AD...`)."
            ),
        }
    ]


def _al_cambiar_desde_sidebar() -> None:
    st.session_state["lote_activo"] = st.session_state["selector_lote_sidebar"]


def _al_cambiar_desde_superior() -> None:
    st.session_state["lote_activo"] = st.session_state["selector_lote_superior"]


def _seleccionar_lote(nombre_lote: str) -> None:
    st.session_state["lote_activo"] = nombre_lote


def _limpiar_chat_lote(nombre_lote: str) -> None:
    res = st.session_state["lotes_cache"].get(nombre_lote)
    if res is None:
        cargado = almacen.cargar_lote(nombre_lote)
        if cargado is None:
            return
        res, _ = cargado
    nuevo_hist = _mensaje_bienvenida_chat(res)
    st.session_state["chat_por_lote"][nombre_lote] = nuevo_hist
    almacen.actualizar_chat(nombre_lote, nuevo_hist)


def _inicializar_estado(nombres_guardados: List[str]) -> None:
    if "lotes_cache" not in st.session_state:
        st.session_state["lotes_cache"] = {}
    if "chat_por_lote" not in st.session_state:
        st.session_state["chat_por_lote"] = {}
    if "pdf_por_lote" not in st.session_state:
        st.session_state["pdf_por_lote"] = {}
    if "exports_por_lote" not in st.session_state:
        st.session_state["exports_por_lote"] = {}
    if "uploader_nonce" not in st.session_state:
        st.session_state["uploader_nonce"] = 0

    if "lote_activo" not in st.session_state or st.session_state["lote_activo"] not in nombres_guardados:
        st.session_state["lote_activo"] = nombres_guardados[0] if nombres_guardados else None

    # Sincronizar SIEMPRE ambos selectores ANTES de instanciar cualquier widget.
    # Esto evita desajustes y elimina por completo cualquier bucle de st.rerun().
    activo = st.session_state["lote_activo"]
    if activo is not None:
        st.session_state["selector_lote_sidebar"] = activo
        st.session_state["selector_lote_superior"] = activo
    else:
        st.session_state.pop("selector_lote_sidebar", None)
        st.session_state.pop("selector_lote_superior", None)


def _obtener_lote(nombre_lote: str) -> Optional[Tuple[ResultadoAuditoria, List[Dict[str, str]]]]:
    if nombre_lote in st.session_state["lotes_cache"]:
        res = st.session_state["lotes_cache"][nombre_lote]
        hist = st.session_state["chat_por_lote"].get(nombre_lote)
        if hist is None:
            hist = _mensaje_bienvenida_chat(res)
            st.session_state["chat_por_lote"][nombre_lote] = hist
        return res, hist

    cargado = almacen.cargar_lote(nombre_lote)
    if cargado is None:
        return None
    res, hist = cargado
    if not hist:
        hist = _mensaje_bienvenida_chat(res)
    st.session_state["lotes_cache"][nombre_lote] = res
    st.session_state["chat_por_lote"][nombre_lote] = hist
    return res, hist


def _obtener_exportables_basicos(res: ResultadoAuditoria) -> Dict[str, bytes]:
    cache = st.session_state["exports_por_lote"]
    clave = f"{res.nombre_lote}__{res.ejecutado_en}"
    if clave not in cache:
        cache[clave] = {
            "calidad_csv": res.etapa_a_hallazgos.to_csv(index=False).encode("utf-8-sig"),
            "alertas_csv": res.etapa_b_alertas.to_csv(index=False).encode("utf-8-sig"),
            "resumen_csv": res.etapa_b_resumen_reglas.to_csv(index=False).encode("utf-8-sig"),
            "completo_json": exportar_reporte_json(res).encode("utf-8"),
            "informe_md": exportar_reporte_markdown(res).encode("utf-8"),
        }
    return cache[clave]


def _sugerir_siguiente_nombre_lote(lotes_existentes: List[str]) -> str:
    existentes_up = {x.upper() for x in lotes_existentes}
    for candidato in ("Lote_A", "Lote_B", "Lote_C", "Lote_D"):
        if candidato.upper() not in existentes_up:
            return candidato
    idx = len(lotes_existentes) + 1
    while f"Lote_{idx}".upper() in existentes_up:
        idx += 1
    return f"Lote_{idx}"


# ---------------------------------------------------------------------------
# Inicialización rápida desde el índice ligero en disco
# ---------------------------------------------------------------------------
lotes_guardados_meta = almacen.listar_lotes()
nombres_guardados = [m["nombre_lote"] for m in lotes_guardados_meta]
_inicializar_estado(nombres_guardados)

st.title("Herramienta Local de Auditoría · Ventas y Devoluciones Digitales")
st.caption(
    "Caso 03 · Comercio Electrónico Jacaranda S. A. | Carga por lote completo, almacenamiento local rápido y cambio instantáneo entre lotes (solo lectura sobre los CSV originales)."
)
st.info(NOTA_METODOLOGICA)

# ---------------------------------------------------------------------------
# Panel lateral: selector de lotes guardados + carga de lote completo + chatbot
# ---------------------------------------------------------------------------
with st.sidebar:
    st.header("1. Lotes guardados")
    if nombres_guardados:
        mapa_etiquetas = {
            m["nombre_lote"]: (
                f"{m['nombre_lote']}  "
                f"({m.get('reglas_aptas', '?')}/5 aptas · {m.get('alertas_etapa_b', 0)} alertas)"
            )
            for m in lotes_guardados_meta
        }
        st.selectbox(
            "Cambiar de lote activo",
            options=nombres_guardados,
            format_func=lambda k: mapa_etiquetas.get(k, k),
            key="selector_lote_sidebar",
            on_change=_al_cambiar_desde_sidebar,
        )
        st.caption(f"Total de lotes guardados localmente: **{len(nombres_guardados)}**")
    else:
        st.caption("Aún no hay lotes guardados. Carga tu primer lote abajo.")

    st.divider()
    st.header("2. Cargar nuevo lote completo")
    nombre_sugerido = _sugerir_siguiente_nombre_lote(nombres_guardados)
    nombre_lote_input = st.text_input(
        "Nombre del lote",
        value=nombre_sugerido,
        key=f"input_nombre_lote_{st.session_state['uploader_nonce']}",
        help="Usa un nombre único por lote (ej. Lote_A, Lote_B, Lote_C). Si repites un nombre existente, se actualizará ese lote.",
    )

    metodo_carga = st.radio(
        "Método de carga",
        options=[
            "Subir los 4 CSV juntos o un .ZIP",
            "Cargar desde carpeta local (solo lectura)",
        ],
        index=0,
    )

    fuentes_preparadas: Optional[Dict[str, Optional[bytes]]] = None
    diag_carga: Optional[Dict] = None

    if metodo_carga == "Subir los 4 CSV juntos o un .ZIP":
        archivos_subidos = st.file_uploader(
            "Arrastra los 4 CSV del lote a la vez (o un .ZIP del lote)",
            type=["csv", "zip"],
            accept_multiple_files=True,
            key=f"batch_uploader_{st.session_state['uploader_nonce']}",
            help=(
                "Se reconocen automáticamente por nombre: clientes.csv, ventas.csv, "
                "devoluciones.csv y autorizaciones_devolucion.csv."
            ),
        )
        if archivos_subidos:
            pares = [(f.name, f.getvalue()) for f in archivos_subidos]
            fuentes_preparadas, diag_carga = extraer_fuentes_de_archivos_subidos(pares)
    else:
        ruta_carpeta = st.text_input(
            "Ruta de la carpeta del lote",
            value="",
            placeholder="Ej: C:/Casos/Lote_A  o  ./Lote_A",
            help="La carpeta y sus CSV se leen en memoria sin modificarse.",
        )
        if ruta_carpeta.strip():
            fuentes_preparadas, diag_carga = extraer_fuentes_de_carpeta(ruta_carpeta)

    if diag_carga is not None:
        st.markdown("**Verificación del lote seleccionado:**")
        for tabla, spec in ESQUEMA_TABLAS.items():
            ok = tabla in diag_carga["detectados"]
            icono = "✅" if ok else "⚠️"
            estado_txt = "detectado" if ok else "faltante"
            st.markdown(f"- {icono} `{spec['archivo_esperado']}` — {estado_txt}")

        for err in diag_carga.get("errores_lectura", []):
            st.error(err)
        for dup in diag_carga.get("duplicados", []):
            st.warning(f"Archivo duplicado en la carga: {dup}")
        if diag_carga.get("ignorados"):
            st.caption(
                "Archivos ignorados (no pertenecen al esquema de 4 CSV): "
                + ", ".join(diag_carga["ignorados"][:5])
            )

    if nombre_lote_input.strip() in nombres_guardados:
        st.caption(f"Nota: `{nombre_lote_input.strip()}` ya existe y será actualizado al analizar.")

    ejecutar = st.button(
        "Analizar y guardar lote",
        type="primary",
        use_container_width=True,
    )

    if ejecutar:
        etiqueta = nombre_lote_input.strip() or nombre_sugerido
        if fuentes_preparadas is None or all(v is None for v in fuentes_preparadas.values()):
            st.error("Selecciona los archivos CSV (o .ZIP / carpeta válida) antes de analizar.")
        else:
            with st.spinner(f"Analizando y guardando '{etiqueta}' (Etapa A → Etapa B)..."):
                res_nuevo = analizar_lote(fuentes_preparadas, nombre_lote=etiqueta)
                chat_inicial = _mensaje_bienvenida_chat(res_nuevo)
                almacen.guardar_lote(res_nuevo, chat_historial=chat_inicial)
                st.session_state["lotes_cache"][etiqueta] = res_nuevo
                st.session_state["chat_por_lote"][etiqueta] = chat_inicial
                st.session_state["pdf_por_lote"].pop(etiqueta, None)
                st.session_state["lote_activo"] = etiqueta
                st.session_state["uploader_nonce"] += 1
            st.rerun()

    st.divider()
    st.header("3. Motor del Asistente Auditor")
    modo_chat = st.radio(
        "Modo de respuesta",
        options=[
            "Local determinista (sin API, 100% offline)",
            "Gemini API (opcional)",
            "Ollama local (opcional)",
        ],
        index=0,
        help="El modo local determinista consulta directamente las políticas D-01 a D-05 y los resultados del lote activo sin conexión externa.",
    )
    api_key_llm = ""
    modelo_llm = ""
    ollama_url = "http://localhost:11434"
    if modo_chat == "Gemini API (opcional)":
        api_key_llm = st.text_input("Gemini API Key", type="password")
        modelo_llm = st.text_input("Modelo Gemini", value="gemini-2.5-flash")
    elif modo_chat == "Ollama local (opcional)":
        ollama_url = st.text_input("URL de Ollama", value="http://localhost:11434")
        modelo_llm = st.text_input("Modelo Ollama", value="llama3.1")

# ---------------------------------------------------------------------------
# Pantalla inicial si no existe ningún lote guardado todavía
# ---------------------------------------------------------------------------
if not st.session_state["lote_activo"]:
    st.markdown(
        """
        ### Cómo empezar
        1. En la barra lateral, escribe el nombre del lote (por ejemplo, `Lote_A`).
        2. Arrastra o selecciona **los 4 CSV juntos** (`clientes.csv`, `ventas.csv`, `devoluciones.csv` y `autorizaciones_devolucion.csv`), sube un archivo **`.ZIP`** con el lote completo, o indica la **carpeta local** del lote.
        3. Presiona **Analizar y guardar lote**.
        4. Repite el proceso para `Lote_B` y `Lote_C`: todos quedarán guardados en `lotes_guardados/` y podrás cambiar instantáneamente de un lote a otro desde la barra lateral, el **Cambio rápido de lote** o la pestaña **Lotes guardados**.
        """
    )
    st.stop()

lote_actual_nombre = st.session_state["lote_activo"]
cargado_activo = _obtener_lote(lote_actual_nombre)
if cargado_activo is None:
    st.error(f"No se pudo cargar el lote guardado '{lote_actual_nombre}'.")
    st.stop()

res, chat_hist_actual = cargado_activo

# ---------------------------------------------------------------------------
# Barra superior de lote activo + Cambio rápido de lote sincronizado + KPIs
# ---------------------------------------------------------------------------
top_col1, top_col2 = st.columns([3, 1])
with top_col1:
    st.markdown(f"### Lote activo: `{res.nombre_lote}`")
    st.caption(f"Analizado el {res.ejecutado_en} · Guardado en `lotes_guardados/`")
with top_col2:
    if len(nombres_guardados) > 1:
        st.selectbox(
            "Cambio rápido de lote",
            options=nombres_guardados,
            key="selector_lote_superior",
            on_change=_al_cambiar_desde_superior,
        )

total_hallazgos_a = len(res.etapa_a_hallazgos)
reglas_aptas = int(res.etapa_a_aptitud["habilitada_etapa_b"].sum())
total_alertas_b = int(
    res.etapa_b_alertas["categoria"].isin(["ALERTA_EMPRESARIAL", "ALERTA_VERIFICACION_D04"]).sum()
)
total_no_eval_b = int((res.etapa_b_alertas["categoria"] == "NO_EVALUABLE_CALIDAD").sum())
monto_alerta_total = int(res.etapa_b_resumen_reglas["monto_en_alerta_crc"].sum())

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Reglas aptas (Etapa A)", f"{reglas_aptas} / 5")
c2.metric("Hallazgos calidad (Etapa A)", total_hallazgos_a)
c3.metric("Alertas reglas (Etapa B)", total_alertas_b)
c4.metric("No evaluables (calidad)", total_no_eval_b)
c5.metric("Monto en alerta", f"CRC {monto_alerta_total:,}")

tab_a, tab_b, tab_chat, tab_exp, tab_lotes = st.tabs(
    [
        "Etapa A · Calidad, integridad y aptitud",
        "Etapa B · Reglas empresariales y alertas",
        "Asistente Auditor (Chatbot)",
        "Exportar reportes y PDF",
        f"Lotes guardados ({len(nombres_guardados)})",
    ]
)

# ---------------------------------------------------------------------------
# Pestaña Etapa A
# ---------------------------------------------------------------------------
with tab_a:
    st.subheader("A.1 Resumen de archivos cargados y trazabilidad (SHA-256)")
    st.dataframe(res.etapa_a_resumen_tablas, use_container_width=True, hide_index=True)

    st.subheader("A.2 Matriz de aptitud para cada regla empresarial")
    st.dataframe(res.etapa_a_aptitud, use_container_width=True, hide_index=True)

    st.subheader("A.3 Hallazgos de calidad, tipos, claves e integridad referencial")
    if res.etapa_a_hallazgos.empty:
        st.success("No se detectaron problemas de calidad ni de integridad referencial en la Etapa A.")
    else:
        col_f1, col_f2 = st.columns(2)
        sev_opts = sorted(res.etapa_a_hallazgos["severidad"].unique().tolist())
        tab_opts = sorted(res.etapa_a_hallazgos["tabla"].unique().tolist())
        sev_sel = col_f1.multiselect("Filtrar por severidad", sev_opts, default=sev_opts, key=f"sev_{lote_actual_nombre}")
        tab_sel = col_f2.multiselect("Filtrar por tabla", tab_opts, default=tab_opts, key=f"tab_{lote_actual_nombre}")
        df_a_filt = res.etapa_a_hallazgos[
            res.etapa_a_hallazgos["severidad"].isin(sev_sel)
            & res.etapa_a_hallazgos["tabla"].isin(tab_sel)
        ]
        st.dataframe(df_a_filt, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Pestaña Etapa B
# ---------------------------------------------------------------------------
with tab_b:
    st.subheader("B.1 Totales por regla empresarial")
    st.dataframe(res.etapa_b_resumen_reglas, use_container_width=True, hide_index=True)

    no_ejec = res.etapa_b_resumen_reglas[
        res.etapa_b_resumen_reglas["estado_ejecucion"] != "EJECUTADA"
    ]
    if not no_ejec.empty:
        st.warning(
            "Reglas omitidas por datos insuficientes en Etapa A: "
            + ", ".join(no_ejec["regla_id"].tolist())
        )

    st.subheader("B.2 Detalle de alertas y registros no evaluables")
    if res.etapa_b_alertas.empty:
        st.success("No se generaron alertas empresariales ni registros no evaluables en las reglas ejecutadas.")
    else:
        col_b1, col_b2, col_b3 = st.columns(3)
        reg_opts = sorted(res.etapa_b_alertas["regla_id"].unique().tolist())
        cat_opts = sorted(res.etapa_b_alertas["categoria"].unique().tolist())
        reg_sel = col_b1.multiselect("Filtrar por regla", reg_opts, default=reg_opts, key=f"reg_{lote_actual_nombre}")
        cat_sel = col_b2.multiselect("Filtrar por categoría", cat_opts, default=cat_opts, key=f"cat_{lote_actual_nombre}")
        busq_id = col_b3.text_input(
            "Buscar por ID (devolución, venta, cliente o autorización)",
            value="",
            key=f"busq_{lote_actual_nombre}",
        )

        df_b_filt = res.etapa_b_alertas[
            res.etapa_b_alertas["regla_id"].isin(reg_sel)
            & res.etapa_b_alertas["categoria"].isin(cat_sel)
        ]
        if busq_id.strip():
            q = busq_id.strip().upper()
            mask = (
                df_b_filt["devolucion_id"].astype(str).str.upper().str.contains(q, na=False)
                | df_b_filt["venta_id"].astype(str).str.upper().str.contains(q, na=False)
                | df_b_filt["cliente_id"].astype(str).str.upper().str.contains(q, na=False)
                | df_b_filt["autorizacion_id"].astype(str).str.upper().str.contains(q, na=False)
            )
            df_b_filt = df_b_filt[mask]

        st.dataframe(df_b_filt, use_container_width=True, hide_index=True)

    st.subheader("B.3 Señales exploratorias complementarias (no constituyen incumplimiento literal)")
    if res.etapa_b_senales_exploratorias.empty:
        st.caption("Sin señales exploratorias de fraccionamiento detectadas.")
    else:
        st.dataframe(res.etapa_b_senales_exploratorias, use_container_width=True, hide_index=True)

    st.subheader("B.4 Limitaciones declaradas")
    for lim in res.limitaciones_declaradas:
        st.markdown(f"- {lim}")

# ---------------------------------------------------------------------------
# Pestaña Asistente Auditor (Chatbot) — historial independiente por lote
# ---------------------------------------------------------------------------
with tab_chat:
    st.subheader(f"Asistente Auditor · Lote `{res.nombre_lote}`")
    st.caption(
        f"Modo activo: **{modo_chat}** | Conocimiento restringido a políticas D-01 a D-05 y resultados reales del lote `{res.nombre_lote}`. "
        "Cada lote conserva su propia conversación guardada."
    )

    st.markdown("**Consultas rápidas:**")
    qb1, qb2, qb3, qb4 = st.columns(4)
    pregunta_rapida = None
    if qb1.button("Resumen ejecutivo del lote", use_container_width=True, key=f"q1_{lote_actual_nombre}"):
        pregunta_rapida = "Dame un resumen ejecutivo del lote analizado."
    if qb2.button("Hallazgos de Etapa A", use_container_width=True, key=f"q2_{lote_actual_nombre}"):
        pregunta_rapida = "¿Qué encontró la Etapa A de calidad, integridad y aptitud?"
    if qb3.button("Política D-03 y resultados", use_container_width=True, key=f"q3_{lote_actual_nombre}"):
        pregunta_rapida = "Explícame la política D-03 y sus resultados en este lote."
    if qb4.button("¿Confirma fraude? / Evidencia", use_container_width=True, key=f"q4_{lote_actual_nombre}"):
        pregunta_rapida = "¿Estos resultados confirman fraude o qué evidencia adicional se necesita?"

    col_limp, _ = st.columns([1, 4])
    col_limp.button(
        "Limpiar conversación de este lote",
        key=f"clr_{lote_actual_nombre}",
        on_click=_limpiar_chat_lote,
        args=(lote_actual_nombre,),
    )

    for msg in chat_hist_actual:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    entrada_usuario = st.chat_input(
        f"Pregunta sobre el lote {res.nombre_lote}, políticas (D-01..D-05) o un ID..."
    )
    consulta_final = pregunta_rapida or entrada_usuario

    if consulta_final:
        chat_hist_actual.append({"role": "user", "content": consulta_final})
        with st.chat_message("user"):
            st.markdown(consulta_final)

        with st.chat_message("assistant"):
            with st.spinner("Consultando políticas y resultados del lote..."):
                if modo_chat == "Local determinista (sin API, 100% offline)":
                    respuesta = responder_local_determinista(consulta_final, res)
                elif modo_chat == "Gemini API (opcional)":
                    respuesta = responder_con_llm(
                        pregunta=consulta_final,
                        res=res,
                        proveedor="gemini",
                        historial=chat_hist_actual,
                        api_key=api_key_llm,
                        modelo=modelo_llm,
                    )
                else:
                    respuesta = responder_con_llm(
                        pregunta=consulta_final,
                        res=res,
                        proveedor="ollama",
                        historial=chat_hist_actual,
                        modelo=modelo_llm,
                        ollama_url=ollama_url,
                    )
            st.markdown(respuesta)

        chat_hist_actual.append({"role": "assistant", "content": respuesta})
        st.session_state["chat_por_lote"][lote_actual_nombre] = chat_hist_actual
        almacen.actualizar_chat(lote_actual_nombre, chat_hist_actual)

# ---------------------------------------------------------------------------
# Pestaña de exportación (PDF bajo demanda + CSV/JSON/MD en caché)
# ---------------------------------------------------------------------------
with tab_exp:
    st.subheader(f"Descarga del Informe Profesional en PDF y reportes técnicos — `{res.nombre_lote}`")
    st.caption(
        "El PDF se genera bajo demanda y queda en memoria para mantener el cambio de lote instantáneo. "
        "Los CSV originales nunca se alteran."
    )

    prefijo = res.nombre_lote.replace(" ", "_")
    clave_pdf = f"{res.nombre_lote}__{res.ejecutado_en}"
    pdf_bytes = st.session_state["pdf_por_lote"].get(clave_pdf)

    if pdf_bytes is None:
        if st.button(
            f"Generar Informe Profesional de {res.nombre_lote} (PDF)",
            type="primary",
            use_container_width=True,
            key=f"gen_pdf_{lote_actual_nombre}",
        ):
            with st.spinner(f"Generando informe PDF profesional de {res.nombre_lote}..."):
                pdf_bytes = exportar_reporte_pdf(res)
                st.session_state["pdf_por_lote"][clave_pdf] = pdf_bytes

    if pdf_bytes is not None:
        st.download_button(
            label=f"Descargar Informe Profesional Completo de {res.nombre_lote} (PDF)",
            data=pdf_bytes,
            file_name=f"{prefijo}_informe_auditoria_profesional.pdf",
            mime="application/pdf",
            type="primary",
            use_container_width=True,
            key=f"dl_pdf_{lote_actual_nombre}",
        )

    st.divider()
    exports = _obtener_exportables_basicos(res)
    col_e1, col_e2 = st.columns(2)

    col_e1.download_button(
        label="Descargar Etapa A · Hallazgos de calidad (CSV)",
        data=exports["calidad_csv"],
        file_name=f"{prefijo}_etapa_a_calidad.csv",
        mime="text/csv",
        use_container_width=True,
        key=f"dl_a_{lote_actual_nombre}",
    )
    col_e1.download_button(
        label="Descargar Etapa B · Alertas detalladas (CSV)",
        data=exports["alertas_csv"],
        file_name=f"{prefijo}_etapa_b_alertas.csv",
        mime="text/csv",
        use_container_width=True,
        key=f"dl_b_{lote_actual_nombre}",
    )
    col_e1.download_button(
        label="Descargar Totales por regla (CSV)",
        data=exports["resumen_csv"],
        file_name=f"{prefijo}_resumen_reglas.csv",
        mime="text/csv",
        use_container_width=True,
        key=f"dl_r_{lote_actual_nombre}",
    )

    col_e2.download_button(
        label="Descargar Reporte estructurado completo (JSON)",
        data=exports["completo_json"],
        file_name=f"{prefijo}_reporte_completo.json",
        mime="application/json",
        use_container_width=True,
        key=f"dl_json_{lote_actual_nombre}",
    )
    col_e2.download_button(
        label="Descargar Informe técnico legible (Markdown)",
        data=exports["informe_md"],
        file_name=f"{prefijo}_informe_auditoria.md",
        mime="text/markdown",
        use_container_width=True,
        key=f"dl_md_{lote_actual_nombre}",
    )

# ---------------------------------------------------------------------------
# Pestaña Panel de Lotes Guardados (navegación instantánea y administración)
# ---------------------------------------------------------------------------
with tab_lotes:
    st.subheader("Repositorio local de lotes guardados")
    st.caption(
        "Los lotes A, B y C son extracciones independientes del mismo proceso empresarial. "
        "Esta tabla muestra únicamente indicadores individuales de cada lote para navegar entre ellos; "
        "no realiza comparaciones fila a fila ni clasificaciones automáticas."
    )

    df_lotes_resumen = pd.DataFrame(
        [
            {
                "Lote": m.get("nombre_lote"),
                "Estado": "ACTIVO" if m.get("nombre_lote") == lote_actual_nombre else "",
                "Ejecutado en": m.get("ejecutado_en"),
                "Reglas aptas": f"{m.get('reglas_aptas', 0)} / 5",
                "Hallazgos Etapa A": m.get("hallazgos_etapa_a", 0),
                "Alertas Etapa B": m.get("alertas_etapa_b", 0),
                "No evaluables": m.get("no_evaluables_calidad", 0),
                "Monto en alerta (CRC)": f"{int(m.get('monto_en_alerta_crc', 0)):,}",
                "Señales exploratorias": m.get("senales_exploratorias", 0),
            }
            for m in lotes_guardados_meta
        ]
    )
    st.dataframe(df_lotes_resumen, use_container_width=True, hide_index=True)

    st.markdown("**Abrir otro lote guardado:**")
    cols_nav = st.columns(min(4, max(1, len(nombres_guardados))))
    for idx, nombre_item in enumerate(nombres_guardados):
        col_btn = cols_nav[idx % len(cols_nav)]
        es_act = nombre_item == lote_actual_nombre
        col_btn.button(
            f"{'● ' if es_act else ''}Ver {nombre_item}",
            key=f"btn_ver_{nombre_item}",
            disabled=es_act,
            use_container_width=True,
            on_click=_seleccionar_lote,
            args=(nombre_item,),
        )

    st.divider()
    with st.expander("Administrar / eliminar un lote guardado (no borra tus CSV originales)"):
        lote_a_borrar = st.selectbox(
            "Seleccionar lote a eliminar del historial guardado",
            options=nombres_guardados,
            key="select_borrar_lote",
        )
        confirmar_borrado = st.checkbox(
            f"Confirmo eliminar el análisis guardado de '{lote_a_borrar}' (los CSV originales permanecen intactos).",
            key="chk_borrar_lote",
        )
        if st.button("Eliminar análisis guardado", disabled=not confirmar_borrado):
            almacen.eliminar_lote(lote_a_borrar)
            st.session_state["lotes_cache"].pop(lote_a_borrar, None)
            st.session_state["chat_por_lote"].pop(lote_a_borrar, None)
            for k in list(st.session_state["pdf_por_lote"].keys()):
                if k.startswith(f"{lote_a_borrar}__"):
                    st.session_state["pdf_por_lote"].pop(k, None)
            for k in list(st.session_state["exports_por_lote"].keys()):
                if k.startswith(f"{lote_a_borrar}__"):
                    st.session_state["exports_por_lote"].pop(k, None)
            if st.session_state["lote_activo"] == lote_a_borrar:
                restantes = [n for n in nombres_guardados if n != lote_a_borrar]
                st.session_state["lote_activo"] = restantes[0] if restantes else None
            st.rerun()
