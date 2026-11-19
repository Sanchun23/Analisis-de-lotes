"""
Suite de pruebas unitarias para:
- auditoria_motor.py
- chatbot_auditor.py
- reporte_pdf.py
- almacen_lotes.py

Ejecución local:
    python -m unittest -v test_auditoria.py
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import unittest
import zipfile

from pypdf import PdfReader

from almacen_lotes import (
    AlmacenLotes,
    extraer_fuentes_de_archivos_subidos,
    extraer_fuentes_de_carpeta,
)
from auditoria_motor import (
    analizar_lote,
    exportar_reporte_json,
    exportar_reporte_markdown,
)
from chatbot_auditor import responder_local_determinista
from reporte_pdf import exportar_reporte_pdf


def _csv_bytes(encabezados: list[str], filas: list[list[str]]) -> bytes:
    lineas = [",".join(encabezados)] + [",".join(f) for f in filas]
    return ("\n".join(lineas) + "\n").encode("utf-8")


def _lote_base_limpio() -> dict[str, bytes]:
    clientes = _csv_bytes(
        ["cliente_id", "nombre_ficticio", "segmento", "cuenta_reembolso", "estado", "registrado_en"],
        [
            ["CLI01", "Cliente 1", "MINORISTA", "CTA-CLI-01", "ACTIVO", "2025-01-10 10:00:00"],
            ["CLI02", "Cliente 2", "CORPORATIVO", "CTA-CLI-02", "ACTIVO", "2025-02-10 10:00:00"],
        ],
    )
    ventas = _csv_bytes(
        ["venta_id", "cliente_id", "vendido_en", "monto_total_crc", "estado", "canal"],
        [
            ["VT01", "CLI01", "2026-09-01 09:00:00", "300000", "ENTREGADA", "WEB"],
            ["VT02", "CLI02", "2026-09-02 10:00:00", "500000", "ENTREGADA", "APP"],
        ],
    )
    devoluciones = _csv_bytes(
        [
            "devolucion_id", "venta_id", "cliente_id", "monto_crc",
            "solicitado_en", "procesado_en", "ejecutor_id",
            "cuenta_destino", "motivo", "estado",
        ],
        [
            [
                "DV01", "VT01", "CLI01", "120000",
                "2026-09-10 08:00:00", "2026-09-10 10:00:00", "AT01",
                "CTA-CLI-01", "AJUSTE_PARCIAL", "PAGADA",
            ],
            [
                "DV02", "VT01", "CLI01", "180000",
                "2026-09-12 08:00:00", "2026-09-12 10:00:00", "AT02",
                "CTA-CLI-01", "CAMBIO", "PAGADA",
            ],
            [
                "DV03", "VT02", "CLI02", "119999",
                "2026-09-15 08:00:00", "2026-09-15 09:00:00", "AT03",
                "CTA-CLI-02", "PRODUCTO_DEFECTUOSO", "PAGADA",
            ],
        ],
    )
    autorizaciones = _csv_bytes(
        ["autorizacion_id", "devolucion_id", "aprobador_id", "aprobado_en", "estado"],
        [
            ["AD01", "DV01", "SV01", "2026-09-10 09:59:59", "APROBADA"],
            ["AD02", "DV02", "SV02", "2026-09-12 09:30:00", "APROBADA"],
        ],
    )
    return {
        "clientes": clientes,
        "ventas": ventas,
        "devoluciones": devoluciones,
        "autorizaciones_devolucion": autorizaciones,
    }


def _lote_con_alertas() -> dict[str, bytes]:
    clientes = _csv_bytes(
        ["cliente_id", "nombre_ficticio", "segmento", "cuenta_reembolso", "estado", "registrado_en"],
        [
            ["CLI01", "Cliente 1", "MINORISTA", "CTA-CLI-01", "ACTIVO", "2025-01-10 10:00:00"],
            ["CLI02", "Cliente 2", "MINORISTA", "CTA-CLI-02", "ACTIVO", "2025-01-10 10:00:00"],
        ],
    )
    ventas = _csv_bytes(
        ["venta_id", "cliente_id", "vendido_en", "monto_total_crc", "estado", "canal"],
        [
            ["VT01", "CLI01", "2026-09-01 09:00:00", "200000", "ENTREGADA", "WEB"],
            ["VT02", "CLI02", "2026-09-02 09:00:00", "300000", "ANULADA", "APP"],
        ],
    )
    devoluciones = _csv_bytes(
        [
            "devolucion_id", "venta_id", "cliente_id", "monto_crc",
            "solicitado_en", "procesado_en", "ejecutor_id",
            "cuenta_destino", "motivo", "estado",
        ],
        [
            [
                "DV01", "VT01", "CLI01", "150000",
                "2026-09-10 08:00:00", "2026-09-10 10:00:00", "AT01",
                "CTA-CLI-01", "CAMBIO", "PAGADA",
            ],
            [
                "DV02", "VT01", "CLI02", "100000",
                "2026-09-11 08:00:00", "2026-09-11 10:00:00", "AT01",
                "CTA-EXT-99", "AJUSTE_PARCIAL", "PAGADA",
            ],
            [
                "DV03", "VT02", "CLI02", "200000",
                "2026-09-12 08:00:00", "2026-09-12 10:00:00", "AT05",
                "CTA-CLI-02", "ERROR_FACTURACION", "PAGADA",
            ],
        ],
    )
    autorizaciones = _csv_bytes(
        ["autorizacion_id", "devolucion_id", "aprobador_id", "aprobado_en", "estado"],
        [
            ["AD03", "DV03", "AT05", "2026-09-12 10:05:00", "APROBADA"],
        ],
    )
    return {
        "clientes": clientes,
        "ventas": ventas,
        "devoluciones": devoluciones,
        "autorizaciones_devolucion": autorizaciones,
    }


class TestAuditoriaMotor(unittest.TestCase):
    def test_lote_limpio_y_fronteras_exactas(self):
        res = analizar_lote(_lote_base_limpio(), nombre_lote="Lote_Limpio")
        self.assertTrue(res.etapa_a_aptitud["habilitada_etapa_b"].all())
        self.assertTrue(res.etapa_a_hallazgos.empty)
        self.assertTrue(res.etapa_b_alertas.empty)
        self.assertEqual(int(res.etapa_b_resumen_reglas["alertas_empresariales"].sum()), 0)

    def test_bloqueo_etapa_b_por_datos_insuficientes_y_pk_duplicada(self):
        lote = _lote_base_limpio()
        lote["clientes"] = _csv_bytes(
            ["cliente_id", "nombre_ficticio", "segmento", "estado", "registrado_en"],
            [["CLI01", "Cliente 1", "MINORISTA", "ACTIVO", "2025-01-10 10:00:00"]],
        )
        lote["ventas"] = _csv_bytes(
            ["venta_id", "cliente_id", "vendido_en", "monto_total_crc", "estado", "canal"],
            [
                ["VT01", "CLI01", "2026-09-01 09:00:00", "300000", "ENTREGADA", "WEB"],
                ["VT01", "CLI01", "2026-09-01 09:00:00", "300000", "ENTREGADA", "WEB"],
            ],
        )
        res = analizar_lote(lote, nombre_lote="Lote_Bloqueado")
        apt = res.etapa_a_aptitud.set_index("regla_id")
        self.assertFalse(apt.loc["R-D01", "habilitada_etapa_b"])
        self.assertFalse(apt.loc["R-D02", "habilitada_etapa_b"])
        self.assertFalse(apt.loc["R-D04", "habilitada_etapa_b"])
        self.assertFalse(apt.loc["R-D05", "habilitada_etapa_b"])
        self.assertTrue(apt.loc["R-D03", "habilitada_etapa_b"])

        res_b = res.etapa_b_resumen_reglas.set_index("regla_id")
        self.assertEqual(res_b.loc["R-D02", "estado_ejecucion"], "NO_EJECUTADA_DATOS_INSUFICIENTES")

    def test_alertas_d01_d02_d03_d04_d05(self):
        res = analizar_lote(_lote_con_alertas(), nombre_lote="Lote_Alertas")
        res_b = res.etapa_b_resumen_reglas.set_index("regla_id")
        self.assertEqual(res_b.loc["R-D01", "alertas_empresariales"], 1)
        self.assertEqual(res_b.loc["R-D02", "alertas_empresariales"], 1)
        self.assertEqual(res_b.loc["R-D02", "monto_en_alerta_crc"], 50000)
        self.assertEqual(res_b.loc["R-D03", "alertas_empresariales"], 2)
        self.assertEqual(res_b.loc["R-D04", "alertas_empresariales"], 1)
        self.assertEqual(res_b.loc["R-D05", "alertas_empresariales"], 1)

        json_out = exportar_reporte_json(res)
        md_out = exportar_reporte_markdown(res)
        self.assertIn("INDICIO_REQUIERE_CORROBORACION", json_out)
        self.assertIn("Ninguna alerta constituye 'fraude confirmado'", md_out)

    def test_archivos_originales_nunca_se_modifican(self):
        lote = _lote_base_limpio()
        with tempfile.TemporaryDirectory() as tmpdir:
            rutas = {}
            hashes_antes = {}
            for k, contenido in lote.items():
                ruta = os.path.join(tmpdir, f"{k}.csv")
                with open(ruta, "wb") as f:
                    f.write(contenido)
                rutas[k] = ruta
                hashes_antes[k] = hashlib.sha256(contenido).hexdigest()

            _ = analizar_lote(rutas, nombre_lote="Lote_Disco")

            for k, ruta in rutas.items():
                with open(ruta, "rb") as f:
                    hash_despues = hashlib.sha256(f.read()).hexdigest()
                self.assertEqual(hashes_antes[k], hash_despues)

    def test_chatbot_politicas_ids_y_no_fraude_confirmado(self):
        res = analizar_lote(_lote_con_alertas(), nombre_lote="Lote_Chat")

        r_pol = responder_local_determinista("Explícame la política D-03 y qué salió", res)
        self.assertIn("R-D03", r_pol)
        self.assertIn("120,000", r_pol)
        self.assertIn("Alertas empresariales: 2", r_pol)

        r_id = responder_local_determinista("¿Qué ocurrió con DV03?", res)
        self.assertIn("DV03", r_id)
        self.assertIn("R-D05", r_id)
        self.assertIn("R-D03", r_id)

        r_fraude = responder_local_determinista("¿Entonces hay fraude confirmado?", res)
        self.assertIn("NO permiten declarar 'fraude confirmado'", r_fraude)

        r_resumen = responder_local_determinista("Dame un resumen ejecutivo del lote", res)
        self.assertIn("Lote_Chat", r_resumen)

    def test_generacion_pdf_profesional_valido(self):
        res = analizar_lote(_lote_con_alertas(), nombre_lote="Lote_PDF_Prueba")
        pdf_bytes = exportar_reporte_pdf(res)

        self.assertTrue(pdf_bytes.startswith(b"%PDF-"))
        reader = PdfReader(io.BytesIO(pdf_bytes))
        self.assertGreaterEqual(len(reader.pages), 2)

        texto_completo = "\n".join(page.extract_text() or "" for page in reader.pages)
        self.assertIn("Lote_PDF_Prueba", texto_completo)
        self.assertIn("Etapa A", texto_completo)
        self.assertIn("Etapa B", texto_completo)
        self.assertIn("R-D03", texto_completo)
        self.assertIn("DV03", texto_completo)
        self.assertIn("fraude confirmado", texto_completo)

    def test_carga_lote_completo_multi_csv_zip_y_carpeta(self):
        lote = _lote_base_limpio()

        # 1. Múltiples CSV subidos a la vez (incluyendo mayúsculas y archivo extra ignorado)
        subidos = [
            ("Clientes.CSV", lote["clientes"]),
            ("ventas.csv", lote["ventas"]),
            ("devoluciones.csv", lote["devoluciones"]),
            ("autorizaciones_devolucion.csv", lote["autorizaciones_devolucion"]),
            ("notas_extra.csv", b"col\n1\n"),
        ]
        fuentes, diag = extraer_fuentes_de_archivos_subidos(subidos)
        self.assertTrue(diag["completo"])
        self.assertEqual(diag["faltantes"], [])
        self.assertEqual(diag["ignorados"], ["notas_extra.csv"])
        self.assertTrue(all(fuentes[k] is not None for k in lote))

        # 2. Archivo ZIP con subcarpeta interna y archivos basura __MACOSX
        buf_zip = io.BytesIO()
        with zipfile.ZipFile(buf_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Lote_A/clientes.csv", lote["clientes"])
            zf.writestr("Lote_A/ventas.csv", lote["ventas"])
            zf.writestr("Lote_A/devoluciones.csv", lote["devoluciones"])
            zf.writestr("Lote_A/autorizaciones_devolucion.csv", lote["autorizaciones_devolucion"])
            zf.writestr("__MACOSX/Lote_A/._clientes.csv", b"junk")

        fuentes_z, diag_z = extraer_fuentes_de_archivos_subidos([("Lote_A.zip", buf_zip.getvalue())])
        self.assertTrue(diag_z["completo"])
        self.assertEqual(diag_z["faltantes"], [])

        # 3. Carpeta local (solo lectura)
        with tempfile.TemporaryDirectory() as tmpdir:
            for k, contenido in lote.items():
                with open(os.path.join(tmpdir, f"{k}.csv"), "wb") as f:
                    f.write(contenido)
            fuentes_c, diag_c = extraer_fuentes_de_carpeta(tmpdir)
            self.assertTrue(diag_c["completo"])
            res = analizar_lote(fuentes_c, nombre_lote="Lote_Carpeta")
            self.assertEqual(int(res.etapa_b_resumen_reglas["alertas_empresariales"].sum()), 0)

    def test_almacen_lotes_persistencia_cambio_y_chat_independiente(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            almacen = AlmacenLotes(directorio=tmpdir)

            res_a = analizar_lote(_lote_base_limpio(), nombre_lote="Lote_A")
            res_b = analizar_lote(_lote_con_alertas(), nombre_lote="Lote_B")

            chat_a = [{"role": "user", "content": "Consulta sobre Lote A"}]
            chat_b = [{"role": "user", "content": "Consulta sobre Lote B"}]

            almacen.guardar_lote(res_a, chat_historial=chat_a)
            almacen.guardar_lote(res_b, chat_historial=chat_b)

            # Simular reinicio de la aplicación con una nueva instancia de AlmacenLotes
            almacen_reiniciado = AlmacenLotes(directorio=tmpdir)
            lista = almacen_reiniciado.listar_lotes()
            self.assertEqual([x["nombre_lote"] for x in lista], ["Lote_A", "Lote_B"])

            rec_a, rec_chat_a = almacen_reiniciado.cargar_lote("Lote_A")
            rec_b, rec_chat_b = almacen_reiniciado.cargar_lote("Lote_B")

            self.assertEqual(rec_a.nombre_lote, "Lote_A")
            self.assertEqual(rec_b.nombre_lote, "Lote_B")
            self.assertEqual(rec_chat_a[0]["content"], "Consulta sobre Lote A")
            self.assertEqual(rec_chat_b[0]["content"], "Consulta sobre Lote B")

            # Verificar que el chatbot y el PDF funcionan idéntico tras recargar desde disco
            self.assertIn("DV03", responder_local_determinista("¿Qué pasó con DV03?", rec_b))
            pdf_rec_b = exportar_reporte_pdf(rec_b)
            self.assertTrue(pdf_rec_b.startswith(b"%PDF-"))

            # Actualizar chat de Lote_A sin afectar Lote_B
            almacen_reiniciado.actualizar_chat("Lote_A", chat_a + [{"role": "assistant", "content": "OK A"}])
            _, rec_chat_a2 = almacen_reiniciado.cargar_lote("Lote_A")
            _, rec_chat_b2 = almacen_reiniciado.cargar_lote("Lote_B")
            self.assertEqual(len(rec_chat_a2), 2)
            self.assertEqual(len(rec_chat_b2), 1)

            # Eliminar Lote_A y verificar que Lote_B sigue disponible
            self.assertTrue(almacen_reiniciado.eliminar_lote("Lote_A"))
            self.assertEqual([x["nombre_lote"] for x in almacen_reiniciado.listar_lotes()], ["Lote_B"])



    def test_indice_rapido_compatibilidad_v3_y_chat_separado(self):
        import json as _json
        with tempfile.TemporaryDirectory() as tmpdir:
            almacen = AlmacenLotes(directorio=tmpdir)
            res_a = analizar_lote(_lote_base_limpio(), nombre_lote="Lote_A")
            res_b = analizar_lote(_lote_con_alertas(), nombre_lote="Lote_B")

            ruta_a = almacen.guardar_lote(res_a, chat_historial=[{"role": "user", "content": "Hola A"}])
            almacen.guardar_lote(res_b, chat_historial=[{"role": "user", "content": "Hola B"}])

            # Verificar que existe el índice ligero y el archivo de chat separado
            self.assertTrue(os.path.isfile(os.path.join(tmpdir, "_indice_lotes.json")))
            self.assertTrue(os.path.isfile(os.path.join(tmpdir, "Lote_A_chat.json")))

            # Actualizar chat no debe reescribir el JSON pesado del lote
            mtime_antes = os.path.getmtime(ruta_a)
            self.assertTrue(
                almacen.actualizar_chat("Lote_A", [{"role": "user", "content": "Nuevo mensaje A"}])
            )
            self.assertEqual(os.path.getmtime(ruta_a), mtime_antes)
            _, chat_rec = almacen.cargar_lote("Lote_A")
            self.assertEqual(chat_rec[0]["content"], "Nuevo mensaje A")

            # Compatibilidad con archivos v3.0: borrar índice y archivo de chat separado
            os.remove(os.path.join(tmpdir, "_indice_lotes.json"))
            os.remove(os.path.join(tmpdir, "Lote_A_chat.json"))

            almacen_legacy = AlmacenLotes(directorio=tmpdir)
            lista = almacen_legacy.listar_lotes()
            self.assertEqual([x["nombre_lote"] for x in lista], ["Lote_A", "Lote_B"])
            self.assertTrue(os.path.isfile(os.path.join(tmpdir, "_indice_lotes.json")))
            # Debe recuperar el chat embebido original de Lote_A
            _, chat_emb = almacen_legacy.cargar_lote("Lote_A")
            self.assertEqual(chat_emb[0]["content"], "Hola A")


if __name__ == "__main__":
    unittest.main()
