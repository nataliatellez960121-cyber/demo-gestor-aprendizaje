#!/usr/bin/env python3
"""Pruebas de la normalización y sincronización de experiencias públicas.

Ejecutar con:
    python3 -m unittest scripts/tests/test_sync_experiencias.py -v
desde la raíz del repositorio.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import sync_experiencias as sync  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def make_historia(titulo, **overrides):
    base = {
        "titulo": titulo,
        "resumen": f"Resumen de {titulo}",
        "historia": f"Historia completa de {titulo}",
        "tipo": "Curricular",
        "metodologia": "Aprendizaje basado en proyectos",
        "unidad": "Facultad de Ingeniería",
    }
    base.update(overrides)
    return base


class TestNormalizacion(unittest.TestCase):
    """Prueba 1: los campos Choice y Lookup deben convertirse a texto."""

    def test_objeto_dict_con_value(self):
        self.assertEqual(sync.normalize_value({"Id": 0, "Value": "Curricular"}), "Curricular")

    def test_objeto_lookup_serializado_como_repr_de_python(self):
        valor = (
            "{'@odata.type': '#Microsoft.Azure.Connectors.SharePoint.SPListExpandedReference', "
            "'Id': -1, 'Value': 'Facultad de Medicina;'}"
        )
        self.assertEqual(sync.normalize_value(valor), "Facultad de Medicina")

    def test_objeto_lookup_serializado_como_json(self):
        valor = json.dumps({"Id": 0, "Value": "Co-curricular"})
        self.assertEqual(sync.normalize_value(valor), "Co-curricular")

    def test_lista_de_valores_choice_multiples(self):
        valor = [{"Value": "A"}, {"Value": "B"}]
        self.assertEqual(sync.normalize_value(valor), "A, B")

    def test_texto_simple_se_conserva(self):
        self.assertEqual(sync.normalize_value("Curricular"), "Curricular")

    def test_nulo_se_convierte_en_cadena_vacia(self):
        self.assertEqual(sync.normalize_value(None), "")

    def test_cadena_vacia_y_espacios(self):
        self.assertEqual(sync.normalize_value(""), "")
        self.assertEqual(sync.normalize_value("   "), "")

    def test_espacios_innecesarios_y_nbsp(self):
        self.assertEqual(sync.normalize_value("  Facultad\xa0de  Medicina   "), "Facultad de Medicina")

    def test_punto_y_coma_final_residual_de_lookup(self):
        self.assertEqual(sync.normalize_value("Instituto Forum;"), "Instituto Forum")

    def test_normalize_record_solo_conserva_los_seis_campos_publicos(self):
        raw = make_historia(
            "X",
            CorreoDocente="docente@unisabana.edu.co",
            IdSharePoint=123,
            AutorizacionPublicacion=True,
        )
        record = sync.normalize_record(raw)
        self.assertEqual(set(record.keys()), set(sync.PUBLIC_FIELDS))
        self.assertNotIn("CorreoDocente", record)
        self.assertNotIn("IdSharePoint", record)


class TestDeduplicacion(unittest.TestCase):
    """Prueba 2: un mismo registro recibido dos veces debe aparecer una sola vez."""

    def test_mismo_id_interno_colapsa_a_un_solo_registro(self):
        raw = make_historia("Historia repetida", id=501)
        lote1, _ = sync.clean_batch([raw])
        lote2, _ = sync.clean_batch([dict(raw)])
        resultado = sync.consolidate([lote1, lote2])
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0]["titulo"], "Historia repetida")

    def test_mismo_titulo_sin_id_tambien_colapsa(self):
        # Caso real detectado en producción: el mismo registro llega una vez
        # limpio y otra con el objeto SharePoint crudo en tipo/unidad.
        version_limpia = make_historia("Innovación con IA", tipo="Curricular", unidad="Facultad de Medicina")
        version_corrupta = make_historia(
            "Innovación con IA",
            tipo="{'Id': 0, 'Value': 'Curricular'}",
            unidad="{'Id': -1, 'Value': 'Facultad de Medicina;'}",
        )
        lote, _ = sync.clean_batch([version_limpia, version_corrupta])
        resultado = sync.consolidate([lote])
        self.assertEqual(len(resultado), 1)
        self.assertEqual(resultado[0]["tipo"], "Curricular")
        self.assertEqual(resultado[0]["unidad"], "Facultad de Medicina")


class TestIntegridadDeLotes(unittest.TestCase):
    """Prueba 3: 42 registros repartidos en nueve lotes deben producir 42 historias únicas."""

    def test_42_en_nueve_lotes(self):
        historias = [make_historia(f"Historia {i}", id=i) for i in range(42)]
        lotes = [historias[i::9] for i in range(9)]
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            sync.reset_state(state_dir, "sync-abc", total_lotes=9)
            completed = False
            for idx, lote in enumerate(lotes, start=1):
                _, _, completed = sync.apply_lote(state_dir, "sync-abc", idx, 9, lote)
            self.assertTrue(completed)
            resultado = sync.consolidate_state(state_dir)
        self.assertEqual(len(resultado), 42)
        self.assertEqual(len({r["titulo"] for r in resultado}), 42)


class TestSincronizacionRepetida(unittest.TestCase):
    """Prueba 4: ejecutar dos veces la misma sincronización produce el mismo JSON."""

    def _ejecutar_sync(self, sync_id, historias, n_lotes):
        lotes = [historias[i::n_lotes] for i in range(n_lotes)]
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            sync.reset_state(state_dir, sync_id, total_lotes=n_lotes)
            for idx, lote in enumerate(lotes, start=1):
                sync.apply_lote(state_dir, sync_id, idx, n_lotes, lote)
            return sync.consolidate_state(state_dir)

    def test_misma_entrada_misma_salida(self):
        historias = [make_historia(f"Historia {i}", id=i) for i in range(15)]
        resultado1 = self._ejecutar_sync("sync-1", historias, 3)
        resultado2 = self._ejecutar_sync("sync-2", historias, 3)
        self.assertEqual(
            json.dumps(resultado1, ensure_ascii=False, sort_keys=True),
            json.dumps(resultado2, ensure_ascii=False, sort_keys=True),
        )


class TestExclusion(unittest.TestCase):
    """Prueba 5: una historia no autorizada (no enviada por Power Automate) no debe aparecer."""

    def test_historia_no_enviada_no_aparece(self):
        autorizadas = [make_historia("Autorizada A"), make_historia("Autorizada B")]
        lote, _ = sync.clean_batch(autorizadas)
        resultado = sync.consolidate([lote])
        titulos = {r["titulo"] for r in resultado}
        self.assertEqual(titulos, {"Autorizada A", "Autorizada B"})
        self.assertNotIn("Historia no autorizada", titulos)


class TestEliminacion(unittest.TestCase):
    """Prueba 6: si una historia deja de estar autorizada, debe desaparecer en el siguiente sync."""

    def test_historia_retirada_desaparece_en_sync_siguiente(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)

            sync.reset_state(state_dir, "sync-1", total_lotes=1)
            sync.apply_lote(
                state_dir,
                "sync-1",
                1,
                1,
                [make_historia("Se mantiene"), make_historia("Se retira")],
            )
            primero = sync.consolidate_state(state_dir)
            self.assertEqual({r["titulo"] for r in primero}, {"Se mantiene", "Se retira"})

            sync.reset_state(state_dir, "sync-2", total_lotes=1)
            sync.apply_lote(state_dir, "sync-2", 1, 1, [make_historia("Se mantiene")])
            segundo = sync.consolidate_state(state_dir)

        self.assertEqual({r["titulo"] for r in segundo}, {"Se mantiene"})

    def test_lote_de_sincronizacion_anterior_es_rechazado(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            sync.reset_state(state_dir, "sync-nuevo", total_lotes=2)
            with self.assertRaises(sync.SyncMismatchError):
                sync.apply_lote(state_dir, "sync-viejo", 1, 2, [make_historia("X")])


class TestCamposObligatorios(unittest.TestCase):
    """Prueba 7: no deben publicarse historias con título, resumen o historia vacíos."""

    def test_titulo_vacio_se_omite(self):
        registros = [make_historia("", id=1), make_historia("Válida", id=2)]
        lote, omitidos = sync.clean_batch(registros)
        self.assertEqual(omitidos, 1)
        resultado = sync.consolidate([lote])
        self.assertEqual([r["titulo"] for r in resultado], ["Válida"])

    def test_resumen_o_historia_vacios_se_omiten(self):
        registros = [
            make_historia("Sin resumen", id=1, resumen="   "),
            make_historia("Sin historia", id=2, historia=""),
            make_historia("Completa", id=3),
        ]
        lote, omitidos = sync.clean_batch(registros)
        self.assertEqual(omitidos, 2)
        resultado = sync.consolidate([lote])
        self.assertEqual([r["titulo"] for r in resultado], ["Completa"])


class TestDatosActuales(unittest.TestCase):
    """Prueba 9: validar el archivo público actual del repositorio con datos reales.

    Nota histórica: antes de la corrección, data/experiencias.json tenía 72
    registros (30 de una sincronización antigua + 42 de la más reciente,
    esta última con objetos Choice/Lookup de SharePoint sin normalizar).
    Al consolidar con la nueva lógica se redujo a 43 títulos únicos, de los
    cuales 1 ("De la teoría a la planta: la contabilidad gerencial en un
    entorno real") no aparecía en el lote más reciente y fue excluido
    manualmente por decisión explícita del usuario, dejando 42. Esta prueba
    verifica que ese resultado se mantenga estable: volver a consolidar el
    archivo ya corregido no debe alterar ni duplicar nada.
    """

    def test_archivo_actual_ya_esta_normalizado_y_sin_duplicados(self):
        data_path = REPO_ROOT / "data" / "experiencias.json"
        datos_actuales = json.loads(data_path.read_text(encoding="utf-8"))

        total_original = len(datos_actuales)
        lote, omitidos = sync.clean_batch(datos_actuales)
        resultado = sync.consolidate([lote])

        print(
            f"\n[Prueba 9] Registros en data/experiencias.json: {total_original}"
            f"\n[Prueba 9] Registros tras normalizar y deduplicar (debe ser igual): {len(resultado)}"
        )

        self.assertEqual(omitidos, 0, "No debería haber registros inválidos en el archivo publicado")
        self.assertEqual(total_original, len(resultado), "Consolidar el archivo ya corregido no debe cambiar su tamaño")
        titulos = [r["titulo"].casefold() for r in resultado]
        self.assertEqual(len(titulos), len(set(titulos)), "No deben quedar títulos duplicados")

        for registro in resultado:
            for campo in sync.PUBLIC_FIELDS:
                valor = registro[campo]
                self.assertNotIn("@odata", valor)
                self.assertNotIn("SPListExpandedReference", valor)


if __name__ == "__main__":
    unittest.main()
