"""Test della risoluzione nomi squadra (team_names.py, 11/09/2026).

Copre il fix della "cecita' del modello": i nomi dei bookmaker (SX Bet) non
coincidono con quelli del DB (`team_ratings`) e senza risoluzione
`get_rating` non trova nulla -> `expected_goals` usa il profilo neutro.
Tutti i casi qui sotto sono presi dal campione REALE misurato sul container.
"""
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

import team_names


# ---------------------------------------------------------------------------
# 1. Normalizzazione
# ---------------------------------------------------------------------------

class TestNormalize:
    def test_token_societari_rimossi(self):
        assert team_names.normalize("AFC Bournemouth") == "bournemouth"
        assert team_names.normalize("AS Monaco FC") == "monaco"
        assert team_names.normalize("KV Mechelen") == "mechelen"
        assert team_names.normalize("RSC Anderlecht") == "anderlecht"
        assert team_names.normalize("Wrexham AFC") == "wrexham"

    def test_accenti_punteggiatura_e_ordine(self):
        assert team_names.normalize("1. FC Köln") == "koln"
        assert team_names.normalize("Tottenham Hotspur") == \
            team_names.normalize("Hotspur Tottenham")
        assert team_names.normalize("  ") == "" and team_names.normalize("") == ""

    def test_idempotente(self):
        once = team_names.normalize("AS Monaco FC")
        assert team_names.normalize(once) == once

    def test_apostrofo_non_lascia_token_s(self):
        """L'apostrofo e' parte del nome, non un separatore: senza il fix
        "Newell's" diventava 'newell s' e non agganciava piu' 'Newells'."""
        assert team_names.normalize("Newell's Old Boys") == \
            team_names.normalize("Newells Old Boys")


# ---------------------------------------------------------------------------
# 1b. Confronto SIMMETRICO tra nomi di provider diversi (`same_team`)
# ---------------------------------------------------------------------------

class TestSameTeam:
    """Casi REALI misurati sul container il 12/09/2026: i nomi SX Bet non
    coincidevano con quelli the-odds-api e le bet restavano aperte per sempre
    (Cienciano/Club Cienciano, CR Flamengo/Flamengo-RJ, ...)."""

    @pytest.mark.parametrize("sx,api", [
        ("Cienciano", "Club Cienciano"),
        ("CR Flamengo", "Flamengo-RJ"),
        ("Vila Nova GO", "Vila Nova"),
        ("Velez Sarsfield", "Velez Sarsfield BA"),
        ("Corinthians SP", "Corinthians-SP"),
        ("Newell's Old Boys", "Newells Old Boys"),
        ("Goias", "Goiás"),
        ("Estudiantes de La Plata", "Estudiantes La Plata"),
        ("Independiente del Valle", "Independiente del Valle"),
        ("Atlanta United", "Atlanta"),
        ("AS Roma", "Roma"),
        ("AFC Bournemouth", "Bournemouth"),
    ])
    def test_stessa_squadra(self, sx, api):
        assert team_names.same_team(sx, api) is True
        assert team_names.same_team(api, sx) is True
        assert team_names.same_team(sx, sx) is True

    def test_squadre_diverse_mai_uguali(self):
        """Mai fuzzy: un falso positivo chiuderebbe una bet col risultato di
        un'altra partita."""
        assert team_names.same_team("Manchester United", "Manchester City") is False
        assert team_names.same_team("Roma", "Lazio") is False
        assert team_names.same_team("Estudiantes", "Velez Sarsfield") is False
        assert team_names.same_team("Corinthians", "Vila Nova") is False
        assert team_names.same_team("Alpha", "Beta") is False

    def test_alias_esplicito_un_livello(self):
        """Caso REALE del 23/10/2026: `team_ratings` contiene SIA 'Wolves' SIA
        'Wolverhampton Wanderers' come voci distinte, e il ledger aveva
        `home_team='Wolves'` con esito 'Wolverhampton Wanderers' -> il segnale
        veniva scartato. L'alias dichiarato (`wolves -> Wolverhampton`) chiude
        il caso.
        """
        assert team_names.same_team("Wolverhampton Wanderers", "Wolves") is True
        assert team_names.same_team("Wolves", "Wolverhampton Wanderers") is True
        # e resta deterministico anche col nome completo
        assert team_names.same_team("Wolves", "Wolverhampton") is True

    def test_alias_non_avvicina_squadre_diverse(self):
        """L'allargamento e' SOLO per alias dichiarati: nessuna coppia estranea
        deve avvicinarsi (un falso positivo chiuderebbe una bet con un'altra
        partita)."""
        for a, b in (("Wolves", "Watford"),
                     ("Wolverhampton Wanderers", "Watford"),
                     ("Tottenham", "Wolverhampton"),
                     ("Manchester United", "Newcastle United")):
            assert team_names.same_team(a, b) is False, (a, b)

    def test_alias_expanded_un_solo_livello(self):
        """Un alias che punta a un altro alias non genera catene: la funzione
        si ferma al primo grado (nessun indovinello)."""
        assert team_names._alias_expanded("Wolves") == "Wolverhampton"
        assert team_names._alias_expanded("Wolverhampton") == "Wolverhampton"
        assert team_names._alias_expanded("Sconosciuta FC") == "Sconosciuta FC"

    def test_alias_confrontati_solo_per_uguaglianza(self):
        """Regressione 23/10/2026: l'alias espanso NON deve passare dal
        contenimento dei token.

        Con `same_team(espanso)` il nome espanso entrava nello stadio 4 e
        'Inter Milan' (-> 'Inter') diventava uguale a QUALSIASI squadra che
        contenesse 'inter': sui 828 nomi reali di `team_ratings` questo
        produceva 6 falsi positivi (Inter Miami, Inter Turku, Inter Club
        d'Escaldes, Sporting Braga) — cioe' partite diverse saldate con lo
        stesso risultato. L'alias unisce solo forme della STESSA squadra.
        """
        for a, b in (("Inter Milan", "Inter Miami"),
                     ("Inter Milan", "Inter Turku"),
                     ("Inter Milan", "FC Inter Turku"),
                     ("Inter Milan", "Inter Club d'Escaldes"),
                     ("FC Internazionale", "Inter Miami"),
                     ("Sporting Lisbon", "Sporting Braga"),
                     ("Sporting Lisbona", "Sporting Braga")):
            assert team_names.same_team(a, b) is False, (a, b)
            assert team_names.same_team(b, a) is False, (a, b)

    def test_alias_resta_equivalenza(self):
        """La stessa squadra con l'alias continua a coincidere (equivalenza,
        non contenimento): e' il caso che la correzione doveva conservare."""
        assert team_names.same_team("Inter Milan", "Inter") is True
        assert team_names.same_team("FC Internazionale", "Inter") is True
        assert team_names.same_team("Sporting Lisbon", "Sporting CP") is True
        assert team_names.same_team("Wolverhampton Wanderers", "Wolverhampton") is True

    def test_contenimento_ambiguo_risolto_dal_chiamante(self):
        """Il contenimento da solo NON e' una prova: 'Manchester' sta sia in
        'Manchester United' sia in 'Manchester City'. Per questo il chiamante
        (settlement) richiede l'UNICITA' del match — vedi
        test_league_mapping.TestSettlementNomiTolleranti."""
        assert team_names.same_team("Manchester", "Manchester United") is True
        assert team_names.same_team("Manchester", "Manchester City") is True

    def test_input_vuoti(self):
        assert team_names.same_team("", "Roma") is False
        assert team_names.same_team("Roma", None) is False
        assert team_names.same_team("   ", "   ") is False


# ---------------------------------------------------------------------------
# 2. Risoluzione contro un pool esplicito (nessun DB)
# ---------------------------------------------------------------------------

POOL = [
    "Tottenham", "Bournemouth", "Ipswich", "Wrexham AFC", "Willem II",
    "Monaco", "Marseille", "Nottm Forest", "Liverpool", "Manchester United",
    "Manchester City", "Mechelen", "Almere City", "Hull City",
    "Inter", "Milan", "Paris Saint-Germain",
]


class TestResolve:
    def test_esatto_e_case_insensitive(self):
        assert team_names.resolve_team("Liverpool", POOL) == "Liverpool"
        assert team_names.resolve_team("liverpool", POOL) == "Liverpool"
        assert team_names.resolve_team("  Liverpool  ", POOL) == "Liverpool"

    def test_token_societari(self):
        assert team_names.resolve_team("AFC Bournemouth", POOL) == "Bournemouth"
        assert team_names.resolve_team("Wrexham", POOL) == "Wrexham AFC"
        assert team_names.resolve_team("KV Mechelen", POOL) == "Mechelen"
        assert team_names.resolve_team("AS Monaco FC", POOL) == "Monaco"

    def test_alias_espliciti(self):
        assert team_names.resolve_team("Nottingham Forest", POOL) == \
            "Nottm Forest"
        assert team_names.resolve_team("Nottm Forest", POOL) == "Nottm Forest"
        assert team_names.resolve_team("Spurs", POOL) == "Tottenham"
        assert team_names.resolve_team("PSG", POOL) == "Paris Saint-Germain"

    def test_contenimento_di_token(self):
        # I casi reali del report sul container.
        assert team_names.resolve_team("Tottenham Hotspur", POOL) == "Tottenham"
        assert team_names.resolve_team("Ipswich Town", POOL) == "Ipswich"
        assert team_names.resolve_team("Willem II Tilburg", POOL) == "Willem II"
        assert team_names.resolve_team("Olympique Marseille", POOL) == \
            "Marseille"
        # Sigla che la normalizzazione non copre: risolve l'alias.
        assert team_names.resolve_team("FC Internazionale", POOL) == "Inter"

    def test_nessun_falso_positivo(self):
        # 'Chelsea' NON deve agganciare 'Almere City' (il vecchio fuzzy 0.60).
        assert team_names.resolve_team("Chelsea", POOL) is None
        # Citta' diverse con token in comune: mai un match.
        assert team_names.resolve_team("Manchester City", POOL) == \
            "Manchester City"
        assert team_names.resolve_team("Squadra Inventata", POOL) is None

    def test_ambiguita_rifiutata(self):
        # Contenimento: entrambi i Botafogo sono a pari merito -> nessuna scelta.
        pool = ["Botafogo SP", "Botafogo FR"]
        assert team_names.resolve_team("Botafogo", pool) is None
        # Omonimie nel DB: il nome normalizzato coincide con DUE squadre.
        pool2 = ["Manchester City FC", "Manchester City AFC"]
        assert team_names.resolve_team("Manchester City", pool2) is None

    def test_pool_vuoto_o_nome_vuoto(self):
        assert team_names.resolve_team("Liverpool", []) is None
        assert team_names.resolve_team("", POOL) is None
        assert team_names.resolve_team(None, POOL) is None
        assert team_names.resolve_team(123, POOL) is None

    def test_ritorna_solo_nomi_del_pool(self):
        for name in ("Tottenham Hotspur", "AFC Bournemouth", "Spurs"):
            assert team_names.resolve_team(name, POOL) in POOL

    def test_coppia(self):
        assert team_names.resolve_pair("Tottenham Hotspur", "AFC Bournemouth",
                                       POOL) == ("Tottenham", "Bournemouth")


# ---------------------------------------------------------------------------
# 3. Integrazione col DB dei rating (`team_ratings`)
# ---------------------------------------------------------------------------

@pytest.fixture()
def temp_ratings(monkeypatch):
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "test.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""CREATE TABLE team_ratings (
            team TEXT PRIMARY KEY, league TEXT, attack_home REAL,
            defense_home REAL, attack_away REAL, defense_away REAL,
            n_home INTEGER, n_away INTEGER, updated_at TEXT)""")
        conn.execute("INSERT INTO team_ratings VALUES (?,?,?,?,?,?,?,?,?)",
                     ("Tottenham", "Premier League", 1.1, 0.9, 1.0, 0.95,
                      10, 9, datetime.now().isoformat()))
        conn.execute("INSERT INTO team_ratings VALUES (?,?,?,?,?,?,?,?,?)",
                     ("Bournemouth", "Premier League", 0.9, 1.1, 0.85, 1.0,
                      8, 8, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        monkeypatch.setattr(team_names, "DB_PATH", db)
        monkeypatch.setattr("rating_engine.DB_PATH", db)
        team_names.invalidate()
        yield db
        team_names.invalidate()


class TestDbIntegration:
    def test_known_teams_dal_db(self, temp_ratings):
        assert team_names.known_teams(refresh=True) == \
            {"Tottenham", "Bournemouth"}

    def test_get_rating_risolve_il_nome_del_bookmaker(self, temp_ratings):
        import rating_engine
        # SX Bet usa 'Tottenham Hotspur': senza risoluzione -> None.
        r = rating_engine.get_rating("Tottenham Hotspur")
        assert r is not None
        assert r["attack_home"] == pytest.approx(1.1)
        assert rating_engine.get_rating("AFC Bournemouth") is not None

    def test_get_rating_squadra_ignota_resta_none(self, temp_ratings):
        import rating_engine
        assert rating_engine.get_rating("Squadra Inventata") is None

    def test_resolve_team_name_fail_safe(self, temp_ratings):
        import rating_engine
        # Ignota: si torna al nome originale (poi get_rating -> None).
        assert rating_engine.resolve_team_name("Squadra Inventata") == \
            "Squadra Inventata"
        assert rating_engine.resolve_team_name("Tottenham Hotspur") == \
            "Tottenham"

    def test_invalidate_vede_i_nuovi_rating(self, temp_ratings):
        assert team_names.resolve_team("Ipswich Town") is None
        team_names.invalidate()
        conn = sqlite3.connect(str(temp_ratings))
        conn.execute("INSERT INTO team_ratings VALUES (?,?,?,?,?,?,?,?,?)",
                     ("Ipswich", "Premier League", 1.0, 1.0, 1.0, 1.0,
                      7, 7, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        assert team_names.resolve_team("Ipswich Town") == "Ipswich"


# ---------------------------------------------------------------------------
# 4. Tripwire: il modello non deve piu' essere "cieco"
# ---------------------------------------------------------------------------

class TestModelloNonCieco:
    def test_poisson_engine_usa_il_rating_risolto(self, temp_ratings,
                                                  monkeypatch):
        """`expected_goals` con nomi SX deve usare il rating reale, non il
        profilo neutro: i lambda coincidono con quelli dei nomi DB."""
        import poisson_engine
        # Lega forzata: isola la variabile in esame (la risoluzione del nome),
        # cosi' la media di lega non entra nel confronto.
        monkeypatch.setattr(poisson_engine, "_find_team_league",
                            lambda n: "Premier League")
        lam_sx = poisson_engine.expected_goals("Tottenham Hotspur",
                                               "AFC Bournemouth")
        lam_db = poisson_engine.expected_goals("Tottenham", "Bournemouth")
        assert lam_sx == pytest.approx(lam_db)

    def test_profilo_neutro_non_usato_coi_nomi_risolti(self, temp_ratings,
                                                       monkeypatch):
        """Prova diretta di non-cecita': con i nomi SX il ramo neutro
        (`_team_profile`) non deve essere toccato; con squadre ignote si'."""
        import poisson_engine

        def _boom(*a, **k):
            raise AssertionError("profilo neutro usato")

        monkeypatch.setattr(poisson_engine, "_team_profile", _boom)
        assert poisson_engine.expected_goals("Tottenham Hotspur",
                                             "AFC Bournemouth")
        with pytest.raises(AssertionError):
            poisson_engine.expected_goals("Squadra Ignota A",
                                          "Squadra Ignota B")
