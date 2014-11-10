import shlex
import subprocess

from tornado.concurrent import Future
from tornado.gen import coroutine
from tornado.process import Subprocess


class Module(object):
    # Override in modules for custom startup and shutdown

    def startup(self):
        pass

    def shutdown(self):
        pass

    # Usage self.say(scope, 'Hello, world!')
    def say(self, scope, message):
        scope.net.privmsg(scope.target, message)

    # Usage: yield self.wait(2.5)
    def wait(self, seconds):
        future = Future()
        handle = self.ioloop.call_later(seconds, future.set_result, None)

        self._timeouts.add(handle)
        future.add_done_callback(lambda f: self._timeouts.remove(handle))

        return future

    # Usage: self.call_later(2.5, self.func, arg1, arg2)
    def call_later(self, seconds, func, *args, **kwds):
        future = Future()
        handle = self.ioloop.call_later(seconds, future.set_result, None)

        self._timeouts.add(handle)
        future.add_done_callback(lambda f: func(*args, **kwds))
        future.add_done_callback(lambda f: self._timeouts.remove(handle))

    @coroutine
    def call_subprocess(self, cmd, stdin_data=None, stdin_async=True):
        stdin = Subprocess.STREAM if stdin_async else subprocess.PIPE

        proc = Subprocess(
            shlex.split(cmd),
            stdin=stdin,
            stdout=Subprocess.STREAM,
            stderr=Subprocess.STREAM,
        )

        if stdin_data:
            if stdin_async:
                yield proc.stdin.write(stdin_data)
            else:
                proc.stdin.write(stdin_data)

        if stdin_async or stdin_data:
            proc.stdin.close()

        stdout, stderr = yield [
            proc.stdout.read_until_close(),
            proc.stderr.read_until_close(),
        ]

        return stdout, stderr

    # Internals

    def __init__(self, bot, config, logger):
        self.bot = bot
        self.ioloop = bot.ioloop
        self.config = config
        self.log = logger

        self._timeouts = set()

    def _startup(self):
        self.startup()

    def _shutdown(self):
        self._cancel_timeouts()
        self.shutdown()

    def _cancel_timeouts(self):
        for handle in self._timeouts:
            self.ioloop.remove_timeout(handle)
        self._timeouts = None
