# -*- coding: utf-8 -*-
"""The mod's entry in "Mods settings+" (ModsSettingsAPI): the settings that are
better off in a panel with room for an explanation than on a hangar popover row,
plus the import controls importer.py provides.

Split by where a setting belongs, not by what it does:

  * the popover carries what a player flips while browsing tanks - auto-install
    and downgrade,
  * this panel carries the rules that hold for the whole account and need a
    sentence or two of context: whether pressing BATTLE saves the loadout, and
    which equipment categories the mod must never put back.

Only settings that live nowhere else are listed here. Offering the popover's two
as well would mean two independent stores of the same flag, and the one the
player did not touch would show a stale state.

Everything degrades quietly when ModsSettingsAPI isn't installed: the settings
keep working off the mod's own config file, they just cannot be changed from a
panel.
"""

from . import config, importer
from .i18n import t
from .log import LOG

# Kept short and stable: this string identifies the panel inside
# ModsSettingsAPI's own storage.
_MOD_LINKAGE = 'z4imon.auto_equipment_return'

# One checkbox per line, in panel order: (config flag, label key, tooltip key).
# The config flag doubles as the panel's variable name, so a setting is added by
# adding a row here and its two strings to the language files.
_CHECKBOX_ROWS = (
    ('autoSaveEnabled', 'setSaveOnBattle', 'setSaveOnBattleTooltip'),
    ('neverRemountImproved', 'setNeverImproved', 'setNeverImprovedTooltip'),
    ('neverRemountExperimental', 'setNeverExperimental', 'setNeverExperimentalTooltip'),
)


def _checkbox_rows(templates):
    """One checkbox per setting. Panel labels are a fixed width and never wrap,
    so each label stays a single short line and the reasoning behind the setting
    lives in its tooltip."""
    flags = config.flags()
    rows = []
    for flag, label_key, tooltip_key in _CHECKBOX_ROWS:
        try:
            rows.append(templates.createCheckbox(t(label_key), flag, flags[flag],
                                                 tooltip=t(tooltip_key)))
        except TypeError:
            # A ModsSettingsAPI version whose checkboxes take no tooltip. The
            # setting itself matters more than its explanation, so it is offered
            # either way.
            LOG.warning('ModsSettingsAPI checkboxes take no tooltip here')
            rows.append(templates.createCheckbox(t(label_key), flag, flags[flag]))
    return rows


def _apply_panel_settings(settings):
    """Takes over the values ModsSettingsAPI has stored for our checkboxes.

    It keeps its own copy of every panel value, so after a restart ITS copy is
    what the player sees - and that is also the copy their last click landed in.
    Reading it back here keeps the mod's config file from drifting away from the
    panel the player is looking at."""
    if not settings:
        return
    for flag, _label_key, _tooltip_key in _CHECKBOX_ROWS:
        if flag not in settings:
            continue      # written by an older version of this panel
        stored = bool(settings[flag])
        if stored != config.flags()[flag]:
            LOG.info('settings panel: %s = %s' % (flag, stored))
            config.set_flag(flag, stored)


def _on_settings_changed(linkage, new_settings):
    if linkage != _MOD_LINKAGE:
        return
    try:
        _apply_panel_settings(new_settings)
    except Exception:
        LOG.exc('_on_settings_changed failed')


def _on_button_clicked(linkage, var_name, value):
    if linkage != _MOD_LINKAGE:
        return
    try:
        importer.handle_button(var_name, value)
    except Exception:
        LOG.exc('_on_button_clicked failed')


def register(account_id):
    """Publishes the panel. Called by mod_auto_equip once the account id is
    known - never before, because the import half of the panel is built from
    that account's save files."""
    try:
        from gui.modsSettingsApi import g_modsSettingsApi, templates
    except Exception:
        LOG.info('ModsSettingsAPI not installed - settings panel disabled')
        return

    importer.prepare(account_id)
    template = {
        'modDisplayName': t('modDisplayName'),
        'enabled': True,
        'column1': _checkbox_rows(templates),
    }
    import_rows = importer.panel_rows(templates)
    if import_rows:
        # Left as the settings column alone when there is nothing to import,
        # rather than shipping an empty second column.
        template['column2'] = import_rows
    try:
        # Unconditionally, rather than only for a first-time registration: this
        # is what carries a template that grew a row since the last version
        # through to the panel, and the mod's own config file is the source of
        # truth for the values anyway.
        g_modsSettingsApi.setModTemplate(_MOD_LINKAGE, template,
                                         _on_settings_changed, _on_button_clicked)
        _apply_panel_settings(g_modsSettingsApi.getModSettings(_MOD_LINKAGE, template))
        LOG.info('registered settings panel (accountId=%s)' % account_id)
    except Exception:
        LOG.exc('settings panel registration failed')
