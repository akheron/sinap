from enum import Enum
from fnmatch import fnmatch
from functools import partial
from getpass import getuser
from io import StringIO
import asyncio
import inspect
import logging
import re
import socket


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


class Disconnected(RuntimeError):
    def __str__(self):
        return 'Connection lost'


class IRCProtocol(asyncio.Protocol):
    def __init__(self, message_callback, logger=None, encoding='utf-8'):
        # message_callback is called with each received message, and
        # with None when the connection is lost.
        self._message_callback = message_callback
        self._encoding = encoding
        self.log = logger or logging.getLogger(__name__ + '.protocol')

        self._transport = None
        self._recv_buffer = b''

    def connection_made(self, transport):
        self.log.debug('Connection made')
        self._transport = transport

    def connection_lost(self, exc):
        self.log.debug('Connection lost')
        self._transport = None
        self._message_callback(None)

    def data_received(self, data):
        if not data:
            return

        if self._recv_buffer:
            data = self._recv_buffer + data

        start = 0
        while True:
            newline_pos = data.find(b'\r\n', start)
            if newline_pos == -1:
                # Leave the last line as the next _recv_buffer. If
                # data ended in \r\n, this will be an empty string.
                self._recv_buffer = data[start:]
                break

            line = data[start:newline_pos]
            start = newline_pos + 2  # len(b'\r\n')

            try:
                message = self.parse_message(line.decode(self._encoding))
            except ValueError:
                self.log.warning('Invalid message from server: %s' % data)
            else:
                self._message_callback(message)

    def parse_message(self, data):
        self.log.debug('<<< %s' % data)

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
        self._transport.write(line.encode('utf-8') + b'\r\n')


class IRCConnection(object):
    def __init__(self, host, port, nick,
                 password=None,
                 username=None,
                 realname=None,
                 logger=None,
                 delegate=None,
                 loop=None):
        self.host = host
        self.port = port
        self.nick = nick

        self.channels = {}
        self.safe_channel_prefix = '!'
        self.safe_channel_idlen = 5

        self.password = password
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
        self._loop = loop or asyncio.get_event_loop()
        self._transport = None
        self._protocol = None

        self._message_listeners = []
        self._connect_future = None
        self._disconnect_future = None

    async def connect(self, reuse_fd=None):
        # If reuse_fd is given, it should be a connected file
        # descriptor number
        if reuse_fd is None:
            connect_kwds = {'host': self.host, 'port': self.port}
        else:
            self.log.debug('Reusing fd %d' % reuse_fd)
            connect_kwds = {'sock': socket.fromfd(reuse_fd)}

        self._transport, self._protocol = await self._loop.create_connection(
            lambda: IRCProtocol(self.process_message, self.log),
            **connect_kwds,
        )

        if reuse_fd:
            # Already registered
            return

        # This is a new connection, register with the server
        # asynchronously
        self._connect_future = asyncio.Future()
        self._loop.create_task(self.register())

        await self._connect_future
        self._connect_future = None
        self._disconnect_future = asyncio.Future()

    async def register(self):
        self.log.debug('Registering connection')

        if self.password:
            self.pass_(self.password)

        registered = False
        while True:
            self.nick_(self.nick)

            if not registered:
                self.user(self._username, '8', self._realname)
                registered = True

            try:
                msg = await self.wait_for_message(['001', '433'])
            except Disconnected:
                break

            if msg.command == '001':
                # RPL_WELCOME
                self._connect_future.set_result(None)
                break
            elif msg.command == '433':
                # ERR_NICKNAMEINUSE
                self.nick += '_'

    def disconnect(self):
        if not self._transport:
            return

        self._transport.close()

    async def wait_for_message(self, commands=None):
        while True:
            future = asyncio.Future()
            self._message_listeners.append(future)
            msg = await future

            if commands is None:
                # Accept any message
                return msg
            else:
                if msg.command in commands:
                    # Accept if it's one of the requested commands
                    return msg

    async def wait_for_disconnect(self):
        if not self._disconnect_future:
            raise ValueError('Can only be called after connect() returns')

        await self._disconnect_future

    def process_message(self, msg):
        if msg is None:
            exc = Disconnected()

            # Disconnected -> dropped from channels, too
            self.channels.clear()
            if self._connect_future:
                self._connect_future.set_exception(exc)
            if self._disconnect_future:
                self._disconnect_future.set_result(None)
                self._disconnect_future = None

            for future in self._message_listeners:
                future.set_exception(exc)
            del self._message_listeners[:]

            return

        for future in self._message_listeners:
            future.set_result(msg)
        del self._message_listeners[:]

        for _, handler in self.handlers('handle_message'):
            self._loop.call_soon(handler, msg)

        if msg.is_command:
            self._loop.call_soon(partial(self.handle_command, msg))

            # Call the command specific handler if any
            for handler_name, handler in self.handlers_for_command(msg):
                sig = inspect.signature(handler)
                if len(sig.parameters) == len(msg.args) + 1:
                    self._loop.call_soon(partial(handler, msg.prefix, *msg.args))
                else:
                    self.log.warning('''\
Command handler signature does not match the command sent by server.
Singature: %s%s
Command: %s''' % (handler_name, sig, msg))
        else:
            for _, handler in self.handlers('handle_reply'):
                self._loop.call_soon(partial(handler, msg))

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

    def send_message(self, command, *args, prefix=None):
        self._protocol.send_message(command, *args, prefix=prefix)

    def handlers(self, handler_name):
        if hasattr(self, handler_name):
            yield handler_name, getattr(self, handler_name)
        if self._delegate and hasattr(self._delegate, handler_name):
            handler = getattr(self._delegate, handler_name)
            yield handler_name, partial(handler, self)

    def handlers_for_command(self, msg):
        handler_name = 'on_%s' % msg.command.lower()
        yield from self.handlers(handler_name)

    USER_RE = re.compile('^(?P<nick>[^!]+)(!(?P<user>[^@]+)@(?P<host>.*))?$')

    def parse_user(self, text):
        match = self.USER_RE.match(text)
        if match:
            return User(*(match.group(x) for x in ('nick', 'user', 'host')))

    def is_channel(self, name):
        return name.startswith(('&', '#', '+', '!'))

    def is_safe_channel(self, name):
        return name.startswith(self.safe_channel_prefix)

    def parse_channel_name(self, name):
        if self.is_safe_channel(name):
            if len(name) >= self.safe_channel_idlen + 2:
                return name[0] + name[self.safe_channel_idlen + 1:], name

        return name, name

    def channel_matches(self, name1, name2):
        if len(name2) > len(name1):
            # Make sure the safe channel's long name is in name1
            name2, name1 = name1, name2

        names = self.parse_channel_name(name1)
        return name2 in names

    # Command helpers

    def pass_(self, password):
        return self.send_message('PASS', password)

    def nick_(self, nick):
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

    # Example:
    #
    # def on_ping(self, prefix, *args):
    #     self.send_message('PONG', *args)

    def on_nick(self, prefix, new_nick):
        user = self.parse_user(prefix)
        if user and user.nick == self.nick:
            self.log.debug('Nick changed to %s' % new_nick)
            self.nick = new_nick

    def on_join(self, prefix, channel):
        user = self.parse_user(prefix)
        if user and user.nick == self.nick:
            self.log.debug('Joined channel %s' % channel)
            short_name, long_name = self.parse_channel_name(channel)
            self.channels[short_name] = long_name
