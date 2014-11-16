from tornado.gen import coroutine
from sinap.module import Module


class KeepNickModule(Module):
    @coroutine
    def startup(self):
        nicks = {
            netname: conf.get('nick', self.bot.config.get('nick'))
            for netname, conf in self.bot.config.get('networks', {}).items()
        }
        while True:
            yield self.wait(120)
            for netname, net in self.bot.networks.items():
                nick = nicks[netname]
                if nick and net.nick != nick:
                    net.nick_(nick)
