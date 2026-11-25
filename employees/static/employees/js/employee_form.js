/* Add / Edit Employee: conditional fields, derived contract status, live ID card
   preview and repeatable certificate rows. All rules are re-validated on the server. */
(function () {
    "use strict";

    var form = document.getElementById("employee-form");
    if (!form) { return; }

    var byId = function (id) { return document.getElementById(id); };
    var value = function (id) { var el = byId(id); return el ? el.value : ""; };
    var selectedText = function (id) {
        var el = byId(id);
        if (!el || !el.value || !el.options[el.selectedIndex]) { return ""; }
        return el.options[el.selectedIndex].text;
    };

    /* ---------- Conditional groups (spouse / termination date) ---------- */
    var toggleGroup = function (name, show) {
        form.querySelectorAll('[data-conditional="' + name + '"]').forEach(function (el) {
            el.classList.toggle("d-none", !show);
        });
    };

    var spouseNotice = byId("spouse-hidden-notice");
    var syncSpouse = function () {
        var married = value("id_marital_status") === "married";
        toggleGroup("spouse", married);
        if (spouseNotice) {
            var typed = ["id_spouse_name", "id_spouse_phone", "id_spouse_email"].some(function (id) {
                return value(id).trim() !== "";
            });
            var hasStored = spouseNotice.dataset.hasStored === "1";
            spouseNotice.classList.toggle("d-none", married || !(hasStored || typed));
        }
    };

    var syncTermination = function () {
        toggleGroup("termination", value("id_employment_status") === "terminated");
    };

    /* ---------- Derived (read-only) contract status ---------- */
    var statusBadge = byId("derived-contract-status");
    var syncContractStatus = function () {
        if (!statusBadge) { return; }
        var today = form.dataset.today || "";
        var status = value("id_employment_status");
        var start = value("id_contract_start_date");
        var end = value("id_contract_end_date");
        var label = "Active", tone = "success";
        if (status === "terminated") { label = "Terminated"; tone = "danger"; }
        else if (!start && !end) { label = "Not set"; tone = "secondary"; }
        else if (end && today && end < today) { label = "Expired"; tone = "warning"; }
        else if (start && today && start > today) { label = "Upcoming"; tone = "info"; }
        statusBadge.textContent = label;
        statusBadge.className = "badge text-bg-" + tone;
    };

    /* ---------- Live ID card preview ---------- */
    var PLACEHOLDER = "\u2014";
    var photoImg = byId("id-card-photo");
    var photoPlaceholder = byId("id-card-photo-placeholder");
    var setText = function (id, text) { var el = byId(id); if (el) { el.textContent = text; } };
    var showPhoto = function (src) {
        if (!photoImg) { return; }
        if (src) {
            photoImg.src = src;
            photoImg.classList.remove("d-none");
            if (photoPlaceholder) { photoPlaceholder.classList.add("d-none"); }
        } else {
            photoImg.removeAttribute("src");
            photoImg.classList.add("d-none");
            if (photoPlaceholder) { photoPlaceholder.classList.remove("d-none"); }
        }
    };

    var syncCard = function () {
        setText("id-card-name", value("id_full_name").trim() || "Employee Name");
        setText("id-card-id", value("id_employee_id").trim() || "Generated after save");
        setText("id-card-office", selectedText("id_office") || PLACEHOLDER);
        setText("id-card-department", selectedText("id_department") || PLACEHOLDER);
        setText("id-card-position", selectedText("id_position") || PLACEHOLDER);
    };

    var photoInput = byId("id_photo");
    var syncPhoto = function () {
        if (photoInput && photoInput.files && photoInput.files[0]) {
            var reader = new FileReader();
            reader.onload = function (event) { showPhoto(event.target.result); };
            reader.readAsDataURL(photoInput.files[0]);
        } else {
            showPhoto(photoImg ? photoImg.dataset.currentPhoto : "");
        }
    };

    var on = function (id, events, handler) {
        var el = byId(id);
        if (el) { events.forEach(function (name) { el.addEventListener(name, handler); }); }
    };
    on("id_marital_status", ["change"], syncSpouse);
    ["id_spouse_name", "id_spouse_phone", "id_spouse_email"].forEach(function (id) { on(id, ["input"], syncSpouse); });
    on("id_employment_status", ["change"], function () { syncTermination(); syncContractStatus(); });
    ["id_contract_start_date", "id_contract_end_date"].forEach(function (id) {
        on(id, ["input", "change"], syncContractStatus);
    });
    ["id_full_name", "id_employee_id", "id_office", "id_department", "id_position"].forEach(function (id) {
        on(id, ["input", "change"], syncCard);
    });
    on("id_photo", ["change"], syncPhoto);

    /* ---------- Repeatable certificates ---------- */
    var list = byId("certificate-list");
    var template = byId("certificate-empty-form");
    var addButton = byId("add-certificate");
    if (list && template) {
        var prefix = list.dataset.prefix;
        var maxForms = parseInt(list.dataset.max, 10) || 25;
        var maxBytes = parseInt(list.dataset.maxBytes, 10) || 10485760;
        var allowedExt = (list.dataset.allowedExt || "").split(",");
        var totalInput = form.querySelector('[name="' + prefix + '-TOTAL_FORMS"]');
        var initialInput = form.querySelector('[name="' + prefix + '-INITIAL_FORMS"]');
        var emptyMessage = byId("certificate-empty");
        var indexPattern = new RegExp("(" + prefix + ")-(\\d+|__prefix__)-");

        var items = function () { return list.querySelectorAll("[data-certificate-item]"); };

        var reindex = function (item, index) {
            var attrs = ["name", "id", "for"];
            [item].concat(Array.prototype.slice.call(item.querySelectorAll("*"))).forEach(function (el) {
                attrs.forEach(function (attr) {
                    var current = el.getAttribute(attr);
                    if (current && indexPattern.test(current)) {
                        el.setAttribute(attr, current.replace(indexPattern, "$1-" + index + "-"));
                    }
                });
            });
        };

        var refresh = function () {
            var count = items().length;
            if (emptyMessage) { emptyMessage.classList.toggle("d-none", count > 0); }
            if (addButton) {
                var label = addButton.querySelector("[data-label]");
                if (label) { label.textContent = count === 0 ? "Add Certificate" : "Add Another Certificate"; }
                addButton.disabled = count >= maxForms;
            }
        };

        /* Keep new rows numbered consecutively right after the saved ones, so the
           formset never sees gaps and every row keeps its own file + metadata. */
        var renumberNewItems = function () {
            var initial = parseInt(initialInput ? initialInput.value : "0", 10) || 0;
            var next = initial;
            items().forEach(function (item) {
                if (item.hasAttribute("data-new-item")) { reindex(item, next); next += 1; }
            });
            if (totalInput) { totalInput.value = next; }
        };

        if (addButton) {
            addButton.addEventListener("click", function () {
                if (items().length >= maxForms || !totalInput) { return; }
                var index = parseInt(totalInput.value, 10) || 0;
                var holder = document.createElement("div");
                holder.innerHTML = template.innerHTML.replace(/__prefix__/g, String(index)).trim();
                var item = holder.firstElementChild;
                item.setAttribute("data-new-item", "");
                list.appendChild(item);
                totalInput.value = index + 1;
                refresh();
                var first = item.querySelector("input:not([type=hidden]), textarea");
                if (first) { first.focus(); }
            });
        }

        list.addEventListener("click", function (event) {
            var button = event.target.closest("[data-remove-certificate]");
            if (!button) { return; }
            var item = button.closest("[data-certificate-item]");
            if (item) { item.remove(); renumberNewItems(); refresh(); }
        });

        list.addEventListener("change", function (event) {
            var input = event.target;
            if (!input.matches || !input.matches('input[type="file"]')) { return; }
            var item = input.closest("[data-certificate-item]");
            var message = item ? item.querySelector("[data-file-error]") : null;
            var file = input.files && input.files[0];
            var problem = "";
            if (file) {
                var dot = file.name.lastIndexOf(".");
                var ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : "";
                if (allowedExt.indexOf(ext) === -1) { problem = "Unsupported file type."; }
                else if (file.size > maxBytes) { problem = "The document must not be larger than 10 MB."; }
            }
            if (problem) { input.value = ""; }
            if (message) { message.textContent = problem; message.classList.toggle("d-none", !problem); }
        });

        refresh();
    }

    syncSpouse();
    syncTermination();
    syncContractStatus();
    syncCard();
    syncPhoto();
})();
