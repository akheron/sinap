from pathlib import Path
import inspect
import logging

from tornado.gen import coroutine
from tornado.ioloop import IOLoop

from sinap.irc import IRCConnection
from sinap.module import Module


class LevelFixingFormatter(logging.Formatter):
    def format(self, record):
        record.name = record.name.rsplit('.', 1)[-1]
        return logging.Formatter.format(self, record)


class Scope(object):
    def __init__(self, net, from_, to):
        self.net = net
        self.user = from_
        self.to = to
        if net.is_channel(to):
            # Channel message
            self.target = to
        else:
            # Private message
            self.target = self.user.nick


class Bot(object):
    def __init__(self, config, io_loop=None):
        if isinstance(config, dict):
            self.config_file = None
            self.config = config
        else:
            self.config_file = config
            self.load_config()

        self.ioloop = io_loop or IOLoop.instance()

    def run(self):
        self.reload(initial=True)

        self.nick = self.config.get('nick', None)
        self.networks = {}
        for name, network_config in self.config.get('networks', {}).items():
            self.ioloop.add_callback(self.connect, name, network_config)

    def load_config(self):
        import yaml
        with open(self.config_file) as fobj:
            self.config = yaml.load(fobj)

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

        # Set up the level fixin formatter for our own loggers
        formatter = LevelFixingFormatter(fmt)
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
        self.message_handlers = []

        self.admin_commands = {}
        self.public_commands = {}
        self.command_prefix = self.config.get('command_prefix', '!')

        moduledirs = {'core': Path(__file__).parent / 'modules'}

        for prefix, moduledir in self.config.get('modulesets', {}).items():
            if prefix == 'core':
                # Don't allow overwriting core
                continue

            moduledirs[prefix] = Path(moduledir)

        for prefix, moduledir in moduledirs.items():
            if not moduledir.exists():
                self.log.warning('Module directory does not exist: %s' % moduledir)
                continue

            for modulepath in moduledir.glob('*.py'):
                module_name = modulepath.stem
                qualified_name = '%s:%s' % (prefix, module_name)
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

                try:
                    module = ctor(self, self.logger(qualified_name))
                    self.modules[qualified_name] = module
                except:
                    self.log.info('Failed to load module %s' % qualified_name)
                    self.log.debug('Uncaught exception', exc_info=True)

                # Start the module
                module._startup()

        for qualified_name, module in self.modules.items():
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

    @coroutine
    def connect(self, netname, config):
        host = config.get('server')
        port = config.get('port', 6667)
        nick = config.get('nick', self.nick)

        if host is None:
            self.error(netname, 'No host specified, aborting')
            return

        if nick is None:
            self.error(netname, 'No nick specified, aborting')
            return

        log = self.logger(netname)
        network = IRCConnection(
            host=host,
            port=port,
            nick=nick,
            password=config.get('password'),
            username=config.get('username', self.config.get('username')),
            realname=config.get('realname', self.config.get('realname')),
            logger=log,
            delegate=self,  # delegate message handers to self
            io_loop=self.ioloop,
        )

        while True:
            log.info('Connecting to %s:%s' % (host, port))
            yield network.connect()
            log.info('Connected')

            self.networks[netname] = network

            for channel in config.get('channels', []):
                network.join(channel)
            yield network.wait_for_disconnect()
            self.info(netname, 'Connection lost to %s:%s, reconnecting' % (host, port))

    def validate_args(self, args, nargs):
        if nargs == '*':
            return [args] if args else []

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

    @coroutine
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
