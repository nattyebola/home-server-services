#!/usr/bin/env python3
"""Tests des chemins DESTRUCTIFS de core.py — `make test`.

Pourquoi ces sept-là et pas d'autres : ce sont les fonctions dont un bug
supprime des fichiers que personne n'a demandé de supprimer, ou laisse croire
qu'une suppression a eu lieu alors qu'elle a échoué. Le reste du module se
diagnostique en le relisant ; ceux-là, non — leurs règles (couverture d'un
fichier, saison terminée, choix du parent d'un groupe cross-seed) ne vivaient
que dans la tête de leur auteur.

Chaque test est ancré sur un défaut RÉEL, trouvé en auditant le dépôt le
2026-08-09 ou documenté dans CLAUDE.md. Ils sont écrits pour ÉCHOUER sur le
code d'avant : un test qui passe des deux côtés ne prouve rien.

unittest et pas pytest : le dépôt n'installe aucune dépendance de
développement, et `python3 -m unittest` marche partout où python3 existe —
même contrainte que celle qui a fait écrire generate-dashboard.py en stdlib
pure. Aucun test ne touche à l'infra réelle : tout se passe dans un
répertoire temporaire, et les appels arr sont remplacés par des bouchons.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Doit précéder l'import de core : le module fige ses racines au chargement.
_SANDBOX = tempfile.mkdtemp(prefix="clearr-tests-")
os.environ["CLEARR_DATA_ROOT"] = _SANDBOX
for _sub in ("library/series", "library/film", "library/anime",
             ".transmission/data/completed/anime", ".arr"):
    os.makedirs(os.path.join(_SANDBOX, _sub), exist_ok=True)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import core  # noqa: E402


def touch(path, content=b"x"):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return str(p)


class ArrCoveredPaths(unittest.TestCase):
    """_arr_covered_paths est la SEULE fonction non best-effort du module : sans
    la liste des fichiers d'un arr, tout ce qu'il gère passe pour orphelin et le
    balayage proposerait de supprimer la moitié de library/.

    Le garde-fou testait `is None`. Or arr_api ne rend None que sur un échec de
    transport et rend {} sur un corps vide (200 sans contenu, 204, réponse
    tronquée par un arr qui redémarre) : {} n'étant pas None, il passait, et
    itérer dessus ne lève pas — couverture vide, silencieusement."""

    def _stub(self, series, movies, episodefiles=()):
        def arr_api(base, key, method, path, params=None, json_body=None):
            if path.endswith("/series"):
                return series
            if path.endswith("/movie"):
                return movies
            return episodefiles
        core.arr_api = arr_api

    def test_reponses_degenerees_levent(self):
        for label, series, movies in (
            ("None (échec de transport)", None, []),
            ("{} corps vide — le défaut réel", {}, []),
            ("Radarr {}", [], {}),
            ("dict d'erreur Servarr", {"message": "Unauthorized"}, []),
            ("chaîne", "oops", []),
        ):
            with self.subTest(reponse=label):
                self._stub(series, movies)
                with self.assertRaises(RuntimeError):
                    core._arr_covered_paths()

    def test_episodefile_degenere_leve(self):
        self._stub([{"id": 1, "title": "S", "path": f"{_SANDBOX}/library/series/S"}], [],
                   episodefiles={})
        with self.assertRaises(RuntimeError):
            core._arr_covered_paths()

    def test_cas_nominal_ne_leve_pas(self):
        self._stub(
            [{"id": 1, "title": "S", "path": f"{_SANDBOX}/library/series/S"}],
            [{"path": f"{_SANDBOX}/library/film/M",
              "movieFile": {"path": f"{_SANDBOX}/library/film/M/m.mkv"}}],
            episodefiles=[{"path": f"{_SANDBOX}/library/series/S/e.mkv"}])
        files, dirs = core._arr_covered_paths()
        self.assertEqual(len(files), 2)
        self.assertEqual(len(dirs), 2)


class LibraryOrphanFiles(unittest.TestCase):
    """Les cinq règles de couverture. Chaque faux positif ici est une
    suppression de données, et elles sont subtiles : un .nfo sous le dossier
    d'un titre connu est couvert, une vidéo au même endroit ne l'est PAS (c'est
    le cas qui a motivé la fonctionnalité), et dans un dossier qu'aucun arr ne
    revendique tout est orphelin, sidecars compris."""

    def setUp(self):
        self.lib = Path(_SANDBOX, "library")
        self.serie = self.lib / "series" / "Suivie"
        self.inconnu = self.lib / "film" / "Inconnu"
        for p in (self.serie, self.inconnu):
            p.mkdir(parents=True, exist_ok=True)
        self.importe = touch(self.serie / "S01E01.mkv")
        self.sidecar = touch(self.serie / "S01E01.nfo")
        self.jamais_importe = touch(self.serie / "S01E02.mkv")
        self.hardlinke = touch(self.lib / "series" / "Suivie" / "hardlink.mkv")
        self.film_inconnu = touch(self.inconnu / "film.mkv")
        self.nfo_inconnu = touch(self.inconnu / "film.nfo")
        st = os.stat(self.hardlinke)
        self.state = {
            "library_index": {},
            "all_torrents": [{"id": 1, "_inodes": [(st.st_dev, st.st_ino)]}],
        }
        for f in (self.importe, self.sidecar, self.jamais_importe,
                  self.hardlinke, self.film_inconnu, self.nfo_inconnu):
            s = os.stat(f)
            self.state["library_index"][(s.st_dev, s.st_ino)] = f
        core._arr_covered_paths = lambda: ({self.importe}, [str(self.serie) + "/"])

    def test_les_cinq_regles(self):
        orphans = {p for p, _size in core.library_orphan_files(self.state)}
        self.assertNotIn(self.importe, orphans, "fichier connu de l'arr")
        self.assertNotIn(self.hardlinke, orphans, "couvert par l'inode d'un torrent")
        self.assertNotIn(self.sidecar, orphans,
                         "sidecar sous le dossier d'un titre connu (241 .nfo en jeu)")
        self.assertIn(self.jamais_importe, orphans,
                      "vidéo dans le dossier d'une série suivie mais jamais importée")
        self.assertIn(self.film_inconnu, orphans, "dossier qu'aucun arr ne revendique")
        self.assertIn(self.nfo_inconnu, orphans,
                      "sidecar dans un dossier inconnu : orphelin lui aussi")

    def test_erreur_arr_ne_propose_rien(self):
        """Le garde-fou doit remonter, pas dégrader : une liste vide ferait
        proposer toute la bibliothèque."""
        def boom():
            raise RuntimeError("Sonarr injoignable")
        core._arr_covered_paths = boom
        with self.assertRaises(RuntimeError):
            core.library_orphan_files(self.state)


class ResolveMediaPath(unittest.TestCase):
    """Seul point d'entrée où une chaîne venue de l'extérieur (Kodi) désigne un
    chemin à supprimer. Le plancher de 2 composants est ce qui empêche un chemin
    finissant par « film » de résoudre sur toute la catégorie."""

    def setUp(self):
        self.cible = Path(_SANDBOX, ".transmission/data/completed/anime/Noragami")
        self.cible.mkdir(parents=True, exist_ok=True)

    def test_resout_avec_prefixe_client_etranger(self):
        self.assertEqual(core.resolve_media_path("/n-importe-quoi/anime/Noragami"),
                         str(self.cible))

    def test_double_slash_kodi(self):
        self.assertEqual(core.resolve_media_path("/x/completed/anime//Noragami"),
                         str(self.cible))

    def test_entrees_hostiles_ne_resolvent_pas(self):
        for hostile in ("/../../../../etc/passwd", "/etc/passwd", "../../library/film",
                        "/mnt/ailleurs/film"):
            with self.subTest(chemin=hostile):
                self.assertIsNone(core.resolve_media_path(hostile))

    def test_racine_de_categorie_refusee(self):
        """Sans le plancher de 2 composants, « film » désignerait toute la
        catégorie — et la supprimerait."""
        for trop_court in ("anime", "/x/anime", "completed/anime"):
            with self.subTest(chemin=trop_court):
                self.assertIsNone(core.resolve_media_path(trop_court))

    def test_ambiguite_refusee(self):
        """Même suffixe sous deux racines : on ne devine pas, c'est une
        suppression de fichiers."""
        double = Path(_SANDBOX, "library/anime/Noragami")
        double.mkdir(parents=True, exist_ok=True)
        try:
            self.assertIsNone(core.resolve_media_path("/x/anime/Noragami"))
        finally:
            double.rmdir()

    def test_library_interdit_a_la_suppression_par_chemin(self):
        """Un titre de library/ est presque toujours suivi par un arr : le
        supprimer sans retirer son entrée le ferait re-télécharger."""
        self.assertTrue(core.is_arr_managed_path(f"{_SANDBOX}/library/film/X/x.mkv"))
        self.assertFalse(core.is_arr_managed_path(
            f"{_SANDBOX}/.transmission/data/completed/anime/N/n.mkv"))

    def test_prefixe_de_chaine_ne_suffit_pas(self):
        """library_old ne doit pas passer pour library."""
        autre = Path(_SANDBOX, "library_old")
        autre.mkdir(exist_ok=True)
        try:
            self.assertFalse(core.is_arr_managed_path(str(autre / "x.mkv")))
        finally:
            autre.rmdir()


class CleanupOrphanFiles(unittest.TestCase):
    """La boucle os.remove() la plus large du module. `covered` a été ajouté le
    2026-08-09 : le raisonnement « après bulk_delete_torrents, ce qui reste est
    orphelin » est faux dès qu'un torrent du lot a échoué — on supprimait alors
    les données de torrents toujours présents dans Transmission, jamais
    annoncées dans l'écran de confirmation."""

    def setUp(self):
        self.root = Path(_SANDBOX, "library")
        self.cible = self.root / "series" / "ACleanup"
        self.cible.mkdir(parents=True, exist_ok=True)
        self.temoin_dir = self.root / "series" / "Temoin"
        self.temoin_dir.mkdir(parents=True, exist_ok=True)
        self.temoin = touch(self.temoin_dir / "garde.mkv")

    def test_covered_preserve_les_torrents_en_echec(self):
        garde = touch(self.cible / "encore_seede.mkv")
        part = touch(self.cible / "orphelin.mkv")
        removed, _freed = core.cleanup_orphan_files(str(self.cible), root=str(self.root),
                                                    covered={garde})
        self.assertEqual(removed, 1)
        self.assertTrue(os.path.exists(garde))
        self.assertFalse(os.path.exists(part))

    def test_borne_a_la_cible_et_elague_jusqu_a_root(self):
        touch(self.cible / "Season 1" / "a.mkv")
        core.cleanup_orphan_files(str(self.cible), root=str(self.root))
        self.assertFalse(self.cible.exists(), "dossier vidé puis élagué")
        self.assertTrue(self.root.exists(), "root JAMAIS supprimé")
        self.assertTrue(os.path.exists(self.temoin), "dossier frère intact")

    def test_prune_ne_remonte_jamais_au_dessus_de_root(self):
        d = self.root / "a" / "b" / "c"
        d.mkdir(parents=True, exist_ok=True)
        core.prune_empty_dirs_from(str(d), str(self.root))
        self.assertTrue(self.root.exists())
        self.assertFalse((self.root / "a").exists())

    def test_cible_inexistante_est_un_no_op(self):
        self.assertEqual(core.cleanup_orphan_files(str(self.cible / "absent"),
                                                   root=str(self.root)), (0, 0))


class PlanSonarrUnmonitor(unittest.TestCase):
    """La condition « saison terminée » protège une diffusion en cours ; elle
    repose sur totalEpisodeCount == episodeCount, invisible à la relecture. Le
    test du préfixe verrouille la protection par composant."""

    def _stub(self, series, episodefiles, episodes):
        def arr_api(base, key, method, path, params=None, json_body=None):
            if path.endswith("/series"):
                return series
            if "episodefile" in path:
                return episodefiles
            if "episode" in path:
                return episodes
            return []
        core.arr_api = arr_api
        core.SONARR_API_KEY = "stub"

    def _series(self, path, total, present):
        return [{"id": 1, "title": "S", "path": path,
                 "seasons": [{"seasonNumber": 1,
                              "statistics": {"totalEpisodeCount": total,
                                             "episodeCount": present}}]}]

    def test_saison_terminee_et_entierement_supprimee(self):
        base = f"{_SANDBOX}/library/series/S"
        ef = [{"id": 10, "path": f"{base}/e1.mkv", "seasonNumber": 1}]
        self._stub(self._series(base, 1, 1), ef,
                   [{"id": 100, "episodeFileId": 10, "seasonNumber": 1}])
        plan = core.plan_sonarr_unmonitor({f"{base}/e1.mkv"})
        self.assertEqual([a["kind"] for a in plan], ["sonarr_season"])

    def test_saison_en_cours_de_diffusion_ne_coupe_que_les_episodes(self):
        base = f"{_SANDBOX}/library/series/S"
        ef = [{"id": 10, "path": f"{base}/e1.mkv", "seasonNumber": 1}]
        self._stub(self._series(base, 12, 1), ef,   # 12 prévus, 1 présent
                   [{"id": 100, "episodeFileId": 10, "seasonNumber": 1}])
        plan = core.plan_sonarr_unmonitor({f"{base}/e1.mkv"})
        self.assertEqual([a["kind"] for a in plan], ["sonarr_episodes"],
                         "une saison en cours ne doit jamais être désactivée en bloc")

    def test_serie_dont_le_chemin_est_prefixe_d_une_autre(self):
        """/library/series/Foo ne doit pas capter les fichiers de Foo 2."""
        base = f"{_SANDBOX}/library/series/Foo"
        ef = [{"id": 10, "path": f"{base}/e1.mkv", "seasonNumber": 1}]
        self._stub(self._series(base, 1, 1), ef,
                   [{"id": 100, "episodeFileId": 10, "seasonNumber": 1}])
        plan = core.plan_sonarr_unmonitor({f"{_SANDBOX}/library/series/Foo 2/e1.mkv"})
        self.assertEqual(plan, [], "aucun croisement entre Foo et Foo 2")


class BuildCrossSeedGroups(unittest.TestCase):
    """Le parent choisi détermine ce que la cascade supprime : une inversion
    supprimerait le téléchargement d'origine en croyant nettoyer un cross-seed."""

    def test_parent_est_l_original_pas_l_entree_cross_seed(self):
        lien = {"id": 2, "addedDate": 1, "_inodes": [(1, 100)],
                "downloadDir": f"{core.TRANSMISSION_DATA_ROOT}/.cross-seed-links/x"}
        original = {"id": 1, "addedDate": 5, "_inodes": [(1, 100)],
                    "downloadDir": f"{core.TRANSMISSION_DATA_ROOT}/completed/anime"}
        groups, child_ids = core.build_cross_seed_groups([lien, original])
        self.assertIn(1, groups, "l'original est parent même s'il est le plus récent")
        self.assertEqual(child_ids, {2})

    def test_groupe_de_un_est_exclu(self):
        seul = {"id": 1, "addedDate": 1, "_inodes": [(1, 1)], "downloadDir": "/x"}
        groups, child_ids = core.build_cross_seed_groups([seul])
        self.assertEqual(groups, {})
        self.assertEqual(child_ids, set())

    def test_sans_inode_partage_aucun_groupe(self):
        a = {"id": 1, "addedDate": 1, "_inodes": [(1, 1)], "downloadDir": "/x"}
        b = {"id": 2, "addedDate": 2, "_inodes": [(1, 2)], "downloadDir": "/y"}
        groups, _ = core.build_cross_seed_groups([a, b])
        self.assertEqual(groups, {})


class TriEtFormatage(unittest.TestCase):
    """Le tri porte sur les valeurs BRUTES, pas sur la chaîne affichée : un tri
    alphabétique sur « 900Mo / 1.5Go / 4.0Go » donne un ordre différent de
    l'ordre numérique réel. Vérifié une fois à la main en 2026-07-31, jamais
    figé jusqu'ici."""

    def test_tri_sur_la_valeur_brute_et_pas_sur_l_affichage(self):
        items = [{"size": 4_000_000_000}, {"size": 900_000_000}, {"size": 1_500_000_000}]
        fields = [("TAILLE", lambda i: i["size"])]
        core.sort_items(items, fields, 0, False)
        self.assertEqual([i["size"] for i in items],
                         [900_000_000, 1_500_000_000, 4_000_000_000])
        affiche = [core.human_size(i["size"]) for i in items]
        self.assertNotEqual(affiche, sorted(affiche),
                            "si l'ordre affiché est aussi l'ordre alphabétique, "
                            "le test ne discrimine plus rien")

    def test_human_size_cas_limites(self):
        self.assertEqual(core.human_size(0), "0o")
        for n in (-1, 10 ** 18):
            with self.subTest(n=n):
                self.assertIsInstance(core.human_size(n), str)



class SeasonDeletion(unittest.TestCase):
    """Suppression par saison (2026-08-30). Trois défauts possibles, tous
    silencieux et tous des pertes de données :

    - un torrent à cheval sur une saison GARDÉE traité comme les autres
      supprimerait les données de cette saison-là ;
    - une série sans dossier de saison ferait balayer le dossier de la série
      entière, donc toutes les autres saisons ;
    - un plan construit sur un Sonarr muet ne verrait presque rien à supprimer
      (61 % des episodefile de cette bibliothèque n'ont plus aucun torrent) tout
      en s'annonçant réussi.
    """

    def setUp(self):
        # Restauration explicite : contrairement aux stubs des autres classes,
        # find_series_torrents est une fonction que le reste du module appelle.
        for name in ("arr_api", "find_series_torrents"):
            self.addCleanup(setattr, core, name, getattr(core, name))
        self.root = Path(_SANDBOX, "library", "series", "Serie")
        self.s01 = self.root / "Season 01"
        self.s02 = self.root / "Season 02"
        self.e01 = touch(self.s01 / "S01E01.mkv", b"aaaa")
        self.e02 = touch(self.s02 / "S02E01.mkv", b"bbbb")
        self.annexe = touch(self.s01 / "S01E01.nfo", b"n")
        self.series = {
            "id": 7, "title": "Serie", "path": str(self.root),
            "seasons": [{"seasonNumber": 1, "monitored": True},
                        {"seasonNumber": 2, "monitored": True}],
        }
        self.files = [
            {"id": 11, "seasonNumber": 1, "path": self.e01, "size": 4},
            {"id": 22, "seasonNumber": 2, "path": self.e02, "size": 4},
        ]
        core.arr_api = lambda *a, **k: self.files
        # Un seul torrent, portant les DEUX saisons : le pack multi-saisons.
        self.pack = ({"id": 1, "name": "Serie S01-S02"},
                     [("/hors/library/pack/S01E01.mkv", 4),
                      ("/hors/library/pack/S02E01.mkv", 4)],
                     [(self.e01, 4), (self.e02, 4)])
        core.find_series_torrents = lambda *a, **k: [self.pack]
        self.state = {"all_torrents": [], "library_index": {}, "cross_seed_child_ids": set()}

    def test_torrent_a_cheval_est_conserve(self):
        """Saison 1 seule : le pack porte aussi la saison 2, il ne doit PAS
        partir — et l'espace annoncé comme libéré ne doit pas compter des octets
        que le torrent continue de seeder."""
        plan = core.plan_season_deletion(self.state, self.series, [1])
        self.assertEqual(plan["matched"], [], "le pack ne doit pas être supprimé")
        self.assertEqual(len(plan["straddling"]), 1)
        self.assertIn(self.e01, plan["straddling_paths"])
        self.assertNotIn(self.e02, plan["straddling_paths"],
                         "un fichier de la saison GARDÉE n'a rien à faire dans le plan")
        self.assertEqual(plan["freed_bytes"], 1,
                         "seul le fichier annexe libère de l'espace : le reste est "
                         "encore référencé par les données du pack")

    def test_pack_entierement_couvert_est_supprime(self):
        """Les deux saisons choisies : le pack ne déborde plus, il part."""
        plan = core.plan_season_deletion(self.state, self.series, [1, 2])
        self.assertEqual(len(plan["matched"]), 1)
        self.assertEqual(plan["straddling"], [])
        self.assertEqual(plan["freed_bytes"], 9, "8 octets de données + le fichier annexe")

    def test_serie_sans_dossier_de_saison_ne_balaie_rien(self):
        """Épisodes à plat dans le dossier de la série : balayer emporterait
        toutes les autres saisons. On ne balaie donc RIEN."""
        plat = touch(self.root / "S01E01.mkv", b"cccc")
        self.files = [{"id": 11, "seasonNumber": 1, "path": plat, "size": 4}]
        core.find_series_torrents = lambda *a, **k: []
        plan = core.plan_season_deletion(self.state, self.series, [1])
        self.assertEqual(plan["season_dirs"], [])
        self.assertEqual(plan["orphans"], [],
                         "aucun fichier annexe ne doit être proposé : le dossier "
                         "balayé serait celui de la série entière")

    def test_fichier_annexe_du_dossier_de_saison_est_annonce(self):
        core.find_series_torrents = lambda *a, **k: []
        plan = core.plan_season_deletion(self.state, self.series, [1])
        self.assertEqual([p for p, _s in plan["orphans"]], [self.annexe])
        self.assertNotIn(self.e01, [p for p, _s in plan["orphans"]],
                         "un episodefile est supprimé par Sonarr, pas balayé — "
                         "l'annoncer deux fois doublerait la taille affichée")

    def test_sonarr_muet_leve(self):
        core.arr_api = lambda *a, **k: None
        with self.assertRaises(RuntimeError):
            core.plan_season_deletion(self.state, self.series, [1])

    def test_saison_inconnue_refusee(self):
        """Client désynchronisé : deviner reviendrait à supprimer au hasard."""
        with self.assertRaises(ValueError):
            core.plan_season_deletion(self.state, self.series, [9])
        with self.assertRaises(ValueError):
            core.plan_season_deletion(self.state, self.series, [])

    def test_saison_sans_fichier_reste_proposable(self):
        """Une saison connue de Sonarr mais jamais téléchargée doit rester
        sélectionnable : la désactiver est justement ce qui l'empêche d'arriver."""
        self.series["seasons"].append({"seasonNumber": 3, "monitored": True})
        self.assertIn(3, core.season_numbers(self.series, self.files))
        core.find_series_torrents = lambda *a, **k: []
        plan = core.plan_season_deletion(self.state, self.series, [3])
        self.assertEqual(plan["episode_file_ids"], [])
        self.assertEqual(plan["season_dirs"], [])


class UnmonitorSeasons(unittest.TestCase):
    """monitorNewItems doit TOUJOURS ressortir à "all" : tout l'intérêt du mode
    sans purge est qu'une saison future soit quand même téléchargée. Une série
    laissée à "none" rendrait la promesse fausse en silence."""

    def setUp(self):
        self.addCleanup(setattr, core, "arr_api", core.arr_api)

    def test_force_monitor_new_items(self):
        envoye = {}

        def arr_api(base, key, method, path, params=None, json_body=None):
            if method == "GET":
                return {"id": 7, "title": "S", "monitored": False, "monitorNewItems": "none",
                        "seasons": [{"seasonNumber": 1, "monitored": True},
                                    {"seasonNumber": 2, "monitored": True}]}
            envoye.update(json_body)
            return {}
        core.arr_api = arr_api
        self.assertTrue(core.unmonitor_seasons({"id": 7, "title": "S"}, [1]))
        self.assertEqual(envoye["monitorNewItems"], "all")
        self.assertTrue(envoye["monitored"], "une série non suivie ne prend aucune saison")
        etat = {s["seasonNumber"]: s["monitored"] for s in envoye["seasons"]}
        self.assertFalse(etat[1])
        self.assertTrue(etat[2], "la saison gardée doit rester suivie")

    def test_echec_d_ecriture_remonte(self):
        """arr_write rend False sur échec : sans ça une saison restait suivie
        alors que ses fichiers venaient d'être supprimés — donc re-téléchargée."""
        core.arr_api = lambda base, key, method, path, params=None, json_body=None: (
            {"id": 7, "seasons": []} if method == "GET" else None)
        self.assertFalse(core.unmonitor_seasons({"id": 7, "title": "S"}, [1]))




class ExecuteDeleteSeasons(unittest.TestCase):
    """L'ORDRE des écritures Sonarr est le cœur de ce chemin.

    Supprimer un episodefile d'une saison ENCORE SUIVIE déclenche quasi
    instantanément la recherche automatique interne de Sonarr, qui
    re-téléchargerait ce qu'on vient d'effacer (piège documenté dans CLAUDE.md).
    L'unmonitor doit donc précéder la suppression — et comme les deux répondent
    200, rien ne le signalerait à l'exécution : seul ce test le verrouille."""

    def setUp(self):
        self.addCleanup(setattr, core, "arr_api", core.arr_api)
        self.addCleanup(setattr, core, "cleanup_orphan_files", core.cleanup_orphan_files)
        self.calls = []

        def arr_api(base, key, method, path, params=None, json_body=None):
            self.calls.append((method, path))
            if method == "GET":
                return {"id": 7, "title": "S", "seasons": [{"seasonNumber": 1, "monitored": True}]}
            return {}
        core.arr_api = arr_api
        # Bouchonné : ces tests portent sur l'ordre des écritures et sur
        # l'élagage, pas sur le balayage lui-même (déjà couvert ailleurs).
        core.cleanup_orphan_files = lambda *a, **k: (0, 0)
        self.plan = {
            "series": {"id": 7, "title": "S", "path": "/nowhere"},
            "seasons": [1], "episode_file_ids": [11, 12],
            "matched": [], "straddling": [], "straddling_paths": [],
            "covered": set(), "season_dirs": [], "orphans": [],
        }

    def test_unmonitor_precede_la_suppression_des_fichiers(self):
        core.execute_delete_seasons(None, self.plan, [], {}, set(), set())
        writes = [c for c in self.calls if c[0] != "GET"]
        self.assertEqual(writes[0][0], "PUT", "l'unmonitor doit venir en premier")
        self.assertIn("/series/7", writes[0][1])
        self.assertEqual(writes[1], ("DELETE", "/api/v3/episodefile/bulk"))

    def test_aucun_retrait_de_serie(self):
        """Le mode sans purge ne doit JAMAIS appeler DELETE /series : c'est
        exactement ce qui empêcherait une saison future d'arriver."""
        core.execute_delete_seasons(None, self.plan, [], {}, set(), set())
        self.assertNotIn(("DELETE", "/api/v3/series/7"),
                         [(m, p) for m, p in self.calls])

    def test_dossiers_de_saison_vides_elagues(self):
        """Sonarr supprime SES fichiers lui-même, donc ni remove_library_paths
        ni cleanup_orphan_files ne tourne dans le cas courant : sans élagage
        explicite, le dossier de saison restait vide sur le disque (constaté le
        2026-08-30 sur One-Punch Man S2 et S3)."""
        vide = Path(_SANDBOX, "library", "series", "Elag", "Season 04")
        vide.mkdir(parents=True, exist_ok=True)
        self.plan["season_dirs"] = [str(vide)]
        core.execute_delete_seasons(None, self.plan, [], {}, set(), set())
        self.assertFalse(vide.exists(), "le dossier de saison vidé doit disparaître")
        self.assertFalse(vide.parent.exists(),
                         "et l'élagage remonte tant que le parent est vide")

    def test_sans_fichier_pas_d_appel_bulk(self):
        """Une saison connue mais jamais téléchargée : l'unmonitor a du sens,
        un DELETE bulk sur une liste vide n'en a aucun."""
        self.plan["episode_file_ids"] = []
        core.execute_delete_seasons(None, self.plan, [], {}, set(), set())
        self.assertNotIn("/api/v3/episodefile/bulk", [p for _m, p in self.calls])



if __name__ == "__main__":
    unittest.main(verbosity=2)
