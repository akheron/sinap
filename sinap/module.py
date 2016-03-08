from functools import partial
import asyncio
import shlex
import subprocess

from sinap.scope import Scope


class Module(object):
    # Override in modules for custom startup and shutdown

    def startup(self):
        pass

    def shutdown(self):
        pass

    # Usage: self.say(scope, 'Hello, World!')
    def say(self, scope, message):
        return scope.net.privmsg(scope.target, message)

    # Usage: self.say_to('network_name', '#channel', 'Hello, World!')
    # Usage: self.say_to('network_name', 'nick', 'Hello, World!')
    def say_to(self, network_name, target, message):
        return self.say(self.make_scope(network_name, target), message)

    def make_scope(self, network_name, target):
        network = self.bot.networks.get(network_name)
        if network is None:
            raise ValueError('No such network: %s' % network_name)

        return Scope(network, None, target, raw=True)

    # Usage: await self.wait(2.5)
    def wait(self, seconds):
        future = asyncio.Future()
        handle = self.loop.call_later(seconds, partial(future.set_result, None))

        self._timeouts.add(handle)
        future.add_done_callback(lambda f: self._timeouts.remove(handle))

        return future

    # Usage: self.call_later(2.5, self.func, arg1, arg2)
    def call_later(self, seconds, func, *args, **kwds):
        def inner():
            func(*args, **kwds)
            self._timeouts.remove(handle)

        handle = self.loop.call_later(seconds, inner)
        self._timeouts.add(handle)

        return handle

    # Pass the return value of call_later() to cancel the timeout
    def cancel_timeout(self, handle):
        handle.cancel()
        self._timeouts.remove(handle)

    async def call_subprocess(self, cmd, stdin_data=None, stdin_async=True, **kwds):
        proc = await asyncio.create_subprocess_exec(
            *shlex.split(cmd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            loop=self.loop,
            **kwds
        )

        # TODO: This probably only works for small amounts of
        # stdin/stdout data. If the stdin read buffer and stdout write
        # buffer fill up at the same time, we have a deadlock.

        if stdin_data:
            proc.stdin.write(stdin_data)
            await proc.stdin.drain()
        proc.stdin.close()

        stdout = await proc.stdout.read()
        stderr = await proc.stderr.read()
        status = await proc.wait()

        return status, stdout, stderr

    # Internals

    def __init__(self, bot, config, logger):
        self.bot = bot
        self.loop = bot.loop
        self.config = config
        self.log = logger

        self._timeouts = set()

    def _startup(self):
        result = self.startup()
        if asyncio.iscoroutine(result):
            self.loop.create_task(result)

    def _shutdown(self):
        self._cancel_timeouts()
        self.shutdown()

    def _cancel_timeouts(self):
        for handle in self._timeouts:
            handle.cancel()
        self._timeouts = set()
