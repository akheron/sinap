from fnmatch import fnmatch
from functools import partial
from getpass import getuser
from io import StringIO
import inspect
import logging
import re

from tornado.ioloop import IOLoop
from tornado.iostream import StreamClosedError
from tornado.tcpclient import TCPClient
from tornado.concurrent import Future
from tornado.gen import coroutine


class Message(object):
    __slots__ = ['prefix', 'command', 'reply', 'args']

    def __init__(self, prefix, command, args):
        self.prefix = prefix
        self.command = command
        self.args = args

    @property
    def is_reply(self):
        return len(self.command) == 3 and self.command.isdigit()

    @property
    def is_command(self):
        return not self.is_reply

    def __repr__(self):
        return 'Message(%r, %r, %r)' % (self.prefix, self.command, self.args)


class User(object):
    def __init__(self, nick, user, host):
        self.nick = nick
        self.user = user
        self.host = host

    def is_full(self):
        return self.nick and self.user and self.host

    def matches(self, mask):
        return fnmatch(str(self), mask)

    def __str__(self):
        if self.is_full():
            return '%s!%s@%s' % (self.nick, self.user, self.host)
        else:
            return self.nick


class IRCConnection(object):
    def __init__(self, host, port, nick,
                 password=None,
                 username=None,
                 realname=None,
                 logger=None,
                 delegate=None,
                 io_loop=None):
        self._host = host
        self._port = port
        self._nick = nick

        self._password = password
        if username:
            self._username = username
        else:
            try:
                # Try to get the user's login name
                self._username = getuser()
            except:
                # Fall back to nick
                self._username = nick

        # Fall back to nick
        self._realname = realname or nick

        self.log = logger or logging.getLogger(__name__)
        self._delegate = delegate
        self._ioloop = io_loop or IOLoop.instance()
        self._conn = None

        self._message_listeners = []
        self._disconnect_future = Future()

    @coroutine
    def connect(self):
        self._conn = yield TCPClient(io_loop=self._ioloop) \
            .connect(self._host, self._port)
        self._ioloop.add_callback(self.read_loop)
        yield self.register()

    @coroutine
    def register(self):
        if self._password:
            yield self.pass_(self._password)

        registered = False
        while True:
            yield self.nick(self._nick)

            if not registered:
                yield self.user(self._username, '8', self._realname)
                registered = True

            msg = yield self.wait_for_message(['001', '433'])
            if msg.command == '001':
                # RPL_WELCOME
                break
            elif msg.command == '433':
                # ERR_NICKNAMEINUSE
                self._nick += '_'

    @coroutine
    def send_message(self, command, *args, prefix=None):
        # Strip trailing Nones from args to make it easier to deal
        # with optional command arguments
        while args and args[-1] is None:
            args = args[:-1]

        data = StringIO()

        if prefix is not None:
            data.write(':%s ' % prefix)

        data.write(command)

        if args:
            data.write(' ')

            args, last_arg = args[:-1], args[-1]
            if args:
                data.write(' '.join(args))
                data.write(' ')

            if ' ' in last_arg:
                data.write(':%s' % last_arg)
            else:
                data.write(last_arg)

        line = data.getvalue()
        self.log.debug('>>> %s' % line)
        yield self._conn.write(line.encode('utf-8') + b'\r\n')

    @coroutine
    def wait_for_message(self, commands=None):
        while True:
            future = Future()
            self._message_listeners.append(future)
            msg = yield future

            if commands is None:
                # Accept any message
                return msg
            else:
                if msg.command in commands:
                    # Accept if it's one of the requested commands
                    return msg

    def wait_for_disconnect(self):
        return self._disconnect_future

    @coroutine
    def read_loop(self):
        # Tornado seems to run futures at least a few ioloop
        # iterations after read_until callbacks. If we have many lines
        # ready to be read from the socket, this results in
        # wait_for_message futures to only be resolved for the first
        # line.
        #
        # Se let's read the next line only after futures' callbacks
        # have been called. This is achieved by yielding on the wait
        # future ourselves.

        while self._conn:
            wait = Future()
            self._ioloop.add_callback(wait.set_result, True)
            yield wait

            try:
                data = yield self._conn.read_until(b'\r\n')
            except StreamClosedError:
                self._conn = None
            else:
                self.process_message(data)

        self._disconnect_future.set_result((self._host, self._port))
        self._disconnect_future = Future()

    def process_message(self, data):
        line = data[:-2].decode('utf-8', 'replace')
        self.log.debug('<<< %s' % line)

        try:
            msg = self.parse_message(line)
        except ValueError:
            self.log.warning('Invalid message from server: %s' % data)
            return

        for future in self._message_listeners:
            future.set_result(msg)
        del self._message_listeners[:]

        self._ioloop.add_callback(self.handle_message, msg)
        if msg.is_command:
            self._ioloop.add_callback(self.handle_command, msg)

            # Call the command specific handler if any
            for handler_name, handler in self.handlers_for_command(msg):
                sig = inspect.signature(handler)
                if len(sig.parameters) == len(msg.args) + 1:
                    self._ioloop.add_callback(handler, msg.prefix, *msg.args)
                else:
                    self.log.warning('''\
Command handler signature does not match the command sent by server.
Singature: %s%s
Command: %s''' % (handler_name, sig, msg))
        else:
            self._ioloop.add_callback(self.handle_reply, msg)

    def parse_message(self, data):
        if data.startswith(':'):
            # Has prefix
            part, data = data.split(' ', 1)
            prefix = part[1:]
        else:
            prefix = None

        if ' ' not in data:
            # No args
            return Message(prefix, data, [])

        cmd, data = data.split(' ', 1)

        if data.startswith(':'):
            return Message(prefix, cmd, [data[1:]])

        if ' :' in data:
            data, trailing = data.split(' :', 1)
        else:
            trailing = None

        args = data.split(' ')
        if trailing:
            args.append(trailing)

        return Message(prefix, cmd, args)

    def handlers_for_command(self, msg):
        handler_name = 'on_%s' % msg.command.lower()
        if hasattr(self, handler_name):
            yield handler_name, getattr(self, handler_name)
        if self._delegate and hasattr(self._delegate, handler_name):
            handler = getattr(self._delegate, handler_name)
            yield handler_name, partial(handler, self)

    USER_RE = re.compile('^(?P<nick>[^!]+)(!(?P<user>[^@]+)@(?P<host>.*))?$')

    def parse_user(self, text):
        match = self.USER_RE.match(text)
        if match:
            return User(*(match.group(x) for x in ('nick', 'user', 'host')))

    def is_channel(self, name):
        return name.startswith(('&', '#', '+', '!'))

    # Command helpers

    def pass_(self, password):
        return self.send_message('PASS', password)

    def nick(self, nick):
        return self.send_message('NICK', nick)

    def user(self, username, mask, realname):
        return self.send_message('USER', username, mask, '*', realname)

    def quit(self, message=None):
        return self.send_message('QUIT', message)

    def join(self, channel, key=None):
        return self.send_message('JOIN', channel, key)

    def part(self, channel, message=None):
        return self.send_message('PART', channel, message)

    def privmsg(self, target, message):
        return self.send_message('PRIVMSG', target, message)

    # Generic message handlers

    def handle_message(self, message):
        pass

    def handle_command(self, message):
        pass

    def handle_reply(self, message):
        pass

    # Command handlers
    #
    # Each handler will receive prefix and zero or more command
    # arguments as its arguments. Depending on the command, prefix may
    # be None.
    #
    # If there's an argument count mismatch with the server's command
    # and the number of arguments expected by the handler, a warning
    # is logged and the handler is not called.

    def on_ping(self, prefix, *args):
        self.send_message('PONG', *args)
