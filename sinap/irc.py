from fnmatch import fnmatch
from functools import partial
from getpass import getuser
from io import StringIO
import asyncio
import collections
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
                 ssl=False,
                 password=None,
                 username=None,
                 realname=None,
                 logger=None,
                 delegate=None,
                 loop=None):
        self.host = host
        self.port = port
        self.nick = nick
        self.ssl = True if ssl else None

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

        self._send_queue = collections.deque()
        self._send_burst_decrementer = None
        self._current_send_burst = 0
        self._max_send_burst = 3
        self._send_burst_wait = 2.0

        self._connect_future = None
        self._disconnect_future = None

    async def connect(self, reuse_fd=None):
        # If reuse_fd is given, it should be a connected file
        # descriptor number
        if reuse_fd is None:
            connect_kwds = {
                'host': self.host,
                'port': self.port,
                'ssl': self.ssl,
            }
        else:
            self.log.debug('Reusing fd %d' % reuse_fd)
            sock = socket.fromfd(reuse_fd, socket.AF_INET, socket.SOCK_STREAM)
            connect_kwds = {'sock': sock}

        self._transport, self._protocol = await self._loop.create_connection(
            lambda: IRCProtocol(self.process_message, self.log),
            **connect_kwds,
        )

        self.start_send_burst_decrementer()

        if reuse_fd:
            # Already registered
            self._disconnect_future = asyncio.Future()
            return

        # This is a new connection, register with the server
        # asynchronously
        self._connect_future = asyncio.Future()
        self.register()

        try:
            await self._connect_future
        except asyncio.CancelledError:
            self.disconnect()
            raise

        self._connect_future = None
        self._disconnect_future = asyncio.Future()

    def register(self):
        self.log.debug('Registering connection')

        if self.password:
            self.pass_(self.password)

        self.nick_(self.nick)
        self.user(self._username, '8', self._realname)

        # Registration is finalized in on_001 and on_433

    def disconnect(self):
        if not self._transport:
            return

        self._transport.close()

    async def wait_for_disconnect(self):
        if not self._disconnect_future:
            raise ValueError('Can only be called after connect() returns')

        await self._disconnect_future

    def process_message(self, msg):
        if msg is None:
            exc = Disconnected()

            # Disconnected -> dropped from channels, too
            self.channels.clear()
            if self._connect_future and not self._connect_future.done():
                self._connect_future.set_exception(exc)
            if self._disconnect_future and not self._disconnect_future.done():
                self._disconnect_future.set_result(None)
                self._disconnect_future = None

            for future in self._message_listeners:
                future.set_exception(exc)
            del self._message_listeners[:]

            self.stop_send_burst_decrementer()
            return

        for future in self._message_listeners:
            future.set_result(msg)
        del self._message_listeners[:]

        for _, handler in self.handlers('handle_message'):
            self._loop.call_soon(handler, msg)

        if msg.is_command:
            type_handler_name = 'handle_command'
        else:
            type_handler_name = 'handle_reply'

        for _, handler in self.handlers(type_handler_name):
            self._loop.call_soon(partial(handler, msg))

        # Call the command specific handler if any
        for handler_name, handler in self.handlers_for_msg(msg):
            sig = inspect.signature(handler)
            try:
                sig.bind(msg.prefix, *msg.args)
            except TypeError:
                self.log.warning('''\
Command handler signature does not match the command sent by server.
Singature: %s%s
Command: %s''' % (handler_name, sig, msg))
            else:
                self._loop.call_soon(partial(handler, msg.prefix, *msg.args))

    def send_message(self, command, *args, prefix=None):
        self._send_queue.append([command, args, prefix])
        self._loop.call_soon(self.send_pending_messages)

    def send_pending_messages(self):
        while self._send_queue and self._current_send_burst < self._max_send_burst:
            command, args, prefix = self._send_queue.popleft()
            self._protocol.send_message(command, *args, prefix=prefix)
            self._current_send_burst += 1

    def start_send_burst_decrementer(self):
        self._send_burst_decrementer = self._loop.call_later(
            self._send_burst_wait,
            self.decrement_send_burst,
        )

    def stop_send_burst_decrementer(self):
        if self._send_burst_decrementer:
            self._send_burst_decrementer.cancel()
            self._send_burst_decrementer = None

    def decrement_send_burst(self):
        if self._current_send_burst > 0:
            self._current_send_burst -= 1
            self._loop.call_soon(self.send_pending_messages)

        self.start_send_burst_decrementer()

    def handlers(self, handler_name):
        if hasattr(self, handler_name):
            yield handler_name, getattr(self, handler_name)
        if self._delegate and hasattr(self._delegate, handler_name):
            handler = getattr(self._delegate, handler_name)
            yield handler_name, partial(handler, self)

    def handlers_for_msg(self, msg):
        handler_name = 'on_%s' % msg.command.lower()
        return self.handlers(handler_name)

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

    def on_001(self, prefix, *args):
        # RPL_WELCOME
        if self._connect_future and not self._connect_future.done():
            # Registration done!
            self._connect_future.set_result(None)

    def on_433(self, prefix, *args):
        # ERR_NICKNAMEINUSE
        if self._connect_future:
            # Registering, try another nick
            self.nick += '_'
            self.nick_(self.nick)

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
