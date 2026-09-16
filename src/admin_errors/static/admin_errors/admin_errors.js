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

  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve) {
      var textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "");
      textarea.style.position = "absolute";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      try {
        document.execCommand("copy");
      } finally {
        document.body.removeChild(textarea);
      }
      resolve();
    });
  }

  function onCopyClick(event) {
    var button = event.target.closest(".ae-copy");
    if (!button) {
      return;
    }
    var targetId = button.getAttribute("data-ae-copy-target");
    var target = targetId && document.getElementById(targetId);
    if (!target) {
      return;
    }
    if (!button.hasAttribute("data-ae-label")) {
      button.setAttribute("data-ae-label", button.textContent);
    }
    var original = button.getAttribute("data-ae-label");
    if (button._aeCopyTimer) {
      clearTimeout(button._aeCopyTimer);
      button._aeCopyTimer = null;
    }
    copyText(target.textContent)
      .then(function () {
        var copied = button.getAttribute("data-ae-copied");
        button.textContent = copied;
        // Left in place (never cleared): a durable proof a copy succeeded, unlike the
        // visible label below, which reverts on the same 2s timer and would race a test.
        var count = parseInt(button.getAttribute("data-ae-copy-count") || "0", 10);
        button.setAttribute("data-ae-copy-count", String(count + 1));
        button._aeCopyTimer = setTimeout(function () {
          button.textContent = original;
          button._aeCopyTimer = null;
        }, 2000);
      })
      .catch(function () {
        // clipboard write rejected (unfocused document, denied permission): leave the label as-is.
      });
  }

  function onClick(event) {
    onToggleClick(event);
    onCopyClick(event);
  }

  document.addEventListener("click", onClick);
})();
