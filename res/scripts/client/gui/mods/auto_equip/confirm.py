# -*- coding: utf-8 -*-
"""The one question this mod ever asks the player: may a device be installed
that cannot be taken off again for free?

It is asked in the notification centre, with an accept and a decline button -
the shape squad invites and clan applications use - because a RUN cannot ask
anything. A run is an adisp chain behind the modal hangar veil, so by the time
the player could reach a button the chain would have been waiting for minutes.
Instead the run holds that device back like any other skip, finishes everything
that costs nothing, and only then is the question put. Accepting starts a
second, short run that installs exactly the devices that notification named.

Nothing here is remembered. Either button removes the notification and forgets
it, and the next run that reaches the same device asks again - the answer is
about one install, not about a device. The single exception is that a question
the player has NOT answered yet is not asked twice: selecting the same vehicle
again re-runs the whole apply, and without that check every visit would add
another identical notification.

Three client pieces make it work, all wired up in register():

  * a client message TYPE of our own (_MSG_TYPE) plus a formatter for it, so
    serviceChannel.pushClientMessage builds a notification we control;
  * `buttonsLayout` on the formatted message - the same list the client's own
    WGNC pop-ups use: one 'submit' entry and one 'cancel' entry, each naming an
    action;
  * an ActionHandler per action name, which is what the notification centre
    calls when its button is clicked.
"""

import BigWorld

from gui.shared.notifications import (NotificationGuiSettings,
                                      NotificationPriorityLevel)
from gui.shared.system_factory import (MESSENGER_CLIENT_FORMATTERS,
                                       registerMessengerClientFormatter,
                                       registerNotificationsActionsHandlers)
from helpers import dependency
from messenger import g_settings
from messenger.formatters.service_channel_helpers import MessageData
from notification import NotificationMVC
from notification.settings import NOTIFICATION_TYPE
from skeletons.gui.system_messages import ISystemMessages

from . import messages
from .i18n import t
from .log import LOG

try:
    from notification.actions_handlers import ActionHandler
except ImportError:
    # Clients from before the class was made public only had the private name.
    from notification.actions_handlers import _ActionHandler as ActionHandler

# A client message type of our own. The client's own SCH_CLIENT_MSG_TYPE values
# are small ints (0..~30) and one mod in the wild uses 10001, so this sits well
# away from both: the formatter registry is keyed by this number alone, and two
# mods picking the same one would fight over who formats the message.
_MSG_TYPE = 774201

_ACTION_INSTALL = 'z4imonAutoEquipConfirmPaidInstall'
_ACTION_CANCEL = 'z4imonAutoEquipDeclinePaidInstall'

# Devices listed by name; anything beyond that is counted instead. The
# notification centre truncates a long body, and one batch run over the whole
# garage can hold back more devices than fit.
_MAX_LISTED = 8

# clientID of a pushed notification -> [(vehicle inv id, device cd)] it asks
# about. Session state only, and deliberately so: an unanswered question dies
# with the client, exactly like the notification it belongs to.
_pending = {}

_registered = False


# ---------------------------------------------------------------------------
# The notification itself
# ---------------------------------------------------------------------------

class _PaidInstallFormatter(object):
    """Turns the dict pushClientMessage was handed into a notification VO.

    The client calls this as format(message, auxData). auxData is unused: this
    mod puts everything the notification needs into `message`."""

    def format(self, data, *_args):
        if not isinstance(data, dict):
            return []
        formatted = g_settings.msgTemplates.format(
            'WarningHeaderSysMessage',
            ctx={'header': data.get('header', ''),
                 'text': data.get('text', '')})
        # Every template declares an empty buttonsLayout, so filling it in
        # AFTER the format call is what puts our two buttons on the message -
        # the same step the client takes for its own WGNC pop-ups.
        formatted['buttonsLayout'] = [
            {'type': 'submit',
             'label': data.get('installLabel', ''),
             'action': _ACTION_INSTALL},
            {'type': 'cancel',
             'label': data.get('cancelLabel', ''),
             'action': _ACTION_CANCEL}]
        settings = NotificationGuiSettings(
            isNotify=True, priorityLevel=NotificationPriorityLevel.HIGH)
        return [MessageData(formatted, settings)]


class _ConfirmHandler(ActionHandler):
    """The accept button: install the devices this notification named."""

    @classmethod
    def getNotType(cls):
        return NOTIFICATION_TYPE.MESSAGE

    @classmethod
    def getActions(cls):
        return (_ACTION_INSTALL,)

    def handleAction(self, model, entityID, action):
        super(_ConfirmHandler, self).handleAction(model, entityID, action)
        items = _take_pending(model, entityID)
        if not items:
            LOG.warning('the confirmed notification %s is no longer known - '
                        'nothing installed' % entityID)
            return
        # Off the click: this runs inside the notification centre's own button
        # dispatch, and a whole install run has no business happening there.
        BigWorld.callback(0.0, lambda: _start_confirmed(items))


class _DeclineHandler(ActionHandler):
    """The decline button: removes the question and forgets it. Nothing is
    installed, and nothing is remembered for next time."""

    @classmethod
    def getNotType(cls):
        return NOTIFICATION_TYPE.MESSAGE

    @classmethod
    def getActions(cls):
        return (_ACTION_CANCEL,)

    def handleAction(self, model, entityID, action):
        super(_DeclineHandler, self).handleAction(model, entityID, action)
        _take_pending(model, entityID)


_HANDLERS = (_ConfirmHandler, _DeclineHandler)


def _start_confirmed(items):
    """apply is imported HERE, not at module level: accepting starts a run, so
    this module needs apply - while apply needs this one to ask the question at
    all. Closing that cycle at load time leaves whichever module the client
    imported first holding a half-built one."""
    try:
        from . import apply as apply_engine
        apply_engine.apply_confirmed(items)
    except Exception:
        LOG.exc('could not start the confirmed install run')


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------

def ask(items):
    """Asks about everything one run held back, as ONE notification.

    `items` is [(vehicle inv id, vehicle name, device cd, device name)].
    Returns True when the question reached the notification centre. When it did
    not, the player still gets a plain warning naming the devices: a silently
    held-back device looks exactly like a mod that forgot it."""
    if not items:
        return False
    _forget_closed()
    pairs = [(int(inv_id), int(device_cd))
             for inv_id, _veh_name, device_cd, _device_name in items]
    if _already_asking(pairs):
        LOG.info('the same paid-install question is already waiting for an '
                 'answer - not asking twice')
        return True
    lines = _lines(items)
    if _push(pairs, lines):
        return True
    messages.push_warning(u'<br/>'.join([t('confirmPaidHeader')] + lines
                                        + [t('confirmPaidFallback')]))
    return False


def _lines(items):
    lines = [t('confirmPaidLine', name=device_name, veh=veh_name)
             for _inv_id, veh_name, _device_cd, device_name in items[:_MAX_LISTED]]
    if len(items) > _MAX_LISTED:
        lines.append(t('confirmPaidMore', count=len(items) - _MAX_LISTED))
    return lines


def _push(pairs, lines):
    try:
        service_channel = dependency.instance(ISystemMessages).proto.serviceChannel
        client_id = service_channel.pushClientMessage(
            {'header': t('confirmPaidHeader'),
             'text': u'<br/>'.join(lines + [u'', t('confirmPaidQuestion')]),
             'installLabel': t('confirmPaidInstall'),
             'cancelLabel': t('confirmPaidCancel')},
            _MSG_TYPE)
    except Exception:
        LOG.exc('could not push the paid-install confirmation')
        return False
    if not client_id:
        # pushClientMessage answers 0 when no formatter was found for the type,
        # which is the one failure that leaves no traceback of its own.
        LOG.warning('the notification centre did not take the paid-install '
                    'confirmation (registered=%s)' % _registered)
        return False
    _pending[client_id] = pairs
    LOG.info('asking about %d paid install(s), notification %s'
             % (len(pairs), client_id))
    return True


def _already_asking(pairs):
    wanted = sorted(pairs)
    return any(sorted(pending) == wanted for pending in _pending.itervalues())


def _take_pending(model, entity_id):
    """Ends one question: removes its notification and returns what it asked
    about. Both buttons come through here - answering either way is what makes
    the next run ask again."""
    items = _pending.pop(entity_id, None)
    _remove_notification(model, entity_id)
    return items or []


def _forget_closed():
    """Drops questions whose notification is gone. The player can clear one from
    the centre without touching either button, and a leftover entry would make
    _already_asking suppress the question for the rest of the session."""
    model = _model()
    if model is None:
        return
    for entity_id in list(_pending):
        try:
            if not model.hasNotification(NOTIFICATION_TYPE.MESSAGE, entity_id):
                del _pending[entity_id]
        except Exception:
            LOG.exc('could not check notification %s' % entity_id)


def _model():
    try:
        return NotificationMVC.g_instance.getModel()
    except Exception:
        LOG.exc('no notification model')
        return None


def _remove_notification(model, entity_id):
    if model is None:
        model = _model()
    if model is None:
        return
    try:
        if model.hasNotification(NOTIFICATION_TYPE.MESSAGE, entity_id):
            model.removeNotification(NOTIFICATION_TYPE.MESSAGE, entity_id)
    except Exception:
        LOG.exc('could not remove notification %s' % entity_id)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register():
    """Wires the formatter and both button handlers into the client. Idempotent,
    because init() has to survive being run twice."""
    global _registered
    if _registered:
        return
    try:
        registerMessengerClientFormatter(_MSG_TYPE, _PaidInstallFormatter())
        registerNotificationsActionsHandlers(list(_HANDLERS))
        # That call only feeds the collector NotificationsActionsHandlers reads
        # WHEN IT IS BUILT. If the notification centre already exists by now
        # that has happened, so the handlers have to go into the live table too.
        _install_into_live_table()
        _registered = True
        LOG.info('paid-install confirmation registered (msgType=%s)' % _MSG_TYPE)
    except Exception:
        LOG.exc('could not register the paid-install confirmation')


def unregister():
    global _registered
    _pending.clear()
    _remove_from_live_table()
    _drop_formatter()
    _registered = False


def _live_handler_table():
    """The notification centre's built action table, or None while it does not
    exist yet. Private on both hops on purpose: the client offers no public way
    to add a handler once that table has been built."""
    try:
        mvc = getattr(NotificationMVC, 'g_instance', None)
        handlers = getattr(mvc, '_NotificationMVC__actionsHandlers', None)
        return getattr(handlers, '_NotificationsActionsHandlers__single', None)
    except Exception:
        LOG.exc('could not reach the notification action table')
        return None


def _install_into_live_table():
    table = _live_handler_table()
    if table is None:
        return
    for handler in _HANDLERS:
        table[NOTIFICATION_TYPE.MESSAGE, handler.getActions()[0]] = handler


def _remove_from_live_table():
    """Removes only what is still ours - another mod may have claimed the same
    action name in the meantime, and taking its handler out would break it."""
    table = _live_handler_table()
    if table is None:
        return
    for handler in _HANDLERS:
        key = (NOTIFICATION_TYPE.MESSAGE, handler.getActions()[0])
        try:
            if table.get(key) is handler:
                table.pop(key, None)
        except Exception:
            LOG.exc('could not remove the action handler for %s' % (key,))


def _drop_formatter():
    """system_factory can ADD a collector listener but not remove one, and it
    does not hand back the closure it registered - so the whole listener list
    for OUR message type goes. Safe precisely because that key carries this
    mod's own number and nothing else can be listening on it."""
    try:
        from gui.shared import system_factory
        collector = getattr(system_factory, '__collectEM', None)
        handlers = getattr(collector, 'handlers', None)
        if handlers is None:
            return
        handlers.pop((MESSENGER_CLIENT_FORMATTERS, _MSG_TYPE), None)
    except Exception:
        LOG.exc('could not drop the paid-install message formatter')
