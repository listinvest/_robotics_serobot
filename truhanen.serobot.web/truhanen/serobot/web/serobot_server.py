
import json
import asyncio as aio
import async_timeout
import io
import logging
from pathlib import Path
import ssl
import base64
from dataclasses import asdict

from aiohttp import web, WSMsgType, ClientError
from cryptography import fernet
import aiohttp_session
from aiohttp_session.cookie_storage import EncryptedCookieStorage
from aiohttp_security import setup as setup_security
from aiohttp_security import SessionIdentityPolicy
from aiohttp_security import (
    remember, forget, authorized_userid,
    check_permission, check_authorized,
)

from truhanen.serobot.api import Serobot

from .authorization import DictionaryAuthorizationPolicy, check_credentials
from .user import User
from .hardware_command import HardwareCommander


# Module-level logger
logger = logging.getLogger(__name__)


class SerobotServer:
    def __init__(self, auth_file=None, ssl_certfile=None, ssl_keyfile=None):
        """
        Parameters
        ----------
        auth_file : Path
            File to be read by user.User.read_user_map().
        ssl_certfile : Path | None
            If provided with ssl_keyfile, setup the server with SSL
            encryption and use port 443. Otherwise setup an http
            server listening on port 80.
        ssl_keyfile : Path | None
            See ssl_certfile.
        """
        if not auth_file:
            raise RuntimeError('Missing argument "auth_file".')
        self._user_map = User.read_user_map(auth_file)

        self._ssl_certfile = ssl_certfile
        self._ssl_keyfile = ssl_keyfile

        self._bot = Serobot()
        self._hardware_commander = HardwareCommander(self.bot)

        # These queues are initialized in the start() coroutine.
        self._client_log_queue = None
        self._client_image_queue = None
        self._hardware_command_queue = None

    async def start(self):
        """Setup and start serving the web application."""
        await self._init_queues()

        # Start background tasks.
        aio.create_task(self._hardware_command_worker())
        aio.create_task(self._camera_capture_worker())

        # Create the web app.
        app = self._create_application()
        app_runner = web.AppRunner(app)
        await app_runner.setup()

        # Setup optional SSL.
        if self._ssl_certfile and self._ssl_keyfile:
            ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(
                certfile=str(self._ssl_certfile.resolve()),
                keyfile=str(self._ssl_keyfile.resolve()))
            port = 443
        else:
            ssl_context = None
            port = 80

        # Start the server.
        tcp_site = web.TCPSite(
            app_runner, host='0.0.0.0', port=port, ssl_context=ssl_context)
        await tcp_site.start()

        logger.info(f'Serving the web app on {tcp_site.name}')

        # Make sound once when everything is ready.
        # aio.create_task(self.bot.buzzer.async_on(duration=.05))
        self.bot.camera.tilt_value -= 100

        try:
            while True:
                await aio.sleep(1)
        finally:
            await app_runner.cleanup()

    @property
    def bot(self) -> Serobot:
        return self._bot

    @property
    def hardware_commander(self) -> HardwareCommander:
        return self._hardware_commander

    async def _init_queues(self):
        """Initialize asyncio queues."""
        self._client_log_queue = aio.Queue()
        self._client_image_queue = aio.Queue(maxsize=1)
        self._hardware_command_queue = aio.Queue()

    @property
    def client_log_queue(self) -> aio.Queue:
        return self._client_log_queue

    @property
    def client_image_queue(self) -> aio.Queue:
        return self._client_image_queue

    @property
    def hardware_command_queue(self) -> aio.Queue:
        return self._hardware_command_queue

    def _create_application(self) -> web.Application:
        """Create and setup the web app."""
        app = web.Application()

        # Setup cookies for authentication.
        fernet_key = fernet.Fernet.generate_key()
        secret_key = base64.urlsafe_b64decode(fernet_key)
        # cookie_max_age = 315360000  # Ten years
        cookie_max_age = 3600  # One hour
        storage = EncryptedCookieStorage(
            secret_key, cookie_name='API_SESSION', max_age=cookie_max_age)
        app.middlewares.append(aiohttp_session.session_middleware(storage))

        # Setup aiohttp-security
        policy = SessionIdentityPolicy()
        setup_security(app, policy, DictionaryAuthorizationPolicy(self._user_map))

        # Setup routes
        app.router.add_get('/', self._index_handler)
        app.router.add_get('/favicon', self._favicon_handler)
        app.router.add_post('/login', self._login_handler)
        app.router.add_get('/logout', self._logout_handler)
        app.router.add_get('/video', self._video_stream_handler)
        app.router.add_get('/ws', self._websocket_handler)
        static_dnames = ['js', 'css', 'img']
        for static_dname in static_dnames:
            path = Path(__file__).parent / 'frontend' / 'dist' / static_dname
            app.router.add_static(
                f'/{static_dname}/', path=path, name=static_dname)

        return app

    async def _status_response_worker(self, ws: web.WebSocketResponse):
        """Coroutine for sending hardware status via a websocket."""
        logger.info('Start sending status messages via the websocket.')

        while True:
            status = asdict(await self.bot.get_status())
            if not ws.closed:
                await ws.send_json(dict(status=status))
            else:
                break
            await aio.sleep(1)

        logger.info('Stopped sending status messages to a client.')

    async def _log_response_worker(self, ws: web.WebSocketResponse):
        """Coroutine for sending log messages to the client via a websocket."""
        logger.info('Start sending log messages via the websocket.')

        while True:
            message = await self.client_log_queue.get()
            message = {'log': f'Log: {message}'}

            if not ws.closed:
                await ws.send_json(message)
            else:
                break

        logger.info('Stopped sending log messages via the websocket.')

    async def _hardware_command_worker(self):
        """Coroutine for handling hardware commands sent from the app."""
        logger.info('Start handling hardware commands.')

        while True:
            # Wait for a command.
            message = await self.hardware_command_queue.get()
            logger.debug(f'Received HW command "{message}"')
            unconsumed_commands = await self.hardware_commander.command(message)
            if unconsumed_commands:
                logger.debug(f'Unknown hardware commands: {unconsumed_commands}')

    async def _camera_capture_worker(self):
        """Coroutine for continuously capturing new images by the camera."""
        jpg_stream = io.BytesIO()

        logger.info('Start capturing camera images.')
        await self.client_log_queue.put('Server is capturing camera')

        while True:
            # Take picture to the stream.
            await self.bot.camera.async_take_picture(
                jpg_stream, format='jpeg', resize=(640, 480))

            # Read jpg bytes from the start of the stream.
            jpg_stream.seek(0)
            jpg_bytes = jpg_stream.read()

            # If the previous image has not yet been taken from the queue,
            # take it away before adding a new one.
            if self.client_image_queue.full():
                self.client_image_queue.get_nowait()

            self.client_image_queue.put_nowait(jpg_bytes)

            # Reset the stream for the next capture.
            jpg_stream.seek(0)
            jpg_stream.truncate()

            await aio.sleep(.2)

    @staticmethod
    def _create_response_from_html_file(filename):
        with open(Path(__file__).parent / 'frontend' / 'dist' / filename) as f:
            text = f.read()
        return web.Response(text=text, content_type='text/html')

    async def _index_handler(self, request: web.Request):
        username = await authorized_userid(request)
        if username:
            filename = 'index.html'
        else:
            filename = 'login.html'
        return self._create_response_from_html_file(filename)

    async def _favicon_handler(self, _: web.Request):
        return web.FileResponse(Path(__file__).parent / 'frontend' / 'dist' / 'favicon.ico')

    async def _login_handler(self, request):
        form = await request.post()
        username = form.get('username')
        password = form.get('password')

        verified = await check_credentials(
            self._user_map, username, password)
        if verified:
            response = web.HTTPFound('/')
            await remember(request, response, username)
            return response

        return web.HTTPUnauthorized(body='Invalid username / password combination')

    async def _logout_handler(self, request):
        await check_authorized(request)
        response = web.HTTPFound('/')
        await forget(request, response)
        return response

    async def _video_stream_handler(self, request: web.Request, timeout: int=10):
        """Handler for streaming camera images."""
        await check_permission(request, 'protected')

        response = web.StreamResponse()
        response.content_type = f'multipart/x-mixed-replace;boundary=ffserver'
        await response.prepare(request)

        logger.info(f'Start streaming camera images to {request.remote}.')

        try:
            while True:
                # Wait for an image.
                with async_timeout.timeout(timeout):
                    data = await self.client_image_queue.get()

                if data is None:
                    raise ValueError

                # Format the binary response.
                data = (b'--ffserver\r\n' +
                        b'Content-Type: image/jpeg\r\n\r\n' +
                        data +
                        b'\r\n')

                await response.write(data)
        except (aio.TimeoutError, ValueError, ClientError):
            # Close connection gracefully if there was a problem
            # capturing images or in the connection with the client.
            await response.write_eof()
        except aio.CancelledError:
            # The connection was closed by the client.
            pass

        logger.info(f'Stopped streaming camera images to {request.remote}.')

        return response

    async def _websocket_handler(self, request: web.Request):
        """Handler for the websocket connection."""
        await check_permission(request, 'protected')

        ws = web.WebSocketResponse()
        await ws.prepare(request)

        logger.info(f'Open a websocket connection to {request.remote}.')

        # Start background tasks that feed data to the client via the websocket.
        aio.create_task(self._status_response_worker(ws))
        aio.create_task(self._log_response_worker(ws))

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                data = json.loads(msg.data)
                if msg.data == 'close':
                    await ws.close()
                elif 'command' in data:
                    self.hardware_command_queue.put_nowait(data['command'])
                else:
                    logger.debug(f'Unrecognized message: {msg.data}')
            elif msg.type == WSMsgType.ERROR:
                logger.info(f'Websocket connection closed with exception {ws.exception()}')

        logger.info(f'Closed websocket connection to {request.remote}')

        return ws
