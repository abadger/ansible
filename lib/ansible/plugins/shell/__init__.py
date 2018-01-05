# (c) 2016 RedHat
#
# This file is part of Ansible.
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
from __future__ import (absolute_import, division, print_function)
__metaclass__ = type

import os
import re
import time
import random

from ansible.module_utils.six import text_type
from ansible.module_utils.six.moves import shlex_quote
from ansible.plugins import AnsiblePlugin

_USER_HOME_PATH_RE = re.compile(r'^~[_.A-Za-z0-9][-_.A-Za-z0-9]*$')


class ShellBase(AnsiblePlugin):

    HOMES_RE = re.compile(r'(\'|\")?(~|\$HOME)(.*)')

    def __init__(self):

        super(ShellBase, self).__init__()

        self.env = {}
        self.tempdir = None

    def set_options(self, task_keys=None, var_options=None, direct=None):

        super(ShellBase, self).set_options(task_keys=task_keys, var_options=var_options, direct=direct)

        # not all shell modules have this option
        if self.get_option('set_module_language'):
            self.env.update(
                dict(
                    LANG=self.get_option('module_language'),
                    LC_ALL=self.get_option('module_language'),
                    LC_MESSAGES=self.get_option('module_language'),
                )
            )

        # set env
        self.env.update(self.get_option('environment'))

    def env_prefix(self, **kwargs):
        return ' '.join(['%s=%s' % (k, shlex_quote(text_type(v))) for k, v in kwargs.items()])

    def join_path(self, *args):
        return os.path.join(*args)

    # some shells (eg, powershell) are snooty about filenames/extensions, this lets the shell plugin have a say
    def get_remote_filename(self, pathname):
        base_name = os.path.basename(pathname.strip())
        return base_name.strip()

    def path_has_trailing_slash(self, path):
        return path.endswith('/')

    def chmod(self, paths, mode):
        cmd = ['chmod', mode]
        cmd.extend(paths)
        cmd = [shlex_quote(c) for c in cmd]

        return ' '.join(cmd)

    def chown(self, paths, user):
        cmd = ['chown', user]
        cmd.extend(paths)
        cmd = [shlex_quote(c) for c in cmd]

        return ' '.join(cmd)

    def set_user_facl(self, paths, user, mode):
        """Only sets acls for users as that's really all we need"""
        cmd = ['setfacl', '-m', 'u:%s:%s' % (user, mode)]
        cmd.extend(paths)
        cmd = [shlex_quote(c) for c in cmd]

        return ' '.join(cmd)

    def remove(self, path, recurse=False):
        path = shlex_quote(path)
        cmd = 'rm -f '
        if recurse:
            cmd += '-r '
        return cmd + "%s %s" % (path, self._SHELL_REDIRECT_ALLNULL)

    def exists(self, path):
        cmd = ['test', '-e', shlex_quote(path)]
        return ' '.join(cmd)

    def mkdtemp(self, basefile=None, system=False, mode=0o700, tmpdir=None):
        if not basefile:
            basefile = 'ansible-tmp-%s-%s' % (time.time(), random.randint(0, 2**48))

        # When system is specified we have to create this in a directory where
        # other users can read and access the temp directory.
        # This is because we use system to create tmp dirs for unprivileged users who are
        # sudo'ing to a second unprivileged user.
        # The 'system_temps' setting defines dirctories we can use for this purpose
        # the default are, /tmp and /var/tmp.
        # So we only allow one of those locations if system=True, using the
        # passed in tmpdir if it is valid or the first one from the setting if not.

        if system:
            if tmpdir.startswith(tuple(self.get_option('system_temps'))):
                basetmpdir = tmpdir
            else:
                basetmpdir = self.get_option('system_temps')[0]
        else:
            if tmpdir is None:
                basetmpdir = self.get_option('remote_temp')
            else:
                basetmpdir = tmpdir

        basetmp = self.join_path(basetmpdir, basefile)

        cmd = 'mkdir -p %s echo %s %s' % (self._SHELL_SUB_LEFT, basetmp, self._SHELL_SUB_RIGHT)
        cmd += ' %s echo %s=%s echo %s %s' % (self._SHELL_AND, basefile, self._SHELL_SUB_LEFT, basetmp, self._SHELL_SUB_RIGHT)

        # change the umask in a subshell to achieve the desired mode
        # also for directories created with `mkdir -p`
        if mode:
            tmp_umask = 0o777 & ~mode
            cmd = '%s umask %o %s %s %s' % (self._SHELL_GROUP_LEFT, tmp_umask, self._SHELL_AND, cmd, self._SHELL_GROUP_RIGHT)

        return cmd

    def expand_user(self, user_home_path):
        ''' Return a command to expand tildes in a path

        It can be either "~" or "~username". We just ignore $HOME
        We use the POSIX definition of a username:
            http://pubs.opengroup.org/onlinepubs/000095399/basedefs/xbd_chap03.html#tag_03_426
            http://pubs.opengroup.org/onlinepubs/000095399/basedefs/xbd_chap03.html#tag_03_276
        '''

        # Check that the user_path to expand is safe
        if user_home_path != '~':
            if not _USER_HOME_PATH_RE.match(user_home_path):
                # shlex_quote will make the shell return the string verbatim
                user_home_path = shlex_quote(user_home_path)
        return 'echo "%s\t%spwd%s"' % (user_home_path, self._SHELL_SUB_LEFT, self._SHELL_SUB_RIGHT)

    def build_module_command(self, env_string, shebang, cmd, arg_path=None):
        # don't quote the cmd if it's an empty string, because this will break pipelining mode
        if cmd.strip() != '':
            cmd = shlex_quote(cmd)

        cmd_parts = []
        if shebang:
            shebang = shebang.replace("#!", "").strip()
        else:
            shebang = ""
        cmd_parts.extend([env_string.strip(), shebang, cmd])
        if arg_path is not None:
            cmd_parts.append(arg_path)
        new_cmd = " ".join(cmd_parts)
        return new_cmd

    def append_command(self, cmd, cmd_to_append):
        """Append an additional command if supported by the shell"""

        if self._SHELL_AND:
            cmd += ' %s %s' % (self._SHELL_AND, cmd_to_append)

        return cmd

    def wrap_for_exec(self, cmd):
        """wrap script execution with any necessary decoration (eg '&' for quoted powershell script paths)"""
        return cmd
