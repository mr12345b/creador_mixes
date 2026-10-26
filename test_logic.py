"""Pruebas de la lógica pura (no necesitan internet ni librerías externas).

Ejecutar desde la raíz del proyecto:   python -m unittest discover -s tests -v
"""
import os
import random
import sys
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import logic  # noqa: E402


class TestCuentas(unittest.TestCase):
    def test_descubre_cuentas_sin_importar_mayusculas(self):
        env = {
            "MIGUEL_CLIENT_ID": "id1", "MIGUEL_CLIENT_SECRET": "s1",
            "music_CLIENT_ID": "id2", "MUSIC_SECRET_ID": "s2",     # formato viejo del secreto
            "RANDOM_VAR": "x", "DATABASE_URL": "postgres://...",
        }
        c = logic.descubrir_cuentas(env)
        self.assertEqual(list(c), ["miguel", "music"])
        self.assertEqual(c["music"]["client_secret"], "s2")
        self.assertTrue(c["miguel"]["configurada"])

    def test_alias_jesus_es_jesusv(self):
        c = logic.descubrir_cuentas({"JESUS_CLIENT_ID": "a", "JESUS_CLIENT_SECRET": "b"})
        self.assertIn("jesusv", c)
        self.assertEqual(c["jesusv"]["env_id"], "JESUS_CLIENT_ID")

    def test_cuenta_sin_secreto_queda_marcada_como_no_configurada(self):
        c = logic.descubrir_cuentas({"JUAN_CLIENT_ID": "a"})
        self.assertFalse(c["juan"]["configurada"])

    def test_ignora_variables_spotipy_y_vacias(self):
        c = logic.descubrir_cuentas({"SPOTIPY_CLIENT_ID": "a", "SPOTIPY_CLIENT_SECRET": "b",
                                     "GUO_CLIENT_ID": "  ", "GUO_CLIENT_SECRET": "x"})
        self.assertEqual(c, {})

    def test_normalizar(self):
        self.assertEqual(logic.normalizar_cuenta("  Music "), "music")
        self.assertEqual(logic.normalizar_cuenta(None), "")


class TestArtistas(unittest.TestCase):
    def test_lineas_quita_vacios_y_repetidos(self):
        self.assertEqual(logic.lineas_artistas("A\n\n b \nA\r\nB\n"), ["A", "b"])

    def test_rotacion_no_repite_hasta_agotar(self):
        lista = [f"Artista{i}" for i in range(10)]
        rng = random.Random(1)
        usados, vistos = set(), []
        for _ in range(2):  # 2 días x 4 = 8 de 10, sin repetir
            sel, reinicio = logic.elegir_artistas(lista, usados, 4, rng)
            self.assertFalse(reinicio)
            self.assertEqual(len(sel), 4)
            usados |= {a.lower() for a in sel}
            vistos += sel
        self.assertEqual(len(set(vistos)), 8)

    def test_al_agotar_se_reinicia_el_ciclo_y_completa_el_volumen(self):
        lista = [f"Artista{i}" for i in range(10)]
        usados = {f"artista{i}" for i in range(8)}      # quedan 2 sin usar
        sel, reinicio = logic.elegir_artistas(lista, usados, 4, random.Random(2))
        self.assertTrue(reinicio)
        self.assertEqual(len(sel), 4)
        self.assertEqual(len(set(sel)), 4)
        self.assertTrue({"Artista8", "Artista9"} <= set(sel))   # primero van los que faltaban

    def test_si_hay_menos_artistas_que_volumen_devuelve_los_que_hay(self):
        sel, _ = logic.elegir_artistas(["A", "B"], set(), 40, random.Random(3))
        self.assertEqual(sorted(sel), ["A", "B"])


class TestCanciones(unittest.TestCase):
    ID = "3w3y8KPTfNeOKPiqUTakBh"

    def test_limpiar_id_track(self):
        self.assertEqual(logic.limpiar_id_track(self.ID), self.ID)
        self.assertEqual(logic.limpiar_id_track(f"spotify:track:{self.ID}"), self.ID)
        self.assertEqual(logic.limpiar_id_track(f"https://open.spotify.com/track/{self.ID}?si=abc"), self.ID)
        self.assertEqual(logic.limpiar_id_track("None"), "")
        self.assertEqual(logic.limpiar_id_track("corto"), "")

    def test_uris_extra_sin_repetidos(self):
        self.assertEqual(logic.uris_extra([self.ID, f"spotify:track:{self.ID}", "x"]), [f"spotify:track:{self.ID}"])

    def test_insertar_extras_posiciones_7_11_15(self):
        base = [f"u{i}" for i in range(20)]
        r = logic.insertar_extras(base, ["e1", "e2", "e3"])
        self.assertEqual(r.index("e1"), 6)
        self.assertEqual(r.index("e2"), 10)
        self.assertEqual(r.index("e3"), 14)
        self.assertEqual(len(r), 23)

    def test_insertar_extras_equivale_al_metodo_antiguo(self):
        """El código viejo insertaba de una en una con position=6, 10, 14 en la playlist."""
        base = [f"u{i}" for i in range(30)]
        extras = ["e1", "e2", "e3", "e4"]
        esperado, pos = list(base), 6
        for e in extras:
            esperado.insert(pos, e)
            pos += 4
        self.assertEqual(logic.insertar_extras(base, extras), esperado)

    def test_extra_ya_presente_no_se_duplica(self):
        r = logic.insertar_extras(["a", "b", "c"], ["b"])
        self.assertEqual(r.count("b"), 1)

    def test_playlist_corta_no_falla(self):
        self.assertEqual(logic.insertar_extras(["a", "b"], ["e"]), ["a", "b", "e"])


class TestTextos(unittest.TestCase):
    def test_nombres_identicos_a_los_de_antes(self):
        a = "Bad Bunny"
        self.assertEqual(logic.nombre_playlist(a, "es"), "Bad Bunny Éxitos de Bad Bunny MIX Mejores canciones de Bad Bunny")
        self.assertEqual(logic.nombre_playlist(a, "en"), "Bad Bunny All Songs - Best of Bad Bunny MIX | Bad Bunny Greatest Hits")
        self.assertEqual(logic.nombre_playlist(a, "pt"), "Bad Bunny As Melhores - Bad Bunny MIX Melhores músicas de Bad Bunny")
        self.assertEqual(logic.nombre_playlist(a, "de"), "Bad Bunny: Hits von Bad Bunny MIX - Die besten Lieder von Bad Bunny")
        self.assertEqual(logic.nombre_playlist(a, "fr"), "Bad Bunny Toutes les chansons - Best of Bad Bunny MIX | Meilleurs titres de Bad Bunny")
        self.assertEqual(logic.nombre_playlist(a, "xx"), logic.nombre_playlist(a, "es"))

    def test_descripcion_max_300_y_18_canciones(self):
        d = logic.descripcion_playlist("X", "es", [f"Cancion{i}" for i in range(40)])
        self.assertLessEqual(len(d), 300)
        self.assertTrue(d.startswith("Mejores canciones de X: Cancion0, Cancion1"))
        self.assertNotIn("Cancion18", d)

    def test_nombre_con_llaves_no_rompe(self):
        self.assertIn("{x}", logic.nombre_playlist("{x}", "es"))


class TestCron(unittest.TestCase):
    def dt(self, h, m=0, d=26):
        return datetime(2026, 10, d, h, m, 0)

    def test_parse_hora(self):
        self.assertEqual(logic.parse_hora("09:05"), (9, 5))
        self.assertEqual(logic.parse_hora("9:05"), (9, 5))
        for malo in ("25:00", "10:60", "abc", "", None):
            with self.assertRaises(ValueError):
                logic.parse_hora(malo)

    def test_no_ejecuta_antes_de_la_hora(self):
        self.assertFalse(logic.debe_ejecutar(9, 0, self.dt(8, 59), self.dt(0, d=1), []))

    def test_recupera_si_el_servidor_estaba_dormido(self):
        self.assertTrue(logic.debe_ejecutar(9, 0, self.dt(9, 14), self.dt(0, d=1), []))

    def test_campana_creada_despues_de_la_hora_no_corre_hoy(self):
        self.assertFalse(logic.debe_ejecutar(9, 0, self.dt(15), self.dt(14), []))

    def test_no_repite_si_hoy_ya_termino_o_esta_en_curso(self):
        for estado in ("Completada", "Parcial", "Pausada: cuota", "En curso"):
            self.assertFalse(logic.debe_ejecutar(9, 0, self.dt(12), self.dt(0, d=1), [estado]), estado)

    def test_reintenta_fallos_hasta_3_veces(self):
        self.assertTrue(logic.debe_ejecutar(9, 0, self.dt(12), None, ["Falla: Sin token"]))
        self.assertTrue(logic.debe_ejecutar(9, 0, self.dt(12), None, ["Interrumpida", "Falla: Crash interno"]))
        self.assertFalse(logic.debe_ejecutar(9, 0, self.dt(12), None, ["Falla: Sin token"] * 3))

    def test_retry_after(self):
        self.assertEqual(logic.segundos_reintento({"Retry-After": "7"}), 7)
        self.assertEqual(logic.segundos_reintento({"retry-after": "3.2"}), 3)
        self.assertEqual(logic.segundos_reintento(None), 5)
        self.assertEqual(logic.segundos_reintento({"Retry-After": "basura"}, 9), 9)


if __name__ == "__main__":
    unittest.main()
