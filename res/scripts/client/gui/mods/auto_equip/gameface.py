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

from . import config, hangar, i18n, inventory, messages, save
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
        'hasSetup2': False,
        'saved1': None,
        'saved2': None,
        'busy': apply_engine.is_busy(),
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
                 'onSaveSet', 'onEquipPrimary')

    def __init__(self):
        super(AutoEquipViewModel, self).__init__(properties=2, commands=5)

    def getDataJson(self):
        return self._getString(0)

    def setDataJson(self, value):
        self._setString(0, value)

    def getUiJson(self):
        return self._getString(1)

    def setUiJson(self, value):
        self._setString(1, value)

    def _initialize(self):
        super(AutoEquipViewModel, self)._initialize()
        self._addStringProperty('dataJson', '{}')
        self._addStringProperty('uiJson', '{}')
        self.onJsLog = self._addCommand('onJsLog')
        self.onToggleEnabled = self._addCommand('onToggleEnabled')
        self.onToggleDowngrade = self._addCommand('onToggleDowngrade')
        self.onSaveSet = self._addCommand('onSaveSet')
        self.onEquipPrimary = self._addCommand('onEquipPrimary')
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
            (self.viewModel.onSaveSet, self._on_save_set),
            (self.viewModel.onEquipPrimary, self._on_equip_primary),
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

    def _on_save_set(self, data=None):
        try:
            which = int(data.get('which', save.BOTH_SETS)) if data else save.BOTH_SETS
            status = save.save_current_vehicle_sets(which)
            LOG.info('_on_save_set(%s): %s' % (which, status))
            push_data()
        except Exception:
            LOG.exc('_on_save_set failed')

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
            inventory.log_equipment_overview()
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
