# -*- coding: utf-8 -*-
"""Turning WoT Plus' equipment recommendation into two sets this mod can save.

The client's "most used devices" assistant (the WoT Plus panel in the tank
setup screen) names devices by ARCHETYPE, not by item: it says "a rammer", not
"which rammer". Two recommendations come out of it per vehicle - what all
players run, and what the best players run - each as a list of loadouts ranked
by how many players use them.

    common, legendary = inventory.recommended_presets(vehicle)
    -> (source, sourceVehicleCD, [VehicleLoadout(devices=[...], percentage=..)])

So the work here is the join: archetype -> a real device, one per slot.

WHICH TWO LOADOUTS: both sets come from the BEST PLAYERS' recommendation - the
most used loadout into set 1, the second most used into set 2. The two are
genuine alternatives among players who know the tank, which is what makes them
worth having side by side; the all-players list mostly repeats the first one
with a cheaper device swapped in. It is only used when the best-players data
does not cover this tank at all, because a dead button on half the garage
serves nobody.

WHICH VARIANT: bounty ("erbeutet") when one exists - the UPGRADED level 2
variant by preference - and the standard device otherwise. Never bond
(Improved) and never Experimental: the player asked for that explicitly, and it
is also the only rule consistent with the rest of the mod, because taking an
Improved device off costs 200 bonds, so a saved set that asks for one turns
every future install run into a demand the mod cannot meet for free. Bounty
devices demount for free under WoT Plus, which is the premise this whole mod is
built on - upgraded ones included.

The saved set is a GOAL, not an inventory list. A recommended device the player
does not own yet still goes in: apply.py sources only what is free and reports
the rest as missing, so the set completes itself the day the device shows up.
"""

from . import inventory
from .log import LOG

try:
    from renewable_subscription_common.optional_devices_usage_config import (
        GENERIC_OPTIONAL_DEVICE_MAP_TO_EQUIPMENT_NAME)
except Exception:
    LOG.exc('equipment assistant constants missing - recommendations disabled')
    GENERIC_OPTIONAL_DEVICE_MAP_TO_EQUIPMENT_NAME = {}

# getOptDeviceAssistPresets() answers with these two, in this order.
COMMON, LEGENDARY = 0, 1

# How many of a recommendation's ranked loadouts we take - one per set.
_WANTED = 2


def _archetype(device):
    """The assistant's generic device -> the equipment name the items cache
    knows it by. Values arrive either as the GenericOptionalDevice enum or as
    its plain int, depending on whether they came from the shipped config or
    from a server update, so both are looked up."""
    name = GENERIC_OPTIONAL_DEVICE_MAP_TO_EQUIPMENT_NAME.get(device)
    if name is None:
        name = GENERIC_OPTIONAL_DEVICE_MAP_TO_EQUIPMENT_NAME.get(
            getattr(device, 'value', None))
    if name is None:
        LOG.warning('unknown generic device in recommendation: %r' % (device,))
    return name


def _is_allowed(item):
    """Bond (Improved) and Experimental devices are out - see the module
    docstring. Everything else is fair game."""
    try:
        return not item.isDeluxe and not item.isModernized
    except Exception:
        return False


def _rank(item):
    """Sort key picking the device to save for one archetype. Lower is better:

    1. upgraded bounty (level 2) - "verbesserte erbeutete Ausruestung",
    2. plain bounty (level 1),
    3. standard device.

    Owning it only breaks ties WITHIN a tier, never across one: a saved set is
    a goal, not an inventory list, so it names the level 2 device even while the
    player still has the level 1. apply.py then installs what it can source for
    free and reports the rest, and the slot upgrades itself the day the player
    upgrades the device."""
    try:
        bounty = bool(item.isTrophy)
        upgraded = bool(item.isUpgraded)
    except Exception:
        bounty = upgraded = False
    if bounty:
        tier = 0 if upgraded else 1
    else:
        tier = 2
    return (tier, 0 if inventory.is_owned(item) else 1)


def _device_for(vehicle, generic_device):
    """One archetype -> the device to put in the saved set, or None."""
    archetype = _archetype(generic_device)
    if archetype is None:
        return None
    candidates = [item for item in inventory.devices_by_archetype(vehicle, archetype)
                  if _is_allowed(item)]
    if not candidates:
        LOG.warning('no bounty or standard device for archetype "%s" on %s'
                    % (archetype, vehicle.userName))
        return None
    candidates.sort(key=_rank)
    return candidates[0]


def _loadout_to_cds(vehicle, loadout):
    """One recommended loadout -> a per-slot intCD list of the vehicle's exact
    slot capacity. A device that cannot be resolved leaves its slot empty
    rather than shifting the rest along."""
    capacity = inventory.slot_capacity(vehicle)
    cds = []
    for generic_device in list(loadout.devices)[:capacity]:
        item = _device_for(vehicle, generic_device)
        cds.append(int(item.intCD) if item is not None else 0)
    cds += [0] * (capacity - len(cds))
    return cds


def _ranked_loadouts(preset):
    """One recommendation's loadouts, most used first - that is the order the
    client stores them in. Empty entries are dropped so "second most used"
    means the second real one."""
    if not preset or len(preset) < 3:
        return []
    return [loadout for loadout in preset[2] if loadout.devices]


def _pick_loadouts(vehicle):
    """(which recommendation, its loadouts) - the best players' if that covers
    this tank, otherwise the all-players one, otherwise nothing."""
    presets = inventory.recommended_presets(vehicle)
    for source in (LEGENDARY, COMMON):
        if len(presets) > source:
            loadouts = _ranked_loadouts(presets[source])
            if loadouts:
                return source, loadouts
    # Both gated paths came back empty. The ungated one still knows the single
    # most used loadout, which beats a dead button just because the player
    # happens to be sitting in a platoon.
    loadout = inventory.most_popular_loadout(vehicle)
    if loadout is not None and loadout.devices:
        return COMMON, [loadout]
    return None, []


def for_vehicle(vehicle):
    """The recommendation for a vehicle as a list of

        {'source': 'legendary'|'common', 'rank': int|None,
         'cds': [...]|None, 'percent': int}

    one entry per set to be saved, in set order. `cds` is None for a set the
    recommendation cannot fill - the caller shows that as "not available"
    rather than silently saving a half loadout.

    An empty LIST means the client knows nothing about this vehicle at all,
    which is normal: the data does not cover every tank and the assistant is
    gated on the WoT Plus subscription."""
    if vehicle is None:
        return []
    try:
        source, loadouts = _pick_loadouts(vehicle)
        if not loadouts:
            return []
        name = 'legendary' if source == LEGENDARY else 'common'
        wanted = _WANTED if inventory.has_second_setup(vehicle) else 1
        entries = []
        for rank, loadout in enumerate(loadouts):
            if len(entries) >= wanted:
                break
            cds = _loadout_to_cds(vehicle, loadout)
            if not all(cds):
                # Some archetypes exist ONLY as experimental equipment - the
                # combined ones (turbo + rotation mechanism and friends). Those
                # leave a hole no allowed device can fill, so the whole loadout
                # is passed over and the next one down the ranking takes its
                # place. Saving it with a gap would silently strip a slot.
                LOG.info('recommendation #%d for %s skipped: %d slot(s) have no '
                         'bounty or standard device'
                         % (rank + 1, vehicle.userName, cds.count(0)))
                continue
            entries.append({'source': name,
                            'rank': rank + 1,
                            'cds': cds,
                            'percent': int(round(loadout.percentage or 0))})
        # Ranking exhausted before both sets were filled. The empty entries stay
        # in the list on purpose: the player asked to be told which of the two
        # could not be built, and an entry that is simply absent says nothing.
        while len(entries) < wanted:
            entries.append({'source': name, 'rank': None, 'cds': None, 'percent': 0})
        return entries
    except Exception:
        LOG.exc('recommended.for_vehicle failed')
        return []


def as_sets(entries):
    """The entries from for_vehicle() as the (set1, set2) pair config.store_sets
    expects. A set the recommendation could not fill stays None, which leaves
    whatever is already saved for it untouched - the player keeps their own set
    rather than losing it to a recommendation that was never complete."""
    set1 = entries[0]['cds'] if len(entries) > 0 else None
    set2 = entries[1]['cds'] if len(entries) > 1 else None
    return set1, set2
