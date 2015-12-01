from aiohttp.web import Application
from sinap.module import Module


class HTTPModule(Module):
    export_as = 'http'

    def __init__(self, *args, **kwds):
        super().__init__(*args, **kwds)
        self._handlers = []
        self._app = Application()
        self._server = None
        self._handler = None
        self._started = False

    def add_route(self, method, path, handler):
        self.app.router.add_route(method, path, handler)

        if not self._started:
            # Start when the first handlers are added
            self.loop.create_task(self._start())

    def reverse_url(self, name, *args):
        baseurl = self.config.get('baseurl', '')
        return baseurl + self.app.reverse_url(name, *args)

    async def _start(self):
        self._started = True

        address = self.config.get('address', '127.0.0.1')
        port = self.config.get('port', 8000)

        self.log.info('Starting HTTP server at %s:%s' % (address, port))
        self._handler = self._app.make_handler()
        await self.loop.create_server(self._handler, address, port)

    def shutdown(self):
        if self._started:
            self.loop.create_task(self._shutdown_server())

    async def _shutdown_server(self):
        await self._handler.finish_connections(1.0)
        self._server.close()
        await self._server.wait_closed()
        await self._app.finish()
