from sinap.module import Module


class CommandsModule(Module):
    admin_commands = {
        'reload': 'Reload configuration and modules',
        'restart': 'Restart the bot without disconnecting from networks',
        'networks': 'List networks',
        'join': {
            'nargs': 2,
            'synopsis': 'join <channel> <network>',
            'help': 'Join a channel on the given network',
        },
        'part': {
            'nargs': (2, '*'),
            'synopsis': 'part <channel> <network> [<message>]',
            'help': 'Part a channel on the given network',
        },
    }

    public_commands = {
        'help': {
            'nargs': (0, 1),
            'synopsis': 'help [<command>]',
            'help': 'Print command help',
        },
    }

    def command_reload(self, user, scope):
        self.bot.reload()
        self.say(scope, 'Reload OK')

    def command_restart(self, user, scope):
        self.say(scope, 'Restarting')
        self.bot.restart()

    def command_networks(self, user, scope):
        names = ', '.join(sorted(self.bot.networks.keys()))
        self.say(scope, 'My networks: %s' % names)

    def _check_channel_and_net(self, scope, channel, network):
        net = self.bot.networks.get(network, None)
        if not net:
            self.say(scope, 'Uknown network: %s' % net)
            return

        if not net.is_channel(channel):
            self.say(scope, 'Invalid channel name: %s' % channel)
            return

        return net

    def command_join(self, user, scope, channel, network):
        net = self._check_channel_and_net(scope, channel, network)
        if net:
            net.join(channel)

    def command_part(self, user, scope, channel, network, message=None):
        net = self._check_channel_and_net(scope, channel, network)
        if net:
            net.part(channel, message)

    def command_help(self, user, scope, command=None):
        is_admin = self.bot.is_admin(user)
        if is_admin:
            commands = self.bot.admin_commands
        else:
            commands = self.bot.public_commands

        if command is None:
            if commands:
                names = ', '.join(sorted(commands.keys()))
                self.say(scope, 'Available commands: %s' % names)
            else:
                self.say(scope, 'No commands available for you, sorry')
        else:
            if command.startswith(self.bot.command_prefix):
                command = command[len(self.bot.command_prefix):]

            cmd = commands.get(command, None)
            if not cmd:
                self.say(scope, 'No such command: %s' % command)
            else:
                self.say(scope, 'Usage: %s' % cmd['synopsis'])
                if cmd['help']:
                    self.say(scope, cmd['help'])
