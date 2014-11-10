from tornado.httpserver import HTTPServer
from tornado.web import Application
from sinap.module import Module


class HTTPModule(Module):
    export_as = 'http'

    def __init__(self, *args, **kwds):
        super().__init__(*args, **kwds)
        self.handlers = []
        self.app = Application()
        self._started = False

    def shutdown(self):
        if self._started:
            self.server.stop()

    def add_handlers(self, handlers):
        self.handlers.extend(handlers)

        # Wipe all existing handlers and replace with our handlers
        del self.app.handlers[:]
        self.app.add_handlers('.*$', self.handlers)

        # Start when the first handlers are added
        self._start()

    def _start(self):
        if self._started:
            return

        address = self.config.get('address', '127.0.0.1')
        port = self.config.get('port', 8000)

        self.log.info('Starting HTTP server at %s:%s' % (address, port))
        self.server = HTTPServer(self.app)
        self.server.listen(port, address)

        self._started = True
