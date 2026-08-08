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
    # §I3 (Handoff #10): the probe endpoint — exact body pinned.
    r = requests.get(f"{BASE}/api/health/")
    assert r.status_code == 200 and r.json() == {"status": "ok"}, (r.status_code, r.text)
    ok('/api/health/ → 200 {"status": "ok"} exactly (§I3)')

    # §G (Handoff #12): the anonymous browse surface. ONE unthrottled GET —
    # re-runnable trivially; seed_demo ships 5 official categories (the
    # standing pristine-seed property). NOTE (§L): NO new register calls in
    # this smoke — the register throttle budget (5/min, 2 calls/run) is
    # already fully spoken for.
    r = requests.get(f"{BASE}/api/categories/public/")
    assert r.status_code == 200, r.text
    pub = r.json()["results"]
    assert len(pub) >= 5, f"expected the 5 seeded official categories, got {len(pub)}"
    for row in pub:
        assert set(row) == {"id", "name", "description", "photo", "question_count"}, row
        assert isinstance(row["question_count"], int)
    ok(f"public categories (anon, unthrottled): {len(pub)} rows, exact 5-key shape (§G)")

    r = requests.post(f"{BASE}/api/auth/register/", json={
        "email": "host@test.com", "password": "sturdy-pass-123", "display_name": "Quizmaster"})
    assert "Verification failed" not in r.text, (
        "This server has TURNSTILE_SECRET_KEY set — the smoke cannot solve a "
        "Turnstile challenge. Run it against an env without the key (dev/"
        "compose smoke env), or unset it for the run.")
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
    # §F2 (#18): the ADDITIVE entitlements summary — a list (empty for a
    # billing-less account like this seeded creator).
    assert isinstance(p["usage"]["entitlements"], list)
    ok(f"profile: plan={p['plan']}, usage block shaped per D3 incl. §F3 storage + #18 entitlements (limit null = unlimited)")

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

    # §G (Handoff #10): the host-facing theme list — seed_demo ships themes.
    r = requests.get(f"{BASE}/api/themes/", headers={"Authorization": f"Token {knox}"})
    assert r.status_code == 200, r.text
    themes = r.json()
    assert len(themes) >= 1, themes
    for t in themes:
        assert set(t) == {"id", "name", "description", "categories"}, t
        for c in t["categories"]:
            assert set(c) == {"id", "name", "usable_question_count"}, c
            assert isinstance(c["usable_question_count"], int)
    ok(f"themes: {[t['name'] for t in themes]} — categories carry per-user usable counts (§G)")

    # shortage 400 path the host UI must surface
    r = requests.post(f"{BASE}/api/games/", headers={"Authorization": f"Token {knox}"},
                      json={"mode": "drinks", "categories": [c["id"] for c in cats[:2]], "questions_per_category": 8})
    assert r.status_code == 400 and "categories" in r.json(), r.text
    ok(f"shortage 400 surfaced: {r.json()['categories'][0][:60]}…")

    r = requests.post(f"{BASE}/api/games/", headers={"Authorization": f"Token {knox}"},
                      json={"mode": "drinks", "categories": [c["id"] for c in cats[:3]], "questions_per_category": 5,
                            "buzz_sound": 3})
    assert r.status_code == 201, r.text
    d = r.json(); code, host_token = d["game"]["code"], d["participant_token"]
    # §H (#13): the host's per-game sound rides the snapshot; §I: a plain
    # game carries tournament: null (the forward-path shape). Both asserted
    # on the create response we already have — zero new requests (§L).
    assert d["game"]["buzz_sound"] == 3, d["game"].get("buzz_sound")
    assert d["game"]["tournament"] is None, d["game"].get("tournament")
    ok(f"create game → {code}; snapshot buzz_sound=3 (host's pick, §H), tournament=null (plain game, §I)")

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
    # #21 owner-drift adaptation: the owner raised MAX_PLAYERS_PER_GAME
    # (6 → 10) in live settings — the smoke now derives the cap from the
    # snapshot's own max_players and pins the MECHANISM (cap enforced,
    # exact 409 shape, reclaim-at-cap, round-robin sounds), never the
    # number, matching the suite's JoinCapAndSoundTests treatment.
    snap = requests.get(f"{BASE}/api/games/{code}/").json()
    cap = snap.get("max_players")
    assert isinstance(cap, int) and cap >= 2, snap.get("max_players")
    filler = [f"Team {chr(ord('C') + i)}" for i in range(cap - 2)]  # A + B already seated
    for nm in filler:
        r = j({"name": nm}); assert r.status_code == 201, r.text
        assert "buzzer_sound" in r.json()["participant"], r.text  # §I: in the join response
    r = j({"name": "Team Overflow"})
    assert r.status_code == 409, r.text
    full = r.json()
    assert set(full) == {"detail", "code", "limit"}, full  # NEW contract, exact shape
    assert full["code"] == "game_full" and full["limit"] == cap, full
    ok(f'join past the cap → 409 {{"detail", code: "game_full", limit: {cap}}} exact shape (§G; cap from snapshot)')
    r = j({"name": "Team A", "participant_token": a_tok})
    assert r.status_code == 200, r.text
    ok("reclaim still 200 AT the cap — reload flow survives a full table (§G)")
    snap = requests.get(f"{BASE}/api/games/{code}/").json()
    sounds = {p["name"]: p.get("buzzer_sound") for p in snap["participants"] if p["role"] == "player"}
    expect = {"TEAM A": 1, "TEAM B": 2}
    for i, nm in enumerate(filler):
        expect[nm.upper()] = ((2 + i) % 4) + 1  # round-robin 1–4 by join order
    assert sounds == expect, sounds  # §H1: snapshot names are ALL CAPS now
    ok(f"buzzer_sound in snapshot, round-robin by join order across {cap} seats (§I)")
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
        # §H (#13): the same snapshot field over the socket — the board's
        # WS path plays this sound (asserted on a frame we already read).
        assert s["buzz_sound"] == 3, s.get("buzz_sound")
        # §H1 (Handoff #10): total = questions_per_category × columns at lobby.
        total_cells = sum(len(c["cells"]) for c in s["columns"])
        assert s["cells_remaining"] == total_cells == 15, (s.get("cells_remaining"), total_cells)
        ok(f"snapshot on connect carries buzz_sound=3 over WS too (§H); cells_remaining == {total_cells} (§H1)")

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
        # §H1: exactly one decrement per close (the flow plays ONE cell).
        assert s["cells_remaining"] == total_cells - 1, s.get("cells_remaining")
        ok(f"cells_remaining decremented to {total_cells - 1} after close_cell (§H1)")
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
        # --- §F (Handoff #11): host removes TEAM A over WS -----------------
        await host.send(json.dumps({"action": "remove_player", "participant_id": a_id}))
        s = (await latest_state(host))["game"]
        assert all(p["id"] != a_id for p in s["participants"]), s["participants"]
        ok("remove_player over WS → TEAM A gone from the snapshot's participants (§F)")
        # The freed seat + the partial name constraint: the SAME name joins
        # fresh (201) even though the table was at the 6-team cap.
        r = requests.post(f"{BASE}/api/games/{code}/join/", json={"name": "TEAM A"})
        assert r.status_code == 201, r.text
        assert r.json()["participant"]["id"] != a_id, r.json()
        ok("fresh join with the SAME name → 201 new seat (cap freed + partial constraint, §F)")
        await drain(host)
        await host.send(json.dumps({"action": "finish_game"}))
        s = (await latest_state(host))["game"]
        assert s["status"] == "finished"; ok("finish_game → finished")
        table_cap = s["max_players"]  # #21: history pin derives from the snapshot too

    # --- Handoff #6 §G: game history + report (host's own games only) ------
    hh = {"Authorization": f"Token {knox}"}
    r = requests.get(f"{BASE}/api/games/history/", headers=hh)
    assert r.status_code == 200 and "results" in r.json(), r.text
    row = next(g for g in r.json()["results"] if g["code"] == code)  # newest-first; host reuses this account
    assert row["status"] == "finished" and row["finished_at"] and row["winners"] == ["TEAM B"], row
    assert row["participant_count"] == table_cap, row  # §G filled the table to the cap earlier (#21: cap from snapshot)
    ok(f"history lists {code} with winners {row['winners']} (§G2)")

    r = requests.get(f"{BASE}/api/games/{code}/report/", headers=hh)
    assert r.status_code == 200, r.text
    rep = r.json()
    assert [w["name"] for w in rep["winners"]] == ["TEAM B"], rep["winners"]
    pmap = {p["name"]: p for p in rep["participants"]}
    assert pmap["TEAM B"]["drinks_given"] == 1
    assert next(p for p in rep["participants"] if p["role"] == "host")["drinks_taken"] == 1
    # §F (#11): the report keeps the KICKED seat with its tallies, flagged —
    # two TEAM A rows now exist (removed original + the fresh rejoin).
    team_a_rows = [p for p in rep["participants"] if p["name"] == "TEAM A"]
    assert {p["removed"] for p in team_a_rows} == {True, False}, team_a_rows
    removed_a = next(p for p in team_a_rows if p["removed"])
    assert removed_a["id"] == a_id and "drinks_taken" in removed_a, removed_a
    ok("report keeps the removed TEAM A with tallies + removed:true; the fresh seat is removed:false (§F)")
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
                      data={"categories": [cat.json()["id"]], "question_text": "Smoke media?", "answer": "Yes",  # §K1: alias removed
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



def tournament_story(knox, fknox):
    """§I (Handoff #13): tournaments over the live wire — with ZERO new
    register calls (§L: the 5/min register budget stays 2/run; joins are
    unthrottled).

    Two layers:
      1. ALWAYS: the plan gate. The guaranteed-free account's create is the
         structured quota_tournaments 403 (free's limit is 0 — that IS the
         creator gate, same choke point as categories).
      2. The FULL round-trip (create → attach a round-1 game → join →
         advance-too-early 409 → WS finish → advance → detail) runs when the
         MAIN smoke account can create. Promote host@test.com to creator in
         /admin (or grant it a tournaments limit override) to light it up; a
         default zero-setup run prints the SKIP line instead — the
         media_round_trip convention.

    RE-RUNNABLE: the tournament name is timestamped, so the per-owner
    live-name constraint never collides across runs (chosen over
    delete-at-end so the record stays inspectable after the run — soft
    delete would also have freed the name, but a vanished tournament is a
    worse debugging artifact than a pile of Smoke Cups).
    """
    fh = {"Authorization": f"Token {fknox}"}
    r = requests.post(f"{BASE}/api/tournaments/", headers=fh, json={"name": "Nope Cup"})
    b = r.json()
    assert r.status_code == 403 and b.get("code") == "quota_tournaments", r.text
    assert b["used"] == 0 and b["limit"] == 0 and "detail" in b, b
    ok("free user tournament create → structured 403 {code: quota_tournaments, used: 0, limit: 0} (§I plan gate)")

    hh = {"Authorization": f"Token {knox}"}
    tname = f"Smoke Cup {int(time.time())}"
    r = requests.post(f"{BASE}/api/tournaments/", headers=hh,
                      json={"name": tname, "location": "Ian's Bar Venue"})
    if r.status_code == 403 and r.json().get("code") == "quota_tournaments":
        ok("SKIP full tournament story — host@test.com isn't a creator; promote it in /admin to light this up")
        return
    assert r.status_code == 201, r.text
    tid = r.json()["id"]
    assert r.json()["finished_at"] is None, r.text
    ok(f"tournament created: '{tname}' at Ian's Bar Venue (id {tid})")

    # Attach a round-1 game: the ORDINARY create call + the two §I fields.
    cats = requests.get(f"{BASE}/api/categories/public/").json()["results"]
    r = requests.post(f"{BASE}/api/games/", headers=hh,
                      json={"mode": "points", "categories": [cats[0]["id"]],
                            "questions_per_category": 1, "tournament": tid, "round_number": 1})
    assert r.status_code == 201, r.text
    d = r.json()
    tcode, ttok = d["game"]["code"], d["participant_token"]
    assert d["game"]["tournament"] == {"id": tid, "name": tname, "location": "Ian's Bar Venue",
                                       "round_number": 1}, d["game"]["tournament"]
    ok(f"round-1 game {tcode} attached; snapshot tournament block exact {{id, name, location, round_number}} (§I3, amended §F4b #17)")

    # One team, so advancement has someone to advance. #20: KEEP its token —
    # that token is the claim credential (the whole §F3 premise).
    r = requests.post(f"{BASE}/api/games/{tcode}/join/", json={"name": "Cup Team"})
    assert r.status_code == 201, r.text
    team_tok = r.json()["participant_token"]

    # Advancing before the round's games finish → the pinned 409.
    r = requests.post(f"{BASE}/api/tournaments/{tid}/rounds/1/advance/", headers=hh, json={"per_game": 1})
    assert r.status_code == 409, r.text
    b = r.json()
    assert set(b) == {"detail", "code"} and b["code"] == "tournament_round_incomplete", b
    ok('advance before the game finishes → 409 {"detail", code: "tournament_round_incomplete"} exact (§I2)')

    async def finish_the_game():
        async with websockets.connect(f"{WS}/ws/game/{tcode}/?token={ttok}", origin=ORIGIN) as host:
            await recv_until(host, "state")
            await host.send(json.dumps({"action": "start_game"}))
            await recv_until(host, "state")
            await host.send(json.dumps({"action": "finish_game"}))
            s = (await latest_state(host))["game"]
            assert s["status"] == "finished", s["status"]
    asyncio.run(finish_the_game())
    ok("round-1 game finished over WS (start → finish; a 0-point solo run is a fine result)")

    r = requests.post(f"{BASE}/api/tournaments/{tid}/rounds/1/advance/", headers=hh, json={"per_game": 1})
    assert r.status_code == 200, r.text
    adv = r.json()
    assert adv["round_number"] == 1 and adv["per_game"] == 1, adv
    assert [(a["name"], a["rank"], a["source_game"]) for a in adv["advancers"]] == \
        [("CUP TEAM", 1, tcode)], adv
    ok("advance top-1 → CUP TEAM (rank 1) goes through (§I2)")

    r = requests.get(f"{BASE}/api/tournaments/{tid}/", headers=hh)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert [a["name"] for a in detail["advancers"]] == ["CUP TEAM"], detail["advancers"]
    games = {g["code"]: g for g in detail["games"]}
    assert games[tcode]["standings"] == [{"name": "CUP TEAM", "score": 0, "rank": 1}], games[tcode]
    blob = json.dumps(detail)
    assert "question_text" not in blob and "SECRET" not in blob, \
        "rule 5: no question content in tournament payloads"
    ok("detail: game standings + advancers; NO question content anywhere in the payload (§I2, rule 5)")

    # --- Handoff #20: the winners seat THEMSELVES ---------------------------
    # 1) Pre-round-2: the buzzer's poll (snapshot + own seat token) says
    #    "through, no target yet" — and says NOTHING to anyone else.
    r = requests.get(f"{BASE}/api/games/{tcode}/?seat={team_tok}")
    adv_block = r.json()["my_advancement"]
    assert adv_block == {"rank": 1, "target": None, "claimed": False}, adv_block
    assert requests.get(f"{BASE}/api/games/{tcode}/").json()["my_advancement"] is None
    ok("my_advancement: {rank 1, target null, claimed false} for the OWN seat token; null without it (§F3b, rule 5)")

    # 2) Create the round-2 game — C-4's creation-side auto-target closes
    #    the gap (advance ran first; this is the common host flow).
    r = requests.post(f"{BASE}/api/games/", headers=hh,
                      json={"mode": "points", "categories": [cats[0]["id"]],
                            "questions_per_category": 1, "tournament": tid, "round_number": 2})
    assert r.status_code == 201, r.text
    r2code = r.json()["game"]["code"]
    adv_block = requests.get(f"{BASE}/api/games/{tcode}/?seat={team_tok}").json()["my_advancement"]
    assert adv_block == {"rank": 1,
                         "target": {"code": r2code, "status": "lobby", "round_number": 2},
                         "claimed": False}, adv_block
    ok(f"round-2 game {r2code} created → auto-target fired (creation direction, §F1b/C-4); poll shows the lobby target")

    # 3) Denials FIRST (order-independent of the happy claim): the host's
    #    seat never advances (403), a forged token is 401.
    r = requests.post(f"{BASE}/api/games/{r2code}/claim/", json={"participant_token": ttok})
    assert r.status_code == 403 and r.json()["code"] == "claim_not_qualified", r.text
    r = requests.post(f"{BASE}/api/games/{r2code}/claim/", json={"participant_token": "forged"})
    assert r.status_code == 401, r.text
    ok("claim denials: host seat → 403 claim_not_qualified; forged token → 401 (§F3a)")

    # 4) The one tap: round-1 token in, round-2 seat out — the join contract.
    r = requests.post(f"{BASE}/api/games/{r2code}/claim/", json={"participant_token": team_tok})
    assert r.status_code == 201, r.text
    claim = r.json()
    assert set(claim) == {"participant", "participant_token", "game"}, claim.keys()
    assert claim["participant"]["name"] == "CUP TEAM" and claim["game"]["code"] == r2code, claim
    r2names = [p["name"] for p in requests.get(f"{BASE}/api/games/{r2code}/").json()["participants"]
               if p["role"] == "player"]
    assert r2names == ["CUP TEAM"], r2names
    ok(f"CLAIM: round-1 token → seated in {r2code} as CUP TEAM, join-shaped response (§F3a)")

    # 5) Double-claim 409; the poll now says claimed; the console shows ✓.
    r = requests.post(f"{BASE}/api/games/{r2code}/claim/", json={"participant_token": team_tok})
    assert r.status_code == 409 and r.json()["code"] == "claim_already_claimed", r.text
    adv_block = requests.get(f"{BASE}/api/games/{tcode}/?seat={team_tok}").json()["my_advancement"]
    assert adv_block["claimed"] is True, adv_block
    (adv_row,) = requests.get(f"{BASE}/api/tournaments/{tid}/", headers=hh).json()["advancers"]
    assert adv_row["target_game"] == r2code and adv_row["claimed"] is True, adv_row
    ok("double-claim → 409 claim_already_claimed; poll + console both read claimed ✓ (§F1c ledger)")

    # 6) Re-run Advance (the host changed their mind): rows REPLACE, the
    #    target re-derives, and the claimed seat neither duplicates nor
    #    orphans — the §F1c design claim, live.
    r = requests.post(f"{BASE}/api/tournaments/{tid}/rounds/1/advance/", headers=hh, json={"per_game": 2})
    assert r.status_code == 200, r.text
    (adv_row,) = requests.get(f"{BASE}/api/tournaments/{tid}/", headers=hh).json()["advancers"]
    assert adv_row["target_game"] == r2code and adv_row["claimed"] is True, adv_row
    r2names = [p["name"] for p in requests.get(f"{BASE}/api/games/{r2code}/").json()["participants"]
               if p["role"] == "player"]
    assert r2names == ["CUP TEAM"], r2names
    ok("re-run Advance → rows replaced, target re-derived, ONE seat still — nothing duplicated (§F1c)")

def password_flows(knox, fknox):
    """Handoff #9 §K4 — RE-RUNNABLE by construction: the smoke user's
    password is changed and changed BACK within this run (with a re-login in
    between to prove the change took). Runs LAST: the second change (made
    with the new token) revokes the run's original knox token, which nothing
    uses afterwards. No delivery assertions here — the console backend
    prints to the server log, and that's enough for smoke."""
    h = {"Authorization": f"Token {knox}"}
    old_pw, new_pw = "sturdy-pass-123", "sturdy-pass-456-tmp"

    # wrong current → 400, nothing changed
    r = requests.post(f"{BASE}/api/auth/password/change/", headers=h,
                      json={"current_password": "definitely-wrong", "new_password": new_pw})
    assert r.status_code == 400 and "current_password" in r.json(), r.text
    ok("change with wrong current password → 400 (§K2)")

    # change → 200; THIS token survives, the old password stops working
    r = requests.post(f"{BASE}/api/auth/password/change/", headers=h,
                      json={"current_password": old_pw, "new_password": new_pw})
    assert r.status_code == 200, r.text
    r = requests.get(f"{BASE}/api/auth/profile/", headers=h)
    assert r.status_code == 200, r.text
    r = requests.post(f"{BASE}/api/auth/login/", json={"email": "host@test.com", "password": old_pw})
    assert r.status_code == 400, r.text
    ok("password change → 200; current session token survives; old password no longer logs in (§K2)")

    # re-login with the NEW password, change BACK with that fresh token
    r = requests.post(f"{BASE}/api/auth/login/", json={"email": "host@test.com", "password": new_pw})
    assert r.status_code == 200, r.text
    knox2 = r.json()["token"]
    r = requests.post(f"{BASE}/api/auth/password/change/",
                      headers={"Authorization": f"Token {knox2}"},
                      json={"current_password": new_pw, "new_password": old_pw})
    assert r.status_code == 200, r.text
    r = requests.post(f"{BASE}/api/auth/login/", json={"email": "host@test.com", "password": old_pw})
    assert r.status_code == 200, r.text
    ok("re-login with the new password, change BACK → original password logs in again (re-runnable)")

    # forgot: a real and a fake email get IDENTICAL 200s (no enumeration).
    # The fake address is randomized so the per-email send cooldown can never
    # make the two calls diverge across quick re-runs.
    r1 = requests.post(f"{BASE}/api/auth/password/forgot/", json={"email": "host@test.com"})
    r2 = requests.post(f"{BASE}/api/auth/password/forgot/",
                       json={"email": f"ghost-{int(time.time())}@test.com"})
    assert r1.status_code == r2.status_code == 200, (r1.text, r2.text)
    assert r1.json() == r2.json(), (r1.json(), r2.json())
    ok("forgot-password: existing vs unknown email → identical 200 bodies (§K1, no enumeration)")


def billing_surface(knox):
    """§F2/§F3 (Handoff #18): the KEYLESS billing surface — what a server
    without Stripe env must serve. Products (public, pinned bare-array
    shape), status (authed, pinned keys), webhook rejecting an unsigned
    POST, checkout answering the clean 503 'billing not configured'."""
    r = requests.get(f"{BASE}/api/billing/products/")
    assert r.status_code == 200, r.text
    products = r.json()
    assert isinstance(products, list) and products, "products must be a non-empty bare array"
    for row in products:
        assert set(row) == {"key", "name", "price", "interval", "blurb", "coming_soon"}, row
    keys = {row["key"] for row in products}
    assert {"party_game_50", "big_game_100", "venue_monthly", "tournament_pass"} <= keys
    assert "party_game_reactivation" not in keys, "reactivation keys stay dark"
    coming = {row["key"]: row["coming_soon"] for row in products}
    assert coming["venue_tournament_monthly"] is True, "C-4: Venue Tournament is coming-soon"
    ok("billing products: public bare array, pinned row shape, reactivations dark, C-4 coming soon")

    r = requests.get(f"{BASE}/api/billing/status/", headers={"Authorization": f"Token {knox}"})
    assert r.status_code == 200, r.text
    s = r.json()
    assert set(s) == {"entitlements", "subscriptions", "purchases", "session"}, s
    assert s["session"] is None and isinstance(s["entitlements"], list)
    r = requests.get(f"{BASE}/api/billing/status/")
    assert r.status_code == 401, r.text
    ok("billing status: pinned 4-key shape for the seeded creator; anonymous 401")

    r = requests.post(f"{BASE}/api/billing/webhook/", data=b"{}", headers={"Content-Type": "application/json"})
    assert r.status_code == 503, r.text  # keyless: no webhook secret configured
    r = requests.post(
        f"{BASE}/api/billing/checkout/",
        json={"product": "party_game_50"},
        headers={"Authorization": f"Token {knox}"},
    )
    assert r.status_code == 503 and r.json().get("code") == "billing_not_configured", r.text
    ok("keyless billing: webhook 503 (no secret → nothing verifiable), checkout billing_not_configured")




# --- Handoff #21: §F1/§F2 THUNDER FUCKED + §F3 buzz check -------------------
async def _thunder_ws(code, host_tok, zap_tok, zap_id, crack_tok, crack_id, spots, knox):
    url = lambda t: f"{WS}/ws/game/{code}/?token={t}"
    async with websockets.connect(url(host_tok), origin=ORIGIN) as host, \
               websockets.connect(url(zap_tok), origin=ORIGIN) as zap, \
               websockets.connect(url(crack_tok), origin=ORIGIN) as crack:
        async def act(ws, action, **payload):
            await ws.send(json.dumps({"action": action, **payload}))

        # §F3: the lobby test smash — the ✓ lands in the shared snapshot
        # (host lobby + TV render it); C-7: a visual aid, never a gate.
        await act(zap, "buzz_check")
        s = (await latest_state(host))["game"]
        checked = {p["name"]: p["buzz_checked"] for p in s["participants"] if p["role"] == "player"}
        assert checked == {"TEAM ZAP": True, "TEAM CRACK": False}, checked
        ok("lobby buzz_check → ZAP ✓ in the snapshot, CRACK unchecked (§F3)")

        await act(host, "start_game")
        s = (await latest_state(host))["game"]; assert s["status"] == "active"
        await asyncio.gather(drain(zap), drain(crack))
        await act(crack, "buzz_check")
        e = await recv_until(crack, "error")
        assert "lobby" in e["detail"].lower(), e
        ok(f"buzz_check after start → '{e['detail']}' (lobby-only, §F3)")

        # --- ⚡ #1: the happy path, stage by stage (C-2/C-4/C-5) -----------
        await act(host, "open_cell", cell_id=spots[0])
        s = (await latest_state(host))["game"]
        chug = s["chug"]
        assert chug["stage"] == "fanfare" and s["buzzer_open"] is False, chug
        assert set(chug) == {"stage", "wager", "chugger_name", "started_at", "seconds"}, chug
        assert s["current_cell"]["thunder"] is True
        assert s["current_cell"]["question_text"] is None  # withheld until reveal
        # No-spoiler mid-flow: every board-grid row keeps the pinned key set
        # — the OTHER unopened ⚡ is indistinguishable.
        for col in s["columns"]:
            for row in col["cells"]:
                assert set(row) == {"id", "row", "value", "state", "answered_by", "answered_correctly"}, row
        ok("open ⚡ → chug.stage=fanfare, exact 5-key block, question WITHHELD, grid rows spoiler-free (§F1/§F2)")

        await act(zap, "buzz")
        e = await recv_until(zap, "error"); ok(f"buzz during fanfare → '{e['detail']}' (locked until reveal)")

        await act(host, "thunder_reveal")
        s = (await latest_state(host))["game"]
        assert s["chug"]["stage"] == "answering" and s["buzzer_open"] is True
        assert s["current_cell"]["question_text"], "question should ride the payload after reveal"
        ok("thunder_reveal → answering, buzzer OPEN, question in the payload (C-5)")

        await asyncio.gather(drain(zap), drain(crack))
        await act(zap, "buzz")
        s = (await latest_state(host))["game"]
        assert s["buzzer_open"] is False, "first buzz must LOCK on a ⚡ (C-2, no steals)"
        ok("first buzz locks the buzzer (C-2)")

        await drain(host)
        await act(host, "judge", participant_id=zap_id, correct=True)
        e = await recv_until(host, "error")
        assert e.get("code") == "thunder_wager_required" and set(e) == {"type", "detail", "code"}, e
        ok('judge before wager → structured {"detail", code: "thunder_wager_required"} (§F2)')

        await act(host, "thunder_wager", seconds=12)
        s = (await latest_state(host))["game"]
        assert s["chug"]["wager"] == 12 and s["chug"]["seconds"] == 12, s["chug"]
        await act(host, "judge", participant_id=zap_id, correct=True)
        s = (await latest_state(host))["game"]
        assert s["chug"]["stage"] == "pick" and s["revealed_answer"], s["chug"]
        ok("wager 12 typed; judge correct → stage pick, answer revealed (C-4 pending the pick)")

        await drain(zap)
        await act(zap, "give_drink", target_participant_id=crack_id)
        s = (await latest_state(host))["game"]
        chug = s["chug"]
        assert chug["stage"] == "ready" and chug["chugger_name"] == "TEAM CRACK", chug
        tallies = {p["name"]: (p["drinks_taken"], p["drinks_given"], p["score"])
                   for p in s["participants"] if p["role"] == "player"}
        assert tallies["TEAM CRACK"][0] == 12 and tallies["TEAM ZAP"][1] == 12 and tallies["TEAM ZAP"][2] == 12, tallies
        ok("winner's phone picks CRACK → ready; seconds ARE the stakes: taken/given/score all 12 (C-4)")

        await act(host, "thunder_clock")
        s = (await latest_state(host))["game"]
        chug = s["chug"]
        assert chug["stage"] == "running" and chug["started_at"], chug
        from datetime import datetime as _dt
        _dt.fromisoformat(chug["started_at"])  # the anchor every screen counts from
        ok("thunder_clock → running with a parseable server anchor (C-5)")

        await act(host, "close_cell")
        s = (await latest_state(host))["game"]
        assert s["chug"] is None and s["current_cell"] is None
        ok("close ⚡ → chug block back to null")

        # --- ⚡ #2: the wrong path — NO steals, reveal, self-chug (C-6) ----
        await act(host, "open_cell", cell_id=spots[1])
        await act(host, "thunder_reveal")
        await latest_state(host)
        await asyncio.gather(drain(zap), drain(crack))
        await act(crack, "buzz")
        await act(host, "thunder_wager", seconds=5)
        await act(host, "judge", participant_id=crack_id, correct=False)
        s = (await latest_state(host))["game"]
        chug = s["chug"]
        assert s["buzzer_open"] is False, "wrong on ⚡ must NOT reopen (no steals)"
        assert s["revealed_answer"], "wrong on ⚡ reveals immediately (C-6)"
        assert chug["stage"] == "ready" and chug["chugger_name"] == "TEAM CRACK", chug
        tallies = {p["name"]: (p["drinks_taken"], p["drinks_given"])
                   for p in s["participants"] if p["role"] == "player"}
        assert tallies["TEAM CRACK"] == (12 + 5, 0), tallies  # self-chug, credit to NOBODY
        ok("wrong on ⚡ → no reopen, answer up, self-chug 5 credited to nobody (C-4/C-6)")
        await act(host, "thunder_clock")
        await act(host, "close_cell")
        s = (await latest_state(host))["game"]
        assert s["chug"] is None
        ok("second ⚡ clocked and closed — board resumes")


def thunder_story(knox):
    """§L5 (#21): the THUNDER FUCKED chapter — its own fresh drinks board
    (2×5 = 10 cells → exactly 2 ⚡ per C-1), ⚡ located via the HOST-PRIVATE
    board view (never the public snapshot — that's the point), then the full
    C-2 flow over WS twice: the correct path and the wrong path."""
    hh = {"Authorization": f"Token {knox}"}
    cats = requests.get(f"{BASE}/api/categories/", headers=hh).json()["results"]
    r = requests.post(f"{BASE}/api/games/", headers=hh,
                      json={"mode": "drinks", "categories": [c["id"] for c in cats[:2]],
                            "questions_per_category": 5})
    assert r.status_code == 201, r.text
    d = r.json(); code, host_tok = d["game"]["code"], d["participant_token"]

    board = requests.get(f"{BASE}/api/games/{code}/board/", headers=hh).json()
    spots = [c["id"] for col in board["columns"] for c in col["cells"] if c.get("is_thunder")]
    rows = {c["id"]: c["row"] for col in board["columns"] for c in col["cells"]}
    cols_of = {c["id"]: i for i, col in enumerate(board["columns"]) for c in col["cells"]}
    assert len(spots) == 2, f"expected 2 ⚡ on a 10-cell drinks board, got {len(spots)}"
    assert all(rows[s] > 0 for s in spots), "row 0 must never be ⚡ (C-1)"
    assert len({cols_of[s] for s in spots}) == 2, "at most one ⚡ per column (C-1)"
    public = requests.get(f"{BASE}/api/games/{code}/").json()
    assert "thunder" not in json.dumps(public).lower(), "public snapshot must not spoil ⚡ (§B)"
    ok(f"fresh drinks 2×5 board {code}: 2 ⚡ via host-private board view, rows>0, distinct columns, public payload spoiler-free (§F1)")

    j = lambda body: requests.post(f"{BASE}/api/games/{code}/join/", json=body)
    r = j({"name": "Team Zap"}); assert r.status_code == 201, r.text
    assert r.json()["participant"]["buzz_checked"] is False  # additive key, join response too
    zap_tok, zap_id = r.json()["participant_token"], r.json()["participant"]["id"]
    r = j({"name": "Team Crack"}); assert r.status_code == 201, r.text
    crack_tok, crack_id = r.json()["participant_token"], r.json()["participant"]["id"]
    asyncio.run(_thunder_ws(code, host_tok, zap_tok, zap_id, crack_tok, crack_id, spots, knox))

    # #21.1: the ⚡ opt-out — a thunder:false drinks board marks NOTHING
    # (host-private board view confirms; the create response's own snapshot
    # is the public no-"thunder" payload).
    r = requests.post(f"{BASE}/api/games/", headers=hh,
                      json={"mode": "drinks", "categories": [cats[0]["id"]],
                            "questions_per_category": 3, "thunder": False})
    assert r.status_code == 201, r.text
    off_code = r.json()["game"]["code"]
    assert "thunder" not in json.dumps(r.json()["game"]).lower()
    off_board = requests.get(f"{BASE}/api/games/{off_code}/board/", headers=hh).json()
    off_spots = [c for col in off_board["columns"] for c in col["cells"] if c.get("is_thunder")]
    assert off_spots == [], off_spots
    ok(f"⚡ opt-out: thunder=false board {off_code} marks nothing (#21.1)")

code, ht, at, aid, bt, bid, knox, fknox = rest()
asyncio.run(ws_flow(code, ht, at, aid, bt, bid, knox, fknox))
# §L5 (#21): THUNDER FUCKED + buzz check — fresh board, re-runnable (its
# own game; no throttled surface touched).
thunder_story(knox)
# §I (#13): BEFORE password_flows — that flow ends by killing the original
# knox session (change-back revokes other sessions), and this story needs it.
tournament_story(knox, fknox)
# §F2 (#18): billing's keyless surface — also before password_flows for the
# same session-revocation reason.
billing_surface(knox)
password_flows(knox, fknox)
if os.environ.get("SMOKE_MEDIA") == "1":
    media_round_trip()
print("\nALL SMOKE CHECKS PASSED")
