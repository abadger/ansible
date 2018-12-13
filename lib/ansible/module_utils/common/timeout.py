# Copyright (c) 2018, Ansible Project
# Simplified BSD License (see licenses/simplified_bsd.txt or https://opensource.org/licenses/BSD-2-Clause)

from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import signal

# timeout function to make sure some fact gathering
# steps do not exceed a time limit

GATHER_TIMEOUT = None
DEFAULT_GATHER_TIMEOUT = 10


class TimeoutError(Exception):
    pass


def timeout(seconds=None, error_message="Timer expired"):

    def decorator(func):
        def _handle_timeout(signum, frame):
            msg = 'Timer expired after %s seconds' % globals().get('GATHER_TIMEOUT')
            raise TimeoutError(msg)

        def wrapper(*args, **kwargs):
            local_seconds = seconds
            if local_seconds is None:
                local_seconds = globals().get('GATHER_TIMEOUT') or DEFAULT_GATHER_TIMEOUT
            signal.signal(signal.SIGALRM, _handle_timeout)
            signal.alarm(local_seconds)

            try:
                result = func(*args, **kwargs)
            finally:
                signal.alarm(0)
            return result

        return wrapper

    # If we were called as @timeout, then the first parameter will be the
    # function we are to wrap instead of the number of seconds.  Detect this
    # and correct it by setting seconds to our default value and return the
    # inner decorator function manually wrapped around the function
    if callable(seconds):
        func = seconds
        seconds = None
        return decorator(func)

    # If we were called as @timeout([...]) then python itself will take
    # care of wrapping the inner decorator around the function

    return decorator
