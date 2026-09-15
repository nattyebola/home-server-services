#!/bin/sh
# Custom Script Connect pour Sonarr — marque les fins de saison / de série /
# de mi-saison dans le <title> des .nfo d'épisode.
#
# Pourquoi le <title> du .nfo : c'est le SEUL canal qui remonte jusqu'au
# listing de saison de Kodi. Sonarr écrit le .nfo (metadata writer XBMC,
# episodeMetadata), Jellyfin le lit en priorité (Nfo en tête du
# LocalMetadataReaderOrder, donc il fait autorité sur le titre), et
# jellyfin-kodi recopie ce titre dans MyVideos*.db, colonne episode.c00 —
# ce que le skin affiche. Sonarr n'expose AUCUN token de renommage pour
# finaleType (vérifié : {Episode FinaleType} se comporte comme un token
# inconnu, remplacé par du vide), et Kodi n'offre aucun hook de décoration
# des listings. Voir .claude/docs/arr-config.md.
#
# Tourne DANS le conteneur sonarr (monté en ro sur /config, comme
# cross-seed-notify.sh) : pas de python dans cette image, mais curl, jq et
# xmlstarlet y sont. Deux appelants, une seule implémentation :
#   - Sonarr lui-même, sur Import/Upgrade/Rename  -> la série concernée
#   - le cron de l'hôte, `docker exec ... --all`  -> toute la bibliothèque
set -eu

# Glyphes choisis dans la police du skin Estuary elle-même
# (NotoSans-Regular.ttf), pas seulement dans la police de repli de Kodi :
# aucun emoji n'est rendu par Kodi (plan 1 absent des deux polices, et pas
# de police emoji sur l'hôte), un emoji s'afficherait en tofu.
MARK_SEASON='†'
MARK_SERIES='‡'
MARK_MID='½'

API="http://localhost:8989/api/v3"
DRY_RUN=0
changed=0

log() { echo "mark-finale: $*"; }

api() {
  # La clé API ne passe jamais en argv (lisible dans un `ps`), même raison
  # que _curl() dans scripts/apply-arr-overrides.py : curl la lit sur stdin
  # via --config. Elle vient de la config de Sonarr, pas d'un .env : le
  # script tourne déjà dans le conteneur qui la possède.
  printf 'header = "X-Api-Key: %s"\n' "$API_KEY" | curl -fsS --config - "$API$1"
}

# finaleType seul ne suffit pas pour « la série est terminée » : TVDB marque
# le dernier épisode DIFFUSÉ, pas une fin définitive — 6 des 16 séries
# marquées "series" étaient encore en cours au moment d'écrire ceci. Le
# statut de la série tranche ; sans lui on afficherait « fin de série » sur
# une série qui aura une saison 2. Et comme un épisode ne porte qu'une
# valeur, un "series" non confirmé DOIT retomber sur la fin de saison,
# sinon le dernier épisode de la saison n'aurait plus aucun marqueur.
marker_for() {
  case "$1" in
    series)    if [ "$2" = ended ]; then printf '%s' "$MARK_SERIES"
               else printf '%s' "$MARK_SEASON"; fi ;;
    season)    printf '%s' "$MARK_SEASON" ;;
    midseason) printf '%s' "$MARK_MID" ;;
  esac
}

# Retire un marqueur déjà posé pour repartir du titre nu : c'est ce qui rend
# le script idempotent ET réversible (un finaleType qui disparaît côté TVDB,
# une série "ended" ressuscitée par un revival).
strip_mark() {
  t=$1
  case "$t" in
    "$MARK_SERIES "*) t=${t#"$MARK_SERIES "} ;;
    "$MARK_SEASON "*) t=${t#"$MARK_SEASON "} ;;
    "$MARK_MID "*)    t=${t#"$MARK_MID "} ;;
  esac
  printf '%s' "$t"
}

# Patch par numéro de ligne, sans parser le XML. Trois raisons, toutes
# vérifiées sur cette bibliothèque — xmlstarlet a été écarté après coup :
#  - un .nfo de fichier multi-épisodes contient DEUX <episodedetails> racine
#    (format Kodi, mais XML illégal) : xmlstarlet rejette tout le fichier avec
#    « Extra content at the end of the document » (Amphibia S01E19-E20) ;
#  - une apostrophe dans le CHEMIN casse son expression XPath interne
#    (« FROM - S01E10 - Oh, the Places We'll Go » -> Invalid expression) ;
#  - en restant dans la forme échappée du fichier on ne décode jamais les
#    entités, donc &amp; ne peut pas se dégrader en &amp;amp; à chaque passage
#    (ce que faisait le couple sel/ed, titre jamais stable donc réécriture
#    sans fin — vu sur « Question & Answer », Dorohedoro S02E11).
# Le glyphe ajouté n'est pas un caractère à échapper : préfixer la forme
# échappée donne exactement le même résultat que passer par le texte décodé.
apply_to_nfo() {
  nfo=$1; epnum=$2; want=$3
  [ -f "$nfo" ] || return 0

  # Le <title> d'un bloc précède son <episode> : on repère l'épisode, puis on
  # remonte au dernier <title> au-dessus, qui est celui du même bloc.
  epline=$(grep -n "<episode>$epnum</episode>" "$nfo" | head -1 | cut -d: -f1)
  [ -n "$epline" ] || { log "pas de <episode>$epnum> dans $nfo" >&2; return 0; }
  tline=$(head -n "$epline" "$nfo" | grep -n "<title>" | tail -1 | cut -d: -f1)
  [ -n "$tline" ] || { log "pas de <title> pour l'épisode $epnum dans $nfo" >&2; return 0; }

  line=$(sed -n "${tline}p" "$nfo")
  indent=${line%%<title>*}
  current=${line#*<title>}; current=${current%</title>*}

  bare=$(strip_mark "$current")
  if [ -n "$want" ]; then target="$want $bare"; else target="$bare"; fi
  [ "$target" = "$current" ] && return 0

  changed=$((changed + 1))
  if [ "$DRY_RUN" = 1 ]; then
    log "[dry-run] $current  ->  $target"
    return 0
  fi

  # La ligne de remplacement passe par un FICHIER, jamais par `awk -v` : un -v
  # traverse le traitement des séquences d'échappement, donc un titre
  # contenant une barre oblique inverse serait altéré au passage. Même piège
  # que celui déjà documenté pour scripts/install-crontab.sh.
  repl=$(mktemp); out=$(mktemp)
  printf '%s<title>%s</title>\n' "$indent" "$target" > "$repl"
  awk -v n="$tline" 'NR==FNR { new=$0; next } FNR==n { print new; next } { print }' \
      "$repl" "$nfo" > "$out"
  cat "$out" > "$nfo"   # réécrit l'inode en place plutôt que de le remplacer
  rm -f "$repl" "$out"
  log "$current  ->  $target"
}

process_series() {
  sid=$1
  status=$(api "/series/$sid" | jq -r '.status')

  tmp=$(mktemp)
  # includeEpisodeFile évite un appel par épisode. finaleType est vide pour
  # l'immense majorité des épisodes : on les traite quand même, c'est ce qui
  # permet de RETIRER un marqueur devenu faux.
  api "/episode?seriesId=$sid&includeEpisodeFile=true" | jq -r '
    .[] | select(.hasFile and .episodeFile != null)
        | "\(.finaleType // "-") \(.episodeNumber) \(.episodeFile.path)"' > "$tmp"

  # Le chemin est en dernier et capté par le reste de la ligne : il contient
  # des espaces, des apostrophes et des virgules, jamais de retour ligne.
  while read -r ftype epnum path; do
    [ -n "${path:-}" ] || continue
    [ "$ftype" = "-" ] && ftype=""
    apply_to_nfo "${path%.*}.nfo" "$epnum" "$(marker_for "$ftype" "$status")"
  done < "$tmp"
  rm -f "$tmp"
}

API_KEY=$(xmlstarlet sel -T -t -v "//ApiKey" -n /config/config.xml)

for arg in "$@"; do
  case "$arg" in
    --all)     MODE=all ;;
    --dry-run) DRY_RUN=1 ;;
    *) log "argument inconnu : $arg" >&2; exit 2 ;;
  esac
done

if [ "${MODE:-hook}" = all ]; then
  for sid in $(api "/series" | jq -r '.[].id'); do process_series "$sid"; done
  log "$changed titre(s) corrigé(s) sur l'ensemble de la bibliothèque"
  exit 0
fi

# Mode hook. Sonarr teste le script depuis son UI avec des variables
# factices : ne pas planter dessus (même garde que cross-seed-notify.sh).
if [ "${sonarr_eventtype:-}" = "Test" ]; then
  log "événement Test, rien à faire"
  exit 0
fi
if [ -z "${sonarr_series_id:-}" ]; then
  log "pas de sonarr_series_id (événement sans série ?), ignoré"
  exit 0
fi

# Deux passes. On ne sait pas si Sonarr écrit le .nfo avant ou après avoir
# lancé ce script, et c'est exactement la classe de bug déjà documentée dans
# CLAUDE.md (écritures Servarr asynchrones, le 202 de qualitydefinition) :
# un test manuel réussi ne prouverait rien. Plutôt que de parier sur l'ordre,
# on repasse — la seconde passe ne réécrit que si Sonarr est repassé derrière,
# et le cron --all reste le filet si les deux passes tombent trop tôt.
process_series "$sonarr_series_id"
sleep 5
process_series "$sonarr_series_id"
