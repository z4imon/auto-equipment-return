// Auto Equipment Return — menu button + popover injected into the hangar.
// Follows the Tank-Stats pattern: own DOM appended outside React's #root,
// self-built button (never clone native MenuButtons), rem sizing only.

import { ModelObserver } from "../../libs/model.js";
import { MediaContext } from "../../libs/media.js";

const TAG = "[AutoEquip][JS] ";
const model = ModelObserver("AutoEquipView");

function jlog(level, msg) {
    const line = TAG + msg;
    try { console.log(line); } catch (e) {}
    try {
        if (model && model.model && model.model.onJsLog) {
            model.model.onJsLog({ level: level, msg: String(msg) });
        }
    } catch (e) {}
}
const log  = (m) => jlog("info", m);
const warn = (m) => jlog("warning", m);
const err  = (m) => jlog("error", m);

const SEL = {
    // Random hangar tags the widget div with VehicleMenu_menuWidget_*; the
    // Comp7 hangar only has the hashed VehicleMenuWidget_* class on it.
    menuWidget: '[class*="VehicleMenu_menuWidget"], [class*="VehicleMenuWidget_"]',
    // A native vehicle-menu popup (crew/vehicle/customization) while it is open.
    nativeMenu: '[class*="VehicleMenuWidget_menu_"]',
};

const OURS_ATTR  = "data-z4ae-button";
const POPOVER_ID = "z4ae-popover";

const DEVICE_ICON = (name) => "img://gui/maps/icons/artefact/" + name + ".png";

let gButton = null;
let gBtnHovered = false;
let gPopoverOpen = false;
let gData = {};
let gUi = {};
let gLastDataJson = null;

// --------------------------------------------------------------------------
function el(tag, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
}
function bg(node, url) { node.style.backgroundImage = "url(" + url + ")"; }

function parseModelJson(key, fallback) {
    try {
        const raw = (model.model && model.model[key]) || "";
        return raw ? (JSON.parse(raw) || fallback) : fallback;
    } catch (e) { err("bad JSON in " + key + ": " + e); return fallback; }
}
function ui(key, fallback) { return gUi[key] || fallback; }

// --------------------------------------------------------------------------
// Menu button (self-built, native background art, inline SVG icon)
// --------------------------------------------------------------------------
const media = MediaContext();
const SVGNS = "http://www.w3.org/2000/svg";
const ICON_FILL = "#D2D0CD";

function buttonIconSvg() {
    // Two opposing arrows — "equipment moves back and forth".
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("width", "30");
    svg.setAttribute("height", "30");
    svg.setAttribute("fill", "none");
    svg.setAttribute("class", "z4ae-btn-svg");
    const p1 = document.createElementNS(SVGNS, "path");
    p1.setAttribute("d", "M3 6.5 H12 V3.5 L17 8 L12 12.5 V9.5 H3 Z");
    p1.setAttribute("fill", ICON_FILL);
    const p2 = document.createElementNS(SVGNS, "path");
    p2.setAttribute("d", "M17 13.5 H8 V10.5 L3 15 L8 19.5 V16.5 H17 Z");
    p2.setAttribute("fill", ICON_FILL);
    svg.appendChild(p1);
    svg.appendChild(p2);
    return svg;
}

// Native button art ships in per-resolution folders; same mapping the native
// MenuButton (and the Tank-Stats button) uses.
function btnSize() {
    return media.scale > 1 ? "upscale" : media.width > 1366 ? "large" : "small";
}

function applyButtonBackground() {
    if (!gButton) return;
    const bgEl = gButton.querySelector(".z4ae-menu-btn-bg");
    if (!bgEl) return;
    // Same art states as the native MenuButton: opened > hover > enabled.
    const postfix = gPopoverOpen ? "_opened" : gBtnHovered ? "_hover" : "";
    bg(bgEl, "img://gui/maps/icons/hangar/vehicleMenu/" + btnSize() + "/btn_enabled" + postfix + ".png");
}

function setButtonHover(hovered) {
    if (gBtnHovered === hovered) return;
    gBtnHovered = hovered;
    applyButtonBackground();
}

function injectButton() {
    const menu = document.querySelector(SEL.menuWidget);
    if (!menu) return false;
    if (menu.querySelector("[" + OURS_ATTR + "]")) return true;

    const btn = el("div", "z4ae-menu-btn");
    btn.setAttribute(OURS_ATTR, "1");
    btn.appendChild(el("div", "z4ae-menu-btn-bg"));
    const iconWrap = el("div", "z4ae-menu-btn-icon");
    iconWrap.appendChild(buttonIconSvg());
    btn.appendChild(iconWrap);

    // Deliberately NO stopPropagation: the click must reach the app's own
    // outside-click handling so any open native menu closes when ours opens.
    // Our document listener below ignores clicks on the button itself.
    btn.addEventListener("click", function () {
        togglePopover();
    });
    btn.addEventListener("mouseenter", function () { setButtonHover(true); });
    btn.addEventListener("mouseleave", function () { setButtonHover(false); });

    menu.appendChild(btn);
    gButton = btn;
    applyButtonBackground();
    return true;
}

// --------------------------------------------------------------------------
// Popover — styled after the native vehicle-menu context popup: flat dark
// panel, vertical rows of [icon + UPPERCASE label], hover highlight, disabled
// rows dimmed.
// --------------------------------------------------------------------------

// Row icons as inline SVG paths (20x20 viewBox, plain fills — like the game's
// own menu glyphs).
const ROW_ICONS = {
    // arrow pointing down into a tray = save
    save: [
        "M9 2.5 h2 v6.5 h3 L10 13.5 6 9 h3 Z",
        "M4 15.5 h12 v2 H4 Z",
    ],
    // two opposing arrows = install/transfer (same motif as the menu button)
    apply: [
        "M3 6.5 H12 V3.5 L17 8 L12 12.5 V9.5 H3 Z",
        "M17 13.5 H8 V10.5 L3 15 L8 19.5 V16.5 H17 Z",
    ],
    // ring = the auto on/off toggle
    auto: [
        "M10 3 a7 7 0 1 0 0.001 0 Z M10 5.5 a4.5 4.5 0 1 1 -0.001 0 Z",
    ],
};

function rowIconSvg(name) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "20");
    svg.setAttribute("fill", "none");
    svg.setAttribute("class", "z4ae-row-svg");
    (ROW_ICONS[name] || []).forEach(function (d) {
        const p = document.createElementNS(SVGNS, "path");
        p.setAttribute("d", d);
        p.setAttribute("fill", "#E8E8E8");
        p.setAttribute("fill-rule", "evenodd");
        svg.appendChild(p);
    });
    return svg;
}

function buildSlotRow(slots) {
    const row = el("div", "z4ae-slots");
    (slots || []).forEach(function (slot) {
        if (slot && slot.icon) {
            const s = el("div", "z4ae-slot");
            bg(s, DEVICE_ICON(slot.icon));
            row.appendChild(s);
        } else {
            row.appendChild(el("div", "z4ae-slot z4ae-slot-empty"));
        }
    });
    return row;
}

function buildSetBlock(label, slots, missingText) {
    const block = el("div", "z4ae-set-block");
    const lab = el("div", "z4ae-set-label");
    lab.textContent = String(label).toUpperCase();
    block.appendChild(lab);
    if (slots) {
        block.appendChild(buildSlotRow(slots));
    } else {
        const miss = el("div", "z4ae-set-missing");
        miss.textContent = missingText;
        block.appendChild(miss);
    }
    return block;
}

// One menu row, exact copy of the native MenuItem markup:
// item > inner > [hover, icon > (svg), title > span] (+ optional switch)
function buildMenuRow(label, iconName, onClick, disabled) {
    const item = el("div", "z4ae-item");
    const inner = el("div", "z4ae-item-inner" + (disabled ? " z4ae-item-inner-disabled" : ""));
    inner.appendChild(el("div", "z4ae-item-hover"));
    const icon = el("div", "z4ae-item-icon");
    icon.appendChild(rowIconSvg(iconName));
    inner.appendChild(icon);
    const title = el("div", "z4ae-item-title");
    const span = el("span");
    span.textContent = String(label).toUpperCase();
    title.appendChild(span);
    inner.appendChild(title);
    if (!disabled && onClick) {
        inner.addEventListener("click", function (e) {
            e.stopPropagation();
            onClick();
        });
    }
    item.appendChild(inner);
    return item;
}

function cmd(name, args) {
    try {
        if (model.model && model.model[name]) model.model[name](args || {});
    } catch (e) { err("command " + name + " failed: " + e); }
}

function buildPopover() {
    // Root copies the native MenuList: content + border overlay + bottom notch.
    const pop = el("div", "z4ae-popover" + (btnSize() === "small" ? "" : " z4ae-large"));
    pop.id = POPOVER_ID;
    pop.addEventListener("click", function (e) { e.stopPropagation(); });

    const content = el("div", "z4ae-list-content");

    // header: vehicle name, dimmed, uppercase
    const header = el("div", "z4ae-header");
    const headText = ui("title", "Equipment-Sets") + (gData.vehicleName ? " — " + gData.vehicleName : "");
    header.textContent = headText.toUpperCase();
    content.appendChild(header);

    // saved sets
    content.appendChild(buildSetBlock(ui("set1", "Set 1"), gData.saved1, ui("notSaved", "Noch nichts gespeichert")));
    if (gData.hasSetup2) {
        content.appendChild(buildSetBlock(ui("set2", "Set 2"), gData.saved2, ui("notSaved", "Noch nichts gespeichert")));
    } else {
        content.appendChild(buildSetBlock(ui("set2", "Set 2"), null, ui("noSetup2", "Kein zweites Loadout verfügbar")));
    }

    content.appendChild(el("div", "z4ae-divider"));

    // auto-install toggle row (menu row + switch on the right)
    const toggleRow = buildMenuRow(ui("autoLabel", "Automatisch einbauen"), "auto", function () {
        cmd("onToggleEnabled");
    });
    const toggle = el("div", "z4ae-switch" + (gData.enabled ? " z4ae-switch-on" : ""));
    toggle.appendChild(el("div", "z4ae-switch-knob"));
    toggleRow.firstChild.appendChild(toggle);   // firstChild = inner
    content.appendChild(toggleRow);

    // actions as menu rows
    content.appendChild(buildMenuRow(ui("save1", "Set 1 speichern"), "save", function () {
        cmd("onSaveSet", { which: 1 });
    }));
    if (gData.hasSetup2) {
        content.appendChild(buildMenuRow(ui("save2", "Set 2 speichern"), "save", function () {
            cmd("onSaveSet", { which: 2 });
        }));
        content.appendChild(buildMenuRow(ui("saveBoth", "Beide Sets speichern"), "save", function () {
            cmd("onSaveSet", { which: 3 });
        }));
    }
    if (gData.busy) {
        content.appendChild(buildMenuRow(ui("busy", "Einbau läuft…"), "apply", null, true));
    } else {
        content.appendChild(buildMenuRow(ui("applyNow", "Jetzt einbauen"), "apply", function () {
            cmd("onApplyNow");
        }));
    }

    pop.appendChild(content);

    // border overlay + bottom notch, like the native MenuList
    pop.appendChild(el("div", "z4ae-list-border"));
    const bottom = el("div", "z4ae-list-bottom");
    const left = el("div", "z4ae-bottom-border");
    bg(left, "img://gui/maps/icons/hangar/vehicleMenu/menu_bottom_left_default.png");
    const notch = el("div", "z4ae-notch");
    const right = el("div", "z4ae-bottom-border");
    bg(right, "img://gui/maps/icons/hangar/vehicleMenu/menu_bottom_right_default.png");
    bottom.appendChild(left);
    bottom.appendChild(notch);
    bottom.appendChild(right);
    pop.appendChild(bottom);

    return pop;
}

function removePopover() {
    const p = document.getElementById(POPOVER_ID);
    if (p && p.parentNode) p.parentNode.removeChild(p);
}

function renderPopover() {
    removePopover();
    if (!gPopoverOpen) return;
    if (!gButton || !document.body.contains(gButton)) { setPopoverOpen(false); return; }
    // Child of our button: anchored above it (bottom: 60/70rem, like the native
    // VehicleMenuWidget_menu) and it rides along when the button slides up.
    gButton.appendChild(buildPopover());
}

// Open/close like the native MenuButton: the button slides up (translateY) and
// switches to the "opened" background while its menu is shown.
function setPopoverOpen(open) {
    gPopoverOpen = open;
    if (gButton && gButton.classList) {
        gButton.classList.toggle("z4ae-menu-btn-opened", open);
    }
    applyButtonBackground();
    renderPopover();
}

function togglePopover() {
    setPopoverOpen(!gPopoverOpen);
}

// close when clicking anywhere else (clicks on our button toggle instead, and
// clicks inside the popover never bubble this far)
document.addEventListener("click", function (e) {
    if (!gPopoverOpen) return;
    if (gButton && (e.target === gButton || gButton.contains(e.target))) return;
    setPopoverOpen(false);
});

// --------------------------------------------------------------------------
function onModelUpdate() {
    const m = model.model;
    if (!m) return;
    gUi = parseModelJson("uiJson", gUi);
    const dataJson = m.dataJson || "";
    if (dataJson !== gLastDataJson) {
        gLastDataJson = dataJson;
        gData = parseModelJson("dataJson", {});
        if (gPopoverOpen) renderPopover();
    }
}

let gNativeMenuWasOpen = false;

function startObserver() {
    try {
        const obs = new MutationObserver(function () {
            const menu = document.querySelector(SEL.menuWidget);
            if (menu && !menu.querySelector("[" + OURS_ATTR + "]")) injectButton();
            // Edge-detect a native menu OPENING (closed -> open). A plain
            // presence check would misfire when ours opens while the previous
            // native menu is still being torn down by React.
            const nativeOpen = !!document.querySelector(SEL.nativeMenu);
            const nativeJustOpened = nativeOpen && !gNativeMenuWasOpen;
            gNativeMenuWasOpen = nativeOpen;
            if (!menu && gPopoverOpen) {
                // left the hangar route — drop the popover
                gPopoverOpen = false;
                removePopover();
            } else if (menu && gPopoverOpen && nativeJustOpened) {
                // a native menu just opened (hotkey etc.) — yield to it
                setPopoverOpen(false);
            } else if (menu && gPopoverOpen && !document.getElementById(POPOVER_ID)) {
                // a re-render recreated the button and took the popover with it
                setPopoverOpen(true);
            }
        });
        obs.observe(document.body, { childList: true, subtree: true });
    } catch (e) { err("observer attach failed: " + e); }
}

// --------------------------------------------------------------------------
engine.whenReady.then(function () {
    log("engine.whenReady — JS starting");
    try {
        model.onUpdate(onModelUpdate);
        model.subscribe();
    } catch (e) { err("model subscribe failed: " + e); }

    try {
        media.onUpdate(function () { applyButtonBackground(); });
        media.subscribe();
    } catch (e) { err("media subscribe failed: " + e); }

    if (model.model) {
        gUi = parseModelJson("uiJson", {});
        gData = parseModelJson("dataJson", {});
        gLastDataJson = model.model.dataJson || null;
    }

    injectButton();
    startObserver();
});
