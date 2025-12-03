import asyncio
import json
import re
import threading
from datetime import datetime
from aiohttp import web
import os

SCOREBOARD_PORT = 6003
WEBSOCKET_PORT = 6000
CONTROLLER_PORT = 6001
REMOTE_PORT = 80
HEX_COLOR_RE = re.compile(r"^#([0-9A-Fa-f]{6})$")

def now_iso():
    return datetime.utcnow().isoformat() + "Z"

### Persistent state backed by `common/state.json`
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(SCRIPT_DIR, 'common', 'state.json')

# Clock state (in-memory only; not persisted)
clock_lock = asyncio.Lock()
clock_running = False
clock_elapsed = 0.0
clock_start_time = None

# File lock for reading/writing persistent state
file_lock = asyncio.Lock()

def _normalize_persistent(p):
    # Ensure teams have integer keys and defaults exist
    teams = p.get("teams", {})
    normalized = {}
    for k, v in teams.items():
        try:
            ik = int(k)
        except Exception:
            continue
        normalized[ik] = v
    # Provide defaults if missing
    if 1 not in normalized:
        normalized[1] = {"name": "INT", "score": 0, "colors": ["#FF0000", "#FFFFFF"]}
    if 2 not in normalized:
        normalized[2] = {"name": "GRE", "score": 0, "colors": ["#006EFF", "#FFFFFF"]}
    return {
        "teams": normalized,
        "events": p.get("events", []) or [],
        "round": p.get("round", 1),
    }

async def _read_persistent_state():
    loop = asyncio.get_running_loop()
    def _read():
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    raw = await loop.run_in_executor(None, _read)
    if raw is None:
        # create default
        default = {"teams": {"1": {"name": "INT", "score": 0, "colors": ["#FF0000", "#FFFFFF"]},
                             "2": {"name": "GRE", "score": 0, "colors": ["#006EFF", "#FFFFFF"]}},
                   "events": [], "round": 1}
        await _write_persistent_state(default)
        raw = default
    p = _normalize_persistent(raw)
    return p

async def _write_persistent_state(persist_dict):
    # Write atomically to STATE_FILE
    loop = asyncio.get_running_loop()
    def _write():
        tmp = STATE_FILE + ".tmp"
        # Convert team keys to strings for JSON
        out = {
            "teams": {str(k): v for k, v in persist_dict.get("teams", {}).items()},
            "events": persist_dict.get("events", []),
            "round": persist_dict.get("round", 1),
        }
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        try:
            os.replace(tmp, STATE_FILE)
        except Exception:
            # fallback: write directly
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
    await loop.run_in_executor(None, _write)

async def get_state():
    async with file_lock:
        p = await _read_persistent_state()
    async with clock_lock:
        elapsed = clock_elapsed
        if clock_running and clock_start_time is not None:
            elapsed += (asyncio.get_running_loop().time() - clock_start_time)
        return {
            "timestamp": now_iso(),
            "teams": {
                1: dict(p["teams"][1]),
                2: dict(p["teams"][2]),
            },
            "events": list(p.get("events", [])),
            "clock": {
                "running": clock_running,
                "elapsed_seconds": int(elapsed),
            },
            "round": p.get("round", 1),
        }

# Mutating helpers (persist changes except clock)
async def set_score(team, score):
    if team not in (1, 2):
        return False, "invalid team"
    if not isinstance(score, int):
        return False, "score must be integer"
    if score <= 0:
        return False, "score must be higher than zero"
    async with file_lock:
        p = await _read_persistent_state()
        p["teams"][team]["score"] = score
        await _write_persistent_state(p)
    return True, "score updated"

async def set_name(team, name):
    if team not in (1, 2):
        return False, "invalid team"
    if not isinstance(name, str):
        return False, "name must be string"
    name = name.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", name):
        return False, "name must be 3 letters A-Z"
    async with file_lock:
        p = await _read_persistent_state()
        p["teams"][team]["name"] = name
        await _write_persistent_state(p)
    return True, "name updated"

async def set_colors(team, colors):
    if team not in (1, 2):
        return False, "invalid team"
    if not isinstance(colors, (list, tuple)) or len(colors) != 2:
        return False, "colors must be a list of 2 hex strings"
    for c in colors:
        if not isinstance(c, str) or not HEX_COLOR_RE.fullmatch(c):
            return False, f"invalid color {c}"
    async with file_lock:
        p = await _read_persistent_state()
        p["teams"][team]["colors"] = [colors[0].upper(), colors[1].upper()]
        await _write_persistent_state(p)
    return True, "colors updated"

async def add_card(team, card):
    if team not in (1, 2):
        return False, "invalid team"
    if card not in ("yellow", "red"):
        return False, "card must be 'yellow' or 'red'"
    ev = {"type": "card", "team": team, "card": card, "when": now_iso()}
    async with file_lock:
        p = await _read_persistent_state()
        p["events"].insert(0, ev)
        p["events"] = p["events"][:20]
        await _write_persistent_state(p)
    return True, "card recorded"

# Clock controls (in-memory)
async def start_clock():
    global clock_running, clock_start_time
    async with clock_lock:
        if not clock_running:
            clock_start_time = asyncio.get_running_loop().time()
            clock_running = True
    return True, "clock started"

async def stop_clock():
    global clock_running, clock_start_time, clock_elapsed
    async with clock_lock:
        if clock_running and clock_start_time is not None:
            now = asyncio.get_running_loop().time()
            clock_elapsed += (now - clock_start_time)
            clock_start_time = None
            clock_running = False
    return True, "clock stopped"

async def reset_clock():
    global clock_running, clock_elapsed, clock_start_time
    async with clock_lock:
        clock_running = False
        clock_elapsed = 0.0
        clock_start_time = None
    return True, "clock reset"

async def add_time(minutes):
    global clock_elapsed
    if not (isinstance(minutes, int) or isinstance(minutes, float)):
        return False, "minutes must be a number"
    seconds = int(minutes * 60)
    async with clock_lock:
        clock_elapsed += seconds
    return True, f"added {minutes} minute(s)"

async def set_round(round_num):
    if round_num not in (1, 2):
        return False, "round must be 1 or 2"
    async with file_lock:
        p = await _read_persistent_state()
        p["round"] = round_num
        await _write_persistent_state(p)
    return True, f"round set to {round_num}"


class Broadcaster:
    def __init__(self):
        self.scoreboard_clients = set()
        self._lock = asyncio.Lock()
        # websocket clients (from browser scoreboards)
        self.ws_clients = set()

    async def register(self, writer):
        async with self._lock:
            self.scoreboard_clients.add(writer)

    async def unregister(self, writer):
        async with self._lock:
            self.scoreboard_clients.discard(writer)

    async def broadcast(self, obj):
        # Prepare data for TCP and WS clients
        json_text = json.dumps(obj, separators=(",", ":"), ensure_ascii=False)
        data = (json_text + "\n").encode()
        async with self._lock:
            # TCP scoreboard clients
            to_remove = []
            for w in list(self.scoreboard_clients):
                try:
                    w.write(data)
                    await w.drain()
                except Exception:
                    to_remove.append(w)
            for w in to_remove:
                self.scoreboard_clients.discard(w)

            # WebSocket clients
            if self.ws_clients:
                to_remove_ws = []
                for ws in list(self.ws_clients):
                    try:
                        # aiohttp WebSocketResponse
                        try:
                            await ws.send_str(json_text)
                        except Exception:
                            # fallback: try send (for other websocket implementations)
                            await ws.send(json_text)
                    except Exception:
                        to_remove_ws.append(ws)
                for ws in to_remove_ws:
                    self.ws_clients.discard(ws)

    async def register_ws(self, ws):
        async with self._lock:
            self.ws_clients.add(ws)

    async def unregister_ws(self, ws):
        async with self._lock:
            self.ws_clients.discard(ws)

broadcaster = Broadcaster()

async def handle_scoreboard(reader, writer):
    # When a scoreboard client connects, send initial state and then keep connection open
    addr = writer.get_extra_info("peername")
    await broadcaster.register(writer)
    try:
        s = await get_state()
        await broadcaster.broadcast(s)  # send to all including this new client
        # Keep connection open until client disconnects; no reads necessary
        while True:
            data = await reader.read(100)
            if not data:
                break
            # ignore any incoming data; scoreboard is read-only
    except Exception:
        pass
    finally:
        await broadcaster.unregister(writer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def handle_ws(request):
    # aiohttp websocket handler for browser scoreboards
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    await broadcaster.register_ws(ws)
    try:
        s = await get_state()
        await ws.send_str(json.dumps(s, separators=(",", ":"), ensure_ascii=False))
        async for msg in ws:
            # ignore incoming messages; keep connection open until client disconnects
            if msg.type == web.WSMsgType.ERROR:
                break
    except Exception:
        pass
    finally:
        await broadcaster.unregister_ws(ws)
    return ws

async def handle_controller(reader, writer):
    addr = writer.get_extra_info("peername")
    peer = f"{addr}"
    try:
        # Send a greeting
        writer.write((json.dumps({"status":"ok","message":"controller connected"}) + "\n").encode())
        await writer.drain()
        buf = b""
        while True:
            data = await reader.readline()
            if not data:
                break
            buf = data.strip()
            if not buf:
                continue
            try:
                cmd = json.loads(buf.decode())
            except Exception as e:
                err = {"status": "error", "error": f"invalid json: {e}"}
                writer.write((json.dumps(err) + "\n").encode())
                await writer.drain()
                continue

            resp = await process_command(cmd)
            # After processing, broadcast full state to scoreboard clients
            if resp.get("status") == "ok":
                s = await get_state()
                await broadcaster.broadcast(s)
            writer.write((json.dumps(resp, separators=(",", ":"), ensure_ascii=False) + "\n").encode())
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

async def process_command(cmd):
    if not isinstance(cmd, dict):
        return {"status":"error","error":"command must be a JSON object"}
    action = cmd.get("action")
    if action == "set_score":
        team = cmd.get("team")
        score = cmd.get("score")
        ok, msg = await set_score(team, score)
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    elif action == "set_name":
        team = cmd.get("team")
        name = cmd.get("name")
        ok, msg = await set_name(team, name)
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    elif action == "set_colors":
        team = cmd.get("team")
        colors = cmd.get("colors")
        ok, msg = await set_colors(team, colors)
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    elif action == "card":
        team = cmd.get("team")
        card = cmd.get("card")
        ok, msg = await add_card(team, card)
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    elif action == "get_state":
        st = await get_state()
        return {"status":"ok","state":st}

    # Clock actions
    elif action == "start_clock":
        ok, msg = await start_clock()
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    elif action == "stop_clock":
        ok, msg = await stop_clock()
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    elif action == "reset_clock":
        ok, msg = await reset_clock()
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    elif action == "add_time":
        # minutes: positive or negative integer/float (for syncing)
        minutes = cmd.get("minutes")
        ok, msg = await add_time(minutes)
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}

    # Round action
    elif action == "set_round":
        round_num = cmd.get("round")
        ok, msg = await set_round(round_num)
        return {"status":"ok","message":msg} if ok else {"status":"error","error":msg}
    else:
        return {"status":"error","error":"unknown action"}

async def clock_ticker():
    # Broadcast clock updates every second while running to keep scoreboards in sync
    try:
        while True:
            await asyncio.sleep(1)
            # Only broadcast when clock is running (to avoid spam)
            async with clock_lock:
                running = clock_running
            if running:
                s = await get_state()
                await broadcaster.broadcast(s)
    except asyncio.CancelledError:
        return



# Embedded HTML templates (global)
scoreboard_html = """
<!DOCTYPE html>
<html>
    <head>
    <title>Scoreboard</title>
    <link rel="stylesheet" href="/common/scoreboard.css">
    </head>
<body>
    <div class="scoreboard">
        <div class="team" id="team1">
                <div class="team-name" id="team1-name">INT</div>
                <div class="color-squares">
                    <div class="color-square" id="team1-color-a"></div>
                    <div class="color-square" id="team1-color-b"></div>
                </div>
                <div class="score" id="team1-score">0</div>
        </div>
            <div class="clock">
                <div id="clock-display">00:00</div>
            </div>
        <div class="team" id="team2">
            <div class="team-name" id="team2-name">GRE</div>
            <div class="color-squares">
                <div class="color-square" id="team2-color-a"></div>
                <div class="color-square" id="team2-color-b"></div>
            </div>
            <div class="score" id="team2-score">0</div>
        </div>
    </div>
    <div class="events" id="events"></div>
    <script>
        const ws = new WebSocket((location.protocol === 'https:' ? 'wss:' : 'ws:') + '//' + location.host + '/ws');
        ws.onmessage = (event) => {
            const state = JSON.parse(event.data);
            document.getElementById('team1-name').textContent = state.teams[1].name;
                    document.getElementById('team1-score').textContent = state.teams[1].score;
                    document.getElementById('team1-color-a').style.backgroundColor = state.teams[1].colors[0];
                    document.getElementById('team1-color-b').style.backgroundColor = state.teams[1].colors[1];
                document.getElementById('team2-name').textContent = state.teams[2].name;
                document.getElementById('team2-score').textContent = state.teams[2].score;
                    document.getElementById('team2-color-a').style.backgroundColor = state.teams[2].colors[0];
                    document.getElementById('team2-color-b').style.backgroundColor = state.teams[2].colors[1];
            const elapsed = state.clock.elapsed_seconds;
            const mins = Math.floor(elapsed / 60);
            const secs = elapsed % 60;
            document.getElementById('clock-display').textContent = 
                String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
        };
    </script>
</body>
</html>
"""

remote_html = """
<!DOCTYPE html>
<html>
<head>
    <title>Remote Control</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f0f0f0; margin: 0; padding: 20px; }
        .container { max-width: 800px; margin: 0 auto; }
        h1 { text-align: center; }
        .section { background: white; padding: 20px; margin: 10px 0; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .team-section { display: flex; gap: 20px; }
        .team { flex: 1; }
        label { display: block; margin: 10px 0 5px 0; font-weight: bold; }
        input, select { width: 100%; padding: 8px; box-sizing: border-box; }
        button { width: 100%; padding: 10px; margin: 5px 0; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        button:hover { background: #0056b3; }
        .button-group { display: flex; gap: 10px; }
        .button-group button { flex: 1; }
    </style>
</head>
<body>
    <div class="container">
        <h1>Scoreboard Remote Control</h1>
        
        <div class="section team-section">
            <div class="team">
                <h2>Team 1</h2>
                <label>Name (3 letters):</label>
                <input type="text" id="team1-name" maxlength="3" value="INT">
                <button onclick="setName(1)">Set Name</button>
                
                <label>Score:</label>
                <input type="number" id="team1-score" value="0">
                <button onclick="setScore(1)">Set Score</button>
                
                <label>Primary Color:</label>
                <input type="color" id="team1-color1" value="#FF0000">
                <label>Secondary Color:</label>
                <input type="color" id="team1-color2" value="#FFFFFF">
                <button onclick="setColors(1)">Set Colors</button>
                
                <button onclick="addCard(1, 'yellow')" style="background: #FFD700; color: black;">Yellow Card</button>
                <button onclick="addCard(1, 'red')" style="background: #FF0000;">Red Card</button>
            </div>
            
            <div class="team">
                <h2>Team 2</h2>
                <label>Name (3 letters):</label>
                <input type="text" id="team2-name" maxlength="3" value="GRE">
                <button onclick="setName(2)">Set Name</button>
                
                <label>Score:</label>
                <input type="number" id="team2-score" value="0">
                <button onclick="setScore(2)">Set Score</button>
                
                <label>Primary Color:</label>
                <input type="color" id="team2-color1" value="#006EFF">
                <label>Secondary Color:</label>
                <input type="color" id="team2-color2" value="#FFFFFF">
                <button onclick="setColors(2)">Set Colors</button>
                
                <button onclick="addCard(2, 'yellow')" style="background: #FFD700; color: black;">Yellow Card</button>
                <button onclick="addCard(2, 'red')" style="background: #FF0000;">Red Card</button>
            </div>
        </div>
        
        <div class="section">
            <h2>Clock Controls</h2>
            <div class="button-group">
                <button onclick="startClock()">Start</button>
                <button onclick="stopClock()">Stop</button>
                <button onclick="resetClock()">Reset</button>
            </div>
            <label>Add/Subtract Minutes:</label>
            <div class="button-group">
                <input type="number" id="minutes" value="1" style="flex: 1;">
                <button onclick="addTime()">Add Time</button>
            </div>
        </div>
        
        <div class="section">
            <h2>Round</h2>
            <label>Select Round:</label>
            <select id="round" onchange="setRound()">
                <option value="1">Round 1</option>
                <option value="2">Round 2</option>
            </select>
        </div>
    </div>
    
    <script>
        async function sendCommand(cmd) {
            try {
                const response = await fetch('/command', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(cmd)
                });
                const result = await response.json();
                console.log(result);
            } catch (e) {
                console.error('Error:', e);
            }
        }
        
        function setName(team) {
            const name = document.getElementById(`team${team}-name`).value.toUpperCase();
            sendCommand({ action: 'set_name', team, name });
        }
        
        function setScore(team) {
            const score = parseInt(document.getElementById(`team${team}-score`).value);
            sendCommand({ action: 'set_score', team, score });
        }
        
        function setColors(team) {
            const color1 = document.getElementById(`team${team}-color1`).value;
            const color2 = document.getElementById(`team${team}-color2`).value;
            sendCommand({ action: 'set_colors', team, colors: [color1, color2] });
        }
        
        function addCard(team, card) {
            sendCommand({ action: 'card', team, card });
        }
        
        function startClock() { sendCommand({ action: 'start_clock' }); }
        function stopClock() { sendCommand({ action: 'stop_clock' }); }
        function resetClock() { sendCommand({ action: 'reset_clock' }); }
        function addTime() {
            const minutes = parseFloat(document.getElementById('minutes').value);
            sendCommand({ action: 'add_time', minutes });
        }
        function setRound() {
            const round = parseInt(document.getElementById('round').value);
            sendCommand({ action: 'set_round', round });
        }
    </script>
</body>
</html>
"""

def _scoreboard_html():
    return scoreboard_html

def _remote_html():
    return remote_html

# aiohttp will serve the HTML and websocket on REMOTE_PORT

async def start_servers():
    sb_server = await asyncio.start_server(handle_scoreboard, host="0.0.0.0", port=SCOREBOARD_PORT)
    ctrl_server = await asyncio.start_server(handle_controller, host="0.0.0.0", port=CONTROLLER_PORT)
    # Start aiohttp web server (HTTP + websocket) on REMOTE_PORT
    app = web.Application()
    # serve files from ./common at /common/
    script_dir = os.path.dirname(os.path.abspath(__file__))
    common_dir = os.path.join(script_dir, 'common')
    app.router.add_static('/common/', common_dir, show_index=False)

    async def scoreboard_route(request):
        return web.Response(text=scoreboard_html, content_type='text/html')

    async def remote_route(request):
        return web.Response(text=remote_html, content_type='text/html')

    async def command_route(request):
        try:
            cmd = await request.json()
        except Exception as e:
            return web.json_response({'status': 'error', 'error': f'invalid json: {e}'})
        resp = await process_command(cmd)
        if resp.get('status') == 'ok':
            s = await get_state()
            await broadcaster.broadcast(s)
        return web.json_response(resp)

    app.router.add_get('/scoreboard', scoreboard_route)
    app.router.add_get('/remote', remote_route)
    app.router.add_get('/ws', handle_ws)
    app.router.add_post('/command', command_route)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', REMOTE_PORT)
    await site.start()

    print(f"Scoreboard TCP server listening on {SCOREBOARD_PORT}")
    print(f"HTTP/WebSocket server listening on {REMOTE_PORT} (ws at /ws)")
    print(f"Controller server listening on {CONTROLLER_PORT}")
    print(f"  - Scoreboard page: http://localhost:{REMOTE_PORT}/scoreboard")
    print(f"  - Remote control: http://localhost:{REMOTE_PORT}/remote")

    ticker_task = asyncio.create_task(clock_ticker())
    async with sb_server, ctrl_server:
        try:
            await asyncio.gather(sb_server.serve_forever(), ctrl_server.serve_forever())
        finally:
            ticker_task.cancel()
            try:
                await ticker_task
            except asyncio.CancelledError:
                pass
            try:
                await runner.cleanup()
            except Exception:
                pass

# Define the server endpoints
scoreboard_endpoint = f'http://localhost:{SCOREBOARD_PORT}'
controller_endpoint = f'http://localhost:{CONTROLLER_PORT}'
remote_endpoint = f'http://localhost:{REMOTE_PORT}'

 # Save the current server info to a JSON file (not the persistent UI state)
server_info = {
    'scoreboard_port': SCOREBOARD_PORT,
    'controller_port': CONTROLLER_PORT,
    'remote_port': REMOTE_PORT,
    'timestamp': now_iso()
}
server_info_path = os.path.join(SCRIPT_DIR, 'common', 'server_info.json')
with open(server_info_path, 'w', encoding='utf-8') as info_file:
    json.dump(server_info, info_file, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    try:
        asyncio.run(start_servers())
    except KeyboardInterrupt:
        print("Shutting down")