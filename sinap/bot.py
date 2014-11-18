from pathlib import Path
import inspect
import logging
import os
import sys
import yaml

from tornado.gen import coroutine
from tornado.ioloop import IOLoop

from sinap.irc import IRCConnection
from sinap.module import Module


class NameMunglingFormatter(logging.Formatter):
    def format(self, record):
        record.name = record.name.rsplit('.', 1)[-1]
        return logging.Formatter.format(self, record)


class Scope(object):
    def __init__(self, net, from_, to):
        self.net = net
        self.user = from_
        if net.is_channel(to):
            # Channel message
            self.target = to
        else:
            # Private message
            self.target = self.user.nick

    def channel_matches(self, channel):
        if not self.net.is_channel(self.target):
            return False

        return self.net.channel_matches(self.target, channel)

    def to(self, target):
        copy = Scope(self.net, self.user, self.target)
        copy.target = target
        return copy


class BotIRCConnection(IRCConnection):
    def __init__(self, bot, name, *args, **kwds):
        super().__init__(*args, delegate=bot, **kwds)
        self.name = name


class Bot(object):
    def __init__(self, config_file, state_file=None, io_loop=None):
        self.config_file = config_file
        self.load_config()

        if state_file:
            with open(state_file) as fobj:
                self.state = yaml.safe_load(fobj)
        else:
            self.state = {}

        if 'datadir' in self.config:
            self.datadir = Path(self.config['datadir'])
            if not self.datadir.exists():
                self.datadir.mkdir(parents=True)
            if not self.datadir.is_dir():
                raise ValueError('datadir must be a directory')
        else:
            self.datadir = None

        self.ioloop = io_loop or IOLoop.instance()

        # netname -> timeout handle
        self.pings = {}

    def run(self):
        self.reload(initial=True)

        self.nick = self.config.get('nick', None)
        self.networks = {}
        for name, config in self.config.get('networks', {}).items():
            state = self.state.get(name)
            self.ioloop.add_callback(self.connect, name, config, state)

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

        # Set up a basic formatter for tornado
        tornado = logging.getLogger('tornado')
        handler = logging.StreamHandler()
        handler.propagate = False
        handler.setLevel(levelno)
        handler.setFormatter(logging.Formatter(fmt))
        tornado.handlers = []
        tornado.addHandler(handler)

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
                        exec(fobj.read(), names)
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
            module._startup()

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
            else:
                nargs = opts.get('nargs', 0)
                synopsis = opts.get('synopsis', name)
                help = opts.get('help', '')

            fn = getattr(module, 'command_%s' % name, None)
            if callable(fn):
                handler = {
                    'module': module,
                    'name': name,
                    'nargs': nargs,
                    'synopsis': synopsis,
                    'help': help,
                    'run': fn,
                }
                for target in targets:
                    target[name] = handler
            else:
                self.log.warning('No callable handler for command %s' % name)

    def reload(self, initial=False):
        if not initial and self.config_file:
            # Reload configuration
            self.load_config()

        self.setup_logging()
        self.log = self.logger('core')

        self.admin_masks = self.config.get('admins', [])
        self.load_modules(initial)

    def restart(self):
        if not self.datadir:
            raise ValueError('Restart is not possible without datadir')

        state = {'networks': {}}
        for netname, net in self.networks.items():
            state['networks'][netname] = net.dump_state()

        state_file = Path(self.datadir) / 'state.yml'
        with state_file.open('w') as fobj:
            yaml.dump(state, fobj)

        new_args = sys.argv + ['--state', ]
        self.log.debug('Executing %s' % new_args)
        os.execv(new_args[0], new_args)

    @coroutine
    def connect(self, netname, config, state):
        host = config.get('server')
        port = config.get('port', 6667)
        nick = config.get('nick', self.nick)

        if host is None:
            self.log.error(netname, 'No host specified, aborting')
            return

        if nick is None:
            self.log.error(netname, 'No nick specified, aborting')
            return

        log = self.logger(netname)
        network = BotIRCConnection(
            self, netname,
            host=host,
            port=port,
            nick=nick,
            password=config.get('password'),
            username=config.get('username', self.config.get('username')),
            realname=config.get('realname', self.config.get('realname')),
            logger=log,
            io_loop=self.ioloop,
        )

        while True:
            if state:
                log.info('Reusing old connection')
                yield network.connect(state)
                state = None
            else:
                log.info('Connecting to %s:%s' % (host, port))
                yield network.connect()
                log.info('Connected')

            self.networks[netname] = network
            self.ping(network, schedule_only=True)

            for channel in config.get('channels', []):
                network.join(channel)
            yield network.wait_for_disconnect()

            if netname in self.pings:
                self.ioloop.remove_timeout(self.pings[netname])

            log.info('Connection lost to %s:%s, reconnecting' % (host, port))

    def validate_args(self, args, nargs):
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
                    self.ioloop.add_callback(run, user, scope, *args)
                else:
                    net.privmsg(scope.target, 'Usage: %s%s' % (
                        self.command_prefix,
                        handler['synopsis'],
                    ))
                return

        # Not a registered command, call plain message handlers
        for handler in self.message_handlers:
            self.ioloop.add_callback(handler, user, scope, message)

    def handle_message(self, net, message):
        # We got a message so the connection is alive. Reschedule the
        # ping for this network.
        if net.name in self.pings:
            self.ioloop.remove_timeout(self.pings[net.name])
            self.ping(net, schedule_only=True)

    def on_ping(self, net, sender, *args):
        net.send_message('PONG', *args)

    def ping(self, net, schedule_only=False):
        if not schedule_only:
            net.send_message('PING', net.host)

        net_config = self.config['networks'][net.name]
        period = net_config.get('ping', self.config.get('ping', 90))
        self.pings[net.name] = self.ioloop.call_later(
            period, self.ping, net,
        )

        # Don't expect a PONG, just trust that the write to the socket
        # causes the socket to be closed if there's a connection
        # error.
