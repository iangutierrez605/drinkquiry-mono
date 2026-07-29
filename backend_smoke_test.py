"""E2E smoke test: mirrors the frontend's REST + WS call sequences exactly.

Paid-tier quota *boundaries* (tiny limits, expiry) and game-logic regressions
live in the Django suite: `python backend/manage.py test accounts trivia games`
— this script checks the live-server surface: profile shape, the structured
free-user 403, and (Handoff #6) the polling-board reveal + history/report.
"""
import asyncio, io, json, os, sys, time, zipfile
import requests, websockets

# Target host is env-driven (no hardcoded IPs — Handoff #5): point SMOKE_BASE
# at any dev box / LAN IP / staging host, e.g. SMOKE_BASE=http://192.168.0.42:8000
BASE = os.environ.get("SMOKE_BASE", "http://localhost:8000").rstrip("/")
WS = BASE.replace("http", "ws", 1)
# Origin header for the WS handshake — must be a host the server's
# ALLOWED_HOSTS accepts (any value passes when DEBUG=true).
ORIGIN = os.environ.get("SMOKE_ORIGIN", "http://localhost:5173")
ok = lambda m: print(f"  ✓ {m}")

def rest():
    r = requests.post(f"{BASE}/api/auth/register/", json={
        "email": "host@test.com", "password": "sturdy-pass-123", "display_name": "Quizmaster"})
    assert r.status_code in (200, 201) or "already exists" in r.text, r.text
    ok("register (or already registered)")
    r = requests.post(f"{BASE}/api/auth/login/", json={"email": "host@test.com", "password": "sturdy-pass-123"})
    assert r.status_code == 200, r.text
    knox = r.json()["token"]; ok(f"login (JSON body) → knox token")

    # --- paid tiers (Handoff #3 §D): profile shape + free-user quota 403 ---
    r = requests.get(f"{BASE}/api/auth/profile/", headers={"Authorization": f"Token {knox}"})
    p = r.json()
    assert r.status_code == 200 and p["plan"] in ("free", "creator"), r.text
    assert "plan_expires_at" in p and isinstance(p["is_creator"], bool)
    for k in ("games_this_month", "categories", "questions", "storage"):
        blk = p["usage"][k]
        assert set(blk) == {"used", "limit"} and isinstance(blk["used"], int)
        assert blk["limit"] is None or isinstance(blk["limit"], int)
    ok(f"profile: plan={p['plan']}, usage block shaped per D3 incl. §F3 storage (limit null = unlimited)")

    free_email = f"free-{int(time.time())}@test.com"
    requests.post(f"{BASE}/api/auth/register/", json={
        "email": free_email, "password": "sturdy-pass-123", "display_name": "Freeloader"})
    fknox = requests.post(f"{BASE}/api/auth/login/",
                          json={"email": free_email, "password": "sturdy-pass-123"}).json()["token"]
    r = requests.post(f"{BASE}/api/categories/", headers={"Authorization": f"Token {fknox}"},
                      json={"name": "Nope", "visibility": "private"})
    b = r.json()
    assert r.status_code == 403 and b.get("code") == "quota_categories", r.text
    assert b["used"] == 0 and b["limit"] == 0 and "detail" in b, b
    ok("free user content create → structured 403 {code: quota_categories, used: 0, limit: 0}")

    # --- Handoff #4 §F/§G live-surface checks -----------------------------
    # host@test.com is a plain (non-staff) account: every moderation endpoint
    # must reject it. If you promote it in /admin, export SMOKE_STAFF_EMAIL /
    # SMOKE_STAFF_PASSWORD instead and the staff round-trip below runs too.

    staff_email, staff_pass = os.environ.get("SMOKE_STAFF_EMAIL"), os.environ.get("SMOKE_STAFF_PASSWORD")
    r = requests.get(f"{BASE}/api/moderation/questions/", headers={"Authorization": f"Token {fknox}"})
    assert r.status_code == 403, r.text
    r = requests.get(f"{BASE}/api/moderation/counts/", headers={"Authorization": f"Token {fknox}"})
    assert r.status_code == 403, r.text
    ok("non-staff → 403 on /api/moderation/* (server-enforced, frontend gate is cosmetic)")
    if staff_email and staff_pass:
        sknox = requests.post(f"{BASE}/api/auth/login/",
                              json={"email": staff_email, "password": staff_pass}).json()["token"]
        sh = {"Authorization": f"Token {sknox}"}
        sp = requests.get(f"{BASE}/api/auth/profile/", headers=sh).json()
        assert sp.get("is_staff") is True, sp
        counts = requests.get(f"{BASE}/api/moderation/counts/", headers=sh).json()
        assert set(counts) == {"categories", "questions"}, counts
        r = requests.get(f"{BASE}/api/moderation/questions/", headers=sh)
        assert r.status_code == 200 and "results" in r.json(), r.text
        rows = r.json()["results"]
        if rows:  # full round-trip only if something is actually pending
            qid = rows[0]["id"]
            r = requests.post(f"{BASE}/api/moderation/questions/{qid}/reject/", headers=sh, json={})
            assert r.status_code == 400, r.text  # note required
            r = requests.post(f"{BASE}/api/moderation/questions/{qid}/approve/", headers=sh)
            assert r.status_code == 200 and r.json()["moderation_status"] == "approved", r.text
            r = requests.post(f"{BASE}/api/moderation/questions/{qid}/approve/", headers=sh)
            assert r.status_code == 409, r.text  # double-act guard
            ok(f"staff moderation round-trip on question {qid} (reject-needs-note, approve, 409 on repeat)")
        else:
            ok("staff moderation surface reachable (queue empty — no round-trip)")
    # Free user bulk dry-run → structured quota 403 with `requested` (§G2).
    csv_bytes = b"category,question_text,answer,difficulty,visibility\nMovies,Smoke test?,Yes,1,private\n"
    r = requests.post(f"{BASE}/api/questions/bulk/", headers={"Authorization": f"Token {fknox}"},
                      files={"file": ("smoke.csv", csv_bytes, "text/csv")}, data={"dry_run": "true"})
    b = r.json()
    assert r.status_code == 403 and b.get("code") == "quota_questions", r.text
    assert b["limit"] == 0 and b["requested"] == 1, b
    ok("free user bulk CSV dry-run → structured 403 {code: quota_questions, requested: 1}")

    # Zip-with-media dry run (Handoff #5 §G): exercise the live zipfile path
    # once. Parsing (member vetting, CSV extraction, media validation) runs
    # BEFORE the quota check, so a free user's structured 403 proves the zip
    # branch works end-to-end without writing anything.
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as zzf:
        zzf.writestr("questions.csv",
                     "category,question_text,answer,difficulty,visibility,media_type,media_file\n"
                     "Movies,Smoke zip?,Yes,1,private,image,pic.png\n")
        zzf.writestr("pic.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    r = requests.post(f"{BASE}/api/questions/bulk/", headers={"Authorization": f"Token {fknox}"},
                      files={"file": ("smoke.zip", zbuf.getvalue(), "application/zip")},
                      data={"dry_run": "true"})
    b = r.json()
    assert r.status_code == 403 and b.get("code") == "quota_questions", r.text
    assert b["requested"] == 1, b
    ok("free user zip-with-media dry-run → parsed as zip, structured 403 (§G live surface)")

    r = requests.get(f"{BASE}/api/categories/", headers={"Authorization": f"Token {knox}"})
    cats = [c for c in r.json()["results"] if c["owner"] is None]
    assert all(c["usable_question_count"] == 5 for c in cats)
    ok(f"categories: {[c['name'] for c in cats]} (paginated, unwrapped like api.js)")

    # shortage 400 path the host UI must surface
    r = requests.post(f"{BASE}/api/games/", headers={"Authorization": f"Token {knox}"},
                      json={"mode": "drinks", "categories": [c["id"] for c in cats[:2]], "questions_per_category": 8})
    assert r.status_code == 400 and "categories" in r.json(), r.text
    ok(f"shortage 400 surfaced: {r.json()['categories'][0][:60]}…")

    r = requests.post(f"{BASE}/api/games/", headers={"Authorization": f"Token {knox}"},
                      json={"mode": "drinks", "categories": [c["id"] for c in cats[:3]], "questions_per_category": 5})
    assert r.status_code == 201, r.text
    d = r.json(); code, host_token = d["game"]["code"], d["participant_token"]
    ok(f"create game → {code}, host participant token")

    r = requests.get(f"{BASE}/api/games/{code}/")
    assert r.status_code == 200 and r.json()["status"] == "lobby"
    ok("GET snapshot without auth (board polling path)")

    j = lambda body: requests.post(f"{BASE}/api/games/{code}/join/", json=body)
    # §H1 (Handoff #8): joins are uppercased SERVER-side — a lowercase name
    # comes back caps in the join response and the snapshot.
    r = j({"name": "team a"}); assert r.status_code == 201, r.text
    assert r.json()["participant"]["name"] == "TEAM A", r.text
    a_tok, a_id = r.json()["participant_token"], r.json()["participant"]["id"]
    r = j({"name": "Team B"}); assert r.status_code == 201
    assert r.json()["participant"]["name"] == "TEAM B", r.text
    b_tok, b_id = r.json()["participant_token"], r.json()["participant"]["id"]
    ok("two teams join by name; lowercase 'team a' comes back 'TEAM A' (§H1)")
    r = j({"name": "Team A"}); assert r.status_code == 400 and "name" in r.json()
    r = j({"name": "team b"}); assert r.status_code == 400 and "name" in r.json()
    ok("duplicate name → 400 name-taken, case-insensitively (§H1)")
    r = j({"name": "Team A", "participant_token": a_tok})
    assert r.status_code == 200 and r.json()["participant"]["id"] == a_id
    ok("reclaim seat with stored token → 200 same participant (reload flow)")

    # --- Handoff #7 §G/§I: player cap + buzzer sound assignment ------------
    snap = requests.get(f"{BASE}/api/games/{code}/").json()
    assert snap.get("max_players") == 6, snap.get("max_players")
    for nm in ("Team C", "Team D", "Team E", "Team F"):
        r = j({"name": nm}); assert r.status_code == 201, r.text
        assert "buzzer_sound" in r.json()["participant"], r.text  # §I: in the join response
    r = j({"name": "Team G"})
    assert r.status_code == 409, r.text
    full = r.json()
    assert set(full) == {"detail", "code", "limit"}, full  # NEW contract, exact shape
    assert full["code"] == "game_full" and full["limit"] == 6, full
    ok('7th team join → 409 {"detail", code: "game_full", limit: 6} exact shape (§G)')
    r = j({"name": "Team A", "participant_token": a_tok})
    assert r.status_code == 200, r.text
    ok("reclaim still 200 AT the cap — reload flow survives a full table (§G)")
    snap = requests.get(f"{BASE}/api/games/{code}/").json()
    sounds = {p["name"]: p.get("buzzer_sound") for p in snap["participants"] if p["role"] == "player"}
    expect = {"TEAM A": 1, "TEAM B": 2, "TEAM C": 3, "TEAM D": 4, "TEAM E": 1, "TEAM F": 2}
    assert sounds == expect, sounds  # §H1: snapshot names are ALL CAPS now
    ok(f"buzzer_sound in snapshot, round-robin by join order: {[sounds[n] for n in sorted(expect)]} (§I)")
    # The extra teams C–F just prove the cap; the game keeps playing with the
    # existing two, so the downstream WS flow is untouched.
    return code, host_token, a_tok, a_id, b_tok, b_id, knox, fknox

async def recv_until(ws, want, timeout=5):
    while True:
        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
        if msg["type"] == want:
            return msg

async def latest_state(ws, timeout=5, settle=0.5):
    """Wait for at least one state, then keep draining until quiet; return last."""
    msg = await recv_until(ws, "state", timeout)
    last = msg
    while True:
        try:
            m = json.loads(await asyncio.wait_for(ws.recv(), settle))
            if m["type"] == "state":
                last = m
        except asyncio.TimeoutError:
            return last

async def drain(ws):
    try:
        while True:
            await asyncio.wait_for(ws.recv(), 0.3)
    except asyncio.TimeoutError:
        pass

async def ws_flow(code, host_tok, a_tok, a_id, b_tok, b_id, knox, fknox):
    url = lambda t: f"{WS}/ws/game/{code}/?token={t}"
    # bad token → accepted, then closed with app code 4001 (Handoff #3 §E1;
    # previously a handshake-level 403 the browser reported as 1006)
    try:
        async with websockets.connect(url("garbage"), origin=ORIGIN) as w:
            await w.recv()
        raise AssertionError("bad token accepted and kept open")
    except websockets.exceptions.ConnectionClosed as e:
        assert e.rcvd and e.rcvd.code == 4001, e
        ok("invalid token → accept-then-close 4001 (hook's direct path; REST probe stays as fallback)")

    async with websockets.connect(url(host_tok), origin=ORIGIN) as host, \
               websockets.connect(url(a_tok), origin=ORIGIN) as pa, \
               websockets.connect(url(b_tok), origin=ORIGIN) as pb:
        s = (await latest_state(host))["game"]
        assert {p["name"] for p in s["participants"]} >= {"TEAM A", "TEAM B"}
        assert all(c["cells"][0]["value"] == 1 for c in s["columns"])
        ok("snapshot on connect; drinks-mode values = row drinks")

        async def act(ws, action, **payload):
            await ws.send(json.dumps({"action": action, **payload}))

        # player tries a host action → error to sender only
        await act(pa, "open_cell", cell_id=s["columns"][0]["cells"][0]["id"])
        e = await recv_until(pa, "error"); ok(f"player host-action → error: '{e['detail']}'")

        await act(host, "start_game")
        s = (await latest_state(host))["game"]; assert s["status"] == "active"
        ok("start_game → active")
        await asyncio.gather(drain(pa), drain(pb))

        cell_id = s["columns"][0]["cells"][0]["id"]
        await act(host, "open_cell", cell_id=cell_id)
        s = (await latest_state(host))["game"]
        assert s["current_cell"]["id"] == cell_id and s["buzzer_open"] is False
        assert "answer" not in s["current_cell"]
        assert s["revealed_answer"] is None  # §F2: null at all times pre-reveal
        ok("open_cell → question visible, buzzer locked, no answer in snapshot, revealed_answer null")

        # §F1: host-private answer side channel (Knox), pre-reveal by design.
        hh = {"Authorization": f"Token {knox}"}
        r = requests.get(f"{BASE}/api/games/{code}/answer/", headers=hh)
        assert r.status_code == 200 and set(r.json()) == {"question_id", "answer"}, r.text
        the_answer = r.json()["answer"]
        r = requests.get(f"{BASE}/api/games/{code}/answer/", headers={"Authorization": f"Token {fknox}"})
        assert r.status_code == 403, r.text
        r = requests.get(f"{BASE}/api/games/{code}/answer/")
        assert r.status_code == 401, r.text
        # Rule 5, live: pre-reveal the answer string appears NOWHERE in the
        # snapshot a polling board fetches.
        polled = requests.get(f"{BASE}/api/games/{code}/").json()
        assert polled["revealed_answer"] is None and the_answer not in json.dumps(polled)
        ok("§F1 host answer endpoint (host 200, other Knox 403, anon 401); polled snapshot pre-reveal is answer-free")

        # buzz while locked → error
        await drain(pa); await act(pa, "buzz")
        e = await recv_until(pa, "error"); ok(f"buzz while locked → '{e['detail']}'")

        await act(host, "open_buzzer")
        await recv_until(host, "state")
        await asyncio.gather(drain(pa), drain(pb))
        await act(pa, "buzz"); await asyncio.sleep(0.15); await act(pb, "buzz")
        bz = await recv_until(host, "buzz")
        assert bz["participant_id"] == a_id and bz["order"] == 1 and bz["name"] == "TEAM A", bz
        ok("incremental {type:'buzz'} event emitted (§E2; hook prefers it over the snapshot diff)")
        s = (await latest_state(host))["game"]
        assert [b["participant_id"] for b in s["current_cell"]["buzzes"]][0] == a_id
        ok("each buzz broadcasts a snapshot (hook synthesizes lastBuzz from the diff)")
        await act(host, "lock_buzzer")
        s = (await latest_state(host))["game"]
        order = [b["participant_id"] for b in s["current_cell"]["buzzes"]]
        assert order == [a_id, b_id], order
        ok(f"buzz race recorded in order: {order}")

        # duplicate buzz → error
        await drain(pa); await act(pa, "buzz")
        e = await recv_until(pa, "error"); ok(f"double buzz → '{e['detail']}'")

        # wrong first → buzzer reopens, verdict marker set, answer STILL hidden
        await act(host, "judge", participant_id=a_id, correct=False)
        s = (await latest_state(host))["game"]
        assert s["buzzer_open"] is True; ok("judge wrong → buzzer reopens")
        lj = s["current_cell"]["last_judgment"]
        assert lj == {"participant_id": a_id, "name": "TEAM A", "correct": False}, lj
        assert s["revealed_answer"] is None and the_answer not in json.dumps(s)
        ok("judge wrong → last_judgment marker set, answer still hidden (§F)")
        # right second — §F: the reveal arrives WITH the judgment, no
        # separate reveal action sent.
        await act(host, "judge", participant_id=b_id, correct=True)
        s = (await latest_state(host))["game"]
        cell = next(c for col in s["columns"] for c in col["cells"] if c["id"] == cell_id)
        assert cell["answered_correctly"] and cell["answered_by"] == b_id
        lj = s["current_cell"]["last_judgment"]
        assert lj == {"participant_id": b_id, "name": "TEAM B", "correct": True}, lj
        assert s["revealed_answer"] == the_answer, s.get("revealed_answer")
        ok("judge correct → cell won by TEAM B, buzzer locked, revealed_answer arrives WITH the judgment (§F)")
        # §F2 live coverage for the polling-board path rides the judge-driven
        # reveal now; the explicit host action stays for "nobody got it" and
        # its legacy WS event is still emitted (idempotent re-reveal).
        polled = requests.get(f"{BASE}/api/games/{code}/").json()
        assert polled["revealed_answer"] == the_answer, polled.get("revealed_answer")
        ok("revealed_answer present in polled snapshot after the judgment (§F2)")
        await act(host, "reveal_answer")
        rv = await recv_until(host, "answer_reveal")
        assert rv["answer"] == the_answer
        ok(f"explicit reveal still works + legacy answer_reveal event: '{rv['answer'][:30]}…'")

        # --- §G: the WINNING team's phone gives the drink — to the HOST ----
        host_seat_id = next(p["id"] for p in s["participants"] if p["role"] == "host")
        assert s["current_cell"]["drinks_assigned"] is False
        await drain(pb)
        await act(pb, "give_drink", target_participant_id=host_seat_id)
        s = (await latest_state(host))["game"]
        pmap = {p["id"]: p for p in s["participants"]}
        assert pmap[host_seat_id]["drinks_taken"] == 1 and pmap[b_id]["drinks_given"] == 1, pmap
        assert s["current_cell"]["drinks_assigned"] is True
        attribution = s["current_cell"]["drink_assignment"]
        assert attribution["from_participant_id"] == b_id and attribution["to_participant_id"] == host_seat_id
        assert attribution["amount"] == 1
        ok("give_drink from the winning phone → the HOST drinks 1, TEAM B credited, attribution in snapshot (§G)")

        # second attempt — from the winner AND the host fallback — rejected
        # with the exact structured shape.
        await drain(pb)
        await act(pb, "give_drink", target_participant_id=a_id)
        e = json.loads(await asyncio.wait_for(pb.recv(), 5))
        while e.get("type") != "error":
            e = json.loads(await asyncio.wait_for(pb.recv(), 5))
        assert e.get("code") == "drinks_already_assigned" and set(e) == {"type", "detail", "code"}, e
        await drain(host)
        await act(host, "assign_drinks", to_participant_id=a_id)
        e = json.loads(await asyncio.wait_for(host.recv(), 5))
        while e.get("type") != "error":
            e = json.loads(await asyncio.wait_for(host.recv(), 5))
        assert e.get("code") == "drinks_already_assigned", e
        s = requests.get(f"{BASE}/api/games/{code}/").json()
        pmap = {p["id"]: p for p in s["participants"]}
        assert pmap[a_id]["drinks_taken"] == 0 and pmap[host_seat_id]["drinks_taken"] == 1
        ok('second assignment (both paths) → {"detail", code: "drinks_already_assigned"}; tallies untouched (§G)')

        await act(host, "close_cell")
        s = (await latest_state(host))["game"]
        assert s["current_cell"] is None
        cell = next(c for col in s["columns"] for c in col["cells"] if c["id"] == cell_id)
        assert cell["state"] == "answered"; ok("close_cell → back to board, cell answered")
        polled = requests.get(f"{BASE}/api/games/{code}/").json()
        assert polled["revealed_answer"] is None and the_answer not in json.dumps(polled)
        ok("revealed_answer gone from polled snapshot after close_cell (§F2)")

    # reload-safety: reconnect fresh, expect full state including tallies
    async with websockets.connect(url(a_tok), origin=ORIGIN) as pa2:
        s = (await latest_state(pa2))["game"]
        pmap = {p["id"]: p for p in s["participants"]}
        host_row = next(p for p in s["participants"] if p["role"] == "host")
        assert pmap[b_id]["drinks_given"] == 1 and host_row["drinks_taken"] == 1
        ok("reconnect after 'reload' → snapshot restores tallies incl. the host's drink (reload-safe)")

    async with websockets.connect(url(host_tok), origin=ORIGIN) as host:
        await recv_until(host, "state")
        await host.send(json.dumps({"action": "finish_game"}))
        s = (await latest_state(host))["game"]
        assert s["status"] == "finished"; ok("finish_game → finished")

    # --- Handoff #6 §G: game history + report (host's own games only) ------
    hh = {"Authorization": f"Token {knox}"}
    r = requests.get(f"{BASE}/api/games/history/", headers=hh)
    assert r.status_code == 200 and "results" in r.json(), r.text
    row = next(g for g in r.json()["results"] if g["code"] == code)  # newest-first; host reuses this account
    assert row["status"] == "finished" and row["finished_at"] and row["winners"] == ["TEAM B"], row
    assert row["participant_count"] == 6, row  # §G filled the table to the cap earlier
    ok(f"history lists {code} with winners {row['winners']} (§G2)")

    r = requests.get(f"{BASE}/api/games/{code}/report/", headers=hh)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert [w["name"] for w in rep["winners"]] == ["TEAM B"], rep["winners"]
    pmap = {p["name"]: p for p in rep["participants"]}
    assert pmap["TEAM B"]["drinks_given"] == 1
    assert next(p for p in rep["participants"] if p["role"] == "host")["drinks_taken"] == 1
    qs = [q for col in rep["columns"] for q in col["questions"]]
    assert qs and all(q.get("answer") for q in qs), "finished report must include every answer"
    assert any(q["answer"] == the_answer for q in qs)
    ok("finished report: participants + tallies, winners, every question WITH its answer (§G2)")

    r = requests.get(f"{BASE}/api/games/{code}/report/", headers={"Authorization": f"Token {fknox}"})
    assert r.status_code == 403, r.text
    r = requests.get(f"{BASE}/api/games/{code}/report/")
    assert r.status_code == 401, r.text
    ok("report: second Knox user 403, unauthenticated 401 (§G2)")

def media_round_trip():
    """Opt-in §F1/§F2 live check (SMOKE_MEDIA=1): upload a big photo through
    the real API, confirm the serialized URL fetches and the stored file is
    the resized one, then delete it (frees the §F3 quota). Needs an account
    with a creator plan — export SMOKE_CREATOR_EMAIL / SMOKE_CREATOR_PASSWORD
    (promote one in /admin). Default smoke runs with zero extra setup.
    Extra dep: `pip install pillow`."""
    email = os.environ.get("SMOKE_CREATOR_EMAIL")
    password = os.environ.get("SMOKE_CREATOR_PASSWORD")
    assert email and password, "SMOKE_MEDIA=1 needs SMOKE_CREATOR_EMAIL / SMOKE_CREATOR_PASSWORD (creator plan)"
    from PIL import Image  # noqa: PLC0415

    knox = requests.post(f"{BASE}/api/auth/login/", json={"email": email, "password": password}).json()["token"]
    h = {"Authorization": f"Token {knox}"}
    cat = requests.post(f"{BASE}/api/categories/", headers=h,
                        data={"name": f"Smoke media {int(time.time())}", "visibility": "private"})
    assert cat.status_code == 201, cat.text
    img = Image.new("RGB", (2600, 1400), (30, 90, 200))
    buf = io.BytesIO(); img.save(buf, format="JPEG", quality=95); raw = buf.getvalue()
    r = requests.post(f"{BASE}/api/questions/", headers=h,
                      data={"category": cat.json()["id"], "question_text": "Smoke media?", "answer": "Yes",
                            "difficulty": 1, "visibility": "private", "media_type": "image"},
                      files={"image": ("photo.jpg", raw, "image/jpeg")})
    assert r.status_code == 201, r.text
    url = r.json()["image"]
    absolute = url if url.startswith("http") else BASE + url
    got = requests.get(absolute)
    assert got.status_code == 200, (absolute, got.status_code)
    stored = Image.open(io.BytesIO(got.content))
    assert max(stored.size) <= 1920 and len(got.content) < len(raw), (stored.size, len(got.content))
    # Local mode serializes /media/... on the app host (DRF absolutizes it);
    # s3/Spaces mode points at the BUCKET host — and then it must be signed.
    from urllib.parse import urlparse  # noqa: PLC0415
    off_host = url.startswith("http") and urlparse(url).netloc != urlparse(BASE).netloc
    if off_host:
        assert "Signature" in url or "X-Amz-Signature" in url, url
        ok(f"media round-trip (Spaces mode): signed bucket URL, stored resized {stored.size}")
    else:
        ok(f"media round-trip (local mode): stored resized {stored.size}")
    requests.delete(f"{BASE}/api/questions/{r.json()['id']}/", headers=h)
    requests.delete(f"{BASE}/api/categories/{cat.json()['id']}/", headers=h)
    ok("media round-trip cleanup: question + category deleted (storage quota freed)")

code, ht, at, aid, bt, bid, knox, fknox = rest()
asyncio.run(ws_flow(code, ht, at, aid, bt, bid, knox, fknox))
if os.environ.get("SMOKE_MEDIA") == "1":
    media_round_trip()
print("\nALL SMOKE CHECKS PASSED")
