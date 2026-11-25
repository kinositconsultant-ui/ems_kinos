(function () {
    "use strict";

    var parentLinkSelector =
        '.app-sidebar [data-lte-toggle="treeview"] > .nav-item > .nav-link';

    function updateExpandedState(link) {
        link.setAttribute(
            "aria-expanded",
            String(link.parentElement.classList.contains("menu-open"))
        );
    }

    document.querySelectorAll(parentLinkSelector).forEach(function (link) {
        if (!link.parentElement.querySelector(":scope > .nav-treeview")) {
            return;
        }
        updateExpandedState(link);
        link.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
            link.parentElement.classList.toggle("menu-open");
            updateExpandedState(link);
        });
    });
})();
