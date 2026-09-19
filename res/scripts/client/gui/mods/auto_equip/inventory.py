# -*- coding: utf-8 -*-
"""Every read against the client's items cache lives here: vehicles, optional
devices, setup layouts, donor lookups. Nothing in this module talks to the
server - it only observes. The one exception is fill_in_missing_vehicle_cds(),
which writes the ids it reads straight back into the config.

Gui items are recreated on every cache sync, so nothing here is ever cached:
each helper re-fetches what it needs.
"""

import sys
import time

from gui.shared.gui_items import GUI_ITEM_TYPE
from gui.shared.utils.requesters import REQ_CRITERIA
from helpers import dependency
from post_progression_common import TankSetupGroupsId
from skeletons.gui.game_control import IWotPlusController
from skeletons.gui.shared import IItemsCache

from . import config, hangar
from .log import LOG

OPT_DEVICE_GROUP = TankSetupGroupsId.OPTIONAL_DEVICES_AND_BOOSTERS


def _items_cache():
    return dependency.instance(IItemsCache)


def _wot_plus():
    return dependency.instance(IWotPlusController)


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

def has_wot_plus():
    try:
        return _wot_plus().hasSubscription()
    except Exception:
        LOG.exc('has_wot_plus check failed')
        return False


def is_free_to_demount(item):
    """True only if demounting this device is guaranteed to cost nothing.
    Removable devices (binoculars & co.) always demount for free; everything
    else only under the realm's free-demount rules. On WG that covers regular,
    trophy and experimental level 1 - but NOT improved, NOT experimental
    level 2/3. The 360 China Plus subscription free-demounts improved (purple)
    as well - see improved_demount_is_free()."""
    try:
        if item.isRemovable:
            return True
        return _wot_plus().isFreeToDemount(item)
    except Exception:
        LOG.exc('is_free_to_demount failed for %s' % getattr(item, 'name', '?'))
        return False


# Cached after the first MEANINGFUL probe - free-demount rules do not change
# mid-session. Nothing else is cached: neither an unready items cache nor an
# unsynced subscription says anything about the realm.
_improved_free_cache = None


def improved_demount_is_free():
    """True when this realm's free-demount rules cover Improved (purple)
    devices.

    WG's WoT Plus does not; the 360 China Plus subscription does (issue #33).
    Probed against a real deluxe item so the game's own IWotPlusController
    stays the source of truth - no hardcoded realm list that could drift with
    a publisher rename.

    Only asked while the subscription is known to be live. isFreeToDemount()
    ends in hasSubscription(), so without it EVERY device answers "not free" -
    an answer about the account, not about the realm. Caching that would pin a
    360 China player to WG rules for the rest of the session, and the hangar
    does call in early: gameface._check_wot_plus() polls for precisely this
    reason, because subscription data is regularly not synced yet when the
    hangar first loads."""
    global _improved_free_cache
    if _improved_free_cache is not None:
        return _improved_free_cache
    if not has_wot_plus():
        return False
    try:
        for item in all_optional_devices().itervalues():
            if getattr(item, 'isDeluxe', False):
                _improved_free_cache = is_free_to_demount(item)
                LOG.info('improved free-demount on this realm: %s'
                         % _improved_free_cache)
                return _improved_free_cache
    except Exception:
        LOG.exc('improved_demount_is_free probe failed')
    return False


# ---------------------------------------------------------------------------
# Vehicles and devices
# ---------------------------------------------------------------------------

def owned_vehicles():
    """Every vehicle in the player's inventory, as a list."""
    try:
        return list(_items_cache().items.getVehicles(REQ_CRITERIA.INVENTORY).itervalues())
    except Exception:
        LOG.exc('owned_vehicles failed')
        return []


def vehicle_by_inv_id(veh_inv_id):
    """Fresh gui item for one owned vehicle, looked up by inventory id (which
    is what the config keys sets by), or None."""
    try:
        vehicles = _items_cache().items.getVehicles(
            REQ_CRITERIA.INVENTORY
            | REQ_CRITERIA.VEHICLE.SPECIFIC_BY_INV_ID([veh_inv_id]))
        for vehicle in vehicles.itervalues():
            return vehicle
        return None
    except Exception:
        LOG.exc('vehicle_by_inv_id(%s) failed' % veh_inv_id)
        return None


def inv_id_for_vehicle_type(veh_cd):
    """This account's own invID for a vehicle TYPE (intCD/compactDescr), or
    None if the account doesn't own that vehicle. An invID only means anything
    within the account that assigned it, so this is how a set saved on a
    DIFFERENT account gets remapped onto this one (see importer.py)."""
    try:
        vehicles = _items_cache().items.getVehicles(
            REQ_CRITERIA.INVENTORY | REQ_CRITERIA.VEHICLE.SPECIFIC_BY_CD([veh_cd]))
        for vehicle in vehicles.itervalues():
            return vehicle.invID
        return None
    except Exception:
        LOG.exc('inv_id_for_vehicle_type(%s) failed' % veh_cd)
        return None


def device_by_cd(int_cd):
    try:
        return _items_cache().items.getItemByCD(int_cd)
    except Exception:
        return None


def all_optional_devices():
    return _items_cache().items.getItems(GUI_ITEM_TYPE.OPTIONALDEVICE,
                                         REQ_CRITERIA.EMPTY)


def devices_by_archetype(vehicle, archetype):
    """Every optional device of one archetype ('rammer', 'stereoscope', ...)
    that fits this vehicle.

    The equipment assistant names devices by archetype rather than by intCD -
    it says "a rammer", not "which rammer" - so this is the join between its
    recommendation and real items. Archetype is the key the client's own
    easy-tank-equip screen joins on; tags are checked as well because the
    assistant's sorting code matches plain devices by tag."""
    try:
        criteria = (REQ_CRITERIA.OPTIONAL_DEVICE.HAS_ANY_BY_ARCHETYPE(archetype)
                    | REQ_CRITERIA.OPTIONAL_DEVICE.IS_COMPATIBLE_WITH_VEHICLE(vehicle))
        found = list(_items_cache().items.getItems(
            GUI_ITEM_TYPE.OPTIONALDEVICE, criteria=criteria).itervalues())
        if found:
            return found
        criteria = (REQ_CRITERIA.OPTIONAL_DEVICE.HAS_ANY_FROM_TAGS({archetype})
                    | REQ_CRITERIA.OPTIONAL_DEVICE.IS_COMPATIBLE_WITH_VEHICLE(vehicle))
        return list(_items_cache().items.getItems(
            GUI_ITEM_TYPE.OPTIONALDEVICE, criteria=criteria).itervalues())
    except Exception:
        LOG.exc('devices_by_archetype(%s) failed' % archetype)
        return []


def is_owned(item):
    """True when the account already has this device somewhere - in the depot
    or mounted on any vehicle. Not a promise that it can be had for free; that
    is apply.py's job."""
    try:
        if item.inventoryCount > 0:
            return True
    except Exception:
        pass
    for vehicle in owned_vehicles():
        try:
            if vehicle.optDevices.setupLayouts.containsIntCD(item.intCD):
                return True
        except Exception:
            continue
    return False


def recommended_presets(vehicle):
    """The equipment assistant's two recommendations for a vehicle, as
    (source, sourceVehicleCD, [VehicleLoadout]) pairs - or an empty tuple.

    This is the WoT Plus "most used devices" data. Two independent gates sit in
    front of it in the client: the subscription, and a prebattle-type check that
    only passes for the random/squad context. Both are the client's own
    business; an empty result simply means "no recommendation to show"."""
    try:
        return _wot_plus().getOptDeviceAssistPresets(vehicle) or ()
    except Exception:
        LOG.exc('recommended_presets failed')
        return ()


def most_popular_loadout(vehicle):
    """The single most-used loadout, read straight from the client's local
    config. Unlike recommended_presets() this path has no prebattle gate, so it
    still answers when the player is in a context the assistant panel hides
    itself in."""
    try:
        return _wot_plus().getMostPopularOptDevicesLoadout(vehicle)
    except Exception:
        LOG.exc('most_popular_loadout failed')
        return None


# ---------------------------------------------------------------------------
# Setup layouts
# ---------------------------------------------------------------------------

def setup_indices(vehicle):
    try:
        return sorted(vehicle.optDevices.setupLayouts.setups.keys())
    except Exception:
        LOG.exc('setup_indices failed')
        return [0]


def has_second_setup(vehicle):
    return 1 in setup_indices(vehicle)


def slot_capacity(vehicle):
    return vehicle.optDevices.installed.getCapacity()


def setup_device_cds(vehicle, setup_idx):
    """Per-slot intCD list of one setup (0 = empty), padded/trimmed to exactly
    the vehicle's slot capacity."""
    capacity = slot_capacity(vehicle)
    cds = list(vehicle.optDevices.setupLayouts.getIntCDs(setupIdx=setup_idx))[:capacity]
    cds += [0] * (capacity - len(cds))
    return [int(cd) if cd else 0 for cd in cds]


def snapshot_setups(vehicle):
    """The vehicle's current setups as {'set1': [...], 'set2': [...] or None}."""
    return {
        'set1': setup_device_cds(vehicle, 0),
        'set2': setup_device_cds(vehicle, 1) if has_second_setup(vehicle) else None,
    }


def active_setup_index(vehicle):
    return vehicle.optDevices.setupLayouts.layoutIndex


def vehicle_has_device(vehicle, device_cd, setup_idx=None):
    layouts = vehicle.optDevices.setupLayouts
    if setup_idx is None:
        return layouts.containsIntCD(device_cd)
    return layouts.containsIntCD(device_cd, setupIdx=setup_idx)


def locate_device_on_vehicle(vehicle, device_cd):
    """(setup index, slot index) of a device on a vehicle, preferring the
    active setup; (None, None) when it isn't mounted at all."""
    active = active_setup_index(vehicle)
    ordered = [active] + [idx for idx in setup_indices(vehicle) if idx != active]
    for setup_idx in ordered:
        cds = setup_device_cds(vehicle, setup_idx)
        if device_cd in cds:
            return setup_idx, cds.index(device_cd)
    return None, None


# ---------------------------------------------------------------------------
# Donor search
#
# Every lookup walks the player's whole vehicle list, so this is where a slow
# run actually spends its time. The counters let one aggregate line be logged
# per run instead of per-vehicle spam.
#
# Timed on _now() rather than time.time(): under Windows the latter resolves to
# about 15.6 ms, so a scan that takes 3 ms reads as either 0 or 15.6 - which is
# how these counters could report 0.000s for real work.
# ---------------------------------------------------------------------------

# time.clock() is the high-resolution wall clock on Windows in Python 2; on
# anything else it measures CPU time, which is the wrong thing, so fall back.
if sys.platform == 'win32':
    _now = time.clock
else:
    _now = time.time

_donor_search_ms = 0.0
_donor_search_count = 0


def reset_donor_search_stats():
    global _donor_search_ms, _donor_search_count
    _donor_search_ms = 0.0
    _donor_search_count = 0


def log_donor_search_stats(context):
    LOG.info('%s: spent %.3fs scanning other vehicles for donors (%d lookup%s)'
             % (context, _donor_search_ms / 1000.0, _donor_search_count,
                '' if _donor_search_count == 1 else 's'))


# Vehicles the game hands out for one mode only. They arrive with equipment
# already fitted that the server refuses to release, so every attempt to move
# it fails - and worse, the mod would be asking to strip a loadout the player
# never chose and cannot rebuild.
#
# Checked as tags on the vehicle (VEHICLE_TAGS.COMP7_BATTLES and friends), which
# is the same test the client itself uses. Onslaught ("Ansturm") is the one that
# actually bit; the others are the same kind of loaner and are listed so the
# next mode's rental does not reopen this bug.
_MODE_ONLY_FLAGS = (
    'isOnlyForComp7Battles',        # Onslaught / Ansturm - the reported case
    'isOnlyForClanWarsBattles',
    'isOnlyForBattleRoyaleBattles',
    'isOnlyForEpicBattles',
    'isOnlyForEventBattles',
    'isOnlyForMapsTrainingBattles',
)


def is_mode_only_vehicle(vehicle):
    """True for a mode-locked loaner whose equipment must not be touched.

    NOT a general "is this vehicle special" test - regular tanks taken into
    Frontline or Onslaught are untagged and stay fully in scope, including for
    the batch run over Primary vehicles."""
    if vehicle is None:
        return False
    for flag in _MODE_ONLY_FLAGS:
        try:
            if getattr(vehicle, flag, False):
                return True
        except Exception:
            continue
    return False


def find_donor_vehicle(device_cd, exclude_inv_id, excluded_inv_ids=None):
    """The cheapest unlocked vehicle (not in battle, queue or a prebattle)
    carrying the device. excluded_inv_ids rules out further vehicles - the batch
    run uses it so Primary vehicles never cannibalise each other's freshly
    installed equipment.

    CHEAPEST, not first: a donor whose ACTIVE setup holds the device costs one
    server call, a donor that keeps it in the other setup costs three, because
    slot indices only ever address the active setup and it has to be switched
    there and back. Measured on the baseline run, those switches were 42 of 80
    change_setup calls and a quarter of the whole run's server time - while only
    16% of borrows actually needed one. With a few hundred vehicles there is
    almost always a candidate that needs none, and looking for it costs client
    time, which the same measurement showed to be 1.8% of the total.

    Mode-locked loaners are never donors: their equipment cannot be released,
    so picking one only produces a failed demount and a skipped install."""
    global _donor_search_ms, _donor_search_count
    started = _now()
    try:
        return _find_donor_vehicle(device_cd, exclude_inv_id, excluded_inv_ids)
    finally:
        _donor_search_ms += (_now() - started) * 1000.0
        _donor_search_count += 1


def _find_donor_vehicle(device_cd, exclude_inv_id, excluded_inv_ids):
    # A donor that needs a setup switch is kept only as a fallback: it is used
    # when the whole list holds nobody cheaper, which is the behaviour this
    # search always had.
    needs_switch = None
    for vehicle in owned_vehicles():
        if vehicle.invID == exclude_inv_id:
            continue
        if excluded_inv_ids and vehicle.invID in excluded_inv_ids:
            continue
        if is_mode_only_vehicle(vehicle):
            continue
        try:
            if vehicle.isLocked or not vehicle_has_device(vehicle, device_cd):
                continue
            if vehicle_has_device(vehicle, device_cd,
                                  setup_idx=active_setup_index(vehicle)):
                return vehicle          # one call instead of three
            if needs_switch is None:
                needs_switch = vehicle
        except Exception:
            continue
    return needs_switch


# ---------------------------------------------------------------------------
# Downgrading special devices
# ---------------------------------------------------------------------------

def standard_variant_of(vehicle, special_item):
    """The plain standard counterpart of a special device that fits this
    vehicle, or None.

    Special and standard variants share the descriptor ARCHETYPE (e.g. both
    'coatedOptics'). groupName looked like the right key at first - trophy
    devices do declare a matching one - but most items leave it unset, where it
    defaults to the item's own unique internal name and never matches anything.
    That is why 'Bounty Rotation Mechanism' never found its standard sibling
    despite plenty being in stock. archetype has no such gap: it is filled in
    for every tier/trophy/deluxe/modernized variant. Should several compatible
    classes pass, the most expensive (best) one wins."""
    try:
        archetype = special_item.descriptor.archetype
        if not archetype:
            return None
        best = None
        best_price = -1
        for item in all_optional_devices().itervalues():
            if not _is_standard_variant(item, archetype, vehicle):
                continue
            price = _credit_price(item)
            if price > best_price:
                best, best_price = item, price
        return best
    except Exception:
        LOG.exc('standard_variant_of failed')
        return None


def _is_standard_variant(item, archetype, vehicle):
    try:
        return item.isRegular and _same_archetype(item, archetype, vehicle)
    except Exception:
        return False


def _same_archetype(item, archetype, vehicle):
    try:
        if item.descriptor.archetype != archetype:
            return False
        fits, _ = item.descriptor.checkCompatibilityWithVehicle(vehicle.descriptor)
        return fits
    except Exception:
        return False


def bounty_upgraded_variant_of(vehicle, special_item):
    """The upgraded (level 2) bounty sibling of a Bond (Improved) device, or
    None.

    Used for equipment PULLED from a streamer's shared set - see gameface.py's
    streamer-set transform - and as the first downgrade fallback of an Improved
    device (downgrade_candidates_of). Same archetype-matching approach as
    plain_bounty_variant_of/standard_variant_of below; only the source
    (isDeluxe) and target (isTrophy+isUpgraded) conditions differ."""
    try:
        archetype = special_item.descriptor.archetype
        if not archetype:
            return None
        for item in all_optional_devices().itervalues():
            if (item.isTrophy and item.isUpgraded
                    and _same_archetype(item, archetype, vehicle)):
                return item
        return None
    except Exception:
        LOG.exc('bounty_upgraded_variant_of failed')
        return None


def plain_bounty_variant_of(vehicle, special_item):
    """The level 1 bounty device of this archetype, for a source that outranks
    it - an UPGRADED bounty or an Improved (Bond) device - or None.

    It is the LAST fallback, not the first - see downgrade_candidates_of().

    The source gate is what keeps this from handing a device back to itself: a
    plain bounty device must never "downgrade" sideways into the very item it
    already is. Improved passes the gate because it sits a full tier above,
    which is why the Improved chain can end here instead of at the standard
    device."""
    try:
        if not ((special_item.isTrophy and special_item.isUpgraded)
                or special_item.isDeluxe):
            return None
        archetype = special_item.descriptor.archetype
        if not archetype:
            return None
        for item in all_optional_devices().itervalues():
            if (item.isTrophy and item.isUpgradable
                    and _same_archetype(item, archetype, vehicle)):
                return item
        return None
    except Exception:
        LOG.exc('plain_bounty_variant_of failed')
        return None


def experimental_level_variant_of(vehicle, special_item, target_level):
    """The Experimental sibling of special_item at exactly target_level, or
    None - e.g. mapping a level 2/3 Experimental device down to level 1.
    Same archetype-matching approach as the other *_variant_of helpers; only
    .level (not tier/trophy/deluxe) distinguishes siblings within the
    Experimental family. Used for streamer-set pulls and as the first
    downgrade step of a level 2/3 device (downgrade_candidates_of)."""
    try:
        archetype = special_item.descriptor.archetype
        if not archetype:
            return None
        for item in all_optional_devices().itervalues():
            if (item.isModernized and getattr(item, 'level', None) == target_level
                    and _same_archetype(item, archetype, vehicle)):
                return item
        return None
    except Exception:
        LOG.exc('experimental_level_variant_of failed')
        return None


def deluxe_variant_of(vehicle, special_item):
    """The Improved (purple) sibling of this archetype, or None. Mirrors
    bounty_upgraded_variant_of; only the target condition (isDeluxe) differs."""
    try:
        archetype = special_item.descriptor.archetype
        if not archetype:
            return None
        for item in all_optional_devices().itervalues():
            if item.isDeluxe and _same_archetype(item, archetype, vehicle):
                return item
        return None
    except Exception:
        LOG.exc('deluxe_variant_of failed')
        return None


def preferred_variant_of(vehicle, item):
    """The device this realm wants to see in item's slot, or None.

        WG         upgraded bounty (red lvl 2)  +  Experimental level 1
        360 China  Improved (purple)            +  Experimental level 1

    This is the TARGET tier - what a saved set should aim at - not what can be
    sourced right now; downgrade_candidates_of() handles the latter.

    Plain bounty (red lvl 1) is never a target on any realm. It is strictly
    worse than the slot-boosted standard device, and dropping it costs no
    coverage: the game gives red lvl 1 and red lvl 2 the exact same seven
    archetypes.

    Experimental devices have their own archetype namespace, so they can only
    resolve inside their own family. Level 1 is the only tier of it that
    demounts for free on any realm, so that is the target and a level 1 device
    is already there.

    None means this archetype has no preferred variant HERE: of the twelve
    classic archetypes five have no bounty device at all, and three have no
    Improved one either. The caller keeps the standard device rather than
    leaving a hole in the loadout."""
    try:
        if getattr(item, 'isModernized', False):
            if getattr(item, 'level', 1) <= 1:
                return item
            return experimental_level_variant_of(vehicle, item, 1)
        if improved_demount_is_free():
            return deluxe_variant_of(vehicle, item)
        return bounty_upgraded_variant_of(vehicle, item)
    except Exception:
        LOG.exc('preferred_variant_of failed')
        return None


def downgrade_candidates_of(vehicle, special_item):
    """What a special device that cannot be sourced may fall back to, STRONGEST
    first:

        Improved (purple)     ->  upgraded bounty -> standard -> plain bounty
        upgraded bounty       ->  standard -> plain bounty
        Experimental lvl 2/3  ->  Experimental level 1
        everything else       ->  standard

    The standard device comes before the level 1 bounty one on purpose. It looks
    like the bigger step down, but a standard device gets the slot's category
    bonus and a level 1 bounty device does not, so the boosted standard device
    is the better of the two in the slot it ends up in.

    An Experimental device can ONLY fall back inside its own family, and level 1
    is the only tier of it that demounts for free on any realm (the client
    hardcodes that in IWotPlusController.isFreeToDemount). There is no standard
    step after it because no standard device can ever match: Experimental
    devices are COMBO devices - one merges two classic devices - so the game
    gives them their own archetype namespace ('modernizedTurbochargerRotation-
    Mechanism' and three siblings, see item_defs/vehicles/common/
    optional_devices/modernized_devices.xml), which does not overlap the
    standard/trophy/deluxe archetypes at all. standard_variant_of() is still
    asked below purely for uniformity; for this family it always answers None.
    A level 1 Experimental device therefore has no fallback whatsoever, and
    apply.py reports it as skipped rather than downgrading it.

    All of these are realm-independent on purpose. Issue #33 surfaced the
    Improved chain on the 360 China server, where Plus free-demounts purple,
    but the ordering is about how strong the device is in the slot, not about
    what a realm demounts for free - so WG gets the same ladder. Whether any of
    these candidates may actually be used is decided per realm, one layer up:
    apply.py takes the first one _is_free_to_obtain() accepts, so a realm that
    cannot source a step simply falls through it."""
    candidates = []
    if getattr(special_item, 'isDeluxe', False):
        candidates.append(bounty_upgraded_variant_of(vehicle, special_item))
    if (getattr(special_item, 'isModernized', False)
            and getattr(special_item, 'level', 1) > 1):
        candidates.append(experimental_level_variant_of(vehicle, special_item, 1))
    candidates.append(standard_variant_of(vehicle, special_item))
    candidates.append(plain_bounty_variant_of(vehicle, special_item))
    return [item for item in candidates if item is not None]


def _credit_price(item):
    try:
        return int(item.buyPrices.itemPrice.price.credits or 0)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Primary ("favourite") vehicles for the batch run
#
# Every mode hangar lists a different set than the random one and remembers the
# player's carousel filter separately, so both halves of the query depend on
# WHICH hangar is open (hangar.py tracks that):
#
#   * the eligibility gate - who may be taken into this mode at all. Each mode
#     answers that itself, so we ask its own controller instead of rebuilding
#     the rules, which would drift with every balance patch.
#   * the carousel filter - the tier/nation/class boxes the player ticked. All
#     mode filters derive from the random one and differ only in which saved
#     sections they read, so picking the right class is the whole job.
#
# Anything unavailable degrades to "no extra restriction" rather than failing:
# too many vehicles is recoverable, none at all is just broken.
# ---------------------------------------------------------------------------

_CAROUSEL_FILTERS = {
    hangar.STANDARD: ('gui.filters.battle_pass_carousel_filter',
                      'BattlePassCarouselFilter'),
    'comp7': ('comp7.gui.Scaleform.daapi.view.lobby.hangar.carousels.carousel_filter',
              'Comp7CarouselFilter'),
    'comp7_light': ('comp7_light.gui.Scaleform.daapi.view.lobby.hangar.carousels'
                    '.carousel_filter', 'Comp7LightCarouselFilter'),
    'frontline': ('gui.filters.epic_battle_carousel_filter',
                  'EpicBattleCarouselFilter'),
    'last_stand': ('last_stand.gui.impl.lobby.vehicles_data_providers.ls_vehicle_filter',
                   'LSBattleCarouselFilter'),
    'fun_random': ('fun_random.gui.filters.fun_random_carousel_filter',
                   'FunRandomCarouselFilter'),
}


def filtered_primary_vehicles():
    """Favourite vehicles of the hangar the player is currently in that pass
    that hangar's carousel filter, best tier first."""
    mode = hangar.active_mode()
    query = REQ_CRITERIA.INVENTORY | REQ_CRITERIA.VEHICLE.FAVORITE
    query |= _eligibility_criteria(REQ_CRITERIA, mode)
    query |= _carousel_filter_criteria(mode)
    try:
        vehicles = _items_cache().items.getVehicles(query)
        targets = sorted(vehicles.itervalues(), key=lambda v: (-v.level, v.userName))
        LOG.info('%d primary vehicle(s) in the %s hangar'
                 % (len(targets), mode or 'standard'))
        return targets
    except Exception:
        LOG.exc('filtered_primary_vehicles failed')
        return []


def selected_playlist():
    """(title, set of vehicle intCDs) of the playlist the player has selected,
    or (None, None) when the feature is off or nothing is selected.

    The playlist lives in the client's own IVehiclePlaylistsController; its
    'list' field holds vehicle intCDs, the same keys getVehicles() is indexed
    by. Imported inside the function so a client without the controller costs
    nothing at module load.
    """
    try:
        from skeletons.gui.game_control import IVehiclePlaylistsController
        controller = dependency.instance(IVehiclePlaylistsController)
        if not controller.isEnabled:
            return (None, None)
        selected_id = controller.getSelectedID()
        if not selected_id:
            return (None, None)
        for playlist_id, raw in controller.iterPlaylists():
            if playlist_id != selected_id:
                continue
            playlist = controller.simplePlayListParser(raw)
            if playlist is None:
                LOG.warning('selected playlist %s could not be parsed' % selected_id)
                return (None, None)
            return (playlist.title, set(playlist.list))
        LOG.warning('selected playlist %s is not in the cache' % selected_id)
        return (None, None)
    except Exception:
        LOG.exc('could not read the selected playlist')
        return (None, None)


def playlist_vehicles():
    """(vehicles, missing count) for the selected playlist, best tier first.

    Only vehicles the player actually owns and that the current hangar allows
    can be equipped; everything else in the playlist is counted so the summary
    can say so instead of quietly dropping it. The carousel filter is
    deliberately NOT applied - a playlist is an explicit choice by the player
    and must not be narrowed by whatever the carousel happens to show.
    """
    title, device_cds = selected_playlist()
    if not device_cds:
        return ([], 0)
    query = REQ_CRITERIA.INVENTORY | REQ_CRITERIA.IN_CD_LIST(list(device_cds))
    query |= _eligibility_criteria(REQ_CRITERIA, hangar.active_mode())
    try:
        vehicles = _items_cache().items.getVehicles(query)
        targets = sorted(vehicles.itervalues(), key=lambda v: (-v.level, v.userName))
        missing = len(device_cds) - len(targets)
        LOG.info('playlist "%s": %d of %d entr(ies) usable here'
                 % (title, len(targets), len(device_cds)))
        return (targets, max(0, missing))
    except Exception:
        LOG.exc('playlist_vehicles failed')
        return ([], 0)


def _random_battle_criteria(criteria):
    """The gate the random-battle carousel applies: everything except vehicles
    that exist only for some other mode."""
    return (~criteria.VEHICLE.MODE_HIDDEN
            | ~criteria.VEHICLE.BATTLE_ROYALE
            | ~criteria.VEHICLE.EVENT_BATTLE
            | criteria.VEHICLE.ACTIVE_IN_NATION_GROUP)


def _suitable_vehicle_criteria(criteria, interface):
    """Onslaught and its light variant both answer per vehicle, returning None
    when nothing speaks against taking it in."""
    controller = dependency.instance(interface)
    return criteria.CUSTOM(lambda vehicle: controller.isSuitableVehicle(vehicle) is None)


def _comp7_gate(criteria):
    from skeletons.gui.game_control import IComp7Controller
    return _suitable_vehicle_criteria(criteria, IComp7Controller)


def _comp7_light_gate(criteria):
    from skeletons.gui.game_control import IComp7LightController
    return _suitable_vehicle_criteria(criteria, IComp7LightController)


def _frontline_gate(criteria):
    from skeletons.gui.game_control import IEpicBattleMetaGameController
    return dependency.instance(IEpicBattleMetaGameController).getBaseEpicCriteria()


def _last_stand_gate(criteria):
    from last_stand.skeletons.ls_controller import ILSController
    return dependency.instance(ILSController).getVehiclesCriteria()


def _fun_random_gate(criteria):
    """Arcade Cabinet changes its rules per sub-mode, so the criteria come from
    whichever one is selected. With none selected the client itself falls back
    to the random-battle gate."""
    from skeletons.gui.game_control import IFunRandomController
    holder = dependency.instance(IFunRandomController).subModesHolder
    sub_mode = holder.getDesiredSubMode()
    if sub_mode is None:
        return _random_battle_criteria(criteria)
    return sub_mode.getCarouselBaseCriteria()


_MODE_GATES = {
    'comp7': _comp7_gate,
    'comp7_light': _comp7_light_gate,
    'frontline': _frontline_gate,
    'last_stand': _last_stand_gate,
    'fun_random': _fun_random_gate,
}


def _eligibility_criteria(criteria, mode):
    try:
        gate = _MODE_GATES.get(mode)
        return gate(criteria) if gate is not None else _random_battle_criteria(criteria)
    except Exception:
        LOG.exc('eligibility rules for "%s" unavailable - not restricting' % mode)
        return criteria.EMPTY


def _carousel_filter_criteria(mode):
    module_name, class_name = _CAROUSEL_FILTERS.get(
        mode, _CAROUSEL_FILTERS[hangar.STANDARD])
    try:
        module = __import__(module_name, {}, {}, [class_name])
        carousel_filter = getattr(module, class_name)()
        carousel_filter.load()
        return carousel_filter.criteria
    except Exception:
        LOG.exc('carousel filter %s unavailable - using all Primary vehicles'
                % class_name)
        return REQ_CRITERIA.EMPTY


# ---------------------------------------------------------------------------
# Backfilling vehicleCD into older saved entries
# ---------------------------------------------------------------------------

def fill_in_missing_vehicle_cds():
    """kurzdor's save format carries no vehicle type id at all - only his own
    account-scoped vehicle key, which we trust as this account's invID. That
    is fine for the import itself (same account, so his key IS our key), but it
    leaves every imported entry without a vehicleCD - and vehicleCD is exactly
    what a LATER cross-account import needs to remap a set onto a DIFFERENT
    account. Left unfixed, anything imported from kurzdor stays stuck as "not
    owned on this account" the moment it is moved elsewhere.

    So: walk every vehicle the account owns and read invID AND intCD off that
    same live object, then backfill whichever saved entry has that invID.
    Resolving ids one at a time right after each import proved unreliable;
    reading both off the object already in hand, in one pass, is what works.

    The scan is deferred until the items cache has synced. The silent first-run
    import happens the moment the account id becomes known, which is BEFORE the
    inventory is there - getVehicles() then simply returns nothing rather than
    failing, which is how this first went unnoticed ("scanned 0 owned
    vehicles"). onSyncCompleted is the same event CurrentVehicle waits on."""
    try:
        cache = _items_cache()
        if cache.isSynced():
            _backfill_now()
            return
        LOG.info('vehicleCD backfill: items cache not synced yet, waiting for onSyncCompleted')
        _subscribe_once(cache)
    except Exception:
        LOG.exc('fill_in_missing_vehicle_cds failed')


def _subscribe_once(cache):
    try:
        cache.onSyncCompleted -= _on_items_synced
    except Exception:
        pass    # not currently subscribed
    cache.onSyncCompleted += _on_items_synced


def _on_items_synced(*_args):
    try:
        _items_cache().onSyncCompleted -= _on_items_synced
    except Exception:
        pass
    _backfill_now()


def _backfill_now():
    try:
        vehicles = owned_vehicles()
        filled = sum(1 for vehicle in vehicles
                     if config.fill_in_vehicle_cd(vehicle.invID, vehicle.intCD))
        LOG.info('vehicleCD backfill: scanned %d owned vehicle(s), filled in %d'
                 % (len(vehicles), filled))
    except Exception:
        LOG.exc('vehicleCD backfill failed')
