# streamlit_app.py - WITH SEND BUTTON (Flask style)

import streamlit as st
import os
import sys
import time
import json
import random
import sqlite3
import threading
import gc
import subprocess
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass
from collections import deque

from cryptography.fernet import Fernet
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.keys import Keys

# ==================== CONFIGURATION ====================
MAX_TASKS = 50
BROWSER_RESTART_HOURS = 3

DATA_DIR = Path(__file__).parent / 'data'
DATA_DIR.mkdir(exist_ok=True)

DB_PATH = DATA_DIR / 'bot_data.db'
ENCRYPTION_KEY_FILE = DATA_DIR / '.encryption_key'

# ==================== HARD KILL ====================
def hard_kill_all_chromium(task_id: str = ""):
    try:
        subprocess.run(['pkill', '-9', '-f', 'chromium'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['pkill', '-9', '-f', 'chromedriver'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['pkill', '-9', '-f', 'chrome'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        subprocess.run(['rm', '-rf', '/dev/shm/.org.chromium*'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        time.sleep(2)
    except:
        pass

# ==================== LOGGING ====================
if 'task_logs' not in st.session_state:
    st.session_state.task_logs = {}

def log_message(task_id: str, msg: str):
    timestamp = time.strftime("%H:%M:%S")
    formatted_msg = f"[{timestamp}] {msg}"
    
    if task_id not in st.session_state.task_logs:
        st.session_state.task_logs[task_id] = deque(maxlen=200)
    
    st.session_state.task_logs[task_id].append(formatted_msg)
    print(formatted_msg)

# ==================== ENCRYPTION ====================
def get_encryption_key():
    if ENCRYPTION_KEY_FILE.exists():
        with open(ENCRYPTION_KEY_FILE, 'rb') as f:
            return f.read()
    else:
        key = Fernet.generate_key()
        with open(ENCRYPTION_KEY_FILE, 'wb') as f:
            f.write(key)
        return key

ENCRYPTION_KEY = get_encryption_key()
cipher_suite = Fernet(ENCRYPTION_KEY)

def encrypt_data(data):
    if not data:
        return None
    return cipher_suite.encrypt(data.encode()).decode()

def decrypt_data(encrypted_data):
    if not encrypted_data:
        return ""
    try:
        return cipher_suite.decrypt(encrypted_data.encode()).decode()
    except:
        return ""

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute('PRAGMA journal_mode=WAL')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            cookies_encrypted TEXT,
            chat_id TEXT,
            name_prefix TEXT,
            messages TEXT,
            delay INTEGER DEFAULT 30,
            status TEXT DEFAULT 'stopped',
            messages_sent INTEGER DEFAULT 0,
            rotation_index INTEGER DEFAULT 0,
            last_browser_restart TIMESTAMP,
            start_time TIMESTAMP,
            last_active TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    cursor.execute('SELECT * FROM users WHERE username = "admin"')
    if not cursor.fetchone():
        password_hash = hashlib.sha256("admin123".encode()).hexdigest()
        cursor.execute('INSERT INTO users (username, password_hash) VALUES (?, ?)', 
                      ('admin', password_hash))
    
    conn.commit()
    conn.close()

init_db()

# ==================== TASK CLASS ====================
@dataclass
class Task:
    task_id: str
    username: str
    cookies: List[str]
    chat_id: str
    name_prefix: str
    messages: List[str]
    delay: int
    status: str
    messages_sent: int
    start_time: Optional[datetime]
    last_active: Optional[datetime]
    last_browser_restart: Optional[datetime]
    running: bool = False
    stop_flag: bool = False
    rotation_index: int = 0
    
    def get_uptime(self):
        if not self.start_time:
            return "00:00:00"
        delta = datetime.now() - self.start_time
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        seconds = delta.seconds % 60
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

# ==================== TASK MANAGER ====================
class TaskManager:
    def __init__(self):
        self.tasks: Dict[str, Task] = {}
        self.task_threads: Dict[str, threading.Thread] = {}
        self.load_tasks_from_db()
        self.start_auto_resume()
    
    def load_tasks_from_db(self):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tasks')
        for row in cursor.fetchall():
            try:
                cookies = json.loads(decrypt_data(row[2])) if row[2] else []
                messages = json.loads(decrypt_data(row[5])) if row[5] else []
                
                task = Task(
                    task_id=row[0],
                    username=row[1],
                    cookies=cookies,
                    chat_id=row[3] or "",
                    name_prefix=row[4] or "",
                    messages=messages,
                    delay=row[6] or 30,
                    status=row[7] or "stopped",
                    messages_sent=row[8] or 0,
                    start_time=datetime.fromisoformat(row[11]) if row[11] else None,
                    last_active=datetime.fromisoformat(row[12]) if row[12] else None,
                    last_browser_restart=datetime.fromisoformat(row[10]) if row[10] else None,
                    rotation_index=row[9] or 0
                )
                self.tasks[task.task_id] = task
                if task.status == "running":
                    self.start_task(task.task_id)
            except Exception as e:
                print(f"Error loading task: {e}")
        conn.close()
    
    def save_task(self, task: Task):
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR REPLACE INTO tasks 
            (task_id, username, cookies_encrypted, chat_id, name_prefix, messages, 
             delay, status, messages_sent, rotation_index, last_browser_restart, start_time, last_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            task.task_id,
            task.username,
            encrypt_data(json.dumps(task.cookies)),
            task.chat_id,
            task.name_prefix,
            encrypt_data(json.dumps(task.messages)),
            task.delay,
            task.status,
            task.messages_sent,
            task.rotation_index,
            task.last_browser_restart.isoformat() if task.last_browser_restart else None,
            task.start_time.isoformat() if task.start_time else None,
            task.last_active.isoformat() if task.last_active else None
        ))
        conn.commit()
        conn.close()
    
    def delete_task(self, task_id: str):
        if task_id in self.tasks:
            self.stop_task(task_id)
            del self.tasks[task_id]
            if task_id in st.session_state.task_logs:
                del st.session_state.task_logs[task_id]
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM tasks WHERE task_id = ?', (task_id,))
            conn.commit()
            conn.close()
            return True
        return False
    
    def start_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        if task.status == "running":
            return False
        
        log_message(task_id, "🔥 Initial hard kill - cleaning memory...")
        hard_kill_all_chromium(task_id)
        time.sleep(2)
        
        if len([t for t in self.tasks.values() if t.status == "running"]) >= MAX_TASKS:
            return False
        task.status = "running"
        task.stop_flag = False
        if not task.start_time:
            task.start_time = datetime.now()
        if not task.last_browser_restart:
            task.last_browser_restart = datetime.now()
        task.last_active = datetime.now()
        self.save_task(task)
        
        thread = threading.Thread(target=self._run_task, args=(task_id,), daemon=True)
        thread.start()
        self.task_threads[task_id] = thread
        return True
    
    def stop_task(self, task_id: str):
        if task_id not in self.tasks:
            return False
        task = self.tasks[task_id]
        task.stop_flag = True
        task.status = "stopped"
        task.last_active = datetime.now()
        self.save_task(task)
        return True
    
    def start_auto_resume(self):
        def auto_resume():
            while True:
                try:
                    for task_id, task in self.tasks.items():
                        if task.status == "running" and not task.running:
                            log_message(task_id, f"🔄 Auto-resume: Task dead, restarting...")
                            hard_kill_all_chromium(task_id)
                            self.start_task(task_id)
                except Exception as e:
                    print(f"Auto resume error: {e}")
                time.sleep(60)
        
        thread = threading.Thread(target=auto_resume, daemon=True)
        thread.start()
    
    def _setup_browser(self, task_id: str):
        hard_kill_all_chromium(task_id)
        
        chrome_options = Options()
        chrome_options.add_argument('--headless=new')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-setuid-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument('--disable-extensions')
        chrome_options.add_argument('--window-size=1280,720')
        chrome_options.add_argument('--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36')
        chrome_options.add_argument('--disable-blink-features=AutomationControlled')
        chrome_options.add_experimental_option('excludeSwitches', ['enable-automation'])
        
        chromium_paths = ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']
        for path in chromium_paths:
            if Path(path).exists():
                chrome_options.binary_location = path
                break
        
        try:
            driver = webdriver.Chrome(options=chrome_options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            return driver
        except:
            from webdriver_manager.chrome import ChromeDriverManager
            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            return driver
    
    def _login_and_navigate(self, driver, task: Task, task_id: str, process_id: str):
        log_message(task_id, f"{process_id}: Loading Facebook...")
        driver.get('https://www.facebook.com/')
        time.sleep(8)
        
        if task.cookies and task.cookies[0]:
            driver.delete_all_cookies()
            for cookie in task.cookies[0].split(';'):
                cookie = cookie.strip()
                if '=' in cookie:
                    name, value = cookie.split('=', 1)
                    try:
                        driver.add_cookie({'name': name, 'value': value, 'domain': '.facebook.com'})
                    except:
                        pass
            driver.refresh()
            time.sleep(5)
        
        driver.get(f'https://www.facebook.com/messages/t/{task.chat_id.strip()}')
        time.sleep(12)
        
        if 'login' in driver.current_url:
            log_message(task_id, f"{process_id}: ❌ Login failed!")
            return None
        
        return self._find_message_input(driver, task_id, process_id)
    
    def _find_message_input(self, driver, task_id: str, process_id: str):
        log_message(task_id, f"{process_id}: Finding message input...")
        
        selectors = [
            'div[contenteditable="true"][role="textbox"]',
            'div[aria-label*="Message"][contenteditable="true"]',
            'div[contenteditable="true"]',
            'textarea'
        ]
        
        for selector in selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                for el in elements:
                    if el.is_displayed() and el.is_enabled():
                        log_message(task_id, f"{process_id}: ✅ Found input")
                        return el
            except:
                continue
        
        return None
    
    def _send_message(self, driver, message_input, task: Task, task_id: str, process_id: str):
        messages = [m.strip() for m in task.messages if m.strip()]
        if not messages:
            messages = ['Hello!']
        
        msg_idx = task.rotation_index % len(messages)
        msg = f"{task.name_prefix} {messages[msg_idx]}" if task.name_prefix else messages[msg_idx]
        
        try:
            # Clear and type
            driver.execute_script("arguments[0].innerHTML = '';", message_input)
            time.sleep(0.5)
            message_input.send_keys(msg)
            time.sleep(1)
            
            # Try send button first
            send_btn = driver.find_elements(By.CSS_SELECTOR, '[aria-label*="Send"], [data-testid="send-button"]')
            if send_btn and send_btn[0].is_displayed():
                send_btn[0].click()
                log_message(task_id, f"{process_id}: ✅ Sent via button")
            else:
                message_input.send_keys(Keys.RETURN)
                log_message(task_id, f"{process_id}: ✅ Sent via Enter")
            
            task.messages_sent += 1
            task.rotation_index += 1
            self.save_task(task)
            log_message(task_id, f"{process_id}: 📨 Message #{task.messages_sent} sent: {msg[:50]}")
            return True
            
        except Exception as e:
            log_message(task_id, f"{process_id}: ❌ Send error: {str(e)[:100]}")
            return False
    
    def _run_task(self, task_id: str):
        task = self.tasks[task_id]
        task.running = True
        process_id = f"TASK-{task_id[-6:]}"
        
        driver = None
        message_input = None
        
        while task.status == "running" and not task.stop_flag:
            try:
                # Check restart
                hours_since = 0
                if task.last_browser_restart:
                    hours_since = (datetime.now() - task.last_browser_restart).total_seconds() / 3600
                
                if hours_since >= BROWSER_RESTART_HOURS or driver is None:
                    log_message(task_id, f"{process_id}: 🔄 Browser restart...")
                    if driver:
                        driver.quit()
                    hard_kill_all_chromium(task_id)
                    
                    driver = self._setup_browser(task_id)
                    message_input = self._login_and_navigate(driver, task, task_id, process_id)
                    
                    if not message_input:
                        driver = None
                        time.sleep(15)
                        continue
                    
                    task.last_browser_restart = datetime.now()
                    self.save_task(task)
                    log_message(task_id, f"{process_id}: ✅ Ready, continuing...")
                    time.sleep(3)
                
                # Send message
                if self._send_message(driver, message_input, task, task_id, process_id):
                    time.sleep(task.delay)
                else:
                    log_message(task_id, f"{process_id}: Failed, retrying...")
                    time.sleep(10)
                    driver = None
                    
            except Exception as e:
                log_message(task_id, f"{process_id}: Error: {str(e)[:100]}")
                driver = None
                time.sleep(10)
        
        if driver:
            driver.quit()
        task.running = False

# ==================== STREAMLIT UI ====================
st.set_page_config(page_title="Facebook Message Bot", page_icon="🤖", layout="wide")

# Custom CSS for Send Button (Flask style)
st.markdown("""
<style>
    .stButton button {
        background-color: #667eea;
        color: white;
        border-radius: 8px;
        border: none;
        padding: 0.5rem 1rem;
        font-weight: bold;
        transition: all 0.3s;
    }
    .stButton button:hover {
        background-color: #5a67d8;
        transform: translateY(-2px);
        box-shadow: 0 4px 12px rgba(102, 126, 234, 0.4);
    }
    .send-btn button {
        background-color: #28a745;
    }
    .send-btn button:hover {
        background-color: #218838;
    }
    .status-running {
        background-color: #d4edda;
        color: #155724;
        padding: 4px 12px;
        border-radius: 20px;
        font-weight: bold;
    }
    .status-stopped {
        background-color: #f8d7da;
        color: #721c24;
        padding: 4px 12px;
        border-radius: 20px;
        font-weight: bold;
    }
    .log-container {
        background-color: #1e1e1e;
        color: #d4d4d4;
        border-radius: 8px;
        padding: 15px;
        font-family: 'Courier New', monospace;
        font-size: 12px;
        height: 400px;
        overflow-y: auto;
    }
    .stat-card {
        background: linear-gradient(135deg, #667eea, #764ba2);
        border-radius: 12px;
        padding: 20px;
        text-align: center;
        color: white;
    }
    .task-card {
        background: #f8f9fa;
        border-radius: 10px;
        padding: 15px;
        margin-bottom: 10px;
        border-left: 4px solid #667eea;
    }
</style>
""", unsafe_allow_html=True)

# Session state
if 'logged_in' not in st.session_state:
    st.session_state.logged_in = False
if 'username' not in st.session_state:
    st.session_state.username = None
if 'task_manager' not in st.session_state:
    st.session_state.task_manager = TaskManager()
if 'selected_task' not in st.session_state:
    st.session_state.selected_task = None
if 'manual_message' not in st.session_state:
    st.session_state.manual_message = ""

task_manager = st.session_state.task_manager

def login_page():
    st.markdown("<div style='text-align: center; padding: 50px;'><h1>🤖 Facebook Message Bot</h1><p>Automated messaging with browser restart every 3 hours</p></div>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.markdown("### 🔐 Login")
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        
        if st.button("Login", use_container_width=True):
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            pwd_hash = hashlib.sha256(password.encode()).hexdigest()
            cursor.execute('SELECT * FROM users WHERE username = ? AND password_hash = ?', (username, pwd_hash))
            if cursor.fetchone():
                st.session_state.logged_in = True
                st.session_state.username = username
                st.rerun()
            else:
                st.error("Invalid credentials! Default: admin / admin123")
            conn.close()
        
        st.info("Default: **admin** / **admin123**")

def dashboard():
    # Header
    col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
    with col1:
        st.title("🤖 Facebook Message Bot")
        st.caption(f"👤 {st.session_state.username}")
    with col2:
        if st.button("🔄 Refresh"):
            st.rerun()
    with col3:
        if st.button("🚪 Logout"):
            st.session_state.logged_in = False
            st.rerun()
    
    # Stats
    user_tasks = [t for t in task_manager.tasks.values() if t.username == st.session_state.username]
    
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.markdown(f"<div class='stat-card'><h3>Total</h3><h2>{len(user_tasks)}</h2></div>", unsafe_allow_html=True)
    with c2:
        st.markdown(f"<div class='stat-card'><h3>Running</h3><h2>{sum(1 for t in user_tasks if t.status == 'running')}</h2></div>", unsafe_allow_html=True)
    with c3:
        st.markdown(f"<div class='stat-card'><h3>Stopped</h3><h2>{sum(1 for t in user_tasks if t.status == 'stopped')}</h2></div>", unsafe_allow_html=True)
    with c4:
        st.markdown(f"<div class='stat-card'><h3>Messages</h3><h2>{sum(t.messages_sent for t in user_tasks)}</h2></div>", unsafe_allow_html=True)
    
    # Create Task
    with st.expander("➕ Create New Task", expanded=len(user_tasks)==0):
        with st.form("create_task"):
            col1, col2 = st.columns(2)
            with col1:
                chat_id = st.text_input("Chat Thread ID", placeholder="1362400298935018")
                name_prefix = st.text_input("Name Prefix (optional)")
                delay = st.number_input("Delay (seconds)", min_value=10, value=30)
            with col2:
                messages = st.text_area("Messages (one per line)", height=100, placeholder="Hello!\nHow are you?")
                cookies = st.text_area("Facebook Cookies", height=100, placeholder="c_user=xxx; xs=xxx")
            
            if st.form_submit_button("🚀 Create & Start"):
                if chat_id and messages and cookies:
                    task_id = f"task_{random.randint(10000, 99999)}"
                    msgs = [m.strip() for m in messages.split('\n') if m.strip()]
                    task = Task(
                        task_id=task_id, username=st.session_state.username,
                        cookies=[cookies], chat_id=chat_id, name_prefix=name_prefix,
                        messages=msgs, delay=delay, status='stopped',
                        messages_sent=0, start_time=None, last_active=None,
                        last_browser_restart=None, rotation_index=0
                    )
                    task_manager.tasks[task_id] = task
                    task_manager.save_task(task)
                    task_manager.start_task(task_id)
                    st.success(f"✅ Task {task_id} created!")
                    st.rerun()
                else:
                    st.error("Please fill all fields!")
    
    # Task List
    st.markdown("### 📋 My Tasks")
    
    for task in user_tasks:
        with st.container():
            col1, col2, col3, col4, col5, col6 = st.columns([2, 1, 1, 1, 1, 1.5])
            
            with col1:
                st.markdown(f"**{task.task_id}**")
                st.caption(f"Chat: {task.chat_id[:20]}... | Sent: {task.messages_sent}")
            with col2:
                st.markdown(f"<span class='status-{task.status}'>{task.status.upper()}</span>", unsafe_allow_html=True)
            with col3:
                if task.status == 'running':
                    if st.button("⏸ Stop", key=f"stop_{task.task_id}"):
                        task_manager.stop_task(task.task_id)
                        st.rerun()
                else:
                    if st.button("▶ Start", key=f"start_{task.task_id}"):
                        task_manager.start_task(task.task_id)
                        st.rerun()
            with col4:
                if st.button("📄 Logs", key=f"logs_{task.task_id}"):
                    st.session_state.selected_task = task.task_id
                    st.rerun()
            with col5:
                if st.button("🗑 Delete", key=f"del_{task.task_id}"):
                    task_manager.delete_task(task.task_id)
                    st.rerun()
            with col6:
                # ============ SEND BUTTON (Flask Style) ============
                st.markdown('<div class="send-btn">', unsafe_allow_html=True)
                manual_msg = st.text_input("", key=f"msg_{task.task_id}", placeholder="Type message...", label_visibility="collapsed")
                if st.button("📤 Send", key=f"send_{task.task_id}"):
                    if manual_msg:
                        # Add to messages queue temporarily
                        original_msgs = task.messages.copy()
                        task.messages = [manual_msg]
                        task.rotation_index = 0
                        # Force send immediately
                        log_message(task.task_id, f"📤 Manual send: {manual_msg}")
                        st.success(f"✅ Sending: {manual_msg}")
                        time.sleep(1)
                        task.messages = original_msgs
                        st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)
        st.divider()
    
    # Logs
    st.markdown("### 📄 Task Logs")
    
    if st.session_state.selected_task and st.session_state.selected_task in task_manager.tasks:
        selected = st.session_state.selected_task
        logs = list(st.session_state.task_logs.get(selected, []))
        
        if logs:
            log_html = '<div class="log-container">'
            for log in logs[-100:]:
                if '✅' in log:
                    log_html += f'<div style="color: #6a9955;">🔹 {log}</div>'
                elif '❌' in log:
                    log_html += f'<div style="color: #f48771;">🔹 {log}</div>'
                else:
                    log_html += f'<div style="color: #4ec9b0;">🔹 {log}</div>'
            log_html += '</div>'
            st.markdown(log_html, unsafe_allow_html=True)
        else:
            st.info("No logs yet")
    else:
        st.info("Click 'Logs' on a task to view")
    
    st.caption(f"🔄 Browser restart: Every {BROWSER_RESTART_HOURS} hours | 🔪 Hard kill enabled")

# Run
if not st.session_state.logged_in:
    login_page()
else:
    dashboard()
