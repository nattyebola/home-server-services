// JS maison minimal (couvre exactement les 3 interactions dont clearr a
// besoin : navigation par lien data-get, soumission de formulaire data-post,
// filtre en direct data-live) — la modale de confirmation, elle, utilise le
// composant Modal natif de Bootstrap (bootstrap.min.js, vendoré à côté de
// bootstrap.min.css) plutôt qu'être réimplémentée à la main : focus trap,
// touche Échap, clic sur le fond, aria-* sont déjà corrects dans son JS,
// les réécrire ici aurait été strictement moins bien. Recharge toujours le
// fragment ciblé en entier depuis le serveur — même principe "on relance
// tout puis on redessine" que la TUI curses.
(function () {
  function formParams(form) {
    return new URLSearchParams(new FormData(form)).toString();
  }

  function showModalIfTarget(target) {
    if (target === "#modal-body") {
      bootstrap.Modal.getOrCreateInstance(document.getElementById("modal")).show();
    }
  }

  function hideModal() {
    var instance = bootstrap.Modal.getInstance(document.getElementById("modal"));
    if (instance) instance.hide();
  }

  async function swapInto(target, resp) {
    var el = document.querySelector(target);
    if (!el) return;
    el.innerHTML = await resp.text();
    showModalIfTarget(target);
  }

  document.addEventListener("click", function (e) {
    var get = e.target.closest("[data-get]");
    if (get) {
      e.preventDefault();
      fetch(get.getAttribute("data-get")).then(function (resp) {
        return swapInto(get.getAttribute("data-target"), resp);
      });
      return;
    }
    var post = e.target.closest("[data-post]");
    if (post) {
      e.preventDefault();
      var form = post.closest("form");
      fetch(post.getAttribute("data-post"), {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: form ? formParams(form) : null,
      }).then(function (resp) {
        hideModal();
        return swapInto(post.getAttribute("data-target"), resp);
      });
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
  var liveTimer;
  document.addEventListener("input", function (e) {
    var input = e.target.closest("[data-live]");
    if (!input) return;
    var form = input.closest("form");
    var name = input.getAttribute("name");
    var cursor = input.selectionStart;
    clearTimeout(liveTimer);
    liveTimer = setTimeout(function () {
      var url = form.getAttribute("data-live-get") + "?" + formParams(form);
      var target = form.getAttribute("data-live-target");
      fetch(url).then(function (resp) {
        return resp.text();
      }).then(function (html) {
        var el = document.querySelector(target);
        if (!el) return;
        el.innerHTML = html;
        showModalIfTarget(target);
        var newInput = el.querySelector('[name="' + name + '"]');
        if (newInput) {
          newInput.focus();
          if (typeof cursor === "number") newInput.setSelectionRange(cursor, cursor);
        }
      });
    }, 300);
  });
})();
