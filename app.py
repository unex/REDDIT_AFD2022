import os
import traceback

from urllib.parse import quote_plus
from secrets import token_urlsafe
from datetime import datetime as dt
from datetime import timezone as tz

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, HTMLResponse

from starlette.middleware.sessions import SessionMiddleware

from motor.motor_asyncio import AsyncIOMotorClient

from aiohttp import ClientSession, BasicAuth

REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET")
REDDIT_REDIRECT_URI = os.environ.get("REDDIT_REDIRECT_URI")

SECRET_KEY = os.environ.get("SECRET_KEY")

MONGO_URI = os.environ.get("MONGO_URI")

app = FastAPI(
    title="",
    description="",
    docs_url=None,
    openapi_url=None,
    redoc_url=None,
)

app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)

REDDIT_AUTH = BasicAuth(REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET)


@app.on_event("startup")
async def on_startup():
    app.session = ClientSession(
        raise_for_status=True,
        headers={"User-Agent": "https://github.com/unex/REDDIT_AFD2022"},
    )

    mongo = AsyncIOMotorClient(MONGO_URI)
    await mongo.admin.command("ismaster")

    app.db = mongo.afd2022


@app.on_event("shutdown")
async def on_shutdown():
    await app.session.close()


async def oauth_confirm(code) -> dict:
    async with app.session.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=REDDIT_AUTH,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDDIT_REDIRECT_URI,
        },
    ) as r:
        return await r.json()


async def get_user(token):
    async with app.session.get(
        "https://oauth.reddit.com/api/v1/me",
        headers={"Authorization": f"Bearer {token}"},
    ) as r:
        return await r.json()


@app.get("/")
async def root(request: Request, state: str = None, code: str = None):
    if "name" in request.session:
        return HTMLResponse(f"<code>Welcome {request.session['name']}</code>")

    if (
        code
        and state
        and "state" in request.session
        and request.session["state"] == state
    ):
        del request.session["state"]

        try:
            auth = await oauth_confirm(code)

            user = await get_user(auth["access_token"])

            await app.db.accounts.find_one_and_update(
                {"id": user["id"]},
                {
                    "$set": {
                        "refresh_token": auth["refresh_token"],
                    },
                    "$setOnInsert": {
                        "name": user["name"],
                        "next_at": dt.fromtimestamp(0),
                    },
                },
                upsert=True,
            )

            request.session["name"] = user["name"]

        except:
            traceback.print_exc()

        return RedirectResponse(app.url_path_for("root"))

    state = token_urlsafe()
    request.session["state"] = state
    return RedirectResponse(
        f"https://www.reddit.com/api/v1/authorize?client_id={REDDIT_CLIENT_ID}&response_type=code&redirect_uri={quote_plus(REDDIT_REDIRECT_URI)}&duration=permanent&scope=identity&state={state}"
    )


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(app.url_path_for("root"))
