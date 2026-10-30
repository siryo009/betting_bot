"""Test del doctor — tutti OFFLINE.

Il doctor tocca file, immagini e (solo se ci sono credenziali) l'API: qui si
verifica che le regole siano quelle dichiarate e che senza credenziali il
verdetto sia `pending`, non `error` (un blocco sul proprietario non e' un
guasto del negozio).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import doctor as D  # noqa: E402
import publish as P  # noqa: E402


def png(path: Path, rgba: tuple[int, int, int, int], size: tuple[int, int] = (80, 100)) -> Path:
    from PIL import Image

    with Image.new("RGBA", size, rgba) as im:
        im.save(path)
    return path


@pytest.fixture()
def real_listings():
    return P.read_listings(P.DEFAULT_LISTINGS)


@pytest.fixture(scope="session")
def session_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("doctor-cache") / "cache.json"


@pytest.fixture(autouse=True)
def _cache_env(session_cache, monkeypatch):
    """Cache fuori dal progetto, condivisa dalla sessione: e' il caso REALE
    (piu' esecuzioni del doctor sulle stesse immagini) e senza condivisione ogni
    test che chiama `run()` ripagherebbe i 30 secondi di decodifica."""
    monkeypatch.setenv("DOCTOR_CACHE", str(session_cache))
    D.reset_ink_cache()
    yield
    D.reset_ink_cache()


# ---------------------------------------------------------------------------
# colore
# ---------------------------------------------------------------------------


class TestColore:
    def test_luminanza_estremi(self):
        assert D.luminance((255, 255, 255)) == pytest.approx(1.0, abs=0.01)
        assert D.luminance((0, 0, 0)) == pytest.approx(0.0, abs=0.01)

    def test_contrasto_bianco_nero(self):
        assert D.contrast((255, 255, 255), (0, 0, 0)) == pytest.approx(21.0, abs=0.2)

    def test_contrasto_simmetrico(self):
        a, b = (12, 34, 56), (250, 250, 240)
        assert D.contrast(a, b) == pytest.approx(D.contrast(b, a))

    def test_contrasto_colori_identici(self):
        assert D.contrast((10, 20, 30), (10, 20, 30)) == pytest.approx(1.0)

    def test_soglie_dichiarate_coerenti(self):
        assert 3.0 <= D.LEGIBLE_CONTRAST <= 7.0
        assert 0.0 < D.MIN_LEGIBLE_SHARE < D.RISK_LEGIBLE_SHARE <= 1.0
        assert 1.0 < D.MIN_MEAN_CONTRAST < 2.0
        assert D.MIN_MARGIN_EUR > 0


class TestMetricheInchiostro:
    def test_bianco_su_capo_bianco_sparisce(self, tmp_path):
        path = png(tmp_path / "white.png", (255, 255, 255, 255))
        metrics = D.ink_metrics(path, (255, 255, 255))
        assert metrics["legible_share"] == 0.0
        assert metrics["mean_contrast"] == pytest.approx(1.0, abs=0.02)

    def test_nero_su_capo_bianco_si_legge(self, tmp_path):
        path = png(tmp_path / "black.png", (16, 18, 20, 255))
        metrics = D.ink_metrics(path, (255, 255, 255))
        assert metrics["legible_share"] > 0.95
        assert metrics["mean_contrast"] > 10

    def test_nero_su_capo_nero_sparisce(self, tmp_path):
        path = png(tmp_path / "black2.png", (12, 12, 12, 255))
        metrics = D.ink_metrics(path, (0, 0, 0))
        assert metrics["legible_share"] == 0.0
        assert metrics["mean_contrast"] < D.MIN_MEAN_CONTRAST

    def test_crema_su_capo_nero_si_legge(self, tmp_path):
        path = png(tmp_path / "cream.png", (247, 239, 217, 255))
        assert D.ink_metrics(path, (0, 0, 0))["legible_share"] > 0.95

    def test_immagine_tutta_trasparente(self, tmp_path):
        path = png(tmp_path / "trasp.png", (0, 0, 0, 0))
        assert D.ink_metrics(path, (255, 255, 255))["legible_share"] == 0.0

    def test_memo_non_mente_se_il_file_cambia(self, tmp_path):
        """La memo e' sulla firma del file: stesso percorso, contenuto nuovo =
        nuova misura (una cache che non se ne accorgesse direbbe 'leggibile' di
        un disegno riscritto in bianco su capo bianco)."""
        path = png(tmp_path / "memo.png", (16, 18, 20, 255))
        assert D.ink_metrics(path, (255, 255, 255))["legible_share"] > 0.95
        path.unlink()
        png(path, (255, 255, 255, 255))
        assert D.ink_metrics(path, (255, 255, 255))["legible_share"] == 0.0
        D.reset_ink_cache()

    def test_disegno_misto_ha_una_parte_leggibile(self, tmp_path):
        """Un disegno con blocchi colorati e testo scuro NON e' 'invisibile':
        la misura deve distinguere il testo (leggibile) dal riempimento."""
        from PIL import Image

        path = tmp_path / "misto.png"
        with Image.new("RGBA", (100, 100), (255, 255, 255, 0)) as im:
            for y in range(100):
                for x in range(100):
                    if y < 70:
                        im.putpixel((x, y), (232, 169, 60, 255))  # ambra: contrasto basso su bianco
                    else:
                        im.putpixel((x, y), (26, 42, 34, 255))  # testo scuro: leggibile
            im.save(path)
        metrics = D.ink_metrics(path, (255, 255, 255), step=1)
        assert metrics["legible_share"] == pytest.approx(0.30, abs=0.02)
        assert metrics["legible_share"] > D.MIN_LEGIBLE_SHARE


# ---------------------------------------------------------------------------
# sezioni
# ---------------------------------------------------------------------------


class TestCacheSuDisco:
    """La prima passata del doctor costava 30s a OGNI lancio: la cache su disco
    esiste per questo, e queste prove verificano che non possa mentire.

    Questi test hanno una cache PROPRIA per ogni caso: partono da un file
    vuoto e possono quindi controllare cosa ci finisce dentro.
    """

    @pytest.fixture(autouse=True)
    def isolated_cache(self, tmp_path, monkeypatch):
        path = tmp_path / "cache.json"
        monkeypatch.setenv("DOCTOR_CACHE", str(path))
        D.reset_ink_cache(drop_disk=True)
        yield path
        D.reset_ink_cache(drop_disk=True)

    def _boom(self, monkeypatch):
        """Se la lettura delle immagini parte, il test deve fallire subito."""
        from PIL import Image

        def explode(*a, **k):
            raise AssertionError("immagine riaperta: la cache non e' stata usata")

        monkeypatch.setattr(Image, "open", explode)

    def test_seconda_esecuzione_non_ridecoda(self, tmp_path, monkeypatch, isolated_cache):
        path = png(tmp_path / "cached.png", (26, 42, 34, 255))
        first = D.ink_metrics(path, (255, 255, 255))
        assert isolated_cache.exists()
        D.reset_ink_cache()  # solo la memoria: il disco resta
        self._boom(monkeypatch)
        assert D.ink_metrics(path, (255, 255, 255)) == first

    def test_file_riscritto_invalida_anche_il_disco(self, tmp_path, isolated_cache):
        path = png(tmp_path / "rewrite.png", (16, 18, 20, 255))
        assert D.ink_metrics(path, (255, 255, 255))["legible_share"] > 0.95
        path.unlink()
        png(path, (255, 255, 255, 255))
        D.reset_ink_cache()
        assert D.ink_metrics(path, (255, 255, 255))["legible_share"] == 0.0

    def test_capo_diverso_non_riusa_la_misura(self, tmp_path, isolated_cache):
        path = png(tmp_path / "same.png", (26, 42, 34, 255))
        white = D.ink_metrics(path, (255, 255, 255))
        D.reset_ink_cache()
        black = D.ink_metrics(path, (0, 0, 0))
        assert white["legible_share"] > 0.9
        assert black["legible_share"] < 0.2

    def test_cache_corrotto_non_rompe(self, tmp_path, isolated_cache):
        isolated_cache.write_text("{non sono json", encoding="utf-8")
        D.reset_ink_cache()
        path = png(tmp_path / "ok.png", (16, 18, 20, 255))
        assert D.ink_metrics(path, (255, 255, 255))["legible_share"] > 0.95
        assert D.ink_metrics(path, (255, 255, 255))["legible_share"] > 0.95

    def test_cache_scrive_voci_valide(self, tmp_path, isolated_cache):
        path = png(tmp_path / "entry.png", (16, 18, 20, 255))
        D.ink_metrics(path, (255, 255, 255))
        payload = json.loads(isolated_cache.read_text(encoding="utf-8"))
        assert payload["version"] == D._DISK_CACHE_VERSION
        (entry,) = payload["entries"].values()
        assert set(entry) == {"legible_share", "mean_contrast", "max_contrast"}

    def test_disattivabile(self, tmp_path, monkeypatch, isolated_cache):
        monkeypatch.setenv("DOCTOR_CACHE", "0")
        assert D.cache_path() is None
        D.reset_ink_cache()
        png(tmp_path / "nosave.png", (16, 18, 20, 255))
        D.ink_metrics(tmp_path / "nosave.png", (255, 255, 255))
        assert not isolated_cache.exists()

    def test_percorso_configurabile(self, tmp_path, monkeypatch):
        custom = tmp_path / "sub" / "misure.json"
        monkeypatch.setenv("DOCTOR_CACHE", str(custom))
        D.reset_ink_cache(drop_disk=True)
        assert D.cache_path() == custom
        image = png(tmp_path / "custom.png", (16, 18, 20, 255))
        D.ink_metrics(image, (255, 255, 255))
        assert custom.exists()

    def test_default_accanto_al_progetto(self, monkeypatch):
        monkeypatch.delenv("DOCTOR_CACHE", raising=False)
        assert D.cache_path() == D.ROOT / ".doctor_cache.json"

    def test_scrittura_impossibile_non_solleva(self, tmp_path, monkeypatch):
        # un percorso che non puo' essere creato: una cache non scrivibile non
        # deve fermare un controllo pre-volo
        monkeypatch.setenv("DOCTOR_CACHE", str(tmp_path / "file" / "sub" / "cache.json"))
        (tmp_path / "file").write_text("sono un file", encoding="utf-8")
        D.reset_ink_cache(drop_disk=True)
        image = png(tmp_path / "nofail.png", (16, 18, 20, 255))
        assert D.ink_metrics(image, (255, 255, 255))["legible_share"] > 0.95


class TestSezioni:
    def _row(self, price: float, margin: float, base_cost: float = 7.90):
        return type("Row", (), {"price": price, "margin": margin, "base_cost": base_cost})()

    def test_economia_sopra_soglia(self):
        section = D.check_economics([self._row(24.99, 12.58)])[0]
        assert section["level"] == "ok"
        assert "12.58" in section["detail"]
        assert section["data"]["base_cost"] == pytest.approx(7.90)

    def test_economia_sotto_soglia_e_errore(self):
        section = D.check_economics([self._row(19.99, 5.0)])[0]
        assert section["level"] == "error"

    def test_economia_senza_righe(self):
        assert D.check_economics([])[0]["level"] == "error"

    def test_api_senza_credenziali_e_attesa(self, monkeypatch):
        monkeypatch.delenv("HOPLIX_API_KEY", raising=False)
        monkeypatch.delenv("HOPLIX_SECRET", raising=False)
        section = D.check_api()[0]
        assert section["level"] == "pending"
        assert "GO_LIVE" in section["detail"]

    def test_api_offline_e_attesa(self):
        assert D.check_api(offline=True)[0]["level"] == "pending"

    def test_ordini_senza_credenziali_e_attesa(self, monkeypatch):
        monkeypatch.delenv("HOPLIX_API_KEY", raising=False)
        monkeypatch.delenv("HOPLIX_SECRET", raising=False)
        assert D.check_orders()[0]["level"] == "pending"

    def test_originalita_trova_i_vettoriali(self, real_listings):
        section = D.check_origin(real_listings)[0]
        assert section["level"] == "ok", section["detail"]
        assert section["data"]["vectors"] >= len(real_listings)

    def test_originalita_con_listing_senza_svg(self, tmp_path, real_listings):
        fake = real_listings[0]
        busted = type(fake)(
            **{
                **fake.__dict__,
                "image_light": png(tmp_path / "tee-qualcosa-di-inesistente.png", (0, 0, 0, 255)),
            }
        )
        section = D.check_origin([busted])[0]
        assert section["level"] == "error"

    def test_slug_da_nome_file(self):
        assert D._design_slug(Path("tee-404-regalo-non-trovato.png")) == "404-regalo-non-trovato"
        assert D._design_slug(Path("tee-dark-404-regalo-non-trovato.png")) == "404-regalo-non-trovato"
        assert D._design_slug(Path("sticker-ctrl-alt-regala.png")) == "ctrl-alt-regala"


# ---------------------------------------------------------------------------
# orchestrazione
# ---------------------------------------------------------------------------


class TestRun:
    def test_run_offline_sui_dati_veri(self):
        report = D.run(offline=True)
        names = {s["name"] for s in report["sections"]}
        assert {
            "schede",
            "immagini",
            "area_di_stampa",
            "abbinamento_capo",
            "testi",
            "marchi",
            "originalita",
            "economia",
            "manifest_sito",
            "peso_immagini",
            "api_hoplix",
            "ordini",
        } <= names
        assert report["errors"] == 0, [s["detail"] for s in report["sections"] if s["level"] == "error"]
        assert report["pending"] >= 2  # credenziali API e ordini
        assert report["ok"] is True

    def test_economia_uguale_alla_produzione(self):
        report = D.run(offline=True)
        economy = next(s for s in report["sections"] if s["name"] == "economia")
        assert economy["level"] == "ok"
        assert economy["data"]["min_margin"] == D.MIN_MARGIN_EUR
        assert economy["data"]["base_cost"] == pytest.approx(7.90)

    def test_verdetto_riporta_le_attese(self):
        report = D.run(offline=True)
        text = D.format_report(report)
        assert "PRONTO (con attese)" in text
        assert "[ATTESA]" in text

    def test_report_senza_attese_e_pronto(self):
        report = {"sections": [D._section("x", "ok", "tutto bene")], "errors": 0, "warnings": 0, "pending": 0, "ok": True}
        assert "Esito: PRONTO —" in D.format_report(report)

    def test_csv_mancante_non_solleva(self, tmp_path):
        report = D.run(listings_path=tmp_path / "assente.csv", offline=True)
        assert report["ok"] is False
        assert report["sections"][0]["name"] == "csv"

    def test_json_serializzabile(self):
        payload = json.dumps(D.run(offline=True), ensure_ascii=False, default=str)
        assert '"sections"' in payload

    def test_exit_code_zero_offline(self):
        assert D.main(["--offline"]) == 0

    def test_exit_code_uno_con_csv_rotto(self, tmp_path):
        broken = tmp_path / "bad.csv"
        broken.write_text("niente,colonne\n1,2\n", encoding="utf-8")
        assert D.main(["--offline", "--listings", str(broken)]) == 1


class TestTripwire:
    def test_nessun_import_di_rete_a_livello_modulo(self):
        source = (ROOT / "doctor.py").read_text(encoding="utf-8")
        assert "import requests" not in source
        assert "import httpx" not in source

    def test_nessun_percorso_d_ordine(self):
        source = (ROOT / "doctor.py").read_text(encoding="utf-8")
        for token in ("create_campaign", "createOrder", "place_order", "save_bet"):
            assert token not in source

    def test_il_margine_non_e_una_costante_ricopiata(self):
        """Il margine si legge da catalog/platforms, non da un numero scritto qui."""
        source = (ROOT / "doctor.py").read_text(encoding="utf-8")
        assert "12.58" not in source
        assert "build_rows" in source

    def test_import_leggero(self):
        code = "import sys; sys.path.insert(0, '.'); import doctor; print('bot' in sys.modules, 'tracker' in sys.modules)"
        import subprocess

        out = subprocess.run(
            [sys.executable, "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=120
        )
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "False False"
