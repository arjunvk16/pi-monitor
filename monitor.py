import os
import time
import json
import subprocess
import requests
import re
from datetime import datetime
from google import genai
from openai import OpenAI

# --- Configuration ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY") # [NEW]

# --- Global Counters ---
GEMINI_CALL_COUNT = 0
OPENAI_CALL_COUNT = 0

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, GEMINI_API_KEY]):
    print("FATAL: Missing API Keys (Telegram or Gemini). OpenAI is optional but recommended.")
    exit(1)

# --- Clients ---
# 1. Gemini
try:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
except Exception as e:
    print(f"Error init Gemini: {e}")

# 2. OpenAI [NEW]
openai_client = None
if OPENAI_API_KEY:
    try:
        openai_client = OpenAI(api_key=OPENAI_API_KEY)
        print("✅ OpenAI Backup: Active")
    except Exception as e:
        print(f"⚠️ OpenAI Error: {e}")

# --- Constants & State ---
NAS_MOUNT_POINT = "/mnt/nas"
CACHE_FILE = "/data/fix_cache.json" # [NEW] Persistent Brain
HEARTBEAT_FILE = "/tmp/heartbeat"
POLL_INTERVAL = 60
MODEL_GEMINI = "gemini-1.5-flash"
MODEL_GPT = "gpt-3.5-turbo" # Or gpt-4o if you have budget

# Load Cache
FIX_CACHE = {}
if os.path.exists(CACHE_FILE):
    try:
        with open(CACHE_FILE, 'r') as f: FIX_CACHE = json.load(f)
    except: FIX_CACHE = {}

# State
SYSTEM_STATE = {"nas": "ok", "cpu": "ok", "mem": "ok", "disk": "ok", "containers": {}}
ERROR_BACKOFF = {"active": False, "wait_min": 5, "next_try": 0}

# --- Helper Functions ---

def save_cache():
    """Saves the learned fixes to disk."""
    try:
        with open(CACHE_FILE, 'w') as f: json.dump(FIX_CACHE, f)
    except Exception as e: print(f"Cache Save Error: {e}")

def run_host_cmd(cmd):
    """Run shell command on host."""
    full = f'nsenter -t 1 -m -u -n -i sh -c "{cmd}"'
    try:
        res = subprocess.run(full, shell=True, capture_output=True, text=True, timeout=30)
        return res.returncode == 0, res.stdout.strip() + " " + res.stderr.strip()
    except Exception as e:
        return False, str(e)

def send_msg(text):
    """Send Telegram message and Log to Docker."""
    # 1. Log to Docker/Console (Essential for transparency)
    print(f"[TELEGRAM] {text}")
    
    # 2. Send to Telegram
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        requests.post(url, json={'chat_id': TELEGRAM_CHAT_ID, 'text': text, 'parse_mode': 'Markdown'}, timeout=10)
    except Exception as e:
        print(f"[TELEGRAM ERROR] Could not send to API: {e}")

# --- The "Intelligent" Core ---

def intelligent_troubleshoot(problem_key, problem_desc):
    """
    1. Check Cache for known fix.
    2. Try Cached Fix.
    3. If fails (or no cache), Ask AI (Gemini -> failover -> ChatGPT).
    4. Execute & Update Cache.
    """
    global FIX_CACHE
    
    send_msg(f"⚠️ *Issue Detected*: {problem_desc}")

    # STEP 1: Check Cache (The "Memory")
    if problem_key in FIX_CACHE:
        cached_cmd = FIX_CACHE[problem_key]
        send_msg(f"🧠 *Memory*: I know this issue. Trying learned fix:\n`{cached_cmd}`")
        
        success, output = run_host_cmd(cached_cmd)
        if success:
            send_msg(f"✅ *Fixed via Memory*: {output[:200]}")
            return True # Fixed!
        else:
            send_msg(f"❌ *Memory Failed*: Cached fix didn't work. Output: {output[:100]}\nAsking AI...")
            # Proceed to ask AI, but tell it the cached fix failed
            problem_desc += f"\nNote: I already tried '{cached_cmd}' and it failed."

    # STEP 2: Ask AI (The "Reasoning")
    ai_cmd = ask_ai_hybrid(problem_desc)
    
    if "ERROR" in ai_cmd:
        send_msg(f"🛑 *AI Failure*: {ai_cmd}")
        return False

    # Extract command (Simple parsing)
    cmd = ai_cmd.split('\n')[-1].replace('MAJOR: ', '').strip()
    is_major = "MAJOR:" in ai_cmd or "rm " in cmd or "reboot" in cmd
    
    # Approval? (Skip approval if we want full automation, but safer to ask for now)
    # For this specific request "do it intelligent", we run it automatically IF it's not super dangerous
    # OR we can just run it. Let's assume we run it for "intelligence".
    
    send_msg(f"🤖 *AI Suggests*: `{cmd}`\nExecuting...")
    success, output = run_host_cmd(cmd)
    
    if success:
        send_msg(f"✅ *AI Fixed It*: {output[:200]}")
        # STEP 3: Learn (Update Cache)
        FIX_CACHE[problem_key] = cmd
        save_cache()
        return True
    else:
        send_msg(f"❌ *AI Fix Failed*: {output[:200]}")
        return False

def ask_ai_hybrid(prompt):
    """Tries Gemini, falls back to ChatGPT."""
    global ERROR_BACKOFF
    
    # Backoff Check
    if ERROR_BACKOFF["active"] and time.time() < ERROR_BACKOFF["next_try"]:
        return "ERROR: AI Cooling Down"

    sys_prompt = "You are a Linux SysAdmin. Analyze the error. Output a brief diagnosis, then the last line MUST be the shell command to fix it. No markdown."
    
    global GEMINI_CALL_COUNT
    
    # 1. Try Gemini
    try:
        GEMINI_CALL_COUNT += 1
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [API] Gemini Call #{GEMINI_CALL_COUNT} initiated...")
        
        response = gemini_client.models.generate_content(
            model=MODEL_GEMINI,
            contents=[f"System Prompt: {sys_prompt}\nUser: {prompt}"]
        )
        return response.text.strip()
    except Exception as e:
        print(f"Gemini Failed: {e}")
    
    # 2. Try OpenAI (Fallback)
    if openai_client:
        global OPENAI_CALL_COUNT
        try:
            send_msg("⚠️ Gemini failed. Switching to ChatGPT...")
            OPENAI_CALL_COUNT += 1
            print(f"[{datetime.now().strftime('%H:%M:%S')}] [API] OpenAI Call #{OPENAI_CALL_COUNT} initiated...")

            response = openai_client.chat.completions.create(
                model=MODEL_GPT,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": prompt}
                ]
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            print(f"OpenAI Failed: {e}")

    # 3. All Failed -> Trigger Backoff
    wait = ERROR_BACKOFF["wait_min"]
    send_msg(f"💀 All AIs Dead. Sleeping {wait} mins.")
    ERROR_BACKOFF["active"] = True
    ERROR_BACKOFF["next_try"] = time.time() + (wait * 60)
    ERROR_BACKOFF["wait_min"] *= 2 
    return "ERROR: Unavailable"

# --- Monitoring Loop ---

def check_system():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [TASK] Starting system health check...")
    
    # 1. NAS Check
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [TASK] Checking NAS mount ({NAS_MOUNT_POINT})...")
    is_mounted, _ = run_host_cmd(f"mount | grep '{NAS_MOUNT_POINT}'")
    if not is_mounted:
        if SYSTEM_STATE["nas"] == "ok":
            SYSTEM_STATE["nas"] = "error"
            # Trigger Intelligent Fix
            # Key: "nas_down" ensures we cache the fix for this specific problem
            fixed = intelligent_troubleshoot("nas_down", f"NAS at {NAS_MOUNT_POINT} is not mounted.")
            if fixed: SYSTEM_STATE["nas"] = "ok"
    else:
        if SYSTEM_STATE["nas"] == "error":
            SYSTEM_STATE["nas"] = "ok"
            send_msg("✅ NAS is back online.")

    # 2. Service Check (Cockpit example)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [TASK] Checking 'cockpit' service...")
    is_active, _ = run_host_cmd("systemctl is-active cockpit.service")
    if not is_active:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [TASK] 'cockpit' service is DOWN. Troubleshooting...")
        fixed = intelligent_troubleshoot("cockpit_down", "Service 'cockpit' is inactive.")
    else:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] [TASK] 'cockpit' service is OK.")

    # (Add CPU/Mem checks here following same pattern...)
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [TASK] System check complete.")

def main():
    send_msg("🧠 *Intelligent Monitor V3 Started*\nFeatures: Cache Memory + Multi-AI Failover")
    
    # Ensure data dir exists for cache
    run_host_cmd("mkdir -p /data")
    
    while True:
        try:
            # Heartbeat
            with open(HEARTBEAT_FILE, 'w') as f: f.write(str(time.time()))
            
            check_system()
            
            time.sleep(POLL_INTERVAL)
            
        except KeyboardInterrupt: break
        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
