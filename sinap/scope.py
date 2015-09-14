class Scope(object):
    def __init__(self, net, from_, to, raw=False):
        self.net = net
        self.user = from_

        if not raw:
            if net.is_channel(to):
                # Channel message
                self.target = to
            else:
                # Private message
                self.target = self.user.nick
        else:
            self.target = to

    def channel_matches(self, channel):
        if not self.net.is_channel(self.target):
            return False

        return self.net.channel_matches(self.target, channel)

    def to(self, target):
        copy = Scope(self.net, self.user, self.target)
        copy.target = target
        return copy
