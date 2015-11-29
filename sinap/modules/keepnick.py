from sinap.module import Module


class KeepNickModule(Module):
    async def startup(self):
        nicks = {
            netname: conf.get('nick', self.bot.config.get('nick'))
            for netname, conf in self.bot.config.get('networks', {}).items()
        }
        while True:
            await self.wait(120)
            for netname, net in self.bot.networks.items():
                nick = nicks[netname]
                if nick and net.nick != nick:
                    net.nick_(nick)
