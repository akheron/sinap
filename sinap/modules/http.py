from aiohttp.web import Application
from sinap.module import Module


class HTTPModule(Module):
    export_as = 'http'

    def __init__(self, *args, **kwds):
        super().__init__(*args, **kwds)
        self._app = Application()
        self._server = None
        self._handler = None
        self._started = False

    def add_route(self, method, path, handler):
        self._app.router.add_route(method, path, handler)

        # Start when the first handlers are added
        self._maybe_start()

    def add_routes(self, routes):
        # routes is a list of (method, path, handler) tuples
        for method, path, handler in routes:
            self._app.router.add_route(method, path, handler)

        # Start when the first handlers are added
        self._maybe_start()

    def reverse_url(self, name, *args):
        baseurl = self.config.get('baseurl', '')
        return baseurl + self._app.reverse_url(name, *args)

    def _maybe_start(self):
        if not self._started:
            # Start when the first handlers are added
            self._started = True
            self.loop.create_task(self._start())

    async def _start(self):
        address = self.config.get('address', '127.0.0.1')
        port = self.config.get('port', 8000)

        self.log.info('Starting HTTP server at %s:%s' % (address, port))
        self._handler = self._app.make_handler()
        self._server = await self.loop.create_server(self._handler, address, port)

    def shutdown(self):
        if self._started:
            self.loop.create_task(self._shutdown_server())

    async def _shutdown_server(self):
        self._server.close()
        await self._server.wait_closed()
        await self._app.shutdown()
        await self._handler.finish_connections(1.0)
        await self._app.cleanup()
