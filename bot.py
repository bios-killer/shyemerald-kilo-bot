#!/usr/bin/env python3
"""Telegram <-> Kilo CLI bridge. Final responses only, session continuity, model switching."""
import json, os, re, signal, subprocess, threading, queue, time, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(BASE, "config.json")))
TOKEN, CHAT_ID = CFG["token"], int(CFG["chat_id"])
API = f"https://api.telegram.org/bot{TOKEN}"
STATE_F = os.path.join(BASE, "state.json")
WORKDIR = os.path.join(BASE, "workspace")
KILO_TIMEOUT = 900
DEFAULT_MODEL = "kilo/nvidia/nemotron-3-ultra-550b-a55b:free"
DEFAULT_AGENT = "plan"

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-B]")

def load():
    return json.load(open(STATE_F))

def save(s):
    json.dump(s, open(STATE_F, "w"))

def api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except Exception as e:
        return {"ok": False, "error": str(e)}

def send(text, chat=CHAT_ID):
    for i in range(0, len(text), 3900):
        api("sendMessage", chat_id=chat, text=text[i:i+3900])

def kilo_models():
    out = subprocess.run(["kilo", "models"], capture_output=True, text=True, timeout=60)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]

def kilo_agents():
    out = subprocess.run(["kilo", "agent", "list"], capture_output=True, text=True, timeout=60)
    return re.findall(r"^(\S+) \((primary|subagent)\)", out.stdout, re.M)

class ModelList:
    last = []

HEADER = re.compile(r"^[a-zA-Z_][\w -]* · \S")

def clean_output(out):
    txt = ANSI.sub("", out)
    lines = [l for l in txt.splitlines() if l.strip() and not HEADER.match(l.strip())]
    return "\n".join(lines).strip()

def empty_fallback(state):
    return ("⚠️ The model returned no text (free models sometimes end after tool calls "
            "without summarizing).\nTry /new, or switch to a steadier model:\n"
            "/model zai/glm-5.3-flash")

def kill_tree(p):
    try:
        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    p.kill()

def worker(q):
    holder = {}
    while True:
        chat, text = q.get()
        if text == "/__stop__":
            if holder.get("p") and holder["p"].poll() is None:
                kill_tree(holder["p"]); send("🛑 Stopped.")
            else:
                send("Nothing running.")
            continue
        send("⏳ Kilo is working…")
        state = load()
        cmd = ["kilo", "run", "--auto", "-m", state["model"]]
        cmd += ["--agent", state.get("agent") or DEFAULT_AGENT]
        if not state.get("fresh"):
            cmd.append("-c")
        cmd.append(msg := text)
        p = subprocess.Popen(cmd, cwd=WORKDIR, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
        holder["p"], holder["done"] = p, False
        def typing():
            while not holder["done"]:
                api("sendChatAction", chat_id=chat, action="typing"); time.sleep(4)
        threading.Thread(target=typing, daemon=True).start()
        try:
            out, err = p.communicate(timeout=KILO_TIMEOUT)
        except subprocess.TimeoutExpired:
            kill_tree(p); holder["done"] = True
            send("⏱ Kilo timed out after 15 min. /new resets the session.")
            continue
        holder["done"] = True
        state["fresh"] = False; save(state)
        if p.returncode != 0 and not out.strip():
            tail = (err or "").strip().splitlines()[-5:]
            send("⚠️ Kilo error:\n" + "\n".join(tail))
        else:
            answer = clean_output(out)
            if not answer:
                print(f"[kilo-bot] EMPTY stdout: rc={p.returncode} "
                      f"raw={out!r:.500} stderr_tail={(err or '')[-500:]!r}", flush=True)
                answer = empty_fallback(state)
            send(answer)

def main():
    if not os.path.exists(STATE_F):
        save({"model": DEFAULT_MODEL, "fresh": True, "agent": DEFAULT_AGENT})
    q = queue.Queue()
    threading.Thread(target=worker, args=(q,), daemon=True).start()
    offset = 0
    while True:
        r = api("getUpdates", offset=offset, timeout=30)
        for u in r.get("result", []):
            offset = u["update_id"] + 1
            m = u.get("message")
            if not m or int(m["chat"]["id"]) != CHAT_ID:
                continue
            photo = None
            if m.get("photo"):
                photo = m["photo"][-1]["file_id"]
            elif (m.get("document") or {}).get("mime_type", "").startswith("image/"):
                photo = m["document"]["file_id"]
            if photo:
                f = api("getFile", file_id=photo)
                fp = (f.get("result") or {}).get("file_path")
                if not fp:
                    send("❌ Не удалось получить файл из Telegram.")
                    continue
                ext = os.path.splitext(fp)[1] or ".jpg"
                path = os.path.join(WORKDIR, "pics", f"img_{int(time.time())}{ext}")
                os.makedirs(os.path.dirname(path), exist_ok=True)
                dl = f"https://api.telegram.org/file/bot{TOKEN}/{fp}"
                try:
                    urllib.request.urlretrieve(dl, path)
                except Exception as e:
                    send(f"❌ Ошибка скачивания: {e}")
                    continue
                cap = (m.get("caption") or "").strip()
                q.put((CHAT_ID, (cap + "\n" if cap else "")
                       + f"Посмотри изображение {path} инструментом Read и ответь по существу."))
                continue
            text = (m.get("text") or "").strip()
            state = load()
            if text in ("/start", "/help"):
                send("🤖 Kilo bridge ready.\n"
                     f"Model: {state['model']}\n"
                     f"Mode: {state.get('agent') or DEFAULT_AGENT}\n\n"
                     "Commands:\n"
                     "/providers — list providers\n"
                     "/models <provider> — list models ('free' for :free)\n"
                     "/model <id|number> — switch model\n"
                     "/mode — list/switch mode (code, plan, ask, debug, orchestrator)\n"
                     "/current — current model & mode\n"
                     "/new — fresh session\n"
                     "/stop — kill running task\n\n"
                     "Anything else you type goes to Kilo.")
            elif text.startswith("/providers"):
                provs = sorted({("/".join(x.split("/")[:2]) if x.startswith("kilo/")
                                 else x.split("/")[0]) for x in kilo_models()})
                send("Providers:\n" + "\n".join(provs))
            elif text.startswith("/models"):
                parts = text.split(maxsplit=1)
                ms = kilo_models()
                arg = parts[1].strip().lower() if len(parts) > 1 else ""
                if arg == "free":
                    sel = [x for x in ms if x.endswith(":free")]
                elif arg:
                    sel = [x for x in ms if arg in x.lower()]
                else:
                    sel = ms
                ModelList.last = sel
                if not sel:
                    send("No match. Try /models free or /providers")
                else:
                    shown = sel[:80]
                    send(f"{len(sel)} models:\n" + "\n".join(f"{i+1}. {x}" for i, x in enumerate(shown)))
            elif text.startswith("/model"):
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    send("Usage: /model <provider/model>  or /model <number from last /models>")
                    continue
                arg = parts[1].strip()
                ms = kilo_models()
                if arg.isdigit() and ModelList.last and 1 <= int(arg) <= len(ModelList.last):
                    pick = ModelList.last[int(arg)-1]
                elif arg in ms:
                    pick = arg
                elif f"kilo/{arg}" in ms:
                    pick = f"kilo/{arg}"
                else:
                    send(f"❌ Unknown model: {arg}\nFind one: /models <provider>")
                    continue
                state["model"] = pick; save(state)
                send(f"✅ Model set: {pick}\nTip: /new starts a clean session with it.")
            elif text.startswith("/mode"):
                parts = text.split(maxsplit=1)
                ags = kilo_agents()
                if len(parts) < 2:
                    prim = [a for a, t in ags if t == "primary"]
                    send("Modes:\n" + "\n".join(prim) +
                         f"\n\nCurrent: {state.get('agent') or DEFAULT_AGENT}\n"
                         "Switch: /mode <name>")
                else:
                    arg = parts[1].strip()
                    if arg in [a for a, t in ags]:
                        state["agent"] = arg; save(state)
                        send(f"✅ Mode set: {arg}\nTip: /new starts a clean session with it.")
                    else:
                        send("❌ Unknown mode: " + arg + "\nAvailable: " +
                             ", ".join(a for a, t in ags))
            elif text.startswith("/current"):
                send(f"Model: {state['model']}\nMode: {state.get('agent') or DEFAULT_AGENT}\n"
                     f"Session: {'new on next msg' if state.get('fresh') else 'continuing'}")
            elif text == "/new":
                state["fresh"] = True; state["agent"] = DEFAULT_AGENT
                save(state); send(f"🆕 Next message starts a fresh session in {DEFAULT_AGENT} mode.")
            elif text == "/stop":
                q.put((CHAT_ID, "/__stop__"))
            elif text:
                q.put((CHAT_ID, text))

if __name__ == "__main__":
    main()
