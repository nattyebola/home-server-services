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


if __name__ == "__main__":
    unittest.main(verbosity=2)
