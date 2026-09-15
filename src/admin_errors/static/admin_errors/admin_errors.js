(function () {
  "use strict";

  function onToggleClick(event) {
    var button = event.target.closest(".ae-frame-toggle");
    if (!button) {
      return;
    }
    var frame = button.closest(".ae-frame");
    if (frame) {
      var expanded = frame.classList.toggle("ae-frame--expanded");
      button.setAttribute("aria-expanded", String(expanded));
    }
  }

  document.addEventListener("click", onToggleClick);
})();
