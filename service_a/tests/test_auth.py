"""Sessions: who may act, and for how long.

| Test | Purpose | Method |
| --- | --- | --- |
| test_register_then_me | Registering signs the user in | Register -> /me shows the user, IDs start at 100000 |
| test_password_is_not_stored | A database leak reveals no password | Stored hash is Argon2id, not the password |
| test_duplicate_username | Usernames are unique | Second register -> 409 USERNAME_TAKEN |
| test_wrong_password_and_unknown_user_look_the_same | No hint about who exists | Both -> 401 with one message |
| test_no_cookie_is_unauthorized | Accounts are private | No login -> 401 |
| test_access_token_expires | A copied token stops working | After 15 minutes -> 401 |
| test_refresh_rotates_and_restores_access | A session lasts beyond the access token | Expired access, refresh -> works, new refresh token |
| test_reuse_after_grace_revokes_the_family | A stolen refresh token is detected | Old token after the grace period -> 401, and the newest dies too |
| test_reuse_within_grace_is_not_theft | Two tabs refreshing at once are not an attack | Same token twice within the grace -> both 204, family alive |
| test_logout_revokes_the_session | Logging out ends the refresh path | After logout, the old refresh token -> 401 |
| test_foreign_origin_is_refused | Another site cannot act with the user's cookies | Foreign Origin -> 403; no Origin (a bot) -> allowed |
| test_body_must_be_json | Cross-site form posts are refused | Form body -> 415 |
| test_login_flood_is_shed | A flood cannot exhaust the hashing capacity | All slots busy -> 429 at once |
| test_role_reaches_the_browser | The frontend can tell an admin from a user | Registered user -> USER in register and /me; seeded admin -> ADMIN in login and /me |
| test_refresh_keeps_the_role | The role survives the 15-minute token | Admin's access expires, refresh -> /me still ADMIN |
| test_admin_name_is_not_a_role | Nobody becomes admin by choosing the name | Register "admin" -> 409; the seeded admin keeps ID 1000 |
| test_token_without_a_known_role_is_a_user | Old or tampered role claims grant nothing | Token without role, or role "ROOT" -> USER |
"""
import asyncio

from sqlalchemy import text

import jwt

from app.auth.deps import ACCESS_COOKIE, REFRESH_COOKIE
from app.auth.tokens import ALGORITHM
from tests.conftest import sign_up

CREDS = {"username": "amy_trader", "password": "correct-horse-1"}


async def test_register_then_me(client):
    # Purpose: registering signs the user in, with a registered-range ID.
    user = await sign_up(client)
    assert user["user_id"] >= 100000
    r = await client.get("/api/auth/me")
    assert r.status_code == 200 and r.json()["username"] == "amy_trader"


async def test_password_is_not_stored(client, container):
    # Purpose: only an Argon2id hash reaches the database.
    await sign_up(client)
    async with container.engine.connect() as conn:
        stored = (await conn.execute(text("SELECT password_hash FROM users"))).scalar()
    assert stored.startswith("$argon2id$") and CREDS["password"] not in stored


async def test_duplicate_username(client):
    # Purpose: usernames identify users.
    await sign_up(client)
    r = await client.post("/api/auth/register", json=CREDS)
    assert r.status_code == 409 and r.json()["reason"] == "USERNAME_TAKEN"


async def test_wrong_password_and_unknown_user_look_the_same(client):
    # Purpose: the answer must not reveal which usernames exist.
    await sign_up(client)
    client.cookies.clear()
    wrong = await client.post("/api/auth/login", json={**CREDS, "password": "wrong-password"})
    unknown = await client.post("/api/auth/login", json={**CREDS, "username": "nobody_here"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


async def test_no_cookie_is_unauthorized(client):
    # Purpose: without a session there is no account access.
    assert (await client.get("/api/accounts/me")).status_code == 401


async def test_access_token_expires(client, clock):
    # Purpose: the accepted window for a copied access token is at most 15 minutes.
    await sign_up(client)
    token = client.cookies.get(ACCESS_COOKIE)
    clock.advance(15 * 60 * 1000 - 1000)
    assert (await client.get("/api/auth/me")).status_code == 200
    clock.advance(1000)
    client.cookies.set(ACCESS_COOKIE, token)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_refresh_rotates_and_restores_access(client, clock):
    # Purpose: a refresh restores access and replaces the refresh token.
    await sign_up(client)
    old = client.cookies.get(REFRESH_COOKIE)
    clock.advance(16 * 60 * 1000)
    assert (await client.post("/api/auth/refresh")).status_code == 204
    assert client.cookies.get(REFRESH_COOKIE) != old
    assert (await client.get("/api/auth/me")).status_code == 200


async def test_reuse_after_grace_revokes_the_family(client, clock):
    # Purpose: replaying a spent token means it was copied, so the attacker's token dies as well.
    await sign_up(client)
    stolen = client.cookies.get(REFRESH_COOKIE)
    await client.post("/api/auth/refresh")
    newest = client.cookies.get(REFRESH_COOKIE)
    clock.advance(6000)
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE, stolen, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE, newest, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_reuse_within_grace_is_not_theft(client, clock):
    # Purpose: two tabs spending one token at once must not log the user out.
    await sign_up(client)
    shared = client.cookies.get(REFRESH_COOKIE)
    assert (await client.post("/api/auth/refresh")).status_code == 204
    newest = client.cookies.get(REFRESH_COOKIE)
    clock.advance(1000)
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE, shared, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 204
    assert (await client.get("/api/auth/me")).status_code == 200
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE, newest, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 204


async def test_logout_revokes_the_session(client):
    # Purpose: after logout the refresh path is closed, even for a copy of the token.
    await sign_up(client)
    token = client.cookies.get(REFRESH_COOKIE)
    assert (await client.post("/api/auth/logout")).status_code == 204
    client.cookies.clear()
    client.cookies.set(REFRESH_COOKIE, token, path="/api/auth")
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_foreign_origin_is_refused(client):
    # Purpose: SameSite does not cover another port on this host, so Origin is checked.
    r = await client.post("/api/auth/register", json=CREDS, headers={"Origin": "http://localhost:8080"})
    assert r.status_code == 403 and r.json()["reason"] == "FORBIDDEN_ORIGIN"
    ok = await client.post("/api/auth/register", json=CREDS, headers={"Origin": "http://localhost:3000"})
    assert ok.status_code == 201
    assert (await client.post("/api/auth/logout")).status_code == 204


async def test_body_must_be_json(client):
    # Purpose: a cross-site form cannot produce a request A accepts.
    r = await client.post("/api/auth/login", content="username=a&password=b",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415


async def test_login_flood_is_shed(client, container):
    # Purpose: with every hashing slot busy, a login is refused at once instead of queueing.
    await sign_up(client)
    slots = container.passwords._slots
    taken = [await slots.acquire() for _ in range(container.settings.hash_concurrency)]
    assert all(taken)
    r = await asyncio.wait_for(client.post("/api/auth/login", json=CREDS), timeout=1)
    assert r.status_code == 429 and r.json()["reason"] == "RATE_LIMITED"
    for _ in taken:
        slots.release()


ADMIN = {"username": "admin", "password": "lame-demo-password"}      # seeded with SEED_PASSWORD's default


async def test_role_reaches_the_browser(client):
    # Purpose: the role the dashboard link depends on is in every answer that describes the user.
    user = await sign_up(client)
    assert user["role"] == "USER" and (await client.get("/api/auth/me")).json()["role"] == "USER"
    client.cookies.clear()
    r = await client.post("/api/auth/login", json=ADMIN)
    assert r.status_code == 200 and r.json() == {"user_id": 1000, "username": "admin", "role": "ADMIN"}
    assert (await client.get("/api/auth/me")).json()["role"] == "ADMIN"


async def test_refresh_keeps_the_role(client, clock):
    # Purpose: a refreshed access token carries the role from the database, not a default.
    await client.post("/api/auth/login", json=ADMIN)
    clock.advance(16 * 60 * 1000)
    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await client.post("/api/auth/refresh")).status_code == 204
    assert (await client.get("/api/auth/me")).json()["role"] == "ADMIN"


async def test_admin_name_is_not_a_role(client, container):
    # Purpose: registering the name "admin" is refused, and only the seeded row holds the role.
    r = await client.post("/api/auth/register", json={"username": "admin", "password": "another-pass-1"})
    assert r.status_code == 409 and r.json()["reason"] == "USERNAME_TAKEN"
    async with container.engine.connect() as conn:
        admins = (await conn.execute(text("SELECT user_id FROM users WHERE role = 'ADMIN'"))).scalars().all()
    assert admins == [1000]


async def test_token_without_a_known_role_is_a_user(container, clock):
    # Purpose: a token issued before roles existed, or a forged claim, never yields ADMIN.
    secret, now = container.settings.jwt_secret, clock.now_ms() // 1000
    base = {"sub": "100000", "name": "amy_trader", "iat": now, "exp": now + 900}
    for claims in (base, {**base, "role": "ROOT"}):
        principal = container.tokens.verify_access(jwt.encode(claims, secret, algorithm=ALGORITHM))
        assert principal is not None and principal.role == "USER"
