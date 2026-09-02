# -*- coding: utf-8 -*-
"""The hangar popover: a Gameface view injected into the hangar's main layout,
and into every game mode that brings a hangar of its own (hangar.py owns that
list).

The mod stays inert without a WoT Plus subscription (a hard gate the player
asked for), so the flow is: hook the hangar load -> check for the subscription
-> inject the view -> keep it fed with data.
"""

import json

import BigWorld

from CurrentVehicle import g_currentVehicle
from gui.shared.notifications import NotificationPriorityLevel

from . import config, hangar, i18n, inventory, messages, recommended, save, streamers
from . import apply as apply_engine
from .i18n import t
from .log import LOG

try:
    from frameworks.wulf import ViewModel
    from gui.impl.pub.view_component import ViewComponent
    from gui.impl.pub.view_impl import ViewImpl
    from openwg_gameface import ModDynAccessor, gf_mod_inject, manager as resmap
except Exception:
    LOG.exc('core gameface imports FAILED - module unusable')
    raise

_VIEW_ALIAS = 'AutoEquipView'
_VIEW_DIR = 'coui://gui/gameface/mods/z4imon/AutoEquipView'

_PLUS_CHECK_MAX_ATTEMPTS = 10
_PLUS_CHECK_INTERVAL = 1.0


# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_view = None                # our injected AutoEquipView
_injected_into_view_id = None
_hangar_view = None
_pending_hangar_view = None     # hangar loaded before init() ran
_initialized = False
_subscribed_to_vehicle = False
_has_wot_plus = None            # None = not checked yet

# ---------------------------------------------------------------------------
# Hook: catch the hangar as it loads
# ---------------------------------------------------------------------------

_original_on_loaded = ViewImpl._onLoaded


def _hooked_on_loaded(self, *args, **kwargs):
    _original_on_loaded(self, *args, **kwargs)
    try:
        if hangar.is_hangar(self.layoutID):
            # Remember WHICH hangar before anything reads it: the batch run
            # queries the vehicles of the mode this one belongs to.
            hangar.set_active(self.layoutID)
            _on_hangar_loaded(self)
    except Exception:
        LOG.exc('error in _hooked_on_loaded')


ViewImpl._onLoaded = _hooked_on_loaded


# ---------------------------------------------------------------------------
# Data pushed to the popover
# ---------------------------------------------------------------------------

def _icon_name(item):
    """The artefact icon filename of a gui item, without path or extension."""
    try:
        return item.icon.split('/')[-1].rsplit('.', 1)[0]
    except Exception:
        return ''


def _overlay_name(item):
    """Overlay art for trophy/deluxe devices ('' = none).

    The same art set the native hangar loadout panel uses
    (gui/maps/icons/components/loadout_item/overlays/<size>/<name>.png). It is
    drawn at 100% of the icon, so it scales along with it."""
    try:
        if item.isDeluxe:
            return 'improved'
        if item.isModernized:
            return 'experimental_%d_level' % item.level
        if item.isUpgradable:
            return 'trophy_1_level'
        if item.isUpgraded:
            return 'trophy_2_level'
    except Exception:
        pass
    return ''


def _slot_payload(device_cd):
    if not device_cd:
        return None
    item = inventory.device_by_cd(int(device_cd))
    if item is None:
        return {'cd': int(device_cd), 'icon': '', 'overlay': '', 'name': '?'}
    return {'cd': int(device_cd), 'icon': _icon_name(item),
            'overlay': _overlay_name(item), 'name': item.userName}


def _set_payload(device_cds):
    """One saved set -> JS payload. None stays None, meaning "not saved"."""
    if device_cds is None:
        return None
    return [_slot_payload(cd) for cd in device_cds]


def _build_data():
    data = {
        'vehicleName': u'',
        'vehicleInvID': 0,
        'enabled': config.is_auto_enabled(),
        'downgrade': config.is_downgrade_enabled(),
        'alwaysSetup1': config.is_always_setup1(),
        'hasSetup2': False,
        'saved1': None,
        'saved2': None,
        'busy': apply_engine.is_busy(),
        'selectedStreamer': config.selected_streamer_account_id(),
        'selectedStreamerName': config.selected_streamer_name(),
    }
    try:
        vehicle = g_currentVehicle.item
        if vehicle is None:
            return data
        data['vehicleName'] = vehicle.userName
        data['vehicleInvID'] = vehicle.invID
        data['hasSetup2'] = inventory.has_second_setup(vehicle)
        saved = config.saved_sets(vehicle.invID)
        if saved:
            data['saved1'] = _set_payload(saved.get('set1'))
            data['saved2'] = _set_payload(saved.get('set2'))
    except Exception:
        LOG.exc('_build_data failed')
    return data


def _transform_streamer_device(vehicle, device_cd):
    """Maps one device from a streamer's shared set to what the PULLING
    player should actually receive:

        Bond (Improved) device      -> the upgraded (level 2) Bounty sibling,
                                        falling back to standard/plain bounty
        Experimental level 2 or 3   -> the level 1 Experimental sibling,
                                        falling back to standard/plain bounty

    Standard, plain-bounty, and already-level-1 devices pass through
    unchanged. This only ever transforms the LOCAL COPY the viewer is about
    to save/apply for themselves - the streamer's own stored equipment is
    fetched read-only (streamers.fetch_vehicle_set) and never written back
    to, so nothing here can overwrite it.

    The best (closest) sibling is tried first, but a Bond/Experimental
    compactDescr is never left as the actual result on a match failure - it
    falls through the same standard/plain-bounty chain
    downgrade_candidates_of() already uses elsewhere, since neither is
    ownable or installable by an account that never bought/earned it. Only
    once every fallback in that chain also comes up empty (no bounty tier
    for this archetype at all) does the original device_cd pass through, on
    the same "a device the player can still source some other way beats a
    hole in the loadout" logic downgrade_candidates_of documents."""
    if not device_cd:
        return device_cd
    item = inventory.device_by_cd(int(device_cd))
    if item is None:
        return device_cd
    try:
        best = None
        if item.isDeluxe:
            best = inventory.bounty_upgraded_variant_of(vehicle, item)
        elif item.isModernized and getattr(item, 'level', 1) > 1:
            best = inventory.experimental_level_variant_of(vehicle, item, 1)
        else:
            return device_cd
        if best is not None:
            return int(best.intCD)
        for fallback in inventory.downgrade_candidates_of(vehicle, item):
            if fallback is not None:
                return int(fallback.intCD)
    except Exception:
        LOG.exc('_transform_streamer_device failed for cd=%s' % device_cd)
    return device_cd


def _transform_streamer_set(vehicle, cds):
    if cds is None:
        return None
    return [_transform_streamer_device(vehicle, cd) for cd in cds]


def _recommended_payload(vehicle):
    """The equipment assistant's proposal, already resolved to real devices, in
    the same slot shape the saved sets use - so the hover preview can be drawn
    with the same JS as the saved sets themselves."""
    try:
        return [{'percent': entry['percent'],
                 'slots': _set_payload(entry['cds'])}
                for entry in recommended.for_vehicle(vehicle)]
    except Exception:
        LOG.exc('_recommended_payload failed')
        return []


def push_data():
    """Sends the current state to the popover. Also the refresh callback the
    apply engine calls after anything changed."""
    global _view, _injected_into_view_id
    if _view is None:
        return
    try:
        view_model = _view.viewModel
        if view_model is None:
            # The native view was already torn down (hangar/subhangar
            # transition). Drop the stale reference so the next hangar load
            # re-injects, instead of failing on every refresh from here on.
            _view = None
            _injected_into_view_id = None
            return
        view_model.setDataJson(json.dumps(_build_data()))
    except Exception:
        LOG.exc('push_data failed')


def _push_preview(payload):
    if _view is None:
        return
    try:
        view_model = _view.viewModel
        if view_model is None:
            return
        view_model.setPreviewJson(json.dumps(payload))
    except Exception:
        LOG.exc('_push_preview failed')


def _push_streamer_list(streamer_list):
    if _view is None:
        return
    try:
        view_model = _view.viewModel
        if view_model is None:
            return
        view_model.setStreamerListJson(json.dumps(streamer_list))
    except Exception:
        LOG.exc('_push_streamer_list failed')


def _push_icon_data_uri(data_uri):
    if _view is None:
        return
    try:
        view_model = _view.viewModel
        if view_model is None:
            return
        view_model.setStreamerIconDataUri(data_uri or '')
    except Exception:
        LOG.exc('_push_icon_data_uri failed')


# ---------------------------------------------------------------------------
# Vehicle subscription
#
# Entering a battle calls g_currentVehicle.destroy(), which clears ALL event
# subscriptions - so this has to be redone on every hangar load. Removing
# before adding keeps it idempotent.
# ---------------------------------------------------------------------------

def _on_vehicle_changed(*args, **kwargs):
    try:
        apply_engine.on_vehicle_changed()
        push_data()
        _push_preview({})
    except Exception:
        LOG.exc('_on_vehicle_changed failed')


def _subscribe_to_vehicle():
    global _subscribed_to_vehicle
    try:
        try:
            g_currentVehicle.onChanged -= _on_vehicle_changed
        except Exception:
            pass    # not currently subscribed
        g_currentVehicle.onChanged += _on_vehicle_changed
        if not _subscribed_to_vehicle:
            LOG.info('subscribed to g_currentVehicle.onChanged')
        _subscribed_to_vehicle = True
    except Exception:
        LOG.exc('failed to subscribe to g_currentVehicle.onChanged')


def _unsubscribe_from_vehicle():
    global _subscribed_to_vehicle
    if not _subscribed_to_vehicle:
        return
    try:
        g_currentVehicle.onChanged -= _on_vehicle_changed
    except Exception:
        LOG.exc('failed to unsubscribe from g_currentVehicle.onChanged')
    finally:
        _subscribed_to_vehicle = False


# ---------------------------------------------------------------------------
# View model and view
# ---------------------------------------------------------------------------

class AutoEquipViewModel(ViewModel):
    __slots__ = ('onJsLog', 'onToggleEnabled', 'onToggleDowngrade',
                 'onToggleAlwaysSetup1', 'onSaveSet', 'onDeleteSets',
                 'onSaveRecommended', 'onEquipPrimary',
                 'onRequestPreview', 'onOpenStreamerList', 'onSelectStreamer')

    def __init__(self):
        super(AutoEquipViewModel, self).__init__(properties=5, commands=11)

    def getDataJson(self):
        return self._getString(0)

    def setDataJson(self, value):
        self._setString(0, value)

    def getUiJson(self):
        return self._getString(1)

    def setUiJson(self, value):
        self._setString(1, value)

    def getPreviewJson(self):
        return self._getString(2)

    def setPreviewJson(self, value):
        self._setString(2, value)

    def getStreamerListJson(self):
        return self._getString(3)

    def setStreamerListJson(self, value):
        self._setString(3, value)

    def getStreamerIconDataUri(self):
        return self._getString(4)

    def setStreamerIconDataUri(self, value):
        self._setString(4, value)

    def _initialize(self):
        super(AutoEquipViewModel, self)._initialize()
        self._addStringProperty('dataJson', '{}')
        self._addStringProperty('uiJson', '{}')
        self._addStringProperty('previewJson', '{}')
        self._addStringProperty('streamerListJson', '[]')
        self._addStringProperty('streamerIconDataUri', '')
        self.onJsLog = self._addCommand('onJsLog')
        self.onToggleEnabled = self._addCommand('onToggleEnabled')
        self.onToggleDowngrade = self._addCommand('onToggleDowngrade')
        self.onToggleAlwaysSetup1 = self._addCommand('onToggleAlwaysSetup1')
        self.onSaveSet = self._addCommand('onSaveSet')
        self.onDeleteSets = self._addCommand('onDeleteSets')
        self.onSaveRecommended = self._addCommand('onSaveRecommended')
        self.onEquipPrimary = self._addCommand('onEquipPrimary')
        self.onRequestPreview = self._addCommand('onRequestPreview')
        self.onOpenStreamerList = self._addCommand('onOpenStreamerList')
        self.onSelectStreamer = self._addCommand('onSelectStreamer')
        gf_mod_inject(self, _VIEW_ALIAS,
                      styles=['%s/AutoEquipView.css' % _VIEW_DIR],
                      modules=['%s/AutoEquipView.js' % _VIEW_DIR])


class AutoEquipView(ViewComponent):
    viewLayoutID = ModDynAccessor(_VIEW_ALIAS)

    def __init__(self):
        super(AutoEquipView, self).__init__(
            layoutID=AutoEquipView.viewLayoutID(),
            model=AutoEquipViewModel,
        )

    @property
    def viewModel(self):
        return super(AutoEquipView, self).getViewModel()

    def _onLoading(self, *args, **kwargs):
        super(AutoEquipView, self)._onLoading()
        try:
            self.viewModel.setUiJson(json.dumps(i18n.ui_strings()))
            self.viewModel.setDataJson(json.dumps(_build_data()))
        except Exception:
            LOG.exc('AutoEquipView._onLoading failed')

    def _getEvents(self):
        return (
            (self.viewModel.onJsLog, self._on_js_log),
            (self.viewModel.onToggleEnabled, self._on_toggle_enabled),
            (self.viewModel.onToggleDowngrade, self._on_toggle_downgrade),
            (self.viewModel.onToggleAlwaysSetup1, self._on_toggle_always_setup1),
            (self.viewModel.onSaveSet, self._on_save_set),
            (self.viewModel.onDeleteSets, self._on_delete_sets),
            (self.viewModel.onSaveRecommended, self._on_save_recommended),
            (self.viewModel.onEquipPrimary, self._on_equip_primary),
            (self.viewModel.onRequestPreview, self._on_request_preview),
            (self.viewModel.onOpenStreamerList, self._on_open_streamer_list),
            (self.viewModel.onSelectStreamer, self._on_select_streamer),
        )

    def _on_js_log(self, data=None):
        try:
            if not data:
                return
            level = str(data.get('level', 'info')).lower()
            message = '[JS] ' + str(data.get('msg', ''))
            if level == 'error':
                LOG.error(message)
            elif level == 'warning':
                LOG.warning(message)
            else:
                LOG.info(message)
        except Exception:
            LOG.exc('_on_js_log failed')

    def _on_toggle_enabled(self, data=None):
        try:
            config.set_auto_enabled(not config.is_auto_enabled())
            push_data()
        except Exception:
            LOG.exc('_on_toggle_enabled failed')

    def _on_toggle_downgrade(self, data=None):
        try:
            config.set_downgrade_enabled(not config.is_downgrade_enabled())
            push_data()
        except Exception:
            LOG.exc('_on_toggle_downgrade failed')

    def _on_toggle_always_setup1(self, data=None):
        try:
            config.set_always_setup1(not config.is_always_setup1())
            push_data()
        except Exception:
            LOG.exc('_on_toggle_always_setup1 failed')

    def _on_save_set(self, data=None):
        try:
            which = int(data.get('which', save.BOTH_SETS)) if data else save.BOTH_SETS
            status = save.save_current_vehicle_sets(which)
            LOG.info('_on_save_set(%s): %s' % (which, status))
            push_data()
        except Exception:
            LOG.exc('_on_save_set failed')

    def _on_delete_sets(self, data=None):
        try:
            save.delete_current_vehicle_sets()
            push_data()
        except Exception:
            LOG.exc('_on_delete_sets failed')

    def _on_save_recommended(self, data=None):
        """Stores whichever source is currently selected (WoT Plus or a
        streamer) as this vehicle's sets and installs it right away - saving
        without installing would leave the player looking at a set that is
        not on the tank.

        Only ever runs on a vehicle with nothing saved: the source fills both
        sets, so on a vehicle that already has one it would silently replace
        the player's own work."""
        try:
            vehicle = g_currentVehicle.item
            if vehicle is None:
                return
            if config.has_saved_sets(vehicle.invID):
                # The popover greys the button out in this case; this is the
                # net under a stale render, so it only has to refuse, not
                # explain.
                LOG.warning('recommendation refused for %s: sets are already saved'
                            % vehicle.userName)
                return
            streamer_id = config.selected_streamer_account_id()
            if streamer_id is None:
                self._apply_wotplus_recommendation(vehicle)
            else:
                self._apply_streamer_recommendation(vehicle, streamer_id)
        except Exception:
            LOG.exc('_on_save_recommended failed')

    def _apply_wotplus_recommendation(self, vehicle):
        entries = recommended.for_vehicle(vehicle)
        if not entries:
            messages.push_warning(t('recNone'))
            return
        set1, set2 = recommended.as_sets(entries)
        if set1 is None and set2 is None:
            # Every ranked loadout had a slot only experimental equipment
            # could fill. Storing now would create an empty entry for a
            # vehicle the player never handed to the mod.
            messages.push_warning(t('recNone'))
            return
        config.store_sets(vehicle.invID, set1=set1, set2=set2, veh_cd=vehicle.intCD)
        LOG.info('recommended sets stored for %s (%s, rank %s): set1=%s set2=%s'
                 % (vehicle.userName, entries[0]['source'],
                    [entry['rank'] for entry in entries], set1, set2))
        push_data()
        messages.push_info(t('recSaved', veh=vehicle.userName),
                           priority=NotificationPriorityLevel.HIGH)
        apply_engine.apply_saved_sets(vehicle.invID)

    def _apply_streamer_recommendation(self, vehicle, streamer_account_id):
        def on_result(set1, set2):
            if set1 is None and set2 is None:
                messages.push_warning(t('recNone'))
                return
            set1 = _transform_streamer_set(vehicle, set1)
            set2 = _transform_streamer_set(vehicle, set2)
            config.store_sets(vehicle.invID, set1=set1, set2=set2, veh_cd=vehicle.intCD)
            push_data()
            messages.push_info(t('recSaved', veh=vehicle.userName),
                               priority=NotificationPriorityLevel.HIGH)
            apply_engine.apply_saved_sets(vehicle.invID)
        streamers.fetch_vehicle_set(streamer_account_id, vehicle.intCD, callback=on_result)

    def _on_request_preview(self, data=None):
        try:
            vehicle = g_currentVehicle.item
            if vehicle is None:
                return
            streamer_id = config.selected_streamer_account_id()
            if streamer_id is None:
                _push_preview({'kind': 'wotplus', 'entries': _recommended_payload(vehicle)})
                return

            def on_result(set1, set2):
                _push_preview({'kind': 'streamer',
                               'slots1': _set_payload(_transform_streamer_set(vehicle, set1)),
                               'slots2': _set_payload(_transform_streamer_set(vehicle, set2))})
            streamers.fetch_vehicle_set(streamer_id, vehicle.intCD, callback=on_result)
        except Exception:
            LOG.exc('_on_request_preview failed')

    def _on_open_streamer_list(self, data=None):
        try:
            def on_result(streamer_list):
                LOG.info('streamers: list fetch returned %s'
                         % ('None (failed)' if streamer_list is None else streamer_list))
                if streamer_list is None:
                    _push_streamer_list([])
                    return
                _push_streamer_list(streamer_list)
                selected_id = config.selected_streamer_account_id()
                if selected_id is not None and not any(
                        s.get('accountId') == selected_id for s in streamer_list):
                    streamers.forget_streamer(config.selected_streamer_name())
                    config.set_selected_streamer(None)
                    _push_preview({})
                    _push_icon_data_uri('')
                    push_data()
            streamers.list_streamers(on_result)
        except Exception:
            LOG.exc('_on_open_streamer_list failed')

    def _on_select_streamer(self, data=None):
        try:
            account_id = (data or {}).get('accountId')
            # JS command args arrive as float - BigWorld/Wulf marshals every
            # JS Number this way, no int/float distinction on that side.
            # config.set_selected_streamer() already coerces internally, but
            # ensure_icon_cached() below needs the same clean int too, or its
            # cache filenames end up "<id>.0.bin"/".0.json" - which every
            # LATER lookup (hangar-reload warm-up, cache cleanup on consent
            # revocation) never finds, since those all read the id back via
            # config.selected_streamer_account_id(), which is always a plain
            # int. Confirmed live: a real download produced exactly those
            # stray ".0"-suffixed files, correct content, wrong name.
            account_id = int(account_id) if account_id is not None else None
            streamer_name = (data or {}).get('streamerName')
            config.set_selected_streamer(account_id, streamer_name)
            _push_preview({})
            _push_icon_data_uri('')
            push_data()
            if account_id is not None:
                streamers.ensure_icon_cached(account_id, streamer_name, callback=_push_icon_data_uri)
        except Exception:
            LOG.exc('_on_select_streamer failed')

    def _on_equip_primary(self, data=None):
        try:
            apply_engine.equip_primary_vehicles()
            push_data()
        except Exception:
            LOG.exc('_on_equip_primary failed')


# ---------------------------------------------------------------------------
# Injection and the WoT Plus gate
# ---------------------------------------------------------------------------

def _on_hangar_loaded(view):
    global _hangar_view, _pending_hangar_view
    _hangar_view = view
    if _has_wot_plus is False:
        return
    if not _initialized:
        _pending_hangar_view = view
        return
    if _has_wot_plus is True:
        _activate(view)
    else:
        _check_wot_plus(attempt=0)


def _activate(view):
    _subscribe_to_vehicle()
    _inject_into(view)
    push_data()
    streamer_id = config.selected_streamer_account_id()
    if streamer_id is not None:
        streamers.ensure_icon_cached(streamer_id, config.selected_streamer_name(), callback=_push_icon_data_uri)


def _check_wot_plus(attempt):
    """Subscription data may not be synced when the hangar first loads, so poll
    a few times before declaring the account Plus-less. Without the
    subscription the mod shuts itself down."""
    global _has_wot_plus
    if _has_wot_plus is not None:
        return
    try:
        if inventory.has_wot_plus():
            _has_wot_plus = True
            LOG.info('WoT Plus subscription found - mod active')
            if _hangar_view is not None:
                _activate(_hangar_view)
            return
        if attempt >= _PLUS_CHECK_MAX_ATTEMPTS:
            _shut_down_without_subscription()
            return
        BigWorld.callback(_PLUS_CHECK_INTERVAL, lambda: _check_wot_plus(attempt + 1))
    except Exception:
        LOG.exc('_check_wot_plus failed')


def _shut_down_without_subscription():
    global _has_wot_plus
    _has_wot_plus = False
    LOG.warning('no WoT Plus subscription - shutting the mod down')
    try:
        messages.push_warning(t('noSubscription'))
    except Exception:
        LOG.exc('could not push no-subscription message')
    config.disable_mod()
    fini()


def _inject_into(view):
    global _view, _injected_into_view_id
    view_id = id(view)
    if _injected_into_view_id == view_id:
        return
    if not resmap.isResMapValidated:
        LOG.error('_inject_into: ResMap NOT validated - cannot inject')
        return
    try:
        _view = AutoEquipView()
        view.setChildView(AutoEquipView.viewLayoutID(), _view)
        _injected_into_view_id = view_id
        LOG.info('_inject_into: AutoEquipView injected OK')
    except Exception:
        LOG.exc('_inject_into: failed to inject AutoEquipView')


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def init():
    global _initialized, _pending_hangar_view
    _initialized = True
    apply_engine.set_refresh_callback(push_data)
    if _pending_hangar_view is not None:
        view = _pending_hangar_view
        _pending_hangar_view = None
        _on_hangar_loaded(view)


def fini():
    global _view, _injected_into_view_id, _hangar_view
    _unsubscribe_from_vehicle()
    _view = None
    _injected_into_view_id = None
    _hangar_view = None
