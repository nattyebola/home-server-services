#!/bin/sh
# Compile mangoapp (MangoHud) dans ~/.local/bin : c'est lui que gamescope --mangoapp lance
# pour l'overlay de performances de Big Picture (compteur d'IPS, menu Performances).
# Ni le paquet Ubuntu mangohud ni le binaire officiel ne le fournissent.
# Appelé par `make steam-tv-install`, après install-root.sh (dépendances de compilation).
# Ne recompile que si la version demandée n'est pas déjà installée.
set -eu
version=v0.8.4
sha256=bbe5a2b976313c53f21dc24b98bbed03226589460a0cce20d923cb2c36c9d8b0
dest=$HOME/.local/bin/mangoapp
stamp=$HOME/.local/state/steam-tv/mangoapp.version

if [ -x "$dest" ] && [ "$(cat "$stamp" 2>/dev/null)" = "$version" ]; then
  echo "mangoapp $version déjà installé"
  exit 0
fi

work=${XDG_CACHE_HOME:-$HOME/.cache}/steam-tv/mangohud-$version
rm -rf "$work"
mkdir -p "$work" "$(dirname "$stamp")" "$(dirname "$dest")"
tarball=$work/source.tar.xz
curl -fsSL -o "$tarball" \
  "https://github.com/flightlessmango/MangoHud/releases/download/$version/MangoHud-$version-Source.tar.xz"
echo "$sha256  $tarball" | sha256sum -c --quiet -
tar -xJf "$tarball" -C "$work"
src=$work/MangoHud-$version

# Sous-projets (imgui, spdlog, en-têtes Vulkan) livrés dans l'archive : aucun téléchargement.
# Seul mangoapp est utile ici : pas de couche Vulkan installée, pas de Wayland (gamescope = X),
# pas de stats NVIDIA (GPU AMD).
meson setup "$src/build" "$src" --buildtype=release --wrap-mode=nodownload \
  -Dmangoapp=true -Dwith_x11=enabled -Dwith_wayland=disabled \
  -Dmangoplot=disabled -Dtests=disabled -Dmangohudctl=false -Dwith_xnvctrl=disabled >/dev/null
ninja -C "$src/build" src/mangoapp >/dev/null
install -m 755 "$src/build/src/mangoapp" "$dest"
echo "$version" > "$stamp"
rm -rf "$work"
echo "mangoapp $version installé dans $dest"
