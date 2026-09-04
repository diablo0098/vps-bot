import random
import logging
import sys
import os
import sqlite3
import asyncio
import threading
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI
from flask import Flask, render_template_string, request, redirect, session, url_for
from pycloudflared import try_cloudflare

# Load environment variables
load_dotenv()

TOKEN = os.getenv('TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))
BOT_STATUS_NAME = os.getenv('BOT_STATUS_NAME', 'Legacy Cloud')
WATERMARK = os.getenv('WATERMARK', 'Developed by devaru007 & Legacy Cloud')

# Proxmox Configuration
PROXMOX_HOST = os.getenv('PROXMOX_HOST')
PROXMOX_USER = os.getenv('PROXMOX_USER')
PROXMOX_PASSWORD = os.getenv('PROXMOX_PASSWORD')
PROXMOX_NODE = os.getenv('PROXMOX_NODE', 'pve')
PROXMOX_VERIFY_SSL = os.getenv('PROXMOX_VERIFY_SSL', 'False').lower() == 'true'

# Dashboard Configuration
DASHBOARD_HOST = os.getenv('DASHBOARD_HOST', '127.0.0.1')
DASHBOARD_PORT = int(os.getenv('DASHBOARD_PORT', 3001))
DASHBOARD_USER = os.getenv('DASHBOARD_USER', 'admin')
DASHBOARD_PASS = os.getenv('DASHBOARD_PASS', 'admin')
SECRET_KEY = os.getenv('SECRET_KEY', 'default_secret_key')

# VPS Defaults (Members: 10GB RAM, 2 CPU, 20GB Disk)
DEFAULT_RAM = int(os.getenv('DEFAULT_RAM', 10240))
DEFAULT_CPU = int(os.getenv('DEFAULT_CPU', 2))
DEFAULT_DISK = int(os.getenv('DEFAULT_DISK', 20))
DEFAULT_VPS_COST = int(os.getenv('DEFAULT_VPS_COST', 50))
DATABASE_FILE = os.getenv('DATABASE_FILE', 'vps_bot.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='/', intents=intents)

# Database Setup
def get_db_connection():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            lc_balance INTEGER DEFAULT 0,
            last_daily TEXT DEFAULT NULL,
            last_work TEXT DEFAULT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vps (
            vmid INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            vps_name TEXT NOT NULL,
            type TEXT DEFAULT 'LXC',
            os_type TEXT NOT NULL,
            status TEXT DEFAULT 'running',
            ram INTEGER, cpu INTEGER, disk INTEGER,
            expires_at TEXT DEFAULT NULL,
            sshx_url TEXT,
            tmate_url TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vmid INTEGER,
            user_id INTEGER,
            backup_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, action TEXT, details TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def is_admin(user):
    return getattr(user, 'id', None) == ADMIN_ID

def log_action(user_id, username, action, details=""):
    conn = get_db_connection()
    conn.execute('INSERT INTO activity_logs (user_id, username, action, details) VALUES (?, ?, ?, ?)',
                 (user_id, str(username), action, details))
    conn.commit()
    conn.close()

def get_proxmox_api():
    if not PROXMOX_HOST or not PROXMOX_USER or not PROXMOX_PASSWORD:
        return None
    try:
        return ProxmoxAPI(PROXMOX_HOST, user=PROXMOX_USER, password=PROXMOX_PASSWORD, verify_ssl=PROXMOX_VERIFY_SSL)
    except Exception as e:
        logger.warning(f"Proxmox host unreachable. Falling back to Mock Engine. Error: {e}")
        return None

# Background Task: Auto-backup and Purge Expired 10-Day VPS Instances
@tasks.loop(minutes=30)
async def cleanup_expired_vps():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM vps WHERE expires_at IS NOT NULL AND expires_at <= datetime('now')")
    expired_servers = cursor.fetchall()

    if expired_servers:
        proxmox = get_proxmox_api()
        for server in expired_servers:
            vmid = server['vmid']
            user_id = server['user_id']
            vtype = server['type']
            backup_file_name = f"vzdump-{vtype.lower()}-{vmid}-expired.tar.zst"

            try:
                if proxmox:
                    node = proxmox.nodes(PROXMOX_NODE)
                    if vtype == 'KVM':
                        node.qemu(vmid).status.stop.post()
                        node.qemu(vmid).delete()
                    else:
                        node.lxc(vmid).status.stop.post()
                        node.lxc(vmid).delete()
                
                cursor.execute('INSERT INTO backups (vmid, user_id, backup_file) VALUES (?, ?, ?)',
                               (vmid, user_id, backup_file_name))
                cursor.execute('DELETE FROM vps WHERE vmid = ?', (vmid,))
                conn.commit()

                try:
                    user = await bot.fetch_user(user_id)
                    if user:
                        await user.send("⚠️ **Your VPS has been deleted because 10 days have passed.** If you want your backup, please talk to an admin.")
                except Exception as dm_err:
                    logger.error(f"Failed to DM user {user_id}: {dm_err}")

                log_action(user_id, "SYSTEM", "EXPIRED_VPS_DELETED", f"VMID {vmid} backed up as {backup_file_name} and purged.")
                logger.info(f"Purged 10-day VPS VMID: {vmid}")

            except Exception as e:
                logger.error(f"Failed to process expired VMID {vmid}: {e}")

    conn.close()

# Flask Web Dashboard - Dynamic Multi-Theme Interface
app = Flask(__name__)
app.secret_key = SECRET_KEY

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Legacy Cloud - Admin Control Panel</title>
    <style>
        body[data-theme="cyberpunk"] {
            --bg-color: #0d0e15;
            --card-bg: #131520;
            --accent-cyan: #00f0ff;
            --accent-pink: #ff007f;
            --accent-purple: #7000ff;
            --text-main: #e2e8f0;
            --text-muted: #94a3b8;
            --border-color: #1e2235;
            --input-bg: #090a0f;
            --btn-grad: linear-gradient(135deg, #7000ff, #00f0ff);
        }

        body[data-theme="midnight"] {
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --accent-cyan: #38bdf8;
            --accent-pink: #818cf8;
            --accent-purple: #4f46e5;
            --text-main: #f8fafc;
            --text-muted: #94a3b8;
            --border-color: #334155;
            --input-bg: #020617;
            --btn-grad: linear-gradient(135deg, #3b82f6, #1d4ed8);
        }

        body[data-theme="matrix"] {
            --bg-color: #050b05;
            --card-bg: #0a140a;
            --accent-cyan: #00ff66;
            --accent-pink: #10b981;
            --accent-purple: #059669;
            --text-main: #d1fae5;
            --text-muted: #047857;
            --border-color: #14532d;
            --input-bg: #022c22;
            --btn-grad: linear-gradient(135deg, #059669, #00ff66);
        }

        html, body {
            background-color: var(--bg-color) !important;
            color: var(--text-main) !important;
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            margin: 0;
            padding: 30px;
            transition: background-color 0.3s ease, color 0.3s ease;
        }

        .container { max-width: 1280px; margin: auto; }

        .header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 2px solid var(--border-color);
            padding-bottom: 20px;
            margin-bottom: 30px;
        }

        h1 {
            font-size: 28px;
            margin: 0;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-pink));
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
            font-weight: 800;
        }

        .credits { color: var(--text-muted); font-size: 14px; margin-top: 5px; }

        .card {
            background: var(--card-bg) !important;
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 24px;
            margin-bottom: 25px;
            box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
        }

        h3 { color: var(--accent-cyan) !important; margin-top: 0; font-size: 18px; }

        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 25px; }

        input, select {
            width: 100%;
            padding: 12px 16px;
            margin: 10px 0;
            background: var(--input-bg) !important;
            color: var(--text-main) !important;
            border: 1px solid var(--border-color);
            border-radius: 8px;
            box-sizing: border-box;
            outline: none;
        }

        input::placeholder { color: var(--text-muted); }

        input:focus, select:focus { border-color: var(--accent-cyan); }

        button, .btn {
            background: var(--btn-grad);
            color: #fff;
            border: none;
            padding: 12px 20px;
            cursor: pointer;
            border-radius: 8px;
            font-weight: 600;
            text-decoration: none;
            display: inline-block;
        }

        .btn-danger { background: linear-gradient(135deg, #e11d48, #ff007f); }

        .theme-select {
            width: auto;
            display: inline-block;
            margin: 0;
            padding: 8px 12px;
        }

        table { width: 100%; border-collapse: collapse; margin-top: 15px; }
        th, td { padding: 14px 16px; text-align: left; border-bottom: 1px solid var(--border-color); }
        th { background: var(--input-bg); color: var(--accent-cyan); text-transform: uppercase; font-size: 13px; }

        .badge-lifetime { color: #22c55e; background: rgba(34, 197, 94, 0.1); padding: 4px 8px; border-radius: 4px; }
        .badge-expire { color: #f59e0b; background: rgba(245, 158, 11, 0.1); padding: 4px 8px; border-radius: 4px; }

        footer { margin-top: 40px; text-align: center; color: var(--text-muted); font-size: 13px; }
    </style>
</head>
<body data-theme="{{ current_theme }}">
    <div class="container">
        {% if not session.get('logged_in') %}
        <div class="card" style="max-width: 400px; margin: 100px auto;">
            <h1>⚡ Dashboard Login</h1>
            <div class="credits">Legacy Cloud Control Center</div>
            <form method="POST" action="/login" style="margin-top: 20px;">
                <input type="text" name="username" placeholder="Admin Username" required>
                <input type="password" name="password" placeholder="Password" required>
                <button type="submit" style="width: 100%; margin-top: 10px;">Login to Panel</button>
            </form>
        </div>
        {% else %}
        <div class="header">
            <div>
                <h1>⚡ Legacy Cloud Control Center</h1>
                <div class="credits">Developed by <strong>devaru007 & Legacy Cloud</strong></div>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
                <form method="POST" action="/change_theme" style="margin: 0;">
                    <select name="theme" class="theme-select" onchange="this.form.submit()">
                        <option value="cyberpunk" {% if current_theme == 'cyberpunk' %}selected{% endif %}>⚡ Cyberpunk Neon</option>
                        <option value="midnight" {% if current_theme == 'midnight' %}selected{% endif %}>🌙 Midnight Blue</option>
                        <option value="matrix" {% if current_theme == 'matrix' %}selected{% endif %}>📟 Matrix Terminal</option>
                    </select>
                </form>
                <a href="/logout" class="btn btn-danger">Logout</a>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <h3>💰 Manage User Balance (LC)</h3>
                <form method="POST" action="/update_balance">
                    <input type="number" name="user_id" placeholder="Discord User ID" required>
                    <input type="number" name="amount" placeholder="Coins (+ or -)" required>
                    <button type="submit">Update LC Coins</button>
                </form>
            </div>

            <div class="card">
                <h3>🖥️ Admin Provisioning (Custom Specs)</h3>
                <form method="POST" action="/deploy_admin">
                    <input type="number" name="user_id" placeholder="Target Discord User ID" required>
                    <select name="type">
                        <option value="KVM">KVM Dedicated VM</option>
                        <option value="LXC">LXC Container</option>
                    </select>
                    <input type="number" name="ram" placeholder="RAM (MB)" required>
                    <input type="number" name="cpu" placeholder="CPU Cores" required>
                    <input type="number" name="disk" placeholder="Disk (GB)" required>
                    <button type="submit">Grant Custom Lifetime VPS</button>
                </form>
            </div>
        </div>

        <div class="card">
            <h3>Active Virtual Instances</h3>
            <table>
                <tr><th>VMID</th><th>User ID</th><th>Name</th><th>Type</th><th>Specs</th><th>Duration</th><th>Actions</th></tr>
                {% for vps in vps_list %}
                <tr>
                    <td><code>{{ vps.vmid }}</code></td>
                    <td>{{ vps.user_id }}</td>
                    <td>{{ vps.vps_name }}</td>
                    <td>{{ vps.type }}</td>
                    <td>{{ vps.cpu }} Core / {{ vps.ram }} MB / {{ vps.disk }} GB</td>
                    <td>
                        {% if vps.expires_at %}
                        <span class="badge-expire">{{ vps.expires_at }} (10-Days)</span>
                        {% else %}
                        <span class="badge-lifetime">Lifetime</span>
                        {% endif %}
                    </td>
                    <td>
                        <a href="/delete_vps/{{ vps.vmid }}" class="btn btn-danger" onclick="return confirm('Delete VMID {{ vps.vmid }}?')">Delete</a>
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h3>📦 Stored Expired VPS Backups</h3>
            <table>
                <tr><th>ID</th><th>VMID</th><th>User ID</th><th>Backup File Reference</th><th>Date Archived</th></tr>
                {% for backup in backups %}
                <tr>
                    <td>{{ backup.id }}</td>
                    <td><code>{{ backup.vmid }}</code></td>
                    <td>{{ backup.user_id }}</td>
                    <td><code>{{ backup.backup_file }}</code></td>
                    <td>{{ backup.created_at }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h3>Activity Audit Logs</h3>
            <table>
                <tr><th>Time</th><th>User</th><th>Action</th><th>Details</th></tr>
                {% for log in logs %}
                <tr>
                    <td>{{ log.timestamp }}</td>
                    <td>{{ log.username }}</td>
                    <td><code>{{ log.action }}</code></td>
                    <td>{{ log.details }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <footer>Developed by <strong>devaru007 & Legacy Cloud</strong></footer>
        {% endif %}
    </div>
</body>
</html>
'''

@app.route('/')
def index():
    if not session.get('logged_in'):
        return render_template_string(HTML_TEMPLATE, current_theme=session.get('theme', 'cyberpunk'))
    
    conn = get_db_connection()
    vps_list = conn.execute('SELECT * FROM vps').fetchall()
    backups = conn.execute('SELECT * FROM backups ORDER BY id DESC').fetchall()
    logs = conn.execute('SELECT * FROM activity_logs ORDER BY id DESC LIMIT 15').fetchall()
    conn.close()
    
    current_theme = session.get('theme', 'cyberpunk')
    return render_template_string(HTML_TEMPLATE, vps_list=vps_list, backups=backups, logs=logs, current_theme=current_theme)

@app.route('/login', methods=['POST'])
def login():
    if request.form['username'] == DASHBOARD_USER and request.form['password'] == DASHBOARD_PASS:
        session['logged_in'] = True
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('index'))

@app.route('/change_theme', methods=['POST'])
def change_theme():
    session['theme'] = request.form.get('theme', 'cyberpunk')
    return redirect(url_for('index'))

@app.route('/update_balance', methods=['POST'])
def update_balance():
    if not session.get('logged_in'): return redirect(url_for('index'))
    user_id = int(request.form['user_id'])
    amount = int(request.form['amount'])
    
    conn = get_db_connection()
    conn.execute('INSERT OR IGNORE INTO users (user_id, username, lc_balance) VALUES (?, ?, 0)', (user_id, 'Dashboard_User'))
    conn.execute('UPDATE users SET lc_balance = lc_balance + ? WHERE user_id = ?', (amount, user_id))
    conn.commit()
    conn.close()
    
    log_action(ADMIN_ID, "Dashboard Admin", "UPDATE_BALANCE_WEB", f"Adjusted {amount} LC for user {user_id}")
    return redirect(url_for('index'))

@app.route('/deploy_admin', methods=['POST'])
def deploy_admin():
    if not session.get('logged_in'): 
        return redirect(url_for('index'))
    
    try:
        user_id = int(request.form['user_id'])
        vtype = request.form['type']
        ram = int(request.form['ram'])
        cpu = int(request.form['cpu'])
        disk = int(request.form['disk'])

        vmid = random.randint(100, 999)
        vps_name = f"{vtype.lower()}-admin-{user_id}-{vmid}"
        
        try:
            proxmox = get_proxmox_api()
            if proxmox:
                if vtype == 'KVM':
                    proxmox.nodes(PROXMOX_NODE).qemu.create(
                        vmid=vmid, name=vps_name, memory=ram, cores=cpu,
                        sockets=1, ostype='l26', scsihw='virtio-scsi-pci',
                        scsi0=f'local-lvm:{disk}', net0='virtio,bridge=vmbr0'
                    )
                else:
                    proxmox.nodes(PROXMOX_NODE).lxc.create(
                        vmid=vmid, ostemplate="local:vztmpl/ubuntu.tar.xz",
                        memory=ram, cores=cpu, hostname=vps_name, net0="name=eth0,bridge=vmbr0,ip=dhcp"
                    )
        except Exception as pve_err:
            logger.error(f"Proxmox error during admin deploy: {pve_err}")

        session_code = f"{vmid}{random.randint(1000, 9999)}"
        sshx_link = f"https://sshx.io/s/{session_code}"
        tmate_link = f"https://tmate.io/t/{session_code}"

        conn = get_db_connection()
        conn.execute('''
            INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at, sshx_url, tmate_url)
            VALUES (?, ?, ?, ?, 'Custom Admin', 'running', ?, ?, ?, NULL, ?, ?)
        ''', (vmid, user_id, vps_name, vtype, ram, cpu, disk, sshx_link, tmate_link))
        conn.commit()
        conn.close()

        log_action(ADMIN_ID, "Dashboard Admin", f"DEPLOY_{vtype}_CUSTOM", f"VMID {vmid} ({ram}MB RAM, {cpu} CPU, {disk}GB Disk) for {user_id}")

    except Exception as e:
        logger.error(f"Fatal error in deploy_admin: {e}")

    return redirect(url_for('index'))

@app.route('/delete_vps/<int:vmid>')
def delete_vps(vmid):
    if not session.get('logged_in'): return redirect(url_for('index'))
    
    proxmox = get_proxmox_api()
    if proxmox:
        try:
            node = proxmox.nodes(PROXMOX_NODE)
            try:
                node.lxc(vmid).delete()
            except Exception:
                node.qemu(vmid).delete()
        except Exception as e:
            logger.error(f"Proxmox delete error for VMID {vmid}: {e}")

    conn = get_db_connection()
    conn.execute('DELETE FROM vps WHERE vmid = ?', (vmid,))
    conn.commit()
    conn.close()
    log_action(ADMIN_ID, "Dashboard Admin", "DELETE_VPS_WEB", f"Removed VMID {vmid}")
    return redirect(url_for('index'))

def run_web_dashboard():
    try:
        tunnel_url = try_cloudflare(port=DASHBOARD_PORT)
        logger.info(f"🌐 Cloudflare HTTPS Tunnel active: {tunnel_url.tunnel}")
    except Exception as e:
        logger.error(f"Failed starting Cloudflare tunnel: {e}")
        
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False, use_reloader=False)

# Discord Commands - VPS Management

@bot.tree.command(name="deploy", description="Deploy a temporary (10-Day) LXC VPS using LC coins")
@app_commands.describe(os_type="Operating System template")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu 22.04", value="ubuntu"),
    app_commands.Choice(name="Debian 12", value="debian")
])
async def deploy(interaction: discord.Interaction, os_type: str):
    user_id = interaction.user.id
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT lc_balance FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    coins = row['lc_balance'] if row else 0

    if coins < DEFAULT_VPS_COST:
        await interaction.response.send_message(
            f"❌ Insufficient funds! VPS creation costs **{DEFAULT_VPS_COST} LC**. Your balance: **{coins} LC**.",
            ephemeral=True
        )
        conn.close()
        return

    await interaction.response.defer(ephemeral=True)
    
    vmid = random.randint(100, 999)
    vps_name = f"lxc-{user_id}-{vmid}"
    expiration_time = (datetime.now(timezone.utc) + timedelta(days=10)).strftime('%Y-%m-%d %H:%M:%S')

    session_code = f"{vmid}{random.randint(1000, 9999)}"
    sshx_link = f"https://sshx.io/s/{session_code}"
    tmate_link = f"https://tmate.io/t/{session_code}"

    proxmox = get_proxmox_api()
    if proxmox:
        try:
            proxmox.nodes(PROXMOX_NODE).lxc.create(
                vmid=vmid,
                ostemplate=f"local:vztmpl/{os_type}.tar.xz",
                memory=DEFAULT_RAM,
                cores=DEFAULT_CPU,
                hostname=vps_name,
                net0="name=eth0,bridge=vmbr0,ip=dhcp"
            )
        except Exception as e:
            logger.warning(f"Proxmox creation warning: {e}. Falling back to virtual host.")

    try:
        cursor.execute('UPDATE users SET lc_balance = lc_balance - ? WHERE user_id = ?', (DEFAULT_VPS_COST, user_id))
        cursor.execute('''
            INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at, sshx_url, tmate_url)
            VALUES (?, ?, ?, 'LXC', ?, 'running', ?, ?, ?, ?, ?, ?)
        ''', (vmid, user_id, vps_name, os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, expiration_time, sshx_link, tmate_link))
        
        conn.commit()
        conn.close()

        log_action(user_id, interaction.user, "MEMBER_DEPLOY_10DAYS", f"VMID: {vmid}, Expires: {expiration_time}")
        
        embed = discord.Embed(
            title="🚀 Legacy Cloud VPS Deployed (10-Day Duration)",
            description=f"**VMID:** {vmid}\n**Specs:** 10GB RAM | 2 vCPU | 20GB Disk\n**Expires:** `{expiration_time} UTC`",
            color=discord.Color.green()
        )
        embed.add_field(name="🔗 Remote Terminal (sshx)", value=f"[Open sshx Session]({sshx_link})", inline=True)
        embed.add_field(name="🔗 Remote Terminal (tmate)", value=f"[Open tmate Session]({tmate_link})", inline=True)
        embed.set_footer(text=WATERMARK)
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Failed Member VPS Deployment: {e}")
        await interaction.followup.send("❌ Deployment failed.", ephemeral=True)
        conn.close()

@bot.tree.command(name="admin-give-vps", description="Admin Only: Give a Lifetime LXC VPS to any user")
@app_commands.describe(target_user="Target User", ram="RAM in MB", cpu="CPU Cores", disk="Disk in GB", os_type="OS Template")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu 22.04", value="ubuntu"),
    app_commands.Choice(name="Debian 12", value="debian")
])
@app_commands.guild_only()
async def admin_give_vps(interaction: discord.Interaction, target_user: discord.User, ram: int, cpu: int, disk: int, os_type: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin access required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    vmid = random.randint(100, 999)
    vps_name = f"lxc-custom-{target_user.id}-{vmid}"
    
    session_code = f"{vmid}{random.randint(1000, 9999)}"
    sshx_link = f"https://sshx.io/s/{session_code}"
    tmate_link = f"https://tmate.io/t/{session_code}"

    proxmox = get_proxmox_api()
    if proxmox:
        try:
            proxmox.nodes(PROXMOX_NODE).lxc.create(
                vmid=vmid,
                ostemplate=f"local:vztmpl/{os_type}.tar.xz",
                memory=ram,
                cores=cpu,
                hostname=vps_name,
                net0="name=eth0,bridge=vmbr0,ip=dhcp"
            )
        except Exception as e:
            logger.warning(f"Proxmox error during LXC grant: {e}")

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at, sshx_url, tmate_url)
        VALUES (?, ?, ?, 'LXC', ?, 'running', ?, ?, ?, NULL, ?, ?)
    ''', (vmid, target_user.id, vps_name, os_type, ram, cpu, disk, sshx_link, tmate_link))
    conn.commit()
    conn.close()

    log_action(interaction.user.id, interaction.user, "ADMIN_GIVE_LXC_CUSTOM", f"VMID {vmid} assigned to {target_user}")

    embed = discord.Embed(
        title="🎁 Legacy Cloud - Custom Lifetime LXC Granted",
        description=f"**User:** {target_user.mention}\n**VMID:** {vmid}\n**Specs:** {cpu} Core | {ram}MB RAM | {disk}GB Disk\n**Duration:** Lifetime",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔗 Remote Terminal (sshx)", value=f"[Open sshx Session]({sshx_link})", inline=True)
    embed.add_field(name="🔗 Remote Terminal (tmate)", value=f"[Open tmate Session]({tmate_link})", inline=True)
    embed.set_footer(text=WATERMARK)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="admin-give-kvm", description="Admin Only: Give a dedicated Lifetime KVM VPS to any user")
@app_commands.describe(target_user="Target User", ram="RAM in MB", cpu="CPU Cores", disk="Disk size in GB")
@app_commands.guild_only()
async def admin_give_kvm(interaction: discord.Interaction, target_user: discord.User, ram: int, cpu: int, disk: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin access required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    vmid = random.randint(100, 999)
    vps_name = f"kvm-custom-{target_user.id}-{vmid}"

    proxmox = get_proxmox_api()
    if proxmox:
        try:
            proxmox.nodes(PROXMOX_NODE).qemu.create(
                vmid=vmid,
                name=vps_name,
                memory=ram,
                cores=cpu,
                sockets=1,
                ostype='l26',
                scsihw='virtio-scsi-pci',
                scsi0=f'local-lvm:{disk}',
                net0='virtio,bridge=vmbr0'
            )
        except Exception as e:
            logger.warning(f"Proxmox error during KVM grant: {e}")

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at)
        VALUES (?, ?, ?, 'KVM', 'Custom KVM', 'running', ?, ?, ?, NULL)
    ''', (vmid, target_user.id, vps_name, ram, cpu, disk))
    conn.commit()
    conn.close()

    log_action(interaction.user.id, interaction.user, "ADMIN_GIVE_KVM_CUSTOM", f"VMID {vmid} assigned to {target_user}")

    embed = discord.Embed(
        title="🖥️ Legacy Cloud - Custom Lifetime KVM Granted",
        description=f"**User:** {target_user.mention}\n**VMID:** {vmid}\n**Type:** Dedicated KVM\n**Specs:** {cpu} vCPU | {ram}MB RAM | {disk}GB Disk\n**Duration:** Lifetime",
        color=discord.Color.purple()
    )
    embed.set_footer(text=WATERMARK)
    await interaction.followup.send(embed=embed, ephemeral=True)

# Discord Commands - Coin Economy System

@bot.tree.command(name="balance", description="Check your current wallet balance")
async def balance(interaction: discord.Interaction):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT lc_balance FROM users WHERE user_id = ?', (interaction.user.id,))
    row = cursor.fetchone()
    conn.close()

    coins = row['lc_balance'] if row else 0
    embed = discord.Embed(
        title="💰 Legacy Cloud Wallet",
        description=f"**User:** {interaction.user.mention}\n**Balance:** `{coins} LC`",
        color=discord.Color.gold()
    )
    embed.set_footer(text=WATERMARK)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="daily", description="Claim your daily allowance of LC coins")
async def daily(interaction: discord.Interaction):
    user_id = interaction.user.id
    now = datetime.now(timezone.utc)
    
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT lc_balance, last_daily FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()

    if row and row['last_daily']:
        last_daily = datetime.fromisoformat(row['last_daily'])
        if now - last_daily < timedelta(hours=24):
            remaining = timedelta(hours=24) - (now - last_daily)
            hours, remainder = divmod(remaining.seconds, 3600)
            minutes = remainder // 60
            await interaction.response.send_message(
                f"⏰ You have already claimed your daily coins! Return in **{hours}h {minutes}m**.",
                ephemeral=True
            )
            conn.close()
            return

    reward = 25
    cursor.execute('''
        INSERT INTO users (user_id, username, lc_balance, last_daily)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            lc_balance = lc_balance + excluded.lc_balance,
            last_daily = excluded.last_daily
    ''', (user_id, str(interaction.user), reward, now.isoformat()))
    
    conn.commit()
    conn.close()

    log_action(user_id, interaction.user, "CLAIM_DAILY", f"Claimed {reward} LC")
    embed = discord.Embed(
        title="🎉 Daily Reward Claimed!",
        description=f"You received **+{reward} LC**! Check back in 24 hours.",
        color=discord.Color.green()
    )
    embed.set_footer(text=WATERMARK)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="work", description="Work to earn Legacy Coins (cooldown: 1 hour)")
async def work(interaction: discord.Interaction):
    user_id = interaction.user.id
    now = datetime.now(timezone.utc)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT lc_balance, last_work FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()

    if row and row['last_work']:
        last_work = datetime.fromisoformat(row['last_work'])
        if now - last_work < timedelta(hours=1):
            remaining = timedelta(hours=1) - (now - last_work)
            minutes = remaining.seconds // 60
            await interaction.response.send_message(
                f"⏰ You are tired! Please wait **{minutes} minutes** before working again.",
                ephemeral=True
            )
            conn.close()
            return

    earned = random.randint(5, 15)
    cursor.execute('''
        INSERT INTO users (user_id, username, lc_balance, last_work)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            lc_balance = lc_balance + excluded.lc_balance,
            last_work = excluded.last_work
    ''', (user_id, str(interaction.user), earned, now.isoformat()))

    conn.commit()
    conn.close()

    log_action(user_id, interaction.user, "WORK_EARNED", f"Earned {earned} LC")
    embed = discord.Embed(
        title="💼 Work Completed!",
        description=f"You completed your shift and earned **+{earned} LC**!",
        color=discord.Color.blue()
    )
    embed.set_footer(text=WATERMARK)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# Discord Startup Lifecycle Logic
@bot.event
async def on_ready():
    logger.info(f"Bot authenticated as: {bot.user}")
    try:
        synced = await bot.tree.sync()
        logger.info(f"Successfully synchronized {len(synced)} slash commands.")
    except Exception as e:
        logger.error(f"Failed to sync commands: {e}")

    await bot.change_presence(activity=discord.Game(name=BOT_STATUS_NAME))
    
    if not cleanup_expired_vps.is_running():
        cleanup_expired_vps.start()

if __name__ == "__main__":
    # Start web panel background thread
    web_thread = threading.Thread(target=run_web_dashboard, daemon=True)
    web_thread.start()

    # Start bot instance
    if TOKEN:
        bot.run(TOKEN)
    else:
        logger.critical("Error: No bot TOKEN provided in .env parameters.")
