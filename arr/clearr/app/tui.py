# TUI curses de clearr — `make clearr` (docker compose run --rm -it clearr
# python -m app tui). Toute la logique de matching/suppression vit dans
# core.py ; ce module ne fait que l'affichage et la boucle de touches.
#
# Marqueur 'M' (torrent dont le fichier a disparu du disque) + Maj+P pour les
# purger tous en un coup côté Transmission. Arbre cross-seed (→/l déplier,
# ←/h replier) : voir core.build_cross_seed_groups.
import curses
import os
import sys
import traceback

from . import core

COLOR_LINKED = 1   # vert : fichier(s) présents dans library/, ratio confortable
COLOR_DANGER = 2   # rouge : action destructive, ratio bas, échec
COLOR_WARN = 3     # jaune : à vérifier (pas de correspondance library/, ratio moyen)
COLOR_HEADER = 4   # cyan : en-têtes/titres
COLORS_ON = False  # positionné dans main() selon curses.has_colors()


def cp(n):
    return curses.color_pair(n) if COLORS_ON else 0


def ratio_color(ratio):
    if ratio < 1.0:
        return COLOR_DANGER
    if ratio < 3.0:
        return COLOR_WARN
    return COLOR_LINKED


def col_label(name, width, active, reverse):
    text = name + (" ▼" if reverse else " ▲") if active else name
    return f"{text:<{width}}"


# Lignes réservées hors liste : onglets (1) + en-tête + séparateur (2) +
# ligne session + pied de page (2) — factorisé car les draw_*() et la boucle
# de main() (pour le défilement) doivent rester d'accord sur ce nombre.
# max(1, ...) pour éviter une slice à borne négative sur un terminal minuscule.
def visible_rows(h):
    return max(1, h - 5)


def draw_list(stdscr, tree_rows, selected, offset, filter_str, linked_ids, missing_ids, sort_idx, sort_reverse,
              session_freed_bytes, session_deletions, expanded_ids, total_torrents, group_count):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    draw_tabs(stdscr, "torrents")
    header = (
        f"{col_label('BIB', 3, sort_idx == 0, sort_reverse)} "
        f"{col_label('ABS', 3, sort_idx == 1, sort_reverse)} "
        f"{col_label('AGE', 7, sort_idx == 2, sort_reverse)} "
        f"{col_label('TAILLE', 9, sort_idx == 3, sort_reverse)} "
        f"{col_label('RATIO', 6, sort_idx == 4, sort_reverse)} "
        f"{col_label('TRACKER', 20, sort_idx == 5, sort_reverse)} "
        f"{col_label('NOM', 3, sort_idx == 6, sort_reverse)}"
    )
    stdscr.addstr(1, 0, header[:w - 1], curses.A_BOLD | cp(COLOR_HEADER))
    stdscr.addstr(2, 0, "-" * min(w - 1, len(header)), cp(COLOR_HEADER))
    visible = visible_rows(h)
    for i, tree_row in enumerate(tree_rows[offset:offset + visible]):
        t = tree_row["torrent"]
        row = 3 + i
        is_selected = offset + i == selected
        linked = t["id"] in linked_ids
        missing = t["id"] in missing_ids
        base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL
        l_attr = base_attr if (is_selected or not linked) else base_attr | cp(COLOR_LINKED)
        m_attr = base_attr if (is_selected or not missing) else base_attr | cp(COLOR_DANGER)

        col = 0
        l_str = f"{'✓' if linked else '':<3} "
        stdscr.addstr(row, col, l_str[:max(0, w - 1 - col)], l_attr)
        col += len(l_str)
        m_str = f"{'✓' if missing else '':<3} "
        stdscr.addstr(row, col, m_str[:max(0, w - 1 - col)], m_attr)
        col += len(m_str)
        age_str = f"{core.human_age(t['addedDate']):<7} "
        stdscr.addstr(row, col, age_str[:max(0, w - 1 - col)], base_attr)
        col += len(age_str)
        size_str = f"{core.human_size(t['totalSize']):<9} "
        stdscr.addstr(row, col, size_str[:max(0, w - 1 - col)], base_attr)
        col += len(size_str)
        ratio_str = f"{t['uploadRatio']:<6.2f} "
        ratio_attr = base_attr if is_selected else base_attr | cp(ratio_color(t["uploadRatio"]))
        stdscr.addstr(row, col, ratio_str[:max(0, w - 1 - col)], ratio_attr)
        col += len(ratio_str)
        tracker_str = f"{t.get('_tracker_name', '?')[:19]:<20} "
        stdscr.addstr(row, col, tracker_str[:max(0, w - 1 - col)], base_attr)
        col += len(tracker_str)
        if tree_row["depth"] == 0 and tree_row["child_count"]:
            glyph = "▾" if t["id"] in expanded_ids else "▸"
            n = tree_row["child_count"]
            name = f"{glyph} {t['name']} ({n} cross-seed{'s' if n > 1 else ''})"
        elif tree_row["depth"] == 1:
            name = f"  └ {t['name']}"
        else:
            name = t["name"]
        stdscr.addstr(row, col, name[:max(0, w - 1 - col)], base_attr)
    session_line = f"Session : {session_deletions} suppression(s), {core.human_size(session_freed_bytes)} libéré(s)"
    stdscr.addstr(h - 2, 0, session_line[:w - 1], curses.A_BOLD | cp(COLOR_LINKED))

    sort_label, _ = core.SORT_FIELDS[sort_idx]
    footer = f"{total_torrents} torrents ({len(linked_ids)} avec fichier(s) bibliothèque BIB, {len(missing_ids)} fichier manquant ABS"
    if group_count:
        footer += f", {group_count} groupe(s) cross-seed"
    footer += ")"
    footer += f" | tri: {sort_label} {'▼' if sort_reverse else '▲'}"
    if filter_str:
        footer += f" | filtre: {filter_str}"
    footer += " | ? aide"
    stdscr.addstr(h - 1, 0, footer[:w - 1], curses.A_DIM)
    stdscr.refresh()


def draw_tabs(stdscr, view_mode):
    """Barre d'onglets sur la ligne 0 — la vue active est en vidéo inverse,
    les autres restent dans la couleur d'en-tête normale. Dessinée par
    chacun des draw_*() plutôt qu'une seule fois dans la boucle de main() :
    ils appellent déjà stdscr.erase() avant, la tracer ailleurs la ferait
    effacer aussitôt."""
    w = stdscr.getmaxyx()[1]
    col = 0
    for v in core.VIEWS:
        label = f" {core.VIEW_LABELS[v]} "
        attr = curses.A_REVERSE | curses.A_BOLD if v == view_mode else cp(COLOR_HEADER)
        stdscr.addstr(0, col, label[:max(0, w - 1 - col)], attr)
        col += len(label)
        if v != core.VIEWS[-1] and col < w - 1:
            stdscr.addstr(0, col, "│", cp(COLOR_HEADER))
            col += 1


def draw_series_list(stdscr, rows, selected, offset, filter_str, total_series, sort_idx, sort_reverse):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    draw_tabs(stdscr, "series")
    header = (
        f"{col_label('MON', 3, sort_idx == 0, sort_reverse)} "
        f"{col_label('SAISONS', 9, sort_idx == 1, sort_reverse)} "
        f"{col_label('EPISODES', 10, sort_idx == 2, sort_reverse)} "
        f"{col_label('TAILLE', 9, sort_idx == 3, sort_reverse)} "
        f"{col_label('TITRE', 5, sort_idx == 4, sort_reverse)}"
    )
    stdscr.addstr(1, 0, header[:w - 1], curses.A_BOLD | cp(COLOR_HEADER))
    stdscr.addstr(2, 0, "-" * min(w - 1, len(header)), cp(COLOR_HEADER))
    visible = visible_rows(h)
    for i, series in enumerate(rows[offset:offset + visible]):
        row = 3 + i
        is_selected = offset + i == selected
        base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL
        stats = series.get("statistics", {})
        seasons = series.get("seasons", [])
        monitored_seasons = sum(1 for s in seasons if s.get("monitored"))
        monitored = series.get("monitored")

        col = 0
        mon_str = f"{'✓' if monitored else '':<3} "
        mon_attr = base_attr if (is_selected or not monitored) else base_attr | cp(COLOR_LINKED)
        stdscr.addstr(row, col, mon_str[:max(0, w - 1 - col)], mon_attr)
        col += len(mon_str)
        seasons_str = f"{monitored_seasons}/{len(seasons)}"
        seasons_str = f"{seasons_str:<9} "
        stdscr.addstr(row, col, seasons_str[:max(0, w - 1 - col)], base_attr)
        col += len(seasons_str)
        ep_str = f"{stats.get('episodeFileCount', 0)}/{stats.get('totalEpisodeCount', 0)}"
        ep_str = f"{ep_str:<10} "
        stdscr.addstr(row, col, ep_str[:max(0, w - 1 - col)], base_attr)
        col += len(ep_str)
        size_str = f"{core.human_size(stats.get('sizeOnDisk', 0)):<9} "
        stdscr.addstr(row, col, size_str[:max(0, w - 1 - col)], base_attr)
        col += len(size_str)
        stdscr.addstr(row, col, series["title"][:max(0, w - 1 - col)], base_attr)
    sort_label, _ = core.SERIES_SORT_FIELDS[sort_idx]
    footer = f"{len(rows)}/{total_series} série(s)"
    footer += f" | tri: {sort_label} {'▼' if sort_reverse else '▲'}"
    if filter_str:
        footer += f" | filtre: {filter_str}"
    footer += " | Tab: vue suivante | Entrée: supprimer toute la série | ? aide"
    stdscr.addstr(h - 1, 0, footer[:w - 1], curses.A_DIM)
    stdscr.refresh()


def draw_films_list(stdscr, rows, selected, offset, filter_str, total_movies, sort_idx, sort_reverse):
    h, w = stdscr.getmaxyx()
    stdscr.erase()
    draw_tabs(stdscr, "films")
    header = (
        f"{col_label('MON', 3, sort_idx == 0, sort_reverse)} "
        f"{col_label('FICH', 4, sort_idx == 1, sort_reverse)} "
        f"{col_label('ANNEE', 6, sort_idx == 2, sort_reverse)} "
        f"{col_label('TAILLE', 9, sort_idx == 3, sort_reverse)} "
        f"{col_label('TITRE', 5, sort_idx == 4, sort_reverse)}"
    )
    stdscr.addstr(1, 0, header[:w - 1], curses.A_BOLD | cp(COLOR_HEADER))
    stdscr.addstr(2, 0, "-" * min(w - 1, len(header)), cp(COLOR_HEADER))
    visible = visible_rows(h)
    for i, movie in enumerate(rows[offset:offset + visible]):
        row = 3 + i
        is_selected = offset + i == selected
        base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL
        monitored = movie.get("monitored")
        has_file = movie.get("hasFile")

        col = 0
        mon_str = f"{'✓' if monitored else '':<3} "
        mon_attr = base_attr if (is_selected or not monitored) else base_attr | cp(COLOR_LINKED)
        stdscr.addstr(row, col, mon_str[:max(0, w - 1 - col)], mon_attr)
        col += len(mon_str)
        fich_str = f"{'✓' if has_file else '':<4} "
        fich_attr = base_attr if (is_selected or not has_file) else base_attr | cp(COLOR_LINKED)
        stdscr.addstr(row, col, fich_str[:max(0, w - 1 - col)], fich_attr)
        col += len(fich_str)
        year_str = f"{movie.get('year', ''):<6} "
        stdscr.addstr(row, col, year_str[:max(0, w - 1 - col)], base_attr)
        col += len(year_str)
        size_str = f"{core.human_size(movie.get('sizeOnDisk', 0)):<9} "
        stdscr.addstr(row, col, size_str[:max(0, w - 1 - col)], base_attr)
        col += len(size_str)
        stdscr.addstr(row, col, movie["title"][:max(0, w - 1 - col)], base_attr)
    sort_label, _ = core.FILMS_SORT_FIELDS[sort_idx]
    footer = f"{len(rows)}/{total_movies} film(s)"
    footer += f" | tri: {sort_label} {'▼' if sort_reverse else '▲'}"
    if filter_str:
        footer += f" | filtre: {filter_str}"
    footer += " | Tab: vue suivante | Entrée: supprimer le film | ? aide"
    stdscr.addstr(h - 1, 0, footer[:w - 1], curses.A_DIM)
    stdscr.refresh()


# Un raccourci par ligne (touche, description) — source unique pour show_help(),
# plutôt qu'une liste condensée dans le footer.
HELP_KEYS = [
    ("Tab", "vue suivante (Torrents → Séries → Films)"),
    ("↑/↓, j/k", "naviguer"),
    ("PgUp/PgDown", "naviguer par page"),
    ("/", "filtrer par nom"),
    ("s", "champ de tri suivant (de la vue courante)"),
    ("S", "inverser le sens du tri (de la vue courante)"),
    ("→ / l", "déplier les cross-seeds du torrent sélectionné (vue Torrents)"),
    ("← / h", "replier (vue Torrents)"),
    ("Entrée", "supprimer (vue Torrents) / supprimer toute la série ou le film (vues Séries/Films)"),
    ("D", "supprimer sans confirmation, même effet qu'Entrée mais sans l'écran de confirmation (les 3 vues)"),
    ("P", "purger tous les torrents marqués ABS (vue Torrents, avec confirmation)"),
    ("?", "cette aide"),
    ("q / Échap", "quitter"),
]


def show_help(stdscr):
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    lines = [("Raccourcis", curses.A_BOLD | cp(COLOR_HEADER)), ("", curses.A_NORMAL)]
    key_width = max(len(k) for k, _ in HELP_KEYS)
    for key, desc in HELP_KEYS:
        lines.append((f"  {key:<{key_width}}  {desc}", curses.A_NORMAL))
    lines.append(("", curses.A_NORMAL))
    lines.append(("Colonnes : BIB = coche verte si fichier(s) présents dans library/, "
                   "ABS = coche rouge si fichier manquant sur disque",
                   curses.A_NORMAL))
    lines.append(("▸/▾ devant un nom : torrent cross-seedé sur plusieurs indexeurs (même fichier",
                   curses.A_NORMAL))
    lines.append(("réel qu'un ou plusieurs autres torrents) — replié par défaut, voir →/l.",
                   curses.A_NORMAL))
    lines.append(("", curses.A_NORMAL))
    lines.append(("Vues Séries/Films : Entrée supprime TOUT le titre d'un coup (tous ses torrents +",
                   curses.A_NORMAL))
    lines.append(("fichiers, puis retrait complet + exclusion côté Sonarr/Radarr) — pas de retour",
                   curses.A_NORMAL))
    lines.append(("en arrière possible autrement qu'en rajoutant le titre à la main.",
                   curses.A_NORMAL))
    lines.append(("", curses.A_NORMAL))
    lines.append(("Appuyez sur une touche pour revenir", curses.A_DIM))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    stdscr.getch()


def confirm_delete(stdscr, torrent, library_index, cross_seed_groups):
    host_files = core.torrent_host_files(torrent)
    lib_matches = core.find_library_matches(host_files, library_index)
    arr_plan = core.plan_arr_actions(lib_matches)
    dependents = cross_seed_groups.get(torrent["id"], [])
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    none_attr = curses.A_NORMAL
    lines = [
        (f"Supprimer : {torrent['name']}", curses.A_BOLD | cp(COLOR_HEADER)),
        ("", none_attr),
    ]
    if dependents:
        # Ce torrent est le parent (téléchargement d'origine) d'un groupe
        # cross-seed — apply_deletion supprime ses enfants avec lui (même
        # contenu, plus rien à seeder une fois ce torrent parti).
        lines.append((f"⚠ {len(dependents)} torrent(s) cross-seedé(s) seront supprimés avec lui "
                       f"(même contenu, ne libère pas d'espace supplémentaire) :",
                       curses.A_BOLD | cp(COLOR_DANGER)))
        for c in dependents[:10]:
            lines.append((f"  - {c['name']} ({c.get('_tracker_name', '?')})", cp(COLOR_DANGER)))
        if len(dependents) > 10:
            lines.append((f"  ... et {len(dependents) - 10} de plus", cp(COLOR_DANGER)))
        lines.append(("", none_attr))
    lines.append((f"Fichiers Transmission ({len(host_files)}) — {core.human_size(sum(s for _, s in host_files))} :", none_attr))
    for path, size in host_files[:10]:
        lines.append((f"  - {os.path.basename(path)} ({core.human_size(size)})", none_attr))
    if len(host_files) > 10:
        lines.append((f"  ... et {len(host_files) - 10} de plus", none_attr))
    lines.append(("", none_attr))
    if lib_matches:
        lines.append((f"Fichiers bibliothèque correspondants ({len(lib_matches)}) — {core.human_size(sum(s for _, s in lib_matches))} :",
                       curses.A_BOLD | cp(COLOR_LINKED)))
        for path, size in lib_matches[:10]:
            lines.append((f"  - {path.replace(core.LIBRARY_ROOT, 'library')} ({core.human_size(size)})", cp(COLOR_LINKED)))
        if len(lib_matches) > 10:
            lines.append((f"  ... et {len(lib_matches) - 10} de plus", cp(COLOR_LINKED)))
    else:
        lines.append(("Aucun fichier bibliothèque correspondant trouvé (jamais importé, ou déjà supprimé).",
                       curses.A_BOLD | cp(COLOR_WARN)))
    lines.append(("", none_attr))
    if arr_plan:
        lines.append((f"Actions Sonarr/Radarr ({len(arr_plan)}) :", curses.A_BOLD | cp(COLOR_LINKED)))
        for action in arr_plan:
            lines.append((f"  - {action['description']}", cp(COLOR_LINKED)))
        lines.append(("", none_attr))
    # host_files seul, pas + lib_matches : mêmes octets physiques comptés deux
    # fois sinon (fichiers library/ hardlinkés, cf. core.apply_deletion).
    total = sum(s for _, s in host_files)
    lines.append((f"Espace total libéré : {core.human_size(total)}", curses.A_BOLD))
    lines.append(("", none_attr))
    lines.append(("Confirmer la suppression ? [o/N]", curses.A_BOLD | cp(COLOR_DANGER)))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    curses.echo()
    key = stdscr.getch()
    curses.noecho()
    if key in (ord("o"), ord("O"), ord("y"), ord("Y")):
        return host_files, lib_matches, arr_plan
    return None


def confirm_bulk_delete(stdscr, torrents):
    """Écran de confirmation pour Maj+P (purge groupée des torrents marqués
    ABS) — mêmes conventions que confirm_delete, mais pas d'espace "libéré"
    à annoncer : par définition ces fichiers ont déjà disparu du disque."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    none_attr = curses.A_NORMAL
    lines = [
        (f"Purger {len(torrents)} torrent(s) au fichier disparu (marqués ABS) :",
         curses.A_BOLD | cp(COLOR_HEADER)),
        ("", none_attr),
    ]
    for t in torrents[:15]:
        lines.append((f"  - {t['name']}", cp(COLOR_DANGER)))
    if len(torrents) > 15:
        lines.append((f"  ... et {len(torrents) - 15} de plus", cp(COLOR_DANGER)))
    lines.append(("", none_attr))
    lines.append(("Retirés de Transmission uniquement (rien à supprimer localement, les fichiers", none_attr))
    lines.append(("sont déjà absents du disque) — vérifiez qu'aucun n'a simplement changé", none_attr))
    lines.append(("d'emplacement avant de confirmer.", none_attr))
    lines.append(("", none_attr))
    lines.append((f"Confirmer la suppression de {len(torrents)} torrent(s) ? [o/N]",
                   curses.A_BOLD | cp(COLOR_DANGER)))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    curses.echo()
    key = stdscr.getch()
    curses.noecho()
    return key in (ord("o"), ord("O"), ord("y"), ord("Y"))


def confirm_delete_series(stdscr, series, matched):
    """Écran de confirmation pour la suppression d'une série entière (vue
    Séries) — contrairement à confirm_delete (un seul torrent, un plan
    Sonarr/Radarr par-épisode/saison), ici l'action finale est toujours un
    retrait complet de la série + exclusion, quel que soit l'état des
    saisons."""
    total_files = sum(len(host_files) for _t, host_files, _lm in matched)
    total_size = sum(s for _t, host_files, _lm in matched for _p, s in host_files)
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    none_attr = curses.A_NORMAL
    lines = [
        (f"Supprimer toute la série : {series['title']}", curses.A_BOLD | cp(COLOR_HEADER)),
        ("", none_attr),
    ]
    if matched:
        lines.append((f"{len(matched)} torrent(s) Transmission ({total_files} fichier(s), {core.human_size(total_size)}) :",
                       none_attr))
        for t, _hf, _lm in matched[:10]:
            lines.append((f"  - {t['name']}", none_attr))
        if len(matched) > 10:
            lines.append((f"  ... et {len(matched) - 10} de plus", none_attr))
    else:
        lines.append(("Aucun torrent Transmission trouvé pour cette série.", cp(COLOR_WARN)))
    lines.append(("", none_attr))
    lines.append(("Tout fichier résiduel dans le dossier de la série (sans torrent correspondant) "
                   "sera aussi supprimé.", none_attr))
    lines.append(("", none_attr))
    lines.append((f'Sonarr : "{series["title"]}" sera retirée complètement (+ exclusion de liste).',
                   curses.A_BOLD | cp(COLOR_DANGER)))
    lines.append(("", none_attr))
    lines.append(("Confirmer la suppression de TOUTE la série ? [o/N]", curses.A_BOLD | cp(COLOR_DANGER)))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    curses.echo()
    key = stdscr.getch()
    curses.noecho()
    return key in (ord("o"), ord("O"), ord("y"), ord("Y"))


def confirm_delete_movie_no_torrent(stdscr, movie):
    """Écran de confirmation pour la vue Films quand find_movie_torrent() ne
    trouve aucun torrent (jamais téléchargé, ou fichier orphelin hors suivi) —
    quand un torrent est trouvé, on réutilise confirm_delete() tel quel (le
    plan_arr_actions qu'il calcule détecte déjà le film via movieFile.path)."""
    stdscr.erase()
    h, w = stdscr.getmaxyx()
    none_attr = curses.A_NORMAL
    lines = [
        (f"Supprimer : {movie['title']}", curses.A_BOLD | cp(COLOR_HEADER)),
        ("", none_attr),
    ]
    if movie.get("hasFile"):
        lines.append(("Aucun torrent Transmission trouvé pour ce film — Radarr supprimera",
                       cp(COLOR_WARN)))
        lines.append((f"lui-même son fichier ({core.human_size(movie.get('sizeOnDisk', 0))}).", cp(COLOR_WARN)))
    else:
        lines.append(("Ce film n'a jamais été téléchargé — rien à supprimer sur le disque.", none_attr))
    lines.append(("", none_attr))
    lines.append((f'Radarr : "{movie["title"]}" sera retiré complètement (+ exclusion de liste).',
                   curses.A_BOLD | cp(COLOR_DANGER)))
    lines.append(("", none_attr))
    lines.append(("Confirmer la suppression ? [o/N]", curses.A_BOLD | cp(COLOR_DANGER)))
    for i, (line, attr) in enumerate(lines[:h - 1]):
        stdscr.addstr(i, 0, line[:w - 1], attr)
    stdscr.refresh()
    curses.echo()
    key = stdscr.getch()
    curses.noecho()
    return key in (ord("o"), ord("O"), ord("y"), ord("Y"))


def delete_single_torrent(stdscr, client, torrent, library_index, cross_seed_groups, all_torrents, linked_ids,
                           missing_ids, confirm=True):
    """Un seul torrent, avec ou sans écran de confirmation — factorise ce que
    la vue Torrents (Entrée = confirmé, D = direct) et la vue Films (Entrée
    sur un film dont find_movie_torrent() a retrouvé le torrent) ont en
    commun. Renvoie None si l'utilisateur a refusé la confirmation (confirm=
    True uniquement, D n'en propose pas), sinon (all_torrents,
    cross_seed_groups, cross_seed_child_ids, freed)."""
    if confirm:
        result = confirm_delete(stdscr, torrent, library_index, cross_seed_groups)
        if not result:
            return None
        host_files, lib_matches, arr_plan = result
    else:
        host_files = core.torrent_host_files(torrent)
        lib_matches = core.find_library_matches(host_files, library_index)
        arr_plan = core.plan_arr_actions(lib_matches)
    all_torrents, freed = core.apply_deletion(client, torrent, host_files, lib_matches, arr_plan, all_torrents,
                                               linked_ids, missing_ids, cross_seed_groups)
    cross_seed_groups, cross_seed_child_ids = core.build_cross_seed_groups(all_torrents)
    return all_torrents, cross_seed_groups, cross_seed_child_ids, freed


def main(stdscr):
    global COLORS_ON
    core.logger.info("=== démarrage clearr TUI ===")
    curses.curs_set(0)
    if curses.has_colors():
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(COLOR_LINKED, curses.COLOR_GREEN, -1)
        curses.init_pair(COLOR_DANGER, curses.COLOR_RED, -1)
        curses.init_pair(COLOR_WARN, curses.COLOR_YELLOW, -1)
        curses.init_pair(COLOR_HEADER, curses.COLOR_CYAN, -1)
        COLORS_ON = True

    stdscr.addstr(0, 0, "Chargement des torrents, résolution des trackers, indexation de library/...")
    stdscr.refresh()
    state = core.load_full_state()
    client = state["client"]
    all_torrents = state["all_torrents"]
    library_index = state["library_index"]
    linked_ids = state["linked_ids"]
    missing_ids = state["missing_ids"]
    cross_seed_groups = state["cross_seed_groups"]
    cross_seed_child_ids = state["cross_seed_child_ids"]
    expanded_ids = set()

    # Un jeu tri (champ + sens) par vue, même principe que selected/offset/filters
    # plus bas : les touches s/S s'appliquent à la vue courante. AGE ascendant
    # par défaut pour les torrents (le plus ancien en premier) ; TITRE
    # (dernier champ des deux autres) pour préserver le tri alphabétique déjà
    # posé par fetch_series_list()/fetch_movies_list() tant que l'utilisateur
    # n'a pas trié lui-même.
    sort_idx = {"torrents": 2, "series": len(core.SERIES_SORT_FIELDS) - 1, "films": len(core.FILMS_SORT_FIELDS) - 1}
    sort_reverse = {v: False for v in core.VIEWS}
    core.sort_items(all_torrents, core.SORT_FIELDS, sort_idx["torrents"], sort_reverse["torrents"])

    stdscr.addstr(0, 0, "Chargement des séries (Sonarr) et films (Radarr)...")
    stdscr.clrtoeol()
    stdscr.refresh()
    series_list = core.fetch_series_list()
    movies_list = core.fetch_movies_list()

    # Vue courante (Tab pour cycler) + un jeu sélection/défilement/filtre par
    # vue plutôt qu'un seul partagé : changer de vue ne doit pas faire perdre
    # la position ou le filtre en cours dans les autres.
    view_mode = "torrents"
    selected = {v: 0 for v in core.VIEWS}
    offset = {v: 0 for v in core.VIEWS}
    filters = {v: "" for v in core.VIEWS}
    message = ""
    message_color = COLOR_LINKED
    session_freed_bytes = 0
    session_deletions = 0

    while True:
        tree_rows, rows = [], []
        if view_mode == "torrents":
            top_level = [t for t in all_torrents if t["id"] not in cross_seed_child_ids]
            tree_rows = core.build_tree(top_level, cross_seed_groups, expanded_ids, filters["torrents"])
            rows_len = len(tree_rows)
        elif view_mode == "series":
            rows = core.filter_by_title(series_list, filters["series"])
            rows_len = len(rows)
        else:
            rows = core.filter_by_title(movies_list, filters["films"])
            rows_len = len(rows)

        sel = max(0, min(selected[view_mode], rows_len - 1)) if rows_len else 0
        selected[view_mode] = sel
        h, _w = stdscr.getmaxyx()
        visible = visible_rows(h)
        off = offset[view_mode]
        if sel < off:
            off = sel
        if sel >= off + visible:
            off = sel - visible + 1
        offset[view_mode] = off

        if view_mode == "torrents":
            draw_list(stdscr, tree_rows, sel, off, filters["torrents"], linked_ids, missing_ids,
                      sort_idx["torrents"], sort_reverse["torrents"], session_freed_bytes, session_deletions,
                      expanded_ids, len(all_torrents), len(cross_seed_groups))
        elif view_mode == "series":
            draw_series_list(stdscr, rows, sel, off, filters["series"], len(series_list), sort_idx["series"],
                              sort_reverse["series"])
        else:
            draw_films_list(stdscr, rows, sel, off, filters["films"], len(movies_list), sort_idx["films"],
                             sort_reverse["films"])
        if message:
            stdscr.addstr(0, 0, message[: stdscr.getmaxyx()[1] - 1], curses.A_BOLD | cp(message_color))
            stdscr.refresh()
            message = ""

        key = stdscr.getch()
        if key in (ord("q"), 27):
            break
        elif key == ord("?"):
            show_help(stdscr)
        elif key == 9:  # Tab : Torrents -> Séries -> Films -> Torrents
            view_mode = core.VIEWS[(core.VIEWS.index(view_mode) + 1) % len(core.VIEWS)]
        elif key in (curses.KEY_DOWN, ord("j")):
            selected[view_mode] = min(sel + 1, rows_len - 1) if rows_len else 0
        elif key in (curses.KEY_UP, ord("k")):
            selected[view_mode] = max(sel - 1, 0)
        elif key == curses.KEY_NPAGE:
            selected[view_mode] = min(sel + visible, rows_len - 1) if rows_len else 0
        elif key == curses.KEY_PPAGE:
            selected[view_mode] = max(sel - visible, 0)
        elif key in (curses.KEY_RIGHT, ord("l")) and view_mode == "torrents" and tree_rows:
            # Déplier : uniquement pertinent sur une ligne racine avec des
            # cross-seeds (child_count > 0) — no-op sinon.
            row = tree_rows[sel]
            if row["child_count"] > 0:
                expanded_ids.add(row["torrent"]["id"])
        elif key in (curses.KEY_LEFT, ord("h")) and view_mode == "torrents" and tree_rows:
            # Replier : sur une ligne enfant, replie le groupe parent et
            # ramène le curseur dessus plutôt que de laisser la sélection
            # retomber arbitrairement sur la ligne suivante après le
            # rétrécissement de la liste.
            row = tree_rows[sel]
            collapse_id = row["torrent"]["id"] if row["depth"] == 0 else row["parent_id"]
            if collapse_id is not None:
                expanded_ids.discard(collapse_id)
                if row["depth"] == 1:
                    tree_rows = core.build_tree(top_level, cross_seed_groups, expanded_ids, filters["torrents"])
                    ids = [r["torrent"]["id"] for r in tree_rows]
                    if collapse_id in ids:
                        selected["torrents"] = ids.index(collapse_id)
        elif key == ord("/"):
            curses.echo()
            stdscr.addstr(stdscr.getmaxyx()[0] - 1, 0, "filtre> ")
            stdscr.clrtoeol()
            stdscr.refresh()
            filters[view_mode] = stdscr.getstr(stdscr.getmaxyx()[0] - 1, 8, 60).decode(errors="replace")
            curses.noecho()
            offset[view_mode] = 0
        elif key in (ord("s"), ord("S")):
            # Même mécanique dans les 3 vues : champ suivant ('s') ou sens
            # inversé ('S') sur le tri de la vue courante — seule la liste de
            # champs triables change.
            if view_mode == "torrents":
                fields, items = core.SORT_FIELDS, all_torrents
            elif view_mode == "series":
                fields, items = core.SERIES_SORT_FIELDS, series_list
            else:
                fields, items = core.FILMS_SORT_FIELDS, movies_list
            selected_id = None
            if view_mode == "torrents" and tree_rows:
                selected_id = tree_rows[sel]["torrent"]["id"]
            elif view_mode != "torrents" and rows:
                selected_id = rows[sel]["id"]
            if key == ord("s"):
                sort_idx[view_mode] = (sort_idx[view_mode] + 1) % len(fields)
                sort_reverse[view_mode] = False
            else:
                sort_reverse[view_mode] = not sort_reverse[view_mode]
            core.sort_items(items, fields, sort_idx[view_mode], sort_reverse[view_mode])
            if view_mode == "torrents":
                top_level = [t for t in all_torrents if t["id"] not in cross_seed_child_ids]
                tree_rows = core.build_tree(top_level, cross_seed_groups, expanded_ids, filters["torrents"])
                if selected_id is not None:
                    ids = [r["torrent"]["id"] for r in tree_rows]
                    selected["torrents"] = ids.index(selected_id) if selected_id in ids else 0
            else:
                rows = core.filter_by_title(items, filters[view_mode])
                if selected_id is not None:
                    ids = [r["id"] for r in rows]
                    selected[view_mode] = ids.index(selected_id) if selected_id in ids else 0
            offset[view_mode] = 0
        elif key in (curses.KEY_ENTER, 10, 13) and view_mode == "torrents" and tree_rows:
            torrent = tree_rows[sel]["torrent"]
            try:
                result = delete_single_torrent(stdscr, client, torrent, library_index, cross_seed_groups,
                                                all_torrents, linked_ids, missing_ids, confirm=True)
                if result:
                    all_torrents, cross_seed_groups, cross_seed_child_ids, freed = result
                    session_freed_bytes += freed
                    session_deletions += 1
                    message = f"Supprimé : {torrent['name']}"
                    message_color = COLOR_LINKED
            except Exception as e:
                core.logger.error("échec de la suppression de %r : %s", torrent["name"], e)
                message = f"ÉCHEC (voir {core.LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key in (curses.KEY_ENTER, 10, 13) and view_mode == "series" and rows:
            series = rows[sel]
            try:
                matched = core.find_series_torrents(all_torrents, library_index, cross_seed_child_ids,
                                                     series["path"])
                if confirm_delete_series(stdscr, series, matched):
                    all_torrents, freed, deleted, failed, arr_ok = core.execute_delete_series(
                        client, series, matched, all_torrents, cross_seed_groups, linked_ids, missing_ids)
                    cross_seed_groups, cross_seed_child_ids = core.build_cross_seed_groups(all_torrents)
                    series_list = [s for s in series_list if s["id"] != series["id"]]
                    session_freed_bytes += freed
                    session_deletions += deleted
                    message = f"Série supprimée : {series['title']} ({deleted} torrent(s)"
                    message += f", {failed} échec(s)" if failed else ""
                    message += ")"
                    message += " — RETRAIT SONARR ÉCHOUÉ, série encore suivie" if not arr_ok else ""
                    message_color = COLOR_DANGER if failed or not arr_ok else COLOR_LINKED
            except Exception as e:
                core.logger.error("échec de la suppression de la série %r : %s", series["title"], e)
                message = f"ÉCHEC (voir {core.LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key in (curses.KEY_ENTER, 10, 13) and view_mode == "films" and rows:
            movie = rows[sel]
            try:
                movie_path = movie["movieFile"]["path"] if movie.get("hasFile") else None
                torrent = core.find_movie_torrent(all_torrents, cross_seed_child_ids, movie_path) if movie_path else None
                if torrent:
                    # Même écran/mécanique qu'une suppression normale dans la
                    # vue Torrents : plan_arr_actions() (appelé par
                    # confirm_delete via delete_single_torrent) détecte déjà le
                    # film via movieFile.path et produit tout seul l'action
                    # "radarr_delete".
                    result = delete_single_torrent(stdscr, client, torrent, library_index, cross_seed_groups,
                                                    all_torrents, linked_ids, missing_ids, confirm=True)
                    if result:
                        all_torrents, cross_seed_groups, cross_seed_child_ids, freed = result
                        movies_list = [m for m in movies_list if m["id"] != movie["id"]]
                        session_freed_bytes += freed
                        session_deletions += 1
                        message = f"Film supprimé : {movie['title']}"
                        message_color = COLOR_LINKED
                elif confirm_delete_movie_no_torrent(stdscr, movie):
                    # Sur ce chemin c'est Radarr qui supprime le fichier : son
                    # échec veut dire que rien n'est parti, la ligne reste.
                    if core.execute_delete_movie_no_torrent(movie):
                        movies_list = [m for m in movies_list if m["id"] != movie["id"]]
                        session_deletions += 1
                        message = f"Film supprimé : {movie['title']}"
                        message_color = COLOR_LINKED
                    else:
                        message = f"ÉCHEC Radarr : {movie['title']} n'a pas été supprimé (voir {core.LOG_PATH})"
                        message_color = COLOR_DANGER
            except Exception as e:
                core.logger.error("échec de la suppression du film %r : %s", movie["title"], e)
                message = f"ÉCHEC (voir {core.LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key == ord("D") and view_mode == "torrents" and tree_rows:
            # Suppression directe, sans écran de confirmation — contrairement
            # à Entrée. À utiliser en connaissance de cause.
            torrent = tree_rows[sel]["torrent"]
            try:
                all_torrents, cross_seed_groups, cross_seed_child_ids, freed = delete_single_torrent(
                    stdscr, client, torrent, library_index, cross_seed_groups, all_torrents, linked_ids,
                    missing_ids, confirm=False)
                session_freed_bytes += freed
                session_deletions += 1
                message = f"Supprimé (sans confirmation) : {torrent['name']}"
                message_color = COLOR_LINKED
            except Exception as e:
                core.logger.error("échec de la suppression rapide de %r : %s", torrent["name"], e)
                message = f"ÉCHEC (voir {core.LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key == ord("D") and view_mode == "series" and rows:
            # Même geste que D en vue Torrents (pas d'écran de confirmation)
            # appliqué à toute la série — même mécanique qu'Entrée sur cette
            # vue sinon.
            series = rows[sel]
            try:
                matched = core.find_series_torrents(all_torrents, library_index, cross_seed_child_ids,
                                                     series["path"])
                all_torrents, freed, deleted, failed, arr_ok = core.execute_delete_series(
                    client, series, matched, all_torrents, cross_seed_groups, linked_ids, missing_ids)
                cross_seed_groups, cross_seed_child_ids = core.build_cross_seed_groups(all_torrents)
                series_list = [s for s in series_list if s["id"] != series["id"]]
                session_freed_bytes += freed
                session_deletions += deleted
                message = f"Série supprimée (sans confirmation) : {series['title']} ({deleted} torrent(s)"
                message += f", {failed} échec(s)" if failed else ""
                message += ")"
                message += " — RETRAIT SONARR ÉCHOUÉ, série encore suivie" if not arr_ok else ""
                message_color = COLOR_DANGER if failed or not arr_ok else COLOR_LINKED
            except Exception as e:
                core.logger.error("échec de la suppression rapide de la série %r : %s", series["title"], e)
                message = f"ÉCHEC (voir {core.LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key == ord("D") and view_mode == "films" and rows:
            # Même geste que D en vue Torrents, appliqué au film — même
            # mécanique qu'Entrée sur cette vue sinon.
            movie = rows[sel]
            try:
                movie_path = movie["movieFile"]["path"] if movie.get("hasFile") else None
                torrent = core.find_movie_torrent(all_torrents, cross_seed_child_ids, movie_path) if movie_path else None
                if torrent:
                    all_torrents, cross_seed_groups, cross_seed_child_ids, freed = delete_single_torrent(
                        stdscr, client, torrent, library_index, cross_seed_groups, all_torrents, linked_ids,
                        missing_ids, confirm=False)
                    session_freed_bytes += freed
                elif not core.execute_delete_movie_no_torrent(movie):
                    raise RuntimeError("Radarr n'a pas pu retirer le film, aucun fichier supprimé")
                movies_list = [m for m in movies_list if m["id"] != movie["id"]]
                session_deletions += 1
                message = f"Film supprimé (sans confirmation) : {movie['title']}"
                message_color = COLOR_LINKED
            except Exception as e:
                core.logger.error("échec de la suppression rapide du film %r : %s", movie["title"], e)
                message = f"ÉCHEC (voir {core.LOG_PATH}) : {e}"
                message_color = COLOR_DANGER
        elif key == ord("P") and view_mode == "torrents":
            # Purge groupée de tous les torrents marqués ABS (fichier disparu
            # du disque), pas seulement ceux du filtre courant. Chaque
            # suppression est indépendante (échec isolé n'interrompt pas les
            # suivantes).
            missing_torrents = [t for t in all_torrents if t["id"] in missing_ids]
            if not missing_torrents:
                message = "Aucun torrent avec fichier manquant (marqué ABS)"
                message_color = COLOR_WARN
            elif confirm_bulk_delete(stdscr, missing_torrents):
                deleted, failed, skipped = 0, 0, 0
                for torrent in missing_torrents:
                    # Un torrent de cette liste peut avoir déjà été supprimé
                    # en cascade par un parent traité plus tôt dans cette même
                    # purge (cross-seed enfant lui-même marqué ABS) — pas une
                    # erreur.
                    current_ids = {t["id"] for t in all_torrents}
                    if torrent["id"] not in current_ids:
                        skipped += 1
                        continue
                    try:
                        host_files = core.torrent_host_files(torrent)
                        lib_matches = core.find_library_matches(host_files, library_index)
                        arr_plan = core.plan_arr_actions(lib_matches)
                        all_torrents, freed = core.apply_deletion(client, torrent, host_files, lib_matches, arr_plan,
                                                                   all_torrents, linked_ids, missing_ids,
                                                                   cross_seed_groups)
                        session_freed_bytes += freed
                        session_deletions += 1
                        deleted += 1
                    except Exception as e:
                        core.logger.error("échec de la purge de %r (id=%s) : %s", torrent["name"], torrent["id"], e)
                        failed += 1
                cross_seed_groups, cross_seed_child_ids = core.build_cross_seed_groups(all_torrents)
                message = f"Purge : {deleted} supprimé(s)"
                if skipped:
                    message += f", {skipped} déjà supprimé(s) en cascade"
                if failed:
                    message += f", {failed} échec(s) (voir {core.LOG_PATH})"
                message_color = COLOR_DANGER if failed else COLOR_LINKED


def run():
    """Point d'entrée appelé par __main__.py (sous-commande `tui`)."""
    try:
        curses.wrapper(main)
        core.logger.info("=== fin normale (touche q) ===")
    except RuntimeError as e:
        core.logger.error("arrêt sur erreur : %s", e)
        print(f"Erreur : {e} (voir {core.LOG_PATH})", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        # curses.wrapper restaure déjà le terminal avant de relaisser filer
        # l'exception (non catchée par `except Exception` ci-dessous,
        # BaseException pas Exception).
        core.logger.info("=== interrompu (Ctrl+C) ===")
        sys.exit(130)
    except Exception:
        core.logger.error("crash inattendu :\n%s", traceback.format_exc())
        print(f"Crash inattendu, trace complète dans {core.LOG_PATH}", file=sys.stderr)
        raise
