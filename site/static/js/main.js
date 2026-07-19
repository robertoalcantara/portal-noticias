/* BRGrid — pequenas interações de front-end (sem framework, sem build):
   1) reveal-on-scroll: seções/cards entram com fade+slide quando
      aparecem na viewport;
   2) carrossel horizontal de categoria (.hscroll-wrap): setas,
      fade nas bordas, arraste com mouse e roda do mouse mapeada pra
      scroll horizontal.
   Ambos são puramente incrementais — sem JS (ou em navegador antigo
   sem IntersectionObserver), o site continua funcional e legível. */
(function () {
  "use strict";

  var reduceMotion =
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  function initReveal() {
    var items = document.querySelectorAll(".reveal");
    if (!items.length) return;

    if (reduceMotion || !("IntersectionObserver" in window)) {
      items.forEach(function (el) {
        el.classList.add("is-visible");
      });
      return;
    }

    var io = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            entry.target.classList.add("is-visible");
            io.unobserve(entry.target);
          }
        });
      },
      { threshold: 0.12, rootMargin: "0px 0px -40px 0px" }
    );

    items.forEach(function (el) {
      io.observe(el);
    });
  }

  function initHscrolls() {
    var wraps = document.querySelectorAll(".hscroll-wrap");

    wraps.forEach(function (wrap) {
      var track = wrap.querySelector(".hscroll");
      var prevBtn = wrap.querySelector(".hscroll-prev");
      var nextBtn = wrap.querySelector(".hscroll-next");
      if (!track) return;

      function updateEdges() {
        var atStart = track.scrollLeft <= 2;
        var atEnd = track.scrollLeft + track.clientWidth >= track.scrollWidth - 2;
        track.classList.toggle("is-at-start", atStart);
        track.classList.toggle("is-at-end", atEnd);
        if (prevBtn) prevBtn.disabled = atStart;
        if (nextBtn) nextBtn.disabled = atEnd;
      }

      function scrollByPage(dir) {
        track.scrollBy({
          left: dir * track.clientWidth * 0.9,
          behavior: reduceMotion ? "auto" : "smooth",
        });
      }

      if (prevBtn) {
        prevBtn.addEventListener("click", function () {
          scrollByPage(-1);
        });
      }
      if (nextBtn) {
        nextBtn.addEventListener("click", function () {
          scrollByPage(1);
        });
      }

      track.addEventListener("scroll", updateEdges, { passive: true });
      window.addEventListener("resize", updateEdges);
      updateEdges();

      // Arraste com o mouse — touch já rola nativamente, então só liga
      // esse comportamento pra ponteiros do tipo "mouse".
      var isDown = false;
      var startX = 0;
      var startScroll = 0;
      var moved = 0;

      track.addEventListener("pointerdown", function (e) {
        if (e.pointerType !== "mouse") return;
        isDown = true;
        moved = 0;
        startX = e.clientX;
        startScroll = track.scrollLeft;
        track.classList.add("is-dragging");
        track.setPointerCapture(e.pointerId);
      });

      track.addEventListener("pointermove", function (e) {
        if (!isDown) return;
        var dx = e.clientX - startX;
        moved = Math.max(moved, Math.abs(dx));
        track.scrollLeft = startScroll - dx;
      });

      function endDrag() {
        if (!isDown) return;
        isDown = false;
        track.classList.remove("is-dragging");
      }
      track.addEventListener("pointerup", endDrag);
      track.addEventListener("pointercancel", endDrag);
      track.addEventListener("pointerleave", endDrag);

      // Depois de um arraste de verdade, não deixa o clique "vazar"
      // pro link do card (senão o usuário arrasta e sem querer abre
      // a matéria de baixo do cursor).
      track.addEventListener(
        "click",
        function (e) {
          if (moved > 6) {
            e.preventDefault();
            e.stopPropagation();
          }
        },
        true
      );

      // Roda do mouse: rola o carrossel na horizontal sem precisar
      // segurar Shift (o gesto "óbvio" de quem só tem mouse).
      track.addEventListener(
        "wheel",
        function (e) {
          if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) {
            track.scrollLeft += e.deltaY;
            e.preventDefault();
          }
        },
        { passive: false }
      );

      // Setas do teclado quando o carrossel está focado (tabindex=0
      // no template) — acessibilidade básica pra quem navega sem mouse.
      track.addEventListener("keydown", function (e) {
        if (e.key === "ArrowRight") {
          scrollByPage(1);
          e.preventDefault();
        } else if (e.key === "ArrowLeft") {
          scrollByPage(-1);
          e.preventDefault();
        }
      });
    });
  }

  function init() {
    initReveal();
    initHscrolls();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
