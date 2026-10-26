"""Pruebas de spotify_utils con un Spotify FALSO (no se hace ninguna llamada real).

Si spotipy está instalado se usa un doble de todos modos: aquí se prueba NUESTRA lógica
(reintentos, extras, fallbacks, número de llamadas), no la librería.
"""
import io
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ------------------------------------------------------------------ doble de spotipy
class SpotifyException(Exception):
    def __init__(self, http_status, code, msg, reason=None, headers=None):
        super().__init__(f"http status: {http_status}, code: {code} - {msg}")
        self.http_status, self.code, self.msg, self.reason, self.headers = http_status, code, msg, reason, headers


class FakeSP:
    """Registra cada llamada. `fallos` permite programar errores: {'search': [exc, None, ...]}."""
    instancia = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.llamadas = []
        self.fallos = {}
        self.me_playlists_existe = True
        FakeSP.instancia = self

    def _maybe_fail(self, nombre):
        cola = self.fallos.get(nombre)
        if cola:
            exc = cola.pop(0)
            if exc:
                raise exc

    # búsqueda: 10 canciones por página; 8 de cada 10 son del artista A1, el resto de otro
    def search(self, q=None, type=None, limit=None, offset=0, **kw):
        self.llamadas.append(("search", q, type, limit, offset))
        self._maybe_fail("search")
        if type == "artist":
            if "inexistente" in q:
                return {"artists": {"items": []}}
            return {"artists": {"items": [
                {"id": "OTRO", "name": "Otro Distinto", "images": []},
                {"id": "A1", "name": "Bad Bunny", "images": [{"url": "http://x/img.jpg"}]},
            ]}}
        assert limit <= 10, "Spotify ya no acepta limit > 10"
        items = []
        for i in range(10):
            n = offset + i
            artistas = [{"id": "ZZZ"}] if n % 5 == 4 else [{"id": "A1"}]   # 8 de cada 10 son del artista
            items.append({"uri": f"spotify:track:t{n:022d}", "name": f"Cancion {n}", "artists": artistas})
        return {"tracks": {"items": items}}

    def _post(self, url, args=None, payload=None, **kw):
        self.llamadas.append(("POST", url, payload))
        self._maybe_fail("_post")
        if url == "me/playlists":
            if not self.me_playlists_existe:
                raise SpotifyException(404, -1, "no existe")
            return {"id": "PL1", "external_urls": {"spotify": "https://open.spotify.com/playlist/PL1"}}
        return {"snapshot_id": "x"}

    def _delete(self, url, args=None, payload=None, **kw):
        self.llamadas.append(("DELETE", url, args))
        self._maybe_fail("_delete")

    def current_user(self):
        self.llamadas.append(("current_user",))
        return {"id": "usuario1"}

    def user_playlist_create(self, user, name, public=True, description=""):
        self.llamadas.append(("user_playlist_create", user, name))
        return {"id": "PL_OLD", "external_urls": {"spotify": "https://open.spotify.com/playlist/PL_OLD"}}

    def playlist_add_items(self, playlist_id, items, position=None):
        self.llamadas.append(("playlist_add_items", playlist_id, items))

    def playlist_upload_cover_image(self, playlist_id, image_b64):
        self.llamadas.append(("cover", playlist_id, len(image_b64)))

    def current_user_unfollow_playlist(self, playlist_id):
        self.llamadas.append(("unfollow", playlist_id))

    def artist_albums(self, *a, **k):
        self.llamadas.append(("artist_albums",))
        return {"items": []}

    def album_tracks(self, *a, **k):
        self.llamadas.append(("album_tracks",))
        return {"items": []}

    def track(self, *a, **k):  # el código viejo lo llamaba 18 veces por playlist
        self.llamadas.append(("track",))
        raise AssertionError("sp.track() ya no debe usarse")

    def contar(self, nombre):
        return sum(1 for c in self.llamadas if c[0] == nombre)


def _instalar_doble():
    spotipy = types.ModuleType("spotipy")
    spotipy.Spotify = FakeSP
    exc = types.ModuleType("spotipy.exceptions")
    exc.SpotifyException = SpotifyException
    oauth = types.ModuleType("spotipy.oauth2")
    oauth.SpotifyOAuth = object
    cache = types.ModuleType("spotipy.cache_handler")
    cache.MemoryCacheHandler = object
    spotipy.exceptions, spotipy.oauth2, spotipy.cache_handler = exc, oauth, cache
    sys.modules.update({"spotipy": spotipy, "spotipy.exceptions": exc,
                        "spotipy.oauth2": oauth, "spotipy.cache_handler": cache})


_instalar_doble()
import spotify_utils as su  # noqa: E402

# Nunca hay red en las pruebas: la foto de respaldo del artista siempre "da 404".
su.requests.get = lambda *a, **k: types.SimpleNamespace(status_code=404, content=b"")

ID_EXTRA_1, ID_EXTRA_2 = "3w3y8KPTfNeOKPiqUTakBh", "49nVhGJCoo7b92dWVKMHQn"


def crear_cliente():
    return su.SpotifyClient(lambda: "token-abc", "miguel")


class TestCrearPlaylist(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(su.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_flujo_completo_y_numero_de_llamadas(self):
        cliente = crear_cliente()
        res = su.crear_playlist_para_artista(
            cliente, "bad bunny", 35, "es", id_extra=[ID_EXTRA_1, f"spotify:track:{ID_EXTRA_2}"],
            imagen_loader=lambda nombre: su.preparar_jpeg(_png(800, 600)),
        )
        self.assertEqual(res["status"], "success", res)
        self.assertEqual(res["playlist_id"], "PL1")
        sp = FakeSP.instancia

        self.assertEqual(sp.contar("track"), 0)            # antes: 18 llamadas a sp.track()
        self.assertEqual(sp.contar("current_user"), 0)     # antes: 1 por playlist
        self.assertEqual(sp.contar("user_playlist_create"), 0)
        self.assertEqual(sp.contar("cover"), 1)
        # 1 búsqueda de artista + 5 páginas de canciones + crear + añadir + portada = 9 llamadas
        # (antes: ~28, por las 18 llamadas a sp.track() y las inserciones de extras una por una)
        total = len(sp.llamadas)
        self.assertLessEqual(total, 10, sp.llamadas)

        crear = [c for c in sp.llamadas if c[0] == "POST" and c[1] == "me/playlists"][0]
        self.assertEqual(crear[2]["name"], "Bad Bunny Éxitos de Bad Bunny MIX Mejores canciones de Bad Bunny")
        self.assertTrue(crear[2]["public"])

        items = [c for c in sp.llamadas if c[0] == "POST" and c[1] == "playlists/PL1/items"]
        self.assertEqual(len(items), 1)                    # todo en una sola llamada
        uris = items[0][2]["uris"]
        self.assertEqual(uris[6], f"spotify:track:{ID_EXTRA_1}")    # posición 7
        self.assertEqual(uris[10], f"spotify:track:{ID_EXTRA_2}")   # posición 11
        self.assertEqual(len(uris), 35 + 2)

    def test_la_busqueda_pagina_con_limit_10(self):
        crear_cliente()
        su.crear_playlist_para_artista(crear_cliente(), "Bad Bunny", 35)
        busquedas = [c for c in FakeSP.instancia.llamadas if c[0] == "search" and c[2] == "track"]
        self.assertTrue(all(c[3] == 10 for c in busquedas))
        self.assertEqual([c[4] for c in busquedas][:3], [0, 10, 20])

    def test_elige_el_artista_con_nombre_exacto_no_el_primero(self):
        cliente = crear_cliente()
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny", 20)
        self.assertEqual(res["artista"], "Bad Bunny")

    def test_artista_inexistente_es_error_definitivo(self):
        res = su.crear_playlist_para_artista(crear_cliente(), "inexistente xyz")
        self.assertEqual(res["status"], "error")
        self.assertTrue(res["definitivo"])

    def test_respaldo_si_me_playlists_no_existe(self):
        cliente = crear_cliente()
        FakeSP.instancia.me_playlists_existe = False
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny", 20)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["playlist_id"], "PL_OLD")
        self.assertEqual(FakeSP.instancia.contar("current_user"), 1)   # solo aquí hace falta el id de usuario

    def test_sin_token_devuelve_auth_required(self):
        cliente = su.SpotifyClient(lambda: "t", "x")
        cliente.proveedor_token = lambda: None
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny")
        self.assertEqual(res["status"], "auth_required")

    def test_si_la_portada_falla_la_playlist_igual_se_crea(self):
        cliente = crear_cliente()
        FakeSP.instancia.playlist_upload_cover_image = mock.Mock(side_effect=SpotifyException(400, -1, "imagen mala"))
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny", 20, imagen_loader=lambda n: su.preparar_jpeg(_png(300, 300)))
        self.assertEqual(res["status"], "success")


class TestReintentos(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(su.time, "sleep")
        self.sleep = p.start()
        self.addCleanup(p.stop)

    def test_429_corto_espera_y_reintenta(self):
        cliente = crear_cliente()
        FakeSP.instancia.fallos["search"] = [SpotifyException(429, -1, "slow", headers={"Retry-After": "3"})]
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny", 12)
        self.assertEqual(res["status"], "success")
        self.assertTrue(any(c.args[0] >= 3 for c in self.sleep.call_args_list))   # respetó Retry-After

    def test_429_largo_corta_la_ejecucion(self):
        cliente = crear_cliente()
        FakeSP.instancia.fallos["search"] = [SpotifyException(429, -1, "limit", headers={"Retry-After": "86400"})]
        with self.assertRaises(su.CuotaAgotada) as cm:
            su.crear_playlist_para_artista(cliente, "Bad Bunny", 12)
        self.assertEqual(cm.exception.espera, 86400)

    def test_quota_exceeded_corta_aunque_el_retry_after_sea_corto(self):
        cliente = crear_cliente()
        FakeSP.instancia.fallos["search"] = [
            SpotifyException(429, -1, '{"reason": "QUOTA_EXCEEDED"}', headers={"Retry-After": "2"})]
        with self.assertRaises(su.CuotaAgotada):
            su.crear_playlist_para_artista(cliente, "Bad Bunny", 12)

    def test_error_503_se_reintenta(self):
        cliente = crear_cliente()
        FakeSP.instancia.fallos["search"] = [SpotifyException(503, -1, "caido")]
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny", 12)
        self.assertEqual(res["status"], "success")

    def test_403_en_me_playlists_prueba_el_endpoint_anterior(self):
        cliente = crear_cliente()
        FakeSP.instancia.fallos["_post"] = [SpotifyException(403, -1, "forbidden")]
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny", 12)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["playlist_id"], "PL_OLD")

    def test_error_de_permisos_se_explica_al_usuario(self):
        cliente = crear_cliente()
        FakeSP.instancia.fallos["search"] = [SpotifyException(403, -1, "Insufficient client scope")]
        res = su.crear_playlist_para_artista(cliente, "Bad Bunny", 12)
        self.assertEqual(res["status"], "error")
        self.assertIn("permisos", res["message"])


class TestEliminar(unittest.TestCase):
    def test_usa_me_library_y_cae_al_endpoint_viejo(self):
        cliente = crear_cliente()
        su.eliminar_playlist(cliente, "PL9")
        self.assertEqual(FakeSP.instancia.llamadas[-1], ("DELETE", "me/library", {"uris": "spotify:playlist:PL9"}))
        FakeSP.instancia.fallos["_delete"] = [SpotifyException(404, -1, "no")]
        su.eliminar_playlist(cliente, "PL9")
        self.assertEqual(FakeSP.instancia.llamadas[-1], ("unfollow", "PL9"))


class TestImagenes(unittest.TestCase):
    def test_reduce_a_menos_de_250kb_y_es_jpeg(self):
        grande = _png(3000, 2000, ruido=True)
        self.assertGreater(len(grande), 250_000)
        jpeg = su.preparar_jpeg(grande)
        self.assertLessEqual(len(jpeg), 250_000)
        self.assertEqual(jpeg[:2], b"\xff\xd8")

    def test_archivo_que_no_es_imagen(self):
        with self.assertRaises(ValueError):
            su.preparar_jpeg(b"esto no es una imagen")


class TestMasivo(unittest.TestCase):
    def test_se_detiene_si_se_agota_la_cuota_y_hace_pausas(self):
        with mock.patch.object(su.time, "sleep") as sleep:
            def fabrica(**kw):
                sp = FakeSP(**kw)
                return sp
            su.spotipy.Spotify = fabrica
            res = su.crear_listas_por_nombres(["Bad Bunny", "Bad Bunny", "Bad Bunny"],
                                              proveedor_token=lambda: "t", pausa=2)
            su.spotipy.Spotify = FakeSP
        self.assertEqual([r["status"] for r in res], ["success"] * 3)
        self.assertEqual(sleep.call_count, 2)    # pausa entre listas, no después de la última


def _png(ancho, alto, ruido=False):
    from PIL import Image
    if ruido:
        datos = os.urandom(ancho * alto * 3)
        img = Image.frombytes("RGB", (ancho, alto), datos)
    else:
        img = Image.new("RGB", (ancho, alto), (30, 180, 80))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


if __name__ == "__main__":
    unittest.main()
