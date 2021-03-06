from functools import partial
from pathlib import Path
import asyncio
import inspect
import logging
import os
import signal
import sys

import yaml

from sinap.irc import IRCConnection
from sinap.module import Module
from sinap.scope import Scope


DEFAULT_PORT = 6667
DEFAULT_PORT_SSL = 6697


class Backoff:
    def __init__(self, max_wait=300, exponent=1.8):
        self._current = 2
        self._max_wait = max_wait
        self._exponent = exponent

    async def sleep(self):
        if self._current > self._max_wait:
            self._current = self._max_wait

        await asyncio.sleep(self._current)
        self._current **= self._exponent

    def reset(self):
        self._current = 2


class NameMunglingFormatter(logging.Formatter):
    def format(self, record):
        record.name = record.name.rsplit('.', 1)[-1]
        return logging.Formatter.format(self, record)


class BotIRCConnection(IRCConnection):
    def __init__(self, bot, name, config, logger, loop=None):
        host = config.get('server')
        nick = config.get('nick')

        if not host:
            raise ValueError('host not specified')

        if not nick:
            raise ValueError('nick not specified')

        ssl = config.get('ssl', False)
        port = config.get('port', DEFAULT_PORT_SSL if ssl else DEFAULT_PORT)

        super().__init__(
            host=host,
            port=port,
            ssl=ssl,
            nick=nick,
            password=config.get('password'),
            username=config.get('username'),
            realname=config.get('realname'),
            logger=logger,
            delegate=bot,
            loop=loop,
        )
        self.name = name
        self.new_host = None
        self.new_port = None

    async def connect(self, reuse_fd=None):
        if self.new_host and self.new_port:
            self.host = self.new_host
            self.port = self.new_port
            self.new_host = self.new_port = None

        await super().connect(reuse_fd=reuse_fd)

    def reconfigure(self, config):
        host = config.get('server')
        nick = config.get('nick')

        if not host:
            raise ValueError('host not specified')

        if not nick:
            raise ValueError('nick not specified')

        self._apply_changes(config)

        # Join new channels
        for channel in config.get('channels', []):
            if channel not in self.channels:
                self.join(channel)

        # We don't part any channels here, because reloading the
        # config would then part from channels that were joined
        # manually.

    def dump_state(self):
        # About to restart, make the fd inheritable
        socket = self._transport.get_extra_info('socket')
        if socket:
            socket.set_inheritable(True)

        return {
            'server': self.host,
            'port': self.port,
            'fileno': socket.fileno(),
            'nick': self.nick,
            'channels': self.channels,
            'password': self.password,
        }

    async def load_state(self, state):
        await self.connect(reuse_fd=state['fileno'])
        self.channels = state['channels']
        self._apply_changes(state)

    def _apply_changes(self, config):
        host = config['server']
        port = config.get('port', 6667)
        nick = config['nick']

        if 'password' in config:
            self.password = config['password']
        if 'username' in config:
            self.username = config['username']
        if 'realname' in config:
            self.realname = config['realname']

        if self.host != host or self.port != port:
            self.new_host = host
            self.new_port = port
            self.nick = nick
            self.disconnect()
        elif self.nick != nick:
            self.nick = nick
            self.nick_(nick)


class Bot(object):
    def __init__(self, config_file, state_file=None, loop=None):
        # Load configuration here to catch errors early on
        self.config_file = config_file
        self.load_config()

        if state_file:
            with open(state_file) as fobj:
                self.state = yaml.safe_load(fobj)
            os.remove(state_file)
        else:
            self.state = {}

        self.loop = loop or asyncio.get_event_loop()

        # netname -> IRCConnection
        self.networks = {}

        # netname -> Future
        self.connecting = {}

        # netname -> timeout handle
        self.pings = {}

    def run(self):
        self.reload(initial=True)
        self.loop.add_signal_handler(signal.SIGUSR1, self.handle_usr1)

    def handle_usr1(self, signum=None, frame=None):
        self.log.info('SIGUSR1 received, reloading config')
        self.reload()

    def load_config(self):
        with open(self.config_file) as fobj:
            self.config = yaml.safe_load(fobj)

    def setup_logging(self):
        config = self.config.get('logging', {})
        level = config.get('level', 'info')

        if level not in ('debug', 'info', 'warning', 'error'):
            level = 'info'

        levelno = getattr(logging, level.upper())
        fmt = '[%(asctime)-15s][%(name)-20s] %(levelname)s %(message)s'

        # Set up the name mungling formatter for our own loggers
        formatter = NameMunglingFormatter(fmt)
        self.logging_handler = logging.StreamHandler()
        self.logging_handler.propagate = False
        self.logging_handler.setLevel(levelno)
        self.logging_handler.setFormatter(formatter)

    def logger(self, name):
        if '.' in name:
            raise ValueError("'.' not allowed in logger name")

        logger = logging.getLogger('sinap.loggers.' + name)
        logger.setLevel('DEBUG')
        logger.handlers = []
        logger.addHandler(self.logging_handler)
        return logger

    def is_admin(self, user):
        return any(user.matches(mask) for mask in self.admin_masks)

    def load_modules(self, initial=False):
        if not initial:
            # Shut down modules
            for qualified_name, module in self.modules.items():
                module._shutdown()

        self.modules = {}
        self.exports = {}
        self.message_handlers = []

        self.admin_commands = {}
        self.public_commands = {}
        self.command_prefix = self.config.get('command_prefix', '!')

        modulesets = self.config.get('modulesets', {})
        if 'core' not in modulesets:
            modulesets['core'] = {}
        modulesets['core']['path'] = str(Path(__file__).parent / 'modules')

        for prefix, moduledir in self.config.get('modulesets', {}).items():
            if prefix == 'core':
                # Don't allow overwriting core
                continue

        for moduleset, config in modulesets.items():
            if isinstance(config, str):
                config = {'path': config}

            moduledir = Path(config['path'])
            if not moduledir.exists():
                self.log.warning('Module directory does not exist: %s' % moduledir)
                continue

            for modulepath in moduledir.glob('*.py'):
                module_name = modulepath.stem
                qualified_name = '%s:%s' % (moduleset, module_name)
                self.log.info('Loading module %s' % qualified_name)

                try:
                    fobj = modulepath.open()
                except:
                    self.log.info('Failed to open module %s' % qualified_name)
                    continue

                names = {}
                with fobj:
                    try:
                        code = compile(fobj.read(), str(modulepath), 'exec')
                        exec(code, names)
                    except:
                        self.log.info('Failed to load module %s' % qualified_name)
                        self.log.debug('Uncaught exception', exc_info=True)
                        continue

                for value in names.values():
                    if (inspect.isclass(value) and
                            issubclass(value, Module) and
                            value != Module):
                        ctor = value
                        break
                else:
                    self.log.info('Failed to load module %s' % qualified_name)
                    self.log.debug("%s doesn't define a Module subclass" %
                                   qualified_name)
                    continue

                module_config = config.get(module_name, {})
                logger = self.logger(qualified_name)
                try:
                    module = ctor(self, module_config, logger)
                    self.modules[qualified_name] = module
                except:
                    self.log.info('Failed to load module %s' % qualified_name)
                    self.log.debug('Uncaught exception', exc_info=True)

                export_as = getattr(module, 'export_as', None)
                if export_as:
                    if export_as in self.exports:
                        self.log.warning('Export %r already exists, '
                                         'overwriting' % export_as)
                    self.exports[export_as] = module

        for qualified_name, module in self.modules.items():
            # Start the module
            try:
                module._startup()
            except:
                self.log.exception('Unable to start module %s' % qualified_name)
                continue

            # Register message and command handlers
            on_message = getattr(module, 'on_message', None)
            if on_message:
                self.message_handlers.append(on_message)

            admin_commands = getattr(module, 'admin_commands', {})
            self.register_commands(module, admin_commands, public=False)

            public_commands = getattr(module, 'public_commands', {})
            self.register_commands(module, public_commands, public=True)

    def register_commands(self, module, commands, public):
        targets = [self.admin_commands]
        if public:
            targets.append(self.public_commands)

        for name, opts in commands.items():
            if isinstance(opts, str):
                nargs = 0
                synopsis = name
                help = opts
                aliases = []
            else:
                nargs = opts.get('nargs', 0)
                synopsis = opts.get('synopsis', name)
                help = opts.get('help', '')
                aliases = opts.get('aliases', [])

            fn = getattr(module, 'command_%s' % name, None)
            if callable(fn):
                handler = {
                    'module': module,
                    'name': name,
                    'nargs': nargs,
                    'synopsis': synopsis,
                    'help': help,
                    'aliases': aliases,
                    'run': fn,
                }
                for target in targets:
                    target[name] = handler
                    for alias in aliases:
                        target[alias] = handler
            else:
                self.log.warning('No callable handler for command %s' % name)

    def reload(self, initial=False):
        if not initial:
            # Reload configuration
            self.load_config()

        self.setup_logging()
        self.log = self.logger('core')

        if 'datadir' in self.config:
            self.datadir = Path(self.config['datadir'])
            if not self.datadir.exists():
                self.datadir.mkdir(parents=True)
            if not self.datadir.is_dir():
                self.log.error('datadir must be a directory')
        else:
            self.datadir = None

        self.admin_masks = self.config.get('admins', [])
        self.load_modules(initial)

        net_configs = self.config.get('networks', {})
        for netname, config in sorted(net_configs.items()):
            # Lookup some defaults from the global config
            for field in ('nick', 'username', 'realname'):
                if field not in config and field in self.config:
                    config[field] = self.config[field]

            connecting = self.connecting.pop(netname, None)
            if connecting:
                self.log.info('Cancelling pending connection attempt to %s' %
                              netname)
                connecting.cancel()

            net = self.networks.get(netname)
            if net:
                # Already connected, reconfigure
                try:
                    net.reconfigure(config)
                except ValueError as exc:
                    net.log.error(str(exc))
            else:
                # Start a new connection
                self.loop.create_task(self.connect(netname, config))

        for netname, net in sorted(self.networks.items()):
            config = net_configs.get(netname)
            if not config and netname in self.networks:
                # Network has been removed from config, disconnect
                self.networks[netname].disconnect()
                del self.networks[netname]

    def restart(self):
        if not self.datadir:
            raise ValueError('Restart is not possible without datadir')

        state = {'networks': {}}
        for netname, net in self.networks.items():
            state['networks'][netname] = net.dump_state()

        state_file = Path(self.datadir) / 'state.yml'
        with state_file.open('w') as fobj:
            yaml.dump(state, fobj)

        new_args = sys.argv.copy()
        try:
            index = new_args.index('--state')
            new_args[index + 1] = str(state_file)
        except ValueError:
            new_args += ['--state', str(state_file)]

        self.log.debug('Executing %s' % new_args)
        os.execv(new_args[0], new_args)

    async def connect(self, netname, config):
        log = self.logger(netname)
        try:
            net = BotIRCConnection(self, netname, config, log, self.loop)
        except ValueError as exc:
            log.error(str(exc))
            return

        state = self.state.get('networks', {}).get(netname)
        backoff = Backoff()

        while True:
            if state:
                log.info('Reusing old connection')
                await net.load_state(state)
                net.reconfigure(config)
                state = None
            else:
                future = asyncio.ensure_future(net.connect())
                self.connecting[netname] = future

                # net.connect may change net.host and net.port, so
                # print them afterwards
                log.info('Connecting to %s:%s' % (net.host, net.port))
                try:
                    await future
                except asyncio.CancelledError:
                    # Connection attempt cancelled
                    return
                except Exception as exc:
                    log.error('Failed to connect to %s:%s: %s' % (
                        net.host, net.port, exc,
                    ))
                    await backoff.sleep()
                    continue
                else:
                    log.info('Connected')
                    backoff.reset()
                finally:
                    self.connecting.pop(netname, None)

            self.networks[netname] = net
            self.ping(net, schedule_only=True)

            for channel in config.get('channels', []):
                if channel not in net.channels:
                    net.join(channel)
            await net.wait_for_disconnect()

            if netname in self.pings:
                self.pings[netname].cancel()
                del self.pings[netname]

            log.info('Connection lost to %s:%s, reconnecting' %
                     (net.host, net.port))

    def validate_args(self, args, nargs):
        # nargs can be one of:
        #
        #   n            accept exactly n arguments
        #   (n, m)       accept between n and m arguments (inclusive)
        #   '*'          varargs list
        #   ':'          nonempty varargs list
        #   (n, '*')     n normal arguments and a varargs list
        #   (n, '*')     n normal arguments and a nonempty varargs list
        #
        if nargs == '*':
            return [args] if args else []
        elif nargs == ':':
            return [args] if args else None

        if isinstance(nargs, tuple):
            min, max = nargs
        else:
            min = max = nargs

        if min == max == 0:
            if args:
                return None
            else:
                return []

        if max in ('*', ':'):
            args = args.split(None, min)
            if max == '*' and not (min <= len(args) <= min + 1):
                return None
            if max == ':'and len(args) != min + 1:
                return None
        else:
            args = args.split()
            if not (min <= len(args) <= max):
                return None

        return args

    def on_privmsg(self, net, sender, target, message):
        user = net.parse_user(sender)
        if not user:
            # Invalid message
            return

        scope = Scope(net, user, target)

        if message.startswith(self.command_prefix):
            # Command
            message = message[len(self.command_prefix):].strip()
            if ' ' in message:
                command, args = message.split(None, 1)
            else:
                command = message
                args = ''

            if self.is_admin(user):
                commands = self.admin_commands
            else:
                commands = self.public_commands

            handler = commands.get(command, None)
            if handler:
                args = self.validate_args(args, handler['nargs'])
                if args is not None:
                    run = handler['run']
                    self.run_async_callback(run, user, scope, *args)
                else:
                    net.privmsg(scope.target, 'Usage: %s%s' % (
                        self.command_prefix,
                        handler['synopsis'],
                    ))
                return

        # Not a registered command, call plain message handlers
        for handler in self.message_handlers:
            self.run_async_callback(handler, user, scope, message)

    def run_async_callback(self, callback, *args, **kwds):
        # Run a callback asynchronously whether it is a normal
        # function or a coroutine function
        if asyncio.iscoroutinefunction(callback):
            self.loop.create_task(callback(*args, **kwds))
        else:
            self.loop.call_soon(partial(callback, *args, **kwds))

    def handle_message(self, net, message):
        # We got a message so the connection is alive. Reschedule the
        # ping for this network.
        if net.name in self.pings:
            self.pings[net.name].cancel()
            self.ping(net, schedule_only=True)

    def on_ping(self, net, sender, *args):
        net.send_message('PONG', *args)

    def ping(self, net, schedule_only=False):
        if not schedule_only:
            net.send_message('PING', net.host)

        net_config = self.config['networks'][net.name]
        period = net_config.get('ping', self.config.get('ping', 90))
        self.pings[net.name] = self.loop.call_later(
            period, partial(self.ping, net),
        )

        # Don't expect a PONG, just trust that the write to the socket
        # causes the socket to be closed if there's a connection
        # error.
