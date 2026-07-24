# -*- coding: utf-8 -*-
import json

import BigWorld

from auto_equip_log import LOG

try:
    from frameworks.wulf import ViewModel
    from gui.impl.pub.view_component import ViewComponent
    from gui.impl.pub.view_impl import ViewImpl
    from gui.impl.gen import R
    from openwg_gameface import ModDynAccessor, gf_mod_inject, manager as resmap
except Exception:
    LOG.exc('core gameface imports FAILED — module unusable')
    raise

_VIEW_ALIAS = 'AutoEquipView'

_HANGAR_MAIN_LAYOUT_ID = R.views.mono.hangar.main()

# Comp7 hangar layout id is registered by the comp7 extension at runtime —
# resolve lazily (may not exist at import time, or at all).
_g_comp7_layout_id = None


def _comp7_hangar_layout_id():
    global _g_comp7_layout_id
    if _g_comp7_layout_id is None:
        try:
            _g_comp7_layout_id = R.views.comp7.mono.lobby.hangar()
        except Exception:
            pass
    return _g_comp7_layout_id


# --- module state ---------------------------------------------------------
_g_view = None               # our injected AutoEquipView
_g_injected_view_id = None
_g_hangar_view = None
_g_initialized = False
_pending_hangar_view = None
_g_vehicle_sub = False
_g_plus_state = None         # None = not checked yet, True = has WoT Plus, False = no sub
_PLUS_CHECK_MAX_ATTEMPTS = 10

def _ui_strings():
    """Popover UI text for the current client language (uiJson payload).
    Fetched fresh — not cached at import time — so it always reflects
    whatever auto_equip_i18n.init() loaded during mod init."""
    import auto_equip_i18n
    return auto_equip_i18n.ui_strings()


# ==========================================================================
# ViewImpl._onLoaded hook — catch the hangar as it loads
# ==========================================================================
_orig_onLoaded = ViewImpl._onLoaded


def _hooked_onLoaded(self, *args, **kwargs):
    _orig_onLoaded(self, *args, **kwargs)
    try:
        if self.layoutID == _HANGAR_MAIN_LAYOUT_ID or self.layoutID == _comp7_hangar_layout_id():
            _on_hangar_main_loaded(self)
    except Exception:
        LOG.exc('error in _hooked_onLoaded')


ViewImpl._onLoaded = _hooked_onLoaded


# ==========================================================================
# Popover data -> view model
# ==========================================================================

def _icon_name(item):
    """artefact icon filename (without extension) of a gui item."""
    try:
        return item.icon.split('/')[-1].rsplit('.', 1)[0]
    except Exception:
        return ''


def _overlay_name(item):
    """Overlay art name for trophy/deluxe devices ('' = none).

    Same art set the native hangar loadout panel uses
    (gui/maps/icons/components/loadout_item/overlays/<size>/<name>.png); it is
    drawn at 100% of the icon, so it scales with it.
    """
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


def _set_payload(cds):
    """One saved set list -> JS payload (None stays None = not saved)."""
    if cds is None:
        return None
    from helpers import dependency
    from skeletons.gui.shared import IItemsCache
    items = dependency.instance(IItemsCache).items
    out = []
    for cd in cds:
        if not cd:
            out.append(None)
            continue
        try:
            item = items.getItemByCD(int(cd))
            out.append({'cd': int(cd), 'icon': _icon_name(item),
                        'overlay': _overlay_name(item), 'name': item.userName})
        except Exception:
            out.append({'cd': int(cd), 'icon': '', 'overlay': '', 'name': '?'})
    return out


def _build_data():
    import mod_auto_equip
    import auto_equip_core
    data = {
        'vehicleName': u'',
        'vehicleCD': 0,
        'enabled': mod_auto_equip.is_auto_enabled(),
        'downgrade': mod_auto_equip.is_downgrade_enabled(),
        'hasSetup2': False,
        'saved1': None,
        'saved2': None,
        'busy': auto_equip_core.is_busy(),
    }
    try:
        from CurrentVehicle import g_currentVehicle
        vehicle = g_currentVehicle.item
        if vehicle is not None:
            data['vehicleName'] = vehicle.userName
            data['vehicleCD'] = vehicle.intCD
            data['hasSetup2'] = auto_equip_core.has_second_setup(vehicle)
            saved = mod_auto_equip.get_sets(vehicle.intCD)
            if saved:
                data['saved1'] = _set_payload(saved.get('set1'))
                data['saved2'] = _set_payload(saved.get('set2'))
    except Exception:
        LOG.exc('_build_data failed')
    return data


def _push_data():
    if _g_view is None:
        return
    try:
        _g_view.viewModel.setDataJson(json.dumps(_build_data()))
    except Exception:
        LOG.exc('_push_data failed')


# ==========================================================================
# Vehicle subscription (battle wipes it — re-subscribe on every hangar load)
# ==========================================================================

def _on_vehicle_changed(*args, **kwargs):
    try:
        import auto_equip_core
        auto_equip_core.on_vehicle_changed()
        _push_data()
    except Exception:
        LOG.exc('_on_vehicle_changed failed')


def _subscribe_vehicle():
    global _g_vehicle_sub
    try:
        from CurrentVehicle import g_currentVehicle
        try:
            g_currentVehicle.onChanged -= _on_vehicle_changed
        except Exception:
            pass   # not currently subscribed
        g_currentVehicle.onChanged += _on_vehicle_changed
        if not _g_vehicle_sub:
            LOG.info('subscribed to g_currentVehicle.onChanged')
        _g_vehicle_sub = True
    except Exception:
        LOG.exc('failed to subscribe to g_currentVehicle.onChanged')


def _unsubscribe_vehicle():
    global _g_vehicle_sub
    if not _g_vehicle_sub:
        return
    try:
        from CurrentVehicle import g_currentVehicle
        g_currentVehicle.onChanged -= _on_vehicle_changed
    except Exception:
        LOG.exc('failed to unsubscribe from g_currentVehicle.onChanged')
    finally:
        _g_vehicle_sub = False


# ==========================================================================
# ViewModel + ViewComponent
# ==========================================================================

class AutoEquipViewModel(ViewModel):
    __slots__ = ('onJsLog', 'onToggleEnabled', 'onToggleDowngrade', 'onSaveSet', 'onApplyNow', 'onEquipPrimary')

    def __init__(self):
        super(AutoEquipViewModel, self).__init__(properties=2, commands=6)

    def getDataJson(self):
        return self._getString(0)

    def setDataJson(self, v):
        self._setString(0, v)

    def getUiJson(self):
        return self._getString(1)

    def setUiJson(self, v):
        self._setString(1, v)

    def _initialize(self):
        super(AutoEquipViewModel, self)._initialize()
        self._addStringProperty('dataJson', '{}')
        self._addStringProperty('uiJson', '{}')
        self.onJsLog = self._addCommand('onJsLog')
        self.onToggleEnabled = self._addCommand('onToggleEnabled')
        self.onToggleDowngrade = self._addCommand('onToggleDowngrade')
        self.onSaveSet = self._addCommand('onSaveSet')
        self.onApplyNow = self._addCommand('onApplyNow')
        self.onEquipPrimary = self._addCommand('onEquipPrimary')
        gf_mod_inject(self, _VIEW_ALIAS, styles=[
            'coui://gui/gameface/mods/z4imon/AutoEquipView/AutoEquipView.css'
        ], modules=[
            'coui://gui/gameface/mods/z4imon/AutoEquipView/AutoEquipView.js'
        ])


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
            self.viewModel.setUiJson(json.dumps(_ui_strings()))
            self.viewModel.setDataJson(json.dumps(_build_data()))
        except Exception:
            LOG.exc('AutoEquipView._onLoading failed')

    def _getEvents(self):
        return (
            (self.viewModel.onJsLog, self._onJsLog),
            (self.viewModel.onToggleEnabled, self._onToggleEnabled),
            (self.viewModel.onToggleDowngrade, self._onToggleDowngrade),
            (self.viewModel.onSaveSet, self._onSaveSet),
            (self.viewModel.onApplyNow, self._onApplyNow),
            (self.viewModel.onEquipPrimary, self._onEquipPrimary),
        )

    def _onJsLog(self, data=None):
        try:
            if not data:
                return
            level = str(data.get('level', 'info')).lower()
            msg = '[JS] ' + str(data.get('msg', ''))
            if level == 'error':
                LOG.error(msg)
            elif level == 'warning':
                LOG.warning(msg)
            else:
                LOG.info(msg)
        except Exception:
            LOG.exc('_onJsLog failed')

    def _onToggleEnabled(self, data=None):
        try:
            import mod_auto_equip
            mod_auto_equip.set_auto_enabled(not mod_auto_equip.is_auto_enabled())
            _push_data()
        except Exception:
            LOG.exc('_onToggleEnabled failed')

    def _onToggleDowngrade(self, data=None):
        try:
            import mod_auto_equip
            mod_auto_equip.set_downgrade_enabled(not mod_auto_equip.is_downgrade_enabled())
            _push_data()
        except Exception:
            LOG.exc('_onToggleDowngrade failed')

    def _onSaveSet(self, data=None):
        try:
            import auto_equip_core
            which = int(data.get('which', 3)) if data else 3
            status = auto_equip_core.save_sets(which)
            LOG.info('_onSaveSet(%s): %s' % (which, status))
            _push_data()
        except Exception:
            LOG.exc('_onSaveSet failed')

    def _onApplyNow(self, data=None):
        try:
            import auto_equip_core
            auto_equip_core.apply_now()
            _push_data()
        except Exception:
            LOG.exc('_onApplyNow failed')

    def _onEquipPrimary(self, data=None):
        try:
            import auto_equip_core
            auto_equip_core.equip_primary_vehicles()
            _push_data()
        except Exception:
            LOG.exc('_onEquipPrimary failed')


# ==========================================================================
# Hangar hook plumbing + WoT Plus gate
# ==========================================================================

def _on_hangar_main_loaded(view):
    global _pending_hangar_view, _g_hangar_view
    _g_hangar_view = view
    if _g_plus_state is False:
        return
    if not _g_initialized:
        _pending_hangar_view = view
        return
    if _g_plus_state is True:
        _subscribe_vehicle()
        _do_inject(view)
        _push_data()
    else:
        _check_wot_plus(0)


def _check_wot_plus(attempt):
    """The subscription data may not be synced when the hangar first loads —
    poll a few times before declaring the account Plus-less. The user asked for
    a hard gate: without WoT Plus the mod shuts itself down."""
    global _g_plus_state
    if _g_plus_state is not None:
        return
    try:
        import auto_equip_core
        if auto_equip_core.has_wot_plus():
            _g_plus_state = True
            LOG.info('WoT Plus subscription found — mod active')
            auto_equip_core.log_equipment_overview()
            if _g_hangar_view is not None:
                _subscribe_vehicle()
                _do_inject(_g_hangar_view)
                _push_data()
            return
        if attempt >= _PLUS_CHECK_MAX_ATTEMPTS:
            _g_plus_state = False
            LOG.warning('no WoT Plus subscription — shutting the mod down')
            try:
                import auto_equip_i18n
                from gui import SystemMessages
                SystemMessages.pushMessage(
                    auto_equip_i18n.t('noSubscription'),
                    type=SystemMessages.SM_TYPE.Warning)
            except Exception:
                LOG.exc('could not push no-subscription message')
            import mod_auto_equip
            mod_auto_equip.set_disabled()
            fini()
            return
        BigWorld.callback(1.0, lambda: _check_wot_plus(attempt + 1))
    except Exception:
        LOG.exc('_check_wot_plus failed')


def _do_inject(view):
    global _g_view, _g_injected_view_id
    view_id = id(view)
    if _g_injected_view_id == view_id:
        return
    if not resmap.isResMapValidated:
        LOG.error('_do_inject: ResMap NOT validated — cannot inject')
        return
    try:
        _g_view = AutoEquipView()
        view.setChildView(AutoEquipView.viewLayoutID(), _g_view)
        _g_injected_view_id = view_id
        LOG.info('_do_inject: AutoEquipView injected OK')
    except Exception:
        LOG.exc('_do_inject: failed to inject AutoEquipView')


# ==========================================================================
# Public API
# ==========================================================================

def init():
    global _g_initialized, _pending_hangar_view
    _g_initialized = True
    try:
        import auto_equip_core
        auto_equip_core.set_refresh_cb(_push_data)
    except Exception:
        LOG.exc('init: set_refresh_cb failed')
    if _pending_hangar_view is not None:
        view = _pending_hangar_view
        _pending_hangar_view = None
        _on_hangar_main_loaded(view)


def fini():
    global _g_view, _g_injected_view_id, _g_hangar_view
    _unsubscribe_vehicle()
    _g_view = None
    _g_injected_view_id = None
    _g_hangar_view = None
