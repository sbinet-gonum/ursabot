# Copyright 2019 RStudio, Inc.
# All rights reserved.
#
# Use of this source code is governed by a BSD 2-Clause
# license that can be found in the LICENSE_BSD file.
#
# This file contains function or sections of code that are marked as being
# derivative works of Buildbot. The above license only applies to code that
# is not marked as such.

from twisted.internet import threads

from buildbot.plugins import steps, util
from buildbot.process import buildstep
from buildbot.process.results import SUCCESS, FAILURE
from buildbot.steps.worker import CompositeStepMixin
from buildbot.interfaces import IRenderable

from .utils import ensure_deferred


class ResultLogMixin(buildstep.BuildStep, CompositeStepMixin):
    """Saves the content of a json file as `log` with name `result`

    Only suitable for saving small amount of data, main purpose to save the
    machine formatted results to be used from reporters. Only jsonlines are
    supported.
    Currently only BenchmarkCommentFormatter uses it. It looks for steps with
    `result` logs and creates a comment with a markdown formatted version of
    `result_file`.

    Parameters
    ----------
    result_file : str, default None
        Path to a jsonlines file, containing a machine formatted "result" of
        the step. If omitted no result is saved.
    """

    def __init__(self, result_file=None, **kwargs):
        self.result_file = result_file
        super().__init__(**kwargs)

    @ensure_deferred
    async def run(self):
        result = await super().run()

        if result == SUCCESS and self.result_file is not None:
            # retrieve the file's content from the worker and ensure its format
            content = await self.getFileContentFromWorker(self.result_file)

            # save name under `result` log
            log = await self.addLog('result', type='t')
            await log.addContent(content)
            await log.finish()

        return result


class ShellMixin(buildstep.ShellMixin):
    """Run command in a login bash shell

    ShellCommand uses the old-style API but commands like CMake uses the
    new style, ShellMixin based API. Buildbot runs each command with a
    non-login /bin/sh, thus .bashrc is not loaded.

    The primary purpose of this mixin to use with conda environments.
    """

    shell = tuple()  # will run sh on unix and batch on windows by default
    command = tuple()
    args = tuple()

    def makeRemoteShellCommand(self, **kwargs):
        import pipes  # only available on unix

        # copied from buildbot_worker.runprocess
        def quote(e):
            """Quote shell arguments

            License note:
                Copied from buildbot_worker.runprocess in the original buildbot
                implementation.
            """
            if not e:
                return '""'
            return pipes.quote(e)

        # follow the semantics of the parent method, but don't flatten
        # command = self.command + kwargs.pop('command', tuple())
        command = kwargs.pop('command')
        if not isinstance(command, (tuple, list)):
            raise ValueError('Command must be an instance of list or tuple')

        if self.shell:
            # render the command and prepend with the shell
            # TODO(kszucs): validate self.shell
            command = ' '.join(map(quote, command))
            command = tuple(self.shell) + (command,)

        return super().makeRemoteShellCommand(command=command, **kwargs)


class ShellCommand(ShellMixin, buildstep.BuildStep):

    name = 'Shell'

    def __init__(self, args=tuple(), command=tuple(), **kwargs):
        command, args = command or self.command, args or self.args

        if not IRenderable.providedBy(command) and not command:
            raise ValueError('No command was provided')

        kwargs['command'] = util.FlattenList([command, args])
        kwargs = self.setupShellMixin(kwargs)
        super().__init__(**kwargs)

    @ensure_deferred
    async def run(self):
        cmd = await self.makeRemoteShellCommand(command=self.command)
        await self.runCommand(cmd)
        return cmd.results()


class CMake(ShellMixin, steps.CMake):

    name = 'CMake'

    @ensure_deferred
    async def run(self):
        """Create and run CMake command

        License note:
            Copied from the original buildbot implementation to handle None
            values as missing ones.
        """
        command = [self.cmake]

        if self.generator:
            command.extend(['-G', self.generator])
        if self.path:
            command.append(self.path)

        if self.definitions is not None:
            for k, v in self.definitions.items():
                # handle None values as missing
                if v is not None:
                    command.append(f'-D{k}={v}')

        if self.options is not None:
            command.extend(self.options)

        cmd = await self.makeRemoteShellCommand(command=command)
        await self.runCommand(cmd)

        return cmd.results()


class SetPropertiesFromEnv(buildstep.BuildStep):
    """Sets properties from environment variables on the worker."""

    name = 'SetPropertiesFromEnv'
    description = ['Setting']
    descriptionDone = ['Set']

    def __init__(self, variables, source='WorkerEnvironment', **kwargs):
        self.variables = variables
        self.source = source
        super().__init__(**kwargs)

    @ensure_deferred
    async def run(self):
        """Set build properties read from the worker's environment

        License note:
            Copied from the original buildbot implementation and ported as a
            new-style buildstep.
        """
        # on Windows, environment variables are case-insensitive, but we have
        # a case-sensitive dictionary in worker_environ.  Fortunately, that
        # dictionary is also folded to uppercase, so we can simply fold the
        # variable names to uppercase to duplicate the case-insensitivity.
        fold_to_uppercase = (self.worker.worker_system == 'win32')

        properties = self.build.getProperties()
        environ = self.worker.worker_environ

        log = []
        for prop, var in self.variables.items():
            if fold_to_uppercase:
                var = var.upper()

            value = environ.get(var, None)
            if value:
                # note that the property is not uppercased
                properties.setProperty(prop, value, self.source, runtime=True)
                log.append(f'{prop} = {value}')

        await self.addCompleteLog('properties', '\n'.join(log))

        return SUCCESS


# TODO(kszucs): this function is executed on the master's side, to execute
# remote functions use cloudpickle
class PythonFunction(buildstep.BuildStep):
    """Executes arbitrary python function."""

    name = 'PythonFunction'
    description = ['Executing']
    descriptionDone = ['Executed']

    def __init__(self, fn, **kwargs):
        self.fn = fn
        super().__init__(**kwargs)

    @ensure_deferred
    async def run(self):
        try:
            result = await threads.deferToThread(self.fn)
        except Exception as e:
            await self.addLogWithException(e)
            return FAILURE
        else:
            await self.addCompleteLog('result', result)
            return SUCCESS


class Env(ShellCommand):
    name = 'Environment'
    command = ['env']


class Ninja(ShellCommand):
    # TODO(kszucs): add proper descriptions
    name = 'Ninja'
    command = ['ninja']


class CTest(ShellCommand):
    name = 'CTest'
    command = ['ctest']


class SetupPy(ShellCommand):
    name = 'Setup.py'
    command = ['python', 'setup.py']


class PyTest(ShellCommand):
    name = 'PyTest'
    command = ['pytest', '-v']


class Pip(ShellCommand):
    name = 'Pip'
    command = ['pip']


Mkdir = steps.MakeDirectory
GitHub = steps.GitHub


class Archery(ResultLogMixin, ShellCommand):
    name = 'Archery'
    command = ['archery']
    env = dict(LC_ALL='C.UTF-8', LANG='C.UTF-8')  # required for click


class Crossbow(ResultLogMixin, ShellCommand):
    name = 'Crossbow'
    command = ['python', 'crossbow.py']
    env = dict(LC_ALL='C.UTF-8', LANG='C.UTF-8')  # required for click


class Maven(ShellCommand):
    name = 'Maven'
    command = ['mvn']


class Go(ShellCommand):
    name = 'Go'
    command = ['go']
