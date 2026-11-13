// JS maison minimal (couvre exactement les 3 interactions dont clearr a
// besoin : navigation par lien data-get, soumission de formulaire data-post,
// filtre en direct data-live) — la modale de confirmation, elle, utilise le
// composant Modal natif de Bootstrap (bootstrap.min.js, vendoré à côté de
// bootstrap.min.css) plutôt qu'être réimplémentée à la main : focus trap,
// touche Échap, clic sur le fond, aria-* sont déjà corrects dans son JS,
// les réécrire ici aurait été strictement moins bien. Recharge toujours le
// fragment ciblé en entier depuis le serveur ("on relance tout puis on
// redessine").
(function () {
  var modalEl = document.getElementById("modal");

  function formParams(form) {
    return new URLSearchParams(new FormData(form)).toString();
  }

  // --- Modale : ouverture, focus initial, retour du focus ---------------------
  // Ouverte par script (pas par data-bs-toggle) : Bootstrap ne sait donc pas
  // quel élément l'a ouverte et ne lui rend pas le focus à la fermeture — le
  // focus tombait sur <body>, et le clavier repartait du haut de la page.
  // modalOpener garde le déclencheur ; s'il a disparu entre-temps (la
  // suppression a remplacé #tab-content), le focus va à l'onglet actif.
  var modalOpener = null;

  // Bootstrap 5.3 met le focus sur le conteneur de la modale et ignore
  // l'attribut autofocus du contenu injecté : on le fait ici. Les écrans de
  // confirmation le posent sur « Annuler » — l'action sûre, pour qu'un Entrée
  // réflexe ferme la modale au lieu de supprimer.
  function focusAutofocus() {
    var target = modalEl.querySelector("[autofocus]");
    if (target) target.focus();
  }

  function openModal() {
    if (modalEl.classList.contains("show")) {
      focusAutofocus();   // déjà ouverte : shown.bs.modal ne se redéclenchera pas
    } else {
      bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
  }

  function hideModal() {
    var instance = bootstrap.Modal.getInstance(modalEl);
    if (instance) instance.hide();
  }

  modalEl.addEventListener("shown.bs.modal", focusAutofocus);
  modalEl.addEventListener("hidden.bs.modal", function () {
    var target = modalOpener && modalOpener.isConnected
      ? modalOpener
      : document.querySelector('#tab-content [aria-current="page"]');
    modalOpener = null;
    if (target) target.focus();
  });

  // --- Zone d'annonce ----------------------------------------------------------
  // Zone permanente hors de #tab-content (voir page.html) : y écrire une erreur
  // plutôt que dans la cible évite d'effacer la barre d'onglets ou les boutons
  // de la modale, ce qui laissait l'utilisateur sans aucun moyen de réessayer.
  // C'est aussi la seule zone role="status" de la page : le message de résultat
  // d'une suppression, rendu par le serveur DANS le fragment (data-flash), y
  // est déplacé — resté dans #tab-content, il n'était pas annoncé du tout.
  function showAlert(html) {
    var zone = document.getElementById("alert-zone");
    if (!zone) return;
    zone.innerHTML = html;
    zone.scrollIntoView({ block: "nearest" });
  }

  function failed(message) {
    showAlert('<div class="alert alert-danger m-0 mb-3">' + message + "</div>");
  }

  function adoptFlash(el) {
    var zone = document.getElementById("alert-zone");
    if (!zone) return;
    var flash = el.querySelector("[data-flash]");
    if (flash) {
      zone.replaceChildren(flash);
      zone.scrollIntoView({ block: "nearest" });
    } else {
      zone.replaceChildren();
    }
  }

  // `after(el)` : rejoué après le remplacement (remise du focus, voir plus bas).
  async function swapInto(target, resp, after) {
    var body = await resp.text();
    // Une réponse d'erreur (503 quand transmission-vpn est injoignable, 403
    // cross-origin) NE doit pas remplacer la cible : le handler renvoie un
    // fragment nu, et #tab-content contient la barre d'onglets.
    if (!resp.ok) {
      showAlert(body);
      return;
    }
    var el = document.querySelector(target);
    if (!el) return;
    el.innerHTML = body;
    adoptFlash(el);
    syncFragmentState();
    if (target === "#modal-body") openModal();
    if (after) after(el);
  }

  // --- Une seule requête à la fois vers #tab-content --------------------------
  // Clic d'onglet, tri et filtre en direct écrivent tous dans #tab-content.
  // Sans coordination, la DERNIÈRE réponse arrivée gagnait, pas la dernière
  // action : taper dans le filtre puis cliquer aussitôt « Séries » laissait
  // partir la recherche du filtre 300 ms plus tard, et sa réponse remplaçait
  // la vue Séries par la vue Torrents. La ligne visée pouvait alors changer
  // entre la lecture et le clic sur une croix. Règle : toute nouvelle action
  // annule la requête en vol et le filtre en attente.
  var liveTimer;
  var tabRequest = null;

  function cancelTabRequest() {
    clearTimeout(liveTimer);
    if (tabRequest) tabRequest.abort();
    tabRequest = null;
  }

  function claimTabContent() {
    cancelTabRequest();
    tabRequest = new AbortController();
    return tabRequest.signal;
  }

  // Le focus est perdu à chaque remplacement de #tab-content (l'élément
  // focalisé est détruit). data-focus-key désigne l'élément équivalent dans le
  // nouveau fragment : l'onglet ou l'en-tête de tri qu'on vient d'activer.
  function refocus(key) {
    return function (el) {
      var target = key && el.querySelector('[data-focus-key="' + key + '"]');
      if (target) target.focus();
    };
  }

  // Désactive le déclencheur le temps de la requête et affiche un spinner
  // Bootstrap (déjà dans le CSS vendoré, aucune dépendance ajoutée). Sans ça,
  // le bouton « Confirmer la suppression » restait cliquable pendant les
  // quelques secondes d'une suppression de série : un second POST partait
  // vraiment, tombait sur « déjà supprimée » et ÉCRASAIT le message de succès
  // du premier — l'utilisateur croyait que rien n'avait été supprimé alors que
  // tout l'avait été.
  function busy(el) {
    if (!el || el.dataset.busy) return function () {};
    el.dataset.busy = "1";
    el.setAttribute("aria-busy", "true");
    el.classList.add("disabled");
    var spinner = document.createElement("span");
    spinner.className = "spinner-border spinner-border-sm ms-2";
    spinner.setAttribute("aria-hidden", "true");
    el.appendChild(spinner);
    return function () {
      delete el.dataset.busy;
      el.removeAttribute("aria-busy");
      el.classList.remove("disabled");
      spinner.remove();
    };
  }

  // --- Jaquette au survol ---------------------------------------------------
  // Un seul conteneur flottant réutilisé, attaché au <body> : les lignes vivent
  // dans .table-responsive (overflow), qui découperait une vignette positionnée
  // à l'intérieur du tableau. L'image n'est demandée qu'au premier survol
  // (data-poster porte l'URL, pas un <img> déjà présent — voir
  // templates/_meta.html) et reste ensuite dans le cache navigateur.
  var POSTER_WIDTH = 180;
  var posterPop = null;

  function posterElement() {
    if (!posterPop) {
      posterPop = document.createElement("div");
      posterPop.id = "poster-pop";
      posterPop.innerHTML = '<img alt="">';
      posterPop.querySelector("img").addEventListener("error", hidePoster);
      document.body.appendChild(posterPop);
    }
    return posterPop;
  }

  function showPoster(host) {
    var pop = posterElement();
    var img = pop.querySelector("img");
    var url = host.getAttribute("data-poster");
    if (img.getAttribute("src") !== url) img.setAttribute("src", url);
    img.alt = host.getAttribute("data-poster-title") || "";
    pop.classList.add("visible");
    // Ancré sur la cellule survolée (pas sur le curseur) : pas de vignette qui
    // suit la souris, et la position reste stable pendant qu'on lit.
    var rect = host.getBoundingClientRect();
    var height = pop.offsetHeight || POSTER_WIDTH * 1.5;
    var left = rect.right + 12;
    if (left + POSTER_WIDTH > window.innerWidth - 8) {
      left = Math.max(8, rect.left - POSTER_WIDTH - 12);
    }
    var top = rect.top + rect.height / 2 - height / 2;
    top = Math.min(Math.max(8, top), Math.max(8, window.innerHeight - height - 8));
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  function hidePoster() {
    if (posterPop) posterPop.classList.remove("visible");
  }

  document.addEventListener("mouseover", function (e) {
    var host = e.target.closest("[data-poster]");
    if (host) showPoster(host);
  });

  document.addEventListener("mouseout", function (e) {
    if (e.target.closest("[data-poster]")) hidePoster();
  });

  // Un swap de fragment ou un défilement peut faire disparaître la cellule
  // survolée sans qu'aucun mouseout ne soit émis — la vignette resterait
  // affichée dans le vide.
  window.addEventListener("scroll", hidePoster, true);

  // --- Filtre par fichier (Séries / Animés / Films) --------------------------
  // Le masquage lui-même est purement CSS, depuis data-clearr-empty sur <html> :
  // l'attribut vit hors de #tab-content, donc rien à rejouer après un swap de
  // fragment, et aucun aller-retour serveur au basculement.
  // Seul l'état COCHÉ des radios ne peut pas venir du CSS, et elles vivent DANS
  // le fragment remplacé — d'où cette resynchro après chaque swap.
  // Volontairement NON mémorisé (demandé le 2026-10-08) : chaque chargement
  // repart sur « Avec fichier », comme le filtre par type des Torrents.

  function syncFileFilters() {
    var state = document.documentElement.getAttribute("data-clearr-empty") || "";
    document.querySelectorAll("[data-file-filter]").forEach(function (radio) {
      radio.checked = radio.value === state;
    });
  }

  document.addEventListener("change", function (e) {
    var radio = e.target.closest("[data-file-filter]");
    if (!radio) return;
    if (radio.value) {
      document.documentElement.setAttribute("data-clearr-empty", radio.value);
    } else {
      document.documentElement.removeAttribute("data-clearr-empty");
    }
  });

  // --- Filtre par type (vue Torrents) ----------------------------------------
  // Même mécanique que le filtre par fichier ci-dessus : masquage CSS depuis
  // data-clearr-kind sur <html>, radios resynchronisées après chaque swap.
  // Volontairement NON mémorisé (pas de localStorage) : il se comporte comme
  // le filtre par nom, qui repart vide à chaque chargement.
  function syncKindFilters() {
    var kind = document.documentElement.getAttribute("data-clearr-kind") || "";
    document.querySelectorAll("[data-kind-filter]").forEach(function (radio) {
      radio.checked = radio.value === kind;
    });
    syncKindEmpty(kind);
  }

  // Le CSS masque les lignes mais ne sait pas dire « il n'en reste aucune » :
  // sans ce message, un type à zéro (ou vidé par le filtre par nom, ou par la
  // dernière suppression) laissait un tableau vide sans explication.
  function syncKindEmpty(kind) {
    document.querySelectorAll("[data-kind-empty]").forEach(function (row) {
      row.hidden = !kind || !!row.closest("tbody").querySelector('tr[data-kind="' + kind + '"]');
    });
  }

  document.addEventListener("change", function (e) {
    var radio = e.target.closest("[data-kind-filter]");
    if (!radio) return;
    if (radio.value) {
      document.documentElement.setAttribute("data-clearr-kind", radio.value);
    } else {
      document.documentElement.removeAttribute("data-clearr-kind");
    }
    syncKindEmpty(radio.value);
  });

  // État porté par <html> mais affiché DANS le fragment : à rejouer après
  // chaque remplacement de #tab-content.
  function syncFragmentState() {
    syncFileFilters();
    syncKindFilters();
  }

  document.addEventListener("click", function (e) {
    // Les liens IMDb/TVDB/TMDB/Sonarr/Radarr sont de vrais liens externes
    // (target=_blank) : ils ne doivent pas être interceptés par le handler
    // data-get ci-dessous, et n'ont aucune raison de le déclencher.
    if (e.target.closest(".meta-link")) return;
    hidePoster();
    var get = e.target.closest("[data-get]");
    if (get) {
      e.preventDefault();
      if (get.dataset.busy) return;
      var getTarget = get.getAttribute("data-target");
      var signal;
      if (getTarget === "#tab-content") {
        signal = claimTabContent();
      } else if (getTarget === "#modal-body") {
        modalOpener = get;
      }
      var doneGet = busy(get);
      fetch(get.getAttribute("data-get"), { signal: signal }).then(function (resp) {
        return swapInto(getTarget, resp, refocus(get.getAttribute("data-focus-key")));
      }).catch(function (err) {
        if (err && err.name === "AbortError") return;   // remplacée par une action plus récente
        // Rejet réseau réel (conteneur en redémarrage après make update,
        // Wi-Fi coupé) : sans .catch(), la promesse était rejetée sans que
        // rien n'apparaisse à l'écran.
        failed("Requête échouée — clearr est-il joignable ? Réessayez.");
      }).finally(doneGet);
      return;
    }
    var post = e.target.closest("[data-post]");
    if (post) {
      e.preventDefault();
      if (post.dataset.busy) return;   // garde anti double-soumission
      // Le POST lui-même n'est jamais annulé (le serveur supprimerait quand
      // même) : on annule seulement ce qui pourrait écraser son résultat.
      if (post.getAttribute("data-target") === "#tab-content") cancelTabRequest();
      var donePost = busy(post);
      // data-form : le déclencheur n'est pas toujours DANS le formulaire qu'il
      // soumet — le pied d'une modale Bootstrap est un frère de son corps, et
      // un <form> qui envelopperait les deux casserait le flex de .modal-content
      // (imbriquer deux <form> étant par ailleurs invalide en HTML). Sans ça, le
      // choix des saisons partait vide : closest("form") ne trouvait rien.
      var selector = post.getAttribute("data-form");
      var form = selector ? document.querySelector(selector) : post.closest("form");
      fetch(post.getAttribute("data-post"), {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: form ? formParams(form) : null,
      }).then(function (resp) {
        hideModal();
        return swapInto(post.getAttribute("data-target"), resp);
      }).catch(function () {
        hideModal();
        // Pas « rien n'a été supprimé » : la connexion peut tomber APRÈS que
        // le serveur a reçu la demande (Traefik ou clearr redémarré pendant
        // une suppression de série), et la suppression a alors bien eu lieu.
        failed("Pas de réponse de clearr : impossible de savoir si la suppression a eu lieu. "
               + "Rechargez l'onglet pour voir l'état réel avant de réessayer.");
      }).finally(donePost);
      return;
    }
  });

  // Le formulaire de filtre porte data-live-get/data-live-target (PAS
  // data-get/data-target) exprès : le handler de clic ci-dessus fait
  // closest("[data-get]") depuis l'élément cliqué, qui remonte jusqu'à
  // n'importe quel ancêtre portant cet attribut — si le <form> lui-même
  // avait porté data-get (comme avant le 2026-08-01), cliquer n'importe où
  // dedans, y compris dans le champ texte, déclenchait immédiatement une
  // navigation/swap au clic, avant même de taper quoi que ce soit (symptôme
  // rapporté : le focus se perdait "tout de suite après" le clic, pas après
  // une frappe). data-live-get/data-live-target ne sont lus qu'ici, jamais
  // par le handler de clic.
  //
  // Le champ filtre est en plus DANS la zone remplacée à chaque frappe (le
  // fragment renvoyé par le serveur inclut le formulaire de filtre
  // lui-même) — un swap nu détruit et recrée l'input à chaque appel, donc
  // perdrait le focus après la première frappe une fois le clic lui-même
  // corrigé. On retrouve l'input équivalent dans le nouveau HTML par son
  // name et on lui rend le focus + la position du curseur.
  document.addEventListener("input", function (e) {
    var input = e.target.closest("[data-live]");
    if (!input) return;
    // Annulée DÈS la frappe, pas au départ de la requête suivante : une
    // réponse arrivée pendant qu'on tape remettait dans le champ l'ancienne
    // valeur, et les caractères tapés entre-temps étaient perdus.
    cancelTabRequest();
    var form = input.closest("form");
    var name = input.getAttribute("name");
    liveTimer = setTimeout(function () {
      var url = form.getAttribute("data-live-get") + "?" + formParams(form);
      var target = form.getAttribute("data-live-target");
      // Lu au départ de la requête, pas à la frappe : le curseur a pu bouger
      // pendant le délai.
      var cursor = input.selectionStart;
      fetch(url, { signal: claimTabContent() }).then(function (resp) {
        return swapInto(target, resp, function (el) {
          var newInput = el.querySelector('[name="' + name + '"]');
          if (newInput) {
            newInput.focus();
            if (typeof cursor === "number") newInput.setSelectionRange(cursor, cursor);
          }
        });
      }).catch(function (err) {
        if (err && err.name === "AbortError") return;   // annulation voulue
        failed("Le filtre n'a pas pu être appliqué — réessayez.");
      });
    }, 300);
  });

  syncFragmentState();   // rendu initial : #tab-content vient de page.html
})();
