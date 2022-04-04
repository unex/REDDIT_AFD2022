from datetime import datetime
import os
import asyncio
import traceback

from typing import List, Dict
from io import BytesIO
from datetime import datetime as dt
from datetime import timezone as tz
from datetime import timedelta
from random import random, sample

import backoff
import aiohttp
from aiohttp import ClientSession, ClientTimeout, ClientWebSocketResponse, BasicAuth
from aiohttp_socks import ProxyConnector, ProxyConnectionError

from fake_headers import Headers

import numpy as np

from PIL import Image

from pydantic import BaseModel

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase


REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")


MONGO_URI = os.environ.get("MONGO_URI")


PROXY_URL = os.environ.get("PROXY_URL")

REDDIT_TOKEN = os.environ.get("REDDIT_TOKEN")

MONA_LISA_URI = "gql-realtime-2.reddit.com/query"

# stupid
# os.environ["TZ"] = "UTC"
# time.tzset()

fheader = Headers(
    # browser="chrome",  # Generate only Chrome UA
    os="win",  # Generate ony Windows platform
    headers=False,  # generate misc headers
)


class Color(BaseModel):
    hex: str
    index: int


class RedditToken(BaseModel):
    refresh_token: str
    access_token: str = None
    expires_at: datetime = None

    @property
    def expired(self):
        return not self.expires_at or self.expires_at >= dt.now(tz.utc)


class Canvas(BaseModel):
    dx: int = 0
    dy: int = 0
    index: int = 0
    data: np.ndarray = None

    class Config:
        arbitrary_types_allowed = True


class Pixel(BaseModel):
    x: int
    y: int
    color: int
    canvas: Canvas


class PixelPlaceException(Exception):
    pass


class RedditAccount:
    def __init__(self, session, id, name, token, next_at) -> None:
        self._session: ClientSession = session

        self.id: str = id
        self.name = name
        self._token: RedditToken = token
        self.next_at: datetime = next_at

    async def token(self):
        if self._token.expired:
            await self._token_refresh(self._token.refresh_token)

        return self._token.access_token

    async def _token_refresh(self, refresh_token) -> RedditToken:
        async with self._session.post(
            "https://www.reddit.com/api/v1/access_token",
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            auth=BasicAuth(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET),
            raise_for_status=True,
        ) as r:
            data = await r.json()

            self._token = RedditToken(
                access_token=data["access_token"],
                refresh_token=data["refresh_token"],
                expires_at=dt.now(tz.utc) + timedelta(seconds=data["expires_in"]),
            )


class AFD2022:
    def __init__(self) -> None:
        self.session: ClientSession = None
        self.proxy: ClientSession = None
        self.ws: ClientWebSocketResponse = None

        self.template: List[list, list] = []

        self.colors: List[Color] = []

        self.canvas: Dict[int, Canvas()] = {}

        self.reddit: RedditAccount = None

        self.width: int = 1000
        self.height: int = 1000

        self.db: AsyncIOMotorDatabase = None

    async def run(self):
        mongo = AsyncIOMotorClient(MONGO_URI)
        await mongo.admin.command("ismaster")

        self.db = mongo.afd2022

        print("Connected to mongo")

        self.session = ClientSession()

        self.proxy = ClientSession(
            connector=ProxyConnector.from_url(PROXY_URL),
            timeout=ClientTimeout(connect=30),
        )

        self.reddit = RedditAccount(
            session=self.session,
            id="",
            name="",
            token=RedditToken(refresh_token=REDDIT_TOKEN),
            next_at=None,
        )

        asyncio.create_task(self.update_template())

        await self._ws_connect()
        await self._ws_subscribe()

        asyncio.create_task(self._ws_read())

        await asyncio.sleep(10)

        asyncio.create_task(self.pixel_loop())

    def get_next_pixel(self):
        for p in sample(self.template, len(self.template)):
            coord, color = p
            y, x = coord

            canvas = self.get_canvas_from_coords((x, y))

            cx = x % self.width
            cy = y % self.height

            color_mapping = {c.hex: c.index for c in self.colors}

            hex_color = "#%02x%02x%02x".upper() % tuple(color)

            try:
                color_index = color_mapping["#%02x%02x%02x".upper() % tuple(color)]
            except KeyError:
                print(f"Template has invalid pixel color ({hex_color}) at {(x, y)}")
                continue

            if not all(canvas.data[cy][cx] == color):
                print(f"Pixel at {(x, y)} is {canvas.data[cy][cx]}, should be {color}")
                return Pixel(
                    x=cx,
                    y=cy,
                    color=color_index,
                    canvas=canvas,
                )

    async def get_next_account(self):
        cur = self.db.accounts.find(
            {"next_at": {"$lt": datetime.now(tz.utc) + timedelta(minutes=5)}}
        ).sort("next_at", 1)

        # print(await cur.to_list(10))

        if doc := await cur.to_list(1):
            doc = doc[0]
            return RedditAccount(
                session=self.session,
                id=doc.get("id"),
                name=doc["name"],
                token=RedditToken(refresh_token=doc.get("refresh_token")),
                next_at=doc.get("next_at"),
            )

    async def pixel_loop(self):
        while True:
            try:
                if account := await self.get_next_account():
                    seconds = (
                        account.next_at.replace(tzinfo=tz.utc) - dt.now(tz.utc)
                    ).total_seconds()

                    if seconds > 0:
                        jitter = 5 + random() * 100

                        wait = seconds + jitter

                        print(
                            f"Next: {account.name} at {account.next_at} ({int(wait)} seconds)"
                        )

                        await asyncio.sleep(wait)

                    pixel = self.get_next_pixel()

                    next_at = await self.place_pixel(account, pixel)

                    print(f"{account.name} next_at {next_at} ")

                    await self.db.accounts.find_one_and_update(
                        {"id": account.id},
                        {"$set": {"next_at": next_at.replace(tzinfo=None)}},
                    )

                else:
                    await asyncio.sleep(60)

                await asyncio.sleep(random() * 10)

            except:
                traceback.print_exc()

    def get_canvas_from_coords(self, coords: tuple) -> Canvas:
        return next(
            filter(
                lambda c: c.dy == (coords[1] // self.height) * 1000
                and c.dx == (coords[0] // self.width) * 1000,
                self.canvas.values(),
            )
        )

    @backoff.on_exception(
        backoff.expo,
        (aiohttp.ClientError, asyncio.exceptions.TimeoutError),
        max_tries=10,
    )
    async def place_pixel(self, account: RedditAccount, pixel: Pixel) -> None:
        token = await account.token()

        async with self.proxy.post(
            f"https://{MONA_LISA_URI}",
            raise_for_status=True,
            headers={**fheader.generate(), "Authorization": f"Bearer {token}"},
            json={
                "operationName": "setPixel",
                "variables": {
                    "input": {
                        "actionName": "r/replace:set_pixel",
                        "PixelMessageData": {
                            "coordinate": {"x": pixel.x, "y": pixel.y},
                            "colorIndex": pixel.color,
                            "canvasIndex": pixel.canvas.index,
                        },
                    }
                },
                "query": "mutation setPixel($input: ActInput!) {\n  act(input: $input) {\n    data {\n      ... on BasicMessage {\n        id\n        data {\n          ... on GetUserCooldownResponseMessageData {\n            nextAvailablePixelTimestamp\n            __typename\n          }\n          ... on SetPixelResponseMessageData {\n            timestamp\n            __typename\n          }\n          __typename\n        }\n        __typename\n      }\n      __typename\n    }\n    __typename\n  }\n}\n",
            },
        ) as r:
            data = await r.json()

            print(data)

            if errors := data.get("errors"):
                print(f"{account.name} pixel place error: {errors}")

                if errors[0]["message"] == "Ratelimited":
                    return dt.fromtimestamp(
                        data["errors"][0]["extensions"]["nextAvailablePixelTs"] / 1000
                    ).replace(tzinfo=tz.utc)

            if data := data.get("data"):
                print(
                    f"{account.name} placed pixel {(pixel.canvas.dx + pixel.x, pixel.canvas.dy + pixel.y)}"
                )

                # if I dont go off server time sometimes I am too fast????
                timestamp = data["act"]["data"][0]["data"][
                    "nextAvailablePixelTimestamp"
                ]
                now = data["act"]["data"][1]["data"]["timestamp"]

                seconds = (timestamp - now) / 1000

                return dt.now(tz.utc) + timedelta(seconds=seconds)

    async def update_template(self):
        while True:
            try:
                async with self.session.get(
                    "https://haykam.com/place/template.png"
                ) as r:
                    img_data = BytesIO(await r.content.read())
                    img_data.seek(0)

                    img = Image.open(img_data).convert("RGBA")

                    # img = Image.open("./template.png").convert("RGBA")
                    np_img = np.array(img)

                    coords = self._get_nontransparent_pixels(np_img)

                    template = []
                    for c in coords:
                        y, x = c

                        template.append([c, np_img[y][x][:-1]])

                    self.template = template

                    print("Loaded template")

            except:
                print("Error in update_template")
                traceback.print_exc()

            await asyncio.sleep(60 * 10)

    async def _ws_connect(self):
        token = await self.reddit.token()

        self.ws = await self.session.ws_connect(
            f"wss://{MONA_LISA_URI}",
            headers={
                "Sec-WebSocket-Protocol": "graphql-ws",
                "Origin": "https://hot-potato.reddit.com",
            },
        )

        await self.ws.send_json(
            {"type": "connection_init", "payload": {"Authorization": f"Bearer {token}"}}
        )

        r = await self.ws.receive_json()
        if r and r["type"] == "connection_ack":
            print("Connected to mona_lisa websocket")

    async def _ws_subscribe(self):
        await self.ws.send_json(
            {
                "id": "config",
                "type": "start",
                "payload": {
                    "variables": {
                        "input": {
                            "channel": {"teamOwner": "AFD2022", "category": "CONFIG"}
                        }
                    },
                    "extensions": {},
                    "operationName": "configuration",
                    "query": """
                    subscription configuration($input: SubscribeInput!) {
                        subscribe(input: $input) {
                            id
                            ... on BasicMessage {
                                data {
                                    __typename
                                    ... on ConfigurationMessageData {
                                        colorPalette {
                                            colors {
                                                hex
                                                index
                                                __typename
                                        }
                                        __typename
                                    }
                                    canvasConfigurations {
                                        index
                                        dx
                                        dy
                                        __typename
                                    }
                                    canvasWidth
                                    canvasHeight
                                    __typename
                                }
                            }
                            __typename
                        }
                        __typename
                    }
                }
            """,
                },
            }
        )

    async def _ws_read(self):
        while not self.ws.closed:
            try:
                data = await self.ws.receive_json()

                # print(data)
                # from pprint import pprint
                # pprint(data)

                _id = data.get("id")

                if data.get("type") == "ka":
                    continue

                if data.get("type") == "data":
                    data = data["payload"]["data"]["subscribe"]["data"]

                    if data["__typename"] == "ConfigurationMessageData":
                        if x := data.get("canvasConfigurations"):
                            print(f"New canvas config: {x}")

                            for c in x:
                                i = c["index"]

                                canvas = Canvas(dx=c["dx"], dy=c["dy"], index=i)

                                self.canvas[i] = canvas

                                await self.ws.send_json(
                                    {
                                        "id": f"canvas{i}",
                                        "type": "start",
                                        "payload": {
                                            "variables": {
                                                "input": {
                                                    "channel": {
                                                        "teamOwner": "AFD2022",
                                                        "category": "CANVAS",
                                                        "tag": str(i),
                                                    }
                                                }
                                            },
                                            "extensions": {},
                                            "operationName": "replace",
                                            "query": """
                                            subscription replace($input: SubscribeInput!) {
                                                subscribe(input: $input) {
                                                    id
                                                    ... on BasicMessage {
                                                    data {
                                                        __typename
                                                        ... on FullFrameMessageData {
                                                            __typename
                                                            name
                                                            timestamp
                                                        }
                                                        ... on DiffFrameMessageData {
                                                            __typename
                                                            name
                                                            currentTimestamp
                                                            previousTimestamp
                                                        }
                                                    }
                                                    __typename
                                                }
                                                __typename
                                            }
                                        }
                                    """,
                                        },
                                    }
                                )

                        if x := data.get("canvasHeight"):
                            print(f"New canvas height: {x}")

                            self.height = x

                        if x := data.get("canvasWidth"):
                            print(f"New canvas width: {x}")

                            self.width = x

                        if x := data.get("colorPalette"):
                            print(f"New color palette")

                            self.colors = [Color(**c) for c in x["colors"]]

                    elif data["__typename"] == "FullFrameMessageData":
                        url = data["name"]

                        async with self.session.get(url) as r:
                            img_data = BytesIO(await r.content.read())
                            img_data.seek(0)

                            img = Image.open(img_data).convert("RGB")

                            i = int(_id.replace("canvas", ""))
                            self.canvas[i].data = np.array(img)

                            print(f"Loaded full frame {i}")

                    elif data["__typename"] == "DiffFrameMessageData":
                        url = data["name"]

                        async with self.session.get(url) as r:
                            img_data = BytesIO(await r.content.read())
                            img_data.seek(0)

                            img = Image.open(img_data).convert("RGBA")
                            np_img = np.array(img)

                            changes = self._get_nontransparent_pixels(np_img)

                            for c in changes:
                                x, y = c

                                i = int(_id.replace("canvas", ""))
                                self.canvas[i].data[x][y] = np_img[x][y][:-1]

                            # print(f"{len(changes)} changed pixels")

            except TypeError:
                msg = await self.ws.receive()

                print(msg)

    def _get_nontransparent_pixels(self, img: np.ndarray) -> np.ndarray:
        x, y = np.where(np.all(img != (0, 0, 0, 0), axis=2))
        return np.column_stack((x, y))

    async def close(self):
        await self.ws.close()
        await self.session.close()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()

    app = AFD2022()

    try:
        loop.run_until_complete(app.run())

        loop.run_forever()

    except KeyboardInterrupt:
        pass

    finally:
        asyncio.run(app.close())
        loop.close()
