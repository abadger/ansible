# (c) 2017, Dag Wieers <dag@wieers.com>
#
# This file is part of Ansible
#
# Ansible is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Ansible is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Ansible.  If not, see <http://www.gnu.org/licenses/>.

# CI-required python3 boilerplate
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import time
from datetime import datetime, timedelta

from ansible.plugins.action import ActionBase

try:
    from __main__ import display
except ImportError:
    from ansible.utils.display import Display
    display = Display()


class TimedOutException(Exception):
    pass


class ActionModule(ActionBase):
    TRANSFERS_FILES = False

    DEFAULT_CONNECT_TIMEOUT = 5
    DEFAULT_DELAY = 0
    DEFAULT_SLEEP = 1
    DEFAULT_TIMEOUT = 600

    def do_until_success_or_timeout(self, what, timeout, connect_timeout, what_desc, sleep=1):
        max_end_time = datetime.utcnow() + timedelta(seconds=timeout)

        e = None
        while datetime.utcnow() < max_end_time:
            try:
                what(connect_timeout)
                if what_desc:
                    display.debug("wait_for_connection: %s success" % what_desc)
                return
            except Exception as e:
                if what_desc:
                    display.debug("wait_for_connection: %s fail (expected), retrying in %d seconds..." % (what_desc, sleep))
                time.sleep(sleep)

        raise TimedOutException("timed out waiting for %s: %s" % (what_desc, e))

    def run(self, tmp=None, task_vars=None):
        if task_vars is None:
            task_vars = dict()

        connect_timeout = int(self._task.args.get('connect_timeout', self.DEFAULT_CONNECT_TIMEOUT))
        delay = int(self._task.args.get('delay', self.DEFAULT_DELAY))
        sleep = int(self._task.args.get('sleep', self.DEFAULT_SLEEP))
        timeout = int(self._task.args.get('timeout', self.DEFAULT_TIMEOUT))

        if self._play_context.check_mode:
            display.vvv("wait_for_connection: skipping for check_mode")
            return dict(skipped=True)

        result = super(ActionModule, self).run(tmp, task_vars)
        tmp = self._connection._shell.tempdir

        def ping_module_test(connect_timeout):
            ''' Test ping module, if available '''
            display.vvv("wait_for_connection: attempting ping module test")
            # call connection reset between runs if it's there
            try:
                self._connection._reset()
            except AttributeError:
                pass

            # Use win_ping on winrm/powershell, else use ping
            if hasattr(self._connection, '_shell_type') and self._connection._shell_type == 'powershell':
                ping_result = self._execute_module(module_name='win_ping', module_args=dict(), tmp=tmp, task_vars=task_vars)
            else:
                ping_result = self._execute_module(module_name='ping', module_args=dict(), tmp=tmp, task_vars=task_vars)

            # Test module output
            if ping_result['ping'] != 'pong':
                raise Exception('ping test failed')

        start = datetime.now()

        if delay:
            time.sleep(delay)

        try:
            # If the connection has a transport_test method, use it first
            if hasattr(self._connection, 'transport_test'):
                self.do_until_success_or_timeout(self._connection.transport_test, timeout, connect_timeout, what_desc="connection port up", sleep=sleep)

            # Use the ping module test to determine end-to-end connectivity
            self.do_until_success_or_timeout(ping_module_test, timeout, connect_timeout, what_desc="ping module test success", sleep=sleep)

        except TimedOutException as e:
            result['failed'] = True
            result['msg'] = str(e)

        elapsed = datetime.now() - start
        result['elapsed'] = elapsed.seconds

        return result
