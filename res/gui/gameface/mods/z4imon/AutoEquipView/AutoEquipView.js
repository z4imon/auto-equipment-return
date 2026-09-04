// Auto Equipment Return — menu button + popover injected into the hangar.
// Follows the Tank-Stats pattern: own DOM appended outside React's #root,
// self-built button (never clone native MenuButtons), rem sizing only.

import { ModelObserver } from "../../libs/model.js";
import { MediaContext } from "../../libs/media.js";
import { playSound } from "../../libs/sound.js";

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
    // The native MenuButton whose popup is currently open.
    openedMenuButton: '[class*="MenuButton_base__opened"]',
};

const OURS_ATTR  = "data-z4ae-button";
const POPOVER_ID = "z4ae-popover";

const DEVICE_ICON = (name) => "img://gui/maps/icons/artefact/" + name + ".png";
// Trophy/deluxe marker art, stretched over the icon at 100% like the native
// hangar loadout slots do (s48x48 = the set used for small slot sizes).
const OVERLAY_ICON = (name) => "img://gui/maps/icons/components/loadout_item/overlays/s48x48/" + name + ".png";

let gButton = null;
let gBtnHovered = false;
let gPopoverOpen = false;
let gData = {};
let gUi = {};
let gLastDataJson = null;
let gPreviewData = null;      // parsed previewJson, or null until a response arrives
let gLastPreviewJson = null;
let gStreamerListOpen = false;
let gStreamerList = null;         // parsed streamerListJson, or null before the first open
let gLastStreamerListJson = null;
let gStreamerIconDataUri = "";
let gLastStreamerIconDataUri = null;
let gVisibleTooltip = null;   // the one check-row tooltip element currently shown, or null

// Explicit JS state instead of pure CSS :hover: a tooltip sits directly above
// its own row (bottom:100%), so it visually overlaps the row above it, and
// relying on :hover ending "naturally" as the cursor crosses that overlap
// left the old tooltip hanging on screen for a moment before the row above's
// own :hover took over. Forcing the previous tooltip closed the instant a new
// row is entered - rather than waiting for its own mouseleave - makes the
// switch immediate no matter how that overlap is hit-tested.
function _showTooltip(tip) {
    if (gVisibleTooltip && gVisibleTooltip !== tip) {
        gVisibleTooltip.classList.remove("z4ae-check-tooltip-visible");
    }
    tip.classList.add("z4ae-check-tooltip-visible");
    gVisibleTooltip = tip;
}
function _hideTooltip(tip) {
    tip.classList.remove("z4ae-check-tooltip-visible");
    if (gVisibleTooltip === tip) {
        gVisibleTooltip = null;
    }
}

// The rec-preview popup used pure CSS :hover for its display:none/block switch
// (like the checkbox tooltips originally did) - but unlike a plain hover
// popup, its visibility needs to be measured (getBoundingClientRect, see
// _positionRecPreview) in the SAME handler that reveals it, to decide whether
// to flip it downward. Gameface's :hover pseudo-class match doesn't reliably
// land before the mouseenter listener runs, so measuring right after showing
// it via :hover could still see the old display:none box (rect all zero,
// top===0, never < 0 -> flip never triggers - the 1080p bug). Showing it via
// an explicit JS class instead removes that race: the class is applied
// synchronously before we ever call getBoundingClientRect.
function _showRecPreview(preview) {
    preview.classList.add("z4ae-rec-popup-visible");
}
function _hideRecPreview(preview) {
    preview.classList.remove("z4ae-rec-popup-visible");
}

// --------------------------------------------------------------------------
function el(tag, cls) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    return e;
}
function bg(node, url) { node.style.backgroundImage = "url(" + url + ")"; }

// --------------------------------------------------------------------------
// Sounds
// --------------------------------------------------------------------------
// Names read out of the client, not guessed. The React UI resolves a sound in
// two steps: a default scheme (soundConfig) that the hangar view overrides for
// single targets (soundsOverrides):
//   default          click -> "play",  mouse-enter -> "highlight"
//   vehicle-menu-widget:button   click -> "yes1", expand -> "gui_vehicle_menu_open"
// "expand" exists ONLY as that override and only on the closed -> open edge;
// there is no counterpart on closing, so closing is just the click sound.
const SND_HOVER = "highlight";
const SND_CLICK = "play";
const SND_MENU_BTN = "yes1";
const SND_MENU_OPEN = "gui_vehicle_menu_open";

function snd(name) {
    try { playSound(name); } catch (e) { warn("playSound(" + name + ") failed: " + e); }
}

// Hover + click sounds for one element, the way the native ui-kit wires them
// (HeadlessButton, HeadlessCheckbox, the vehicle-menu MenuItem all do exactly
// this). `enabled` false stays silent — a disabled native control plays
// nothing, not even on hover.
function addSounds(node, enabled, clickSound) {
    if (!enabled) return node;
    node.addEventListener("mouseenter", function () { snd(SND_HOVER); });
    node.addEventListener("click", function () { snd(clickSound || SND_CLICK); });
    return node;
}

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

const BUTTON_ICON = "img://gui/maps/icons/z4imon/AutoEquipmentIcon.png";
const BIN_ICON = "img://gui/maps/icons/z4imon/bin.png";

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
    applyButtonArrow();
}

// Small chevron the native MenuButton shows above every expandable button
// (crew/vehicle/customization) — the "click to open" affordance. Hidden once
// the popover is open, exactly like the native one hides it once its menu
// is open (arrow_enabled is the only state our button ever needs; the
// warning/critical variants exist for native menu items only).
function applyButtonArrow() {
    if (!gButton) return;
    const arrowEl = gButton.querySelector(".z4ae-menu-btn-arrow");
    if (!arrowEl) return;
    if (gPopoverOpen) {
        arrowEl.style.display = "none";
        return;
    }
    arrowEl.style.display = "";
    bg(arrowEl, "img://gui/maps/icons/hangar/vehicleMenu/" + btnSize() + "/arrow_enabled.png");
}

function setButtonHover(hovered) {
    if (gBtnHovered === hovered) return;
    gBtnHovered = hovered;
    applyButtonBackground();
}

// Close any open native vehicle-menu popup by re-clicking its opened toggle
// button — the native MenuButton onClick calls close() when already open.
// Returns whether a click was actually handed to a native button: that button
// plays the menu click sound itself, so ours must stay quiet then.
function closeNativeMenus() {
    const opened = document.querySelectorAll(SEL.openedMenuButton);
    let clicked = false;
    for (let i = 0; i < opened.length; i++) {
        try {
            opened[i].dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
            clicked = true;
        } catch (e) {
            try {
                const ev = document.createEvent("MouseEvents");
                ev.initEvent("click", true, true);
                opened[i].dispatchEvent(ev);
                clicked = true;
            } catch (e2) { warn("closeNativeMenus failed: " + e2); }
        }
    }
    return clicked;
}

function injectButton() {
    const menu = document.querySelector(SEL.menuWidget);
    if (!menu) return false;
    if (menu.querySelector("[" + OURS_ATTR + "]")) return true;

    const btn = el("div", "z4ae-menu-btn");
    btn.setAttribute(OURS_ATTR, "1");
    btn.appendChild(el("div", "z4ae-menu-btn-bg"));
    // like the Tank-Stats button: the icon div IS the full button area,
    // the image drawn on it directly (56rem, media-scaled via CSS)
    const iconWrap = el("div", "z4ae-menu-btn-icon");
    bg(iconWrap, BUTTON_ICON);
    btn.appendChild(iconWrap);
    btn.appendChild(el("div", "z4ae-menu-btn-arrow"));

    btn.addEventListener("click", function () {
        // The app's outside-click handler will not close an open native menu
        // for us: it sits on the widget container and our button lives INSIDE
        // that container. Close native menus first, while gPopoverOpen is
        // still false — the synthetic click bubbles through our document
        // listener too, which must not close the popover we open right after.
        const closedNative = gPopoverOpen ? false : closeNativeMenus();
        // Natively only ever ONE click sound is heard: the old menu closes
        // silently through the outside-click handler. We close it by faking a
        // click on its button instead, and that button plays the sound — so
        // adding ours on top would double it.
        if (!closedNative) snd(SND_MENU_BTN);
        togglePopover();
    });
    btn.addEventListener("mouseenter", function () {
        snd(SND_HOVER);
        setButtonHover(true);
    });
    btn.addEventListener("mouseleave", function () { setButtonHover(false); });

    // Native buttons render left-to-right as crew, vehicle, customization
    // (each tagged data-test-id="<type>"). Slot ours in right before
    // customization, i.e. between vehicle and customization, instead of
    // appending it after everything.
    const customizationBtn = menu.querySelector('[data-test-id="customization"]');
    if (customizationBtn && customizationBtn.parentNode) {
        customizationBtn.parentNode.insertBefore(btn, customizationBtn);
    } else {
        menu.appendChild(btn);
    }
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
    // star = the "Primary" vehicle marker
    star: [
        "M10 1.8 L12.47 6.79 L17.98 7.59 L14 11.47 L14.94 16.96 L10 14.36 L5.06 16.96 L6 11.47 L2.02 7.59 L7.53 6.79 Z",
    ],
    // small filled chevron pointing down = "more options below"
    chevronDown: [
        "M4 7 L10 14 L16 7 Z",
    ],
};

const XLINKNS = "http://www.w3.org/1999/xlink";

// A downloaded streamer icon (data: URI) as an inline SVG <image>, sized and
// classed exactly like rowIconSvg's glyphs so it drops into the same corner-
// icon slot the star uses. This engine's CSS does not paint
// `background-image: url(data:...)` at all (confirmed live: the exact same,
// correctly-sized data URI reaches the DOM every time - logged and verified -
// yet nothing ever renders via `node.style.backgroundImage`) - but the native
// client's own hangar bundle embeds data: URIs exactly this way
// (`<image xlink:href="data:image/png;base64,...">` inside an inline <svg>,
// confirmed by inspecting its shipped bundle), so this mirrors that proven
// pattern instead of CSS background-image.
function buildImageIcon(dataUri) {
    const svg = document.createElementNS(SVGNS, "svg");
    svg.setAttribute("viewBox", "0 0 20 20");
    svg.setAttribute("width", "20");
    svg.setAttribute("height", "20");
    svg.setAttribute("class", "z4ae-row-svg");
    const image = document.createElementNS(SVGNS, "image");
    image.setAttributeNS(XLINKNS, "href", dataUri);
    image.setAttribute("x", "0");
    image.setAttribute("y", "0");
    image.setAttribute("width", "20");
    image.setAttribute("height", "20");
    image.setAttribute("preserveAspectRatio", "xMidYMid slice");
    svg.appendChild(image);
    return svg;
}

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
            if (slot.overlay) {
                const ov = el("div", "z4ae-slot-overlay");
                bg(ov, OVERLAY_ICON(slot.overlay));
                s.appendChild(ov);
            }
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

// Bin in the bottom-right corner of the saved-sets area: forgets what is
// stored for the vehicle currently in the hangar. No confirm dialog — nothing
// here is expensive to redo, the two SAVE rows sit right below it.
// Dimmed and inert while nothing is saved: the block next to it already says
// so, and a bin that would do nothing must not look clickable.
function buildDeleteButton() {
    const hasSaved = !!(gData.saved1 || gData.saved2);
    const btn = el("div", "z4ae-corner-btn z4ae-sets-delete"
                        + (hasSaved ? "" : " z4ae-corner-btn-disabled"));
    const icon = el("div", "z4ae-corner-icon z4ae-sets-delete-icon");
    bg(icon, BIN_ICON);
    btn.appendChild(icon);
    if (hasSaved) {
        btn.addEventListener("click", function (e) {
            e.stopPropagation();
            cmd("onDeleteSets");
        });
    }
    addSounds(btn, hasSaved);
    return btn;
}

// The corner button in the saved-sets area: applies the currently selected
// source (WoT Plus recommendation, or a chosen streamer's set - see the
// picker Task 6 adds) as this vehicle's sets and installs it. Hovering asks
// Python for a fresh preview every time (onRequestPreview) rather than
// showing something precomputed - a network round trip only for the
// streamer source, effectively instant for WoT Plus.
// Dimmed only by hasSaved: applying would overwrite a set the player already
// saved, and a saved set is the player's own decision, not something a
// single mis-click should undo. Neither source's own availability is known
// in advance any more - "nothing available for this tank" is something the
// hover preview itself says now, the same way it always said "not available"
// for a single missing set.
function buildRecommendButton() {
    const hasSaved = !!(gData.saved1 || gData.saved2);
    const btn = el("div", "z4ae-corner-btn z4ae-sets-recommend"
                        + (hasSaved ? " z4ae-corner-btn-inactive" : ""));
    const icon = el("div", "z4ae-corner-icon z4ae-sets-recommend-icon");
    if (gData.selectedStreamer != null && gStreamerIconDataUri) {
        log("streamer icon APPLIED: selectedStreamer=" + gData.selectedStreamer + " dataUriLen=" + gStreamerIconDataUri.length);
        icon.appendChild(buildImageIcon(gStreamerIconDataUri));
    } else {
        if (gData.selectedStreamer != null) {
            log("streamer icon NOT applied (falling back to star): selectedStreamer=" + gData.selectedStreamer + " dataUriLen=" + (gStreamerIconDataUri ? gStreamerIconDataUri.length : 0));
        }
        icon.appendChild(rowIconSvg("star"));
    }
    btn.appendChild(icon);
    btn.appendChild(buildRecommendPreview(hasSaved));
    if (!hasSaved) {
        btn.addEventListener("click", function (e) {
            e.stopPropagation();
            cmd("onSaveRecommended");
        });
    }
    // Unconditional: previewing why nothing can be applied is exactly what
    // a dimmed button's hover is for.
    btn.addEventListener("mouseenter", function () {
        cmd("onRequestPreview");
        gRecPreviewHovering = true;
        _positionRecPreview(btn);
    });
    btn.addEventListener("mouseleave", function () {
        gRecPreviewHovering = false;
        const preview = btn.querySelector(".z4ae-rec-popup");
        if (preview) _hideRecPreview(preview);
    });
    addSounds(btn, !hasSaved);
    return btn;
}

// Opens upward (z4ae-rec-popup's default) unless that would push it above
// the window - at 1080p and below the popover sits high enough in the
// hangar that the preview's own height, added on top, clears the top edge.
// Re-run from renderPopover() too, not just on hover: onRequestPreview's
// response can arrive after the popup is already showing (still "Loading…"
// on the first measurement) and rebuilds it taller, which needs the same
// check redone against the real content - see gRecPreviewHovering.
//
// Shows the popup itself via _showRecPreview (a JS class, not :hover) before
// measuring: measuring at the same instant :hover would first apply raced
// with Gameface's hover-state update and consistently read the pre-hover
// display:none box (top===0, so the < 0 check never flipped it) - the
// closed-source 1080p bug. Showing it explicitly first removes that race.
let gRecPreviewHovering = false;

function _positionRecPreview(btn) {
    const preview = btn.querySelector(".z4ae-rec-popup");
    if (!preview) return;
    _showRecPreview(preview);
    preview.classList.remove("z4ae-rec-popup-below");
    const rect = preview.getBoundingClientRect();
    // TEMP DIAGNOSTIC - remove once the 1080p clipping bug is confirmed fixed.
    log("_positionRecPreview: rect.top=" + rect.top + " rect.bottom=" + rect.bottom
        + " rect.height=" + rect.height + " innerHeight=" + window.innerHeight
        + " display=" + getComputedStyle(preview).display
        + " classes=" + preview.className);
    if (rect.top < 0) {
        preview.classList.add("z4ae-rec-popup-below");
    }
    log("_positionRecPreview: after-check classes=" + preview.className);
}

// The hover preview. Pure CSS visibility (:hover on the button); its CONTENT
// now comes from gPreviewData, filled in asynchronously by onRequestPreview -
// so it renders a loading state until the first response for the current
// vehicle/source arrives.
function buildRecommendPreview(hasSaved) {
    const pop = el("div", "z4ae-rec-popup");
    if (!gPreviewData || !gPreviewData.kind) {
        const loading = el("div", "z4ae-rec-head");
        loading.textContent = String(ui("recLoading", "Loading…")).toUpperCase();
        pop.appendChild(loading);
        return pop;
    }
    const kind = gPreviewData.kind;
    const head = el("div", "z4ae-rec-head");
    head.textContent = String(kind === "streamer"
        ? (gData.selectedStreamerName
            ? gData.selectedStreamerName + " " + ui("streamerSetupSuffix", "Setup")
            : ui("streamerRecTitle", "Streamer Equipment"))
        : ui("recTitle", "Empfohlenes Equipment")).toUpperCase();
    pop.appendChild(head);

    const blocks = kind === "streamer"
        ? [{ slots: gPreviewData.slots1, percent: 0 }, { slots: gPreviewData.slots2, percent: 0 }]
        : (gPreviewData.entries || []);

    blocks.forEach(function (entry, index) {
        const block = el("div", "z4ae-rec-block");
        const lab = el("div", "z4ae-set-label");
        // The entries arrive in set order, so entry N fills set N - naming
        // them after the sets is what tells the player what the click
        // overwrites.
        let text = String(ui(index === 0 ? "set1" : "set2", "Set " + (index + 1))).toUpperCase();
        if (entry.percent) text += "  " + entry.percent + " %";
        lab.textContent = text;
        block.appendChild(lab);
        if (entry.slots && entry.slots.length) {
            block.appendChild(buildSlotRow(entry.slots));
        } else {
            // No usable set for this slot - say which one, "not available"
            // on its own would leave the player guessing whether the other
            // one still works. Same wording for both sources - the player's
            // next step is identical either way (nothing to do).
            const miss = el("div", "z4ae-set-missing");
            miss.textContent = ui(index === 0 ? "recUnavailable1" : "recUnavailable2",
                                  index === 0 ? "Erste Empfehlung nicht verfügbar"
                                              : "Zweite Empfehlung nicht verfügbar");
            block.appendChild(miss);
        }
        pop.appendChild(block);
    });
    if (hasSaved) {
        // The button is greyed out for a reason the sets above cannot show:
        // there is already something saved, and applying would overwrite it.
        const note = el("div", "z4ae-rec-note");
        note.textContent = ui("recAlreadySaved",
                              "Gespeichertes Equipment zuerst löschen, um dies zu übernehmen");
        pop.appendChild(note);
    }
    return pop;
}

// Small trigger next to the recommend button: opens the streamer picker.
// Always visible - unlike the recommend button, its own content is fetched
// fresh on every open (onOpenStreamerList), so there is nothing to
// pre-check for emptiness.
function buildStreamerTrigger() {
    const btn = el("div", "z4ae-corner-btn z4ae-streamer-trigger");
    const icon = el("div", "z4ae-corner-icon z4ae-streamer-trigger-icon");
    icon.appendChild(rowIconSvg("chevronDown"));
    btn.appendChild(icon);
    btn.addEventListener("click", function (e) {
        e.stopPropagation();
        toggleStreamerList();
    });
    if (gStreamerListOpen) btn.appendChild(buildStreamerList());
    addSounds(btn, true);
    return btn;
}

function buildStreamerList() {
    log("buildStreamerList: gStreamerList=" + JSON.stringify(gStreamerList));
    const list = el("div", "z4ae-streamer-list");
    list.addEventListener("click", function (e) { e.stopPropagation(); });
    const head = el("div", "z4ae-streamer-list-head");
    head.textContent = String(ui("streamerListTitle", "Streamers")).toUpperCase();
    list.appendChild(head);
    // Always first: switches back to the WoT Plus recommendation.
    list.appendChild(buildStreamerRow(ui("streamerListReset", "WoT Plus Recommendation"), null));
    (gStreamerList || []).forEach(function (streamer) {
        list.appendChild(buildStreamerRow(streamer.streamerName, streamer.accountId));
    });
    return list;
}

function buildStreamerRow(label, accountId) {
    const row = el("div", "z4ae-streamer-row");
    row.textContent = label;
    row.addEventListener("click", function (e) {
        e.stopPropagation();
        cmd("onSelectStreamer", { accountId: accountId, streamerName: accountId === null ? null : label });
        setStreamerListOpen(false);
    });
    addSounds(row, true);
    return row;
}

function setStreamerListOpen(open) {
    gStreamerListOpen = open;
    if (open) {
        // Force a fresh fetch every time the list opens - the catalog is
        // intentionally never session-cached. gLastStreamerListJson must
        // reset so a repeat open that happens to get back byte-identical
        // content (nothing changed server-side) still counts as "new" and
        // re-renders once it arrives, rather than being silently skipped as
        // "no change" against what an earlier open already saw.
        //
        // gStreamerList itself is deliberately NOT reset to null here
        // anymore: the round trip takes a few hundred ms, and blanking the
        // list for that whole window (with no loading indicator, by
        // request) made a quick reopen look permanently broken - every
        // fresh open wiped the previous, still-valid answer before the new
        // one had a chance to land. Now the dropdown just keeps showing
        // whatever it last knew until the fresh fetch quietly replaces it.
        gLastStreamerListJson = null;
        cmd("onOpenStreamerList");
    }
    if (gPopoverOpen) renderPopover();
}

function toggleStreamerList() {
    setStreamerListOpen(!gStreamerListOpen);
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
    addSounds(inner, !disabled && !!onClick);
    item.appendChild(inner);
    return item;
}

// Native ui-kit checkbox: layered box (background texture, border image,
// hover overlay) + the check icon, which is slightly larger than the box.
function buildCheckboxRow(label, checked, onClick, tooltip) {
    const row = el("div", "z4ae-check-row" + (checked ? " z4ae-check-row-checked" : ""));
    const check = el("div", "z4ae-check");
    check.appendChild(el("div", "z4ae-check-fill z4ae-check-bg"));
    check.appendChild(el("div", "z4ae-check-fill z4ae-check-border"));
    check.appendChild(el("div", "z4ae-check-fill z4ae-check-overlay"));
    const tick = el("div", "z4ae-check-icon");
    bg(tick, "img://gui/maps/icons/ui_kit/checkbox/icon_check.png");
    check.appendChild(tick);
    row.appendChild(check);
    const lab = el("div", "z4ae-check-label");
    lab.textContent = label;
    row.appendChild(lab);
    // Gameface is CEF in off-screen-rendering mode - there is no OS window to
    // draw a native title-attribute tooltip into, so it never renders here.
    // Same custom hover-popup technique buildRecommendPreview already uses
    // (pure CSS :hover visibility on an absolutely positioned child).
    if (tooltip) {
        const tip = el("div", "z4ae-check-tooltip");
        tip.textContent = tooltip;
        row.appendChild(tip);
        row.addEventListener("mouseenter", function () { _showTooltip(tip); });
        row.addEventListener("mouseleave", function () { _hideTooltip(tip); });
    }
    row.addEventListener("click", function (e) {
        e.stopPropagation();
        onClick();
    });
    addSounds(row, true);
    return row;
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

    // saved sets — wrapped so the bin can be anchored to the block's corner
    const sets = el("div", "z4ae-sets");
    sets.appendChild(buildSetBlock(ui("set1", "Set 1"), gData.saved1, ui("notSaved", "Noch nichts gespeichert")));
    if (gData.hasSetup2) {
        sets.appendChild(buildSetBlock(ui("set2", "Set 2"), gData.saved2, ui("notSaved", "Noch nichts gespeichert")));
    } else {
        sets.appendChild(buildSetBlock(ui("set2", "Set 2"), null, ui("noSetup2", "Kein zweites Loadout verfügbar")));
    }
    sets.appendChild(buildRecommendButton());
    sets.appendChild(buildStreamerTrigger());
    sets.appendChild(buildDeleteButton());
    content.appendChild(sets);

    content.appendChild(el("div", "z4ae-divider"));

    // auto-install toggle — native ui-kit checkbox row, same style as the
    // crew menu's "auto return" checkbox (not uppercased there either)
    content.appendChild(buildCheckboxRow(ui("autoLabel", "Automatisch einbauen"), !!gData.enabled, function () {
        cmd("onToggleEnabled");
    }, ui("autoTooltip", "Installiert das gespeicherte Equipment automatisch, sobald dieses Fahrzeug ausgewählt wird")));
    // downgrade: fall back to the standard variant when a trophy device
    // cannot be sourced for free
    content.appendChild(buildCheckboxRow(ui("downgradeLabel", "Enable Downgrade"), !!gData.downgrade, function () {
        cmd("onToggleDowngrade");
    }, ui("downgradeTooltip", "Weicht auf die Standard-Ausrüstung aus, falls ein Trophäen-Gerät nicht kostenlos verfügbar ist")));
    // always end on set 1: also stops donors being switched back, which is the
    // one server call the game reliably rate-limits
    content.appendChild(buildCheckboxRow(ui("alwaysSetup1Label", "Always select setup 1"), !!gData.alwaysSetup1, function () {
        cmd("onToggleAlwaysSetup1");
    }, ui("alwaysSetup1Tooltip", "Wählt nach dem Einbauen immer Setup 1 aus, statt beim zuletzt genutzten Setup zu bleiben")));

    // actions as menu rows - the manual Save buttons are redundant (and
    // would fight with it) once "confirmEquipment" (set in the mod's
    // ModsSettingsAPI panel, not here) auto-saves every change from the
    // native setup screen.
    if (gData.equipmentSaveMode !== "confirmEquipment") {
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
    }
    if (gData.busy) {
        content.appendChild(buildMenuRow(ui("busy", "Einbau läuft…"), "apply", null, true));
    } else {
        content.appendChild(buildMenuRow(ui("equipPrimary", "Alle Primärpanzer ausstatten"), "star", function () {
            cmd("onEquipPrimary");
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
    if (gRecPreviewHovering) {
        const recBtn = gButton.querySelector(".z4ae-sets-recommend");
        if (recBtn) _positionRecPreview(recBtn);
    }
}

// Open/close like the native MenuButton: the button slides up (translateY) and
// switches to the "opened" background while its menu is shown.
function setPopoverOpen(open) {
    const wasOpen = gPopoverOpen;
    gPopoverOpen = open;
    if (!open) {
        gStreamerListOpen = false;
    }
    if (gButton && gButton.classList) {
        gButton.classList.toggle("z4ae-menu-btn-opened", open);
    }
    applyButtonBackground();
    renderPopover();
    // The native button plays this from an effect on the closed -> open edge,
    // i.e. after the menu is on screen and only when it really opened —
    // renderPopover() bailing out must not leave a sound behind.
    if (open && !wasOpen && gPopoverOpen) snd(SND_MENU_OPEN);
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
        gPreviewData = null;
        gLastPreviewJson = null;
        if (gPopoverOpen) renderPopover();
    }
    const previewJson = m.previewJson || "";
    if (previewJson !== gLastPreviewJson) {
        gLastPreviewJson = previewJson;
        gPreviewData = previewJson ? parseModelJson("previewJson", null) : null;
        if (gPopoverOpen) renderPopover();
    }
    const streamerListJson = m.streamerListJson || "";
    if (streamerListJson !== gLastStreamerListJson) {
        gLastStreamerListJson = streamerListJson;
        gStreamerList = parseModelJson("streamerListJson", []);
        log("onModelUpdate: streamerListJson changed, parsed=" + JSON.stringify(gStreamerList) + " gPopoverOpen=" + gPopoverOpen);
        if (gPopoverOpen) renderPopover();
    }
    const streamerIconDataUri = m.streamerIconDataUri || "";
    if (streamerIconDataUri !== gLastStreamerIconDataUri) {
        gLastStreamerIconDataUri = streamerIconDataUri;
        gStreamerIconDataUri = streamerIconDataUri;
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
        gLastPreviewJson = model.model.previewJson || null;
    }

    injectButton();
    startObserver();
});
