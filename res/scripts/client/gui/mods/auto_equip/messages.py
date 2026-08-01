# -*- coding: utf-8 -*-
"""Everything this mod shows the player outside its own popover: system
messages in the notification centre, and the blocking hangar veil."""

from gui import SystemMessages
from gui.Scaleform.Waiting import Waiting

from .log import LOG

# Native veil text keys. Waiting.show() resolves these through the client's own
# R.strings.waiting and accepts nothing else, so a mod cannot name its own text
# here - see show_waiting() for how we get our wording in anyway.
_WAITING_KEY = 'installEquipment'          # "Mounting equipment..."
WAITING_KEY_SERVICE = 'techMaintenance'    # "Re-equipping the vehicle..."


def _push(text, type_name, priority):
    """type_name is an attribute of SystemMessages.SM_TYPE, resolved here so
    nothing else has to know the client's message plumbing."""
    try:
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


def show_waiting(key=_WAITING_KEY, text=None):
    """Shows the blocking grey hangar veil. Returns True when it actually
    appeared, so the caller knows a matching hide_waiting(key) is required.

    `text` puts our own wording on it. Note what does NOT depend on that: the
    veil comes up through Waiting.show() alone, so if the relabelling ever
    stops working the player still gets the veil - just with the client's text
    for `key`. Pick a key whose own wording is an acceptable fallback."""
    try:
        Waiting.show(key)
    except Exception:
        LOG.exc('show_waiting failed')
        return False
    if text:
        _relabel_waiting(text)
    return True


def _relabel_waiting(text):
    """Swaps the veil's label for a string of ours.

    Waiting.show() only ever names a client resource id. The view resolves that
    id and hands Flash a plain string (WaitingView.showWaiting -> backport.text
    -> as_showWaitingS), so calling as_showWaitingS ourselves with the text
    already rendered is the same call one step later."""
    try:
        view = Waiting.getWaitingView(True)
        if view is None:
            LOG.warning('no waiting view to relabel - keeping the default text')
            return
        view.as_showWaitingS(text, False, True)
    except Exception:
        LOG.exc('could not relabel the waiting veil - keeping the default text')


def hide_waiting(key=_WAITING_KEY):
    try:
        Waiting.hide(key)
    except Exception:
        LOG.exc('hide_waiting failed')
