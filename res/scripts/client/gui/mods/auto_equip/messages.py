# -*- coding: utf-8 -*-
"""Everything this mod shows the player outside its own popover: system
messages in the notification centre, and the blocking hangar veil."""

from .log import LOG

# The native veil text key - reuses the client's own "Mounting equipment..."
_WAITING_KEY = 'installEquipment'


def _push(text, type_name, priority):
    """type_name is an attribute of SystemMessages.SM_TYPE. Both it and the
    module are resolved here so nothing else has to import the client's
    message plumbing."""
    try:
        from gui import SystemMessages
        SystemMessages.pushMessage(text,
                                   type=getattr(SystemMessages.SM_TYPE, type_name),
                                   priority=priority)
    except Exception:
        LOG.exc('could not push system message')


def push_info(text, priority=None):
    _push(text, 'Information', priority)


def push_warning(text, priority=None):
    _push(text, 'Warning', priority)


def push_error(text, priority=None):
    _push(text, 'Error', priority)


def push_lines(lines, warning=False):
    """One message built from several lines - how every run summary is shown."""
    text = u'<br/>'.join(lines)
    if warning:
        push_warning(text)
    else:
        push_info(text)


def show_waiting():
    """Shows the blocking grey hangar veil. Returns True when it actually
    appeared, so the caller knows a matching hide_waiting() is required."""
    try:
        from gui.Scaleform.Waiting import Waiting
        Waiting.show(_WAITING_KEY)
        return True
    except Exception:
        LOG.exc('show_waiting failed')
        return False


def hide_waiting():
    try:
        from gui.Scaleform.Waiting import Waiting
        Waiting.hide(_WAITING_KEY)
    except Exception:
        LOG.exc('hide_waiting failed')
