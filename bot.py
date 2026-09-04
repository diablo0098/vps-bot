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

# VPS Defaults
DEFAULT_RAM = int(os.getenv('DEFAULT_RAM', 2048))
DEFAULT_CPU = int(os.getenv('DEFAULT_CPU', 1))
DEFAULT_DISK = int(os.getenv('DEFAULT_DISK', 10))
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
            lc_balance INTEGER DEFAULT 0
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vps (
            vmid INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            vps_name TEXT NOT NULL,
            type TEXT DEFAULT 'LXC',
            os_type TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            ram INTEGER, cpu INTEGER, disk INTEGER,
            expires_at TEXT DEFAULT NULL
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
    try:
        return ProxmoxAPI(PROXMOX_HOST, user=PROXMOX_USER, password=PROXMOX_PASSWORD, verify_ssl=PROXMOX_VERIFY_SSL)
    except Exception as e:
        logger.error(f"Proxmox Connection Error: {e}")
        return None

# Background Task to Auto-Clean Expired 2-Day VPS Instances
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
            vtype = server['type']
            try:
                if proxmox:
                    node = proxmox.nodes(PROXMOX_NODE)
                    if vtype == 'KVM':
                        node.qemu(vmid).status.stop.post()
                        node.qemu(vmid).delete()
                    else:
                        node.lxc(vmid).status.stop.post()
                        node.lxc(vmid).delete()
                
                cursor.execute('DELETE FROM vps WHERE vmid = ?', (vmid,))
                log_action(server['user_id'], "SYSTEM", "EXPIRED_VPS_DELETED", f"VMID {vmid} expired and was purged automatically.")
                logger.info(f"Purged expired VPS VMID: {vmid}")
            except Exception as e:
                logger.error(f"Failed to auto-delete expired VMID {vmid}: {e}")

    conn.commit()
    conn.close()

# Web Dashboard
app = Flask(__name__)
app.secret_key = SECRET_KEY

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Legacy Cloud - Admin Control Center</title>
    <style>
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background: #0b0f19; color: #f8fafc; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: auto; }
        .card { background: #1e293b; padding: 20px; border-radius: 10px; margin-bottom: 20px; border: 1px solid #334155; }
        h1, h2, h3 { color: #38bdf8; margin-top: 0; }
        .credits { color: #94a3b8; font-size: 14px; margin-bottom: 20px; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { border: 1px solid #334155; padding: 12px; text-align: left; }
        th { background: #0f172a; color: #38bdf8; }
        .login-box { max-width: 350px; margin: 100px auto; }
        input, select { width: 100%; padding: 10px; margin: 8px 0; background: #0f172a; color: white; border: 1px solid #334155; border-radius: 5px; box-sizing: border-box; }
        button, .btn { background: #0284c7; color: white; border: none; padding: 10px 15px; cursor: pointer; border-radius: 5px; text-decoration: none; display: inline-block; }
        button:hover, .btn:hover { background: #0369a1; }
        .btn-danger { background: #ef4444; }
        .btn-danger:hover { background: #dc2626; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }
        footer { margin-top: 30px; text-align: center; color: #64748b; font-size: 13px; }
    </style>
</head>
<body>
    <div class="container">
        {% if not session.get('logged_in') %}
        <div class="card login-box">
            <h2>Dashboard Login</h2>
            <form method="POST" action="/login">
                <input type="text" name="username" placeholder="Admin Username" required>
                <input type="password" name="password" placeholder="Password" required>
                <button type="submit">Login</button>
            </form>
        </div>
        {% else %}
        <h1>⚡ Legacy Cloud Control Center</h1>
        <div class="credits">Developed by <strong>devaru007 & Legacy Cloud</strong></div>
        <a href="/logout" class="btn btn-danger" style="float: right; margin-top: -50px;">Logout</a>

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
                <h3>🖥️ Admin KVM Grant (Lifetime)</h3>
                <form method="POST" action="/deploy_kvm">
                    <input type="number" name="user_id" placeholder="Target Discord User ID" required>
                    <input type="number" name="ram" placeholder="RAM (MB)" value="2048">
                    <input type="number" name="cpu" placeholder="CPU Cores" value="2">
                    <input type="number" name="disk" placeholder="Disk (GB)" value="20">
                    <button type="submit">Grant Lifetime KVM VPS</button>
                </form>
            </div>
        </div>

        <div class="card">
            <h3>Active Virtual Instances</h3>
            <table>
                <tr><th>VMID</th><th>User ID</th><th>Name</th><th>Type</th><th>Specs</th><th>Duration</th><th>Actions</th></tr>
                {% for vps in vps_list %}
                <tr>
                    <td>{{ vps.vmid }}</td>
                    <td>{{ vps.user_id }}</td>
                    <td>{{ vps.vps_name }}</td>
                    <td>{{ vps.type }}</td>
                    <td>{{ vps.cpu }} Core / {{ vps.ram }} MB / {{ vps.disk }} GB</td>
                    <td>{% if vps.expires_at %}{{ vps.expires_at }} (2-Days){% else %}<strong style="color:#22c55e;">Lifetime</strong>{% endif %}</td>
                    <td>
                        <a href="/delete_vps/{{ vps.vmid }}" class="btn btn-danger" onclick="return confirm('Delete VMID {{ vps.vmid }}?')">Delete</a>
                    </td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <div class="card">
            <h3>User Database</h3>
            <table>
                <tr><th>User ID</th><th>Username</th><th>LC Balance</th></tr>
                {% for user in users %}
                <tr>
                    <td>{{ user.user_id }}</td>
                    <td>{{ user.username }}</td>
                    <td>{{ user.lc_balance }} LC</td>
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
                    <td>{{ log.action }}</td>
                    <td>{{ log.details }}</td>
                </tr>
                {% endfor %}
            </table>
        </div>

        <footer>Developed by devaru007 & Legacy Cloud</footer>
        {% endif %}
    </div>
</body>
</html>
'''

@app.route('/')
def index():
    if not session.get('logged_in'):
        return render_template_string(HTML_TEMPLATE)
    
    conn = get_db_connection()
    vps_list = conn.execute('SELECT * FROM vps').fetchall()
    users = conn.execute('SELECT * FROM users').fetchall()
    logs = conn.execute('SELECT * FROM activity_logs ORDER BY id DESC LIMIT 15').fetchall()
    conn.close()
    return render_template_string(HTML_TEMPLATE, vps_list=vps_list, users=users, logs=logs)

@app.route('/login', methods=['POST'])
def login():
    if request.form['username'] == DASHBOARD_USER and request.form['password'] == DASHBOARD_PASS:
        session['logged_in'] = True
    return redirect(url_for('index'))

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
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

@app.route('/deploy_kvm', methods=['POST'])
def deploy_kvm():
    if not session.get('logged_in'): return redirect(url_for('index'))
    user_id = int(request.form['user_id'])
    ram = int(request.form['ram'])
    cpu = int(request.form['cpu'])
    disk = int(request.form['disk'])

    proxmox = get_proxmox_api()
    if proxmox:
        vmid = random.randint(100, 999)
        vps_name = f"kvm-web-{user_id}-{vmid}"
        try:
            proxmox.nodes(PROXMOX_NODE).qemu.create(
                vmid=vmid, name=vps_name, memory=ram, cores=cpu,
                sockets=1, ostype='l26', scsihw='virtio-scsi-pci',
                scsi0=f'local-lvm:{disk}', net0='virtio,bridge=vmbr0'
            )
            conn = get_db_connection()
            conn.execute('''
                INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at)
                VALUES (?, ?, ?, 'KVM', 'Custom KVM', 'stopped', ?, ?, ?, NULL)
            ''', (vmid, user_id, vps_name, ram, cpu, disk))
            conn.commit()
            conn.close()
            log_action(ADMIN_ID, "Dashboard Admin", "DEPLOY_KVM_LIFETIME", f"Created Lifetime KVM VMID {vmid} for {user_id}")
        except Exception as e:
            logger.error(f"Web Deploy Error: {e}")

    return redirect(url_for('index'))

@app.route('/delete_vps/<int:vmid>')
def delete_vps(vmid):
    if not session.get('logged_in'): return redirect(url_for('index'))
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

# Discord Commands

@bot.tree.command(name="deploy", description="Deploy a temporary (2-Day) LXC VPS using LC coins")
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
    
    proxmox = get_proxmox_api()
    if not proxmox:
        await interaction.followup.send("❌ Error connecting to Proxmox Cluster.", ephemeral=True)
        conn.close()
        return

    vmid = random.randint(100, 999)
    vps_name = f"lxc-{user_id}-{vmid}"
    expiration_time = (datetime.now(timezone.utc) + timedelta(days=2)).strftime('%Y-%m-%d %H:%M:%S')

    try:
        cursor.execute('UPDATE users SET lc_balance = lc_balance - ? WHERE user_id = ?', (DEFAULT_VPS_COST, user_id))
        
        proxmox.nodes(PROXMOX_NODE).lxc.create(
            vmid=vmid,
            ostemplate=f"local:vztmpl/{os_type}.tar.xz",
            memory=DEFAULT_RAM,
            cores=DEFAULT_CPU,
            hostname=vps_name,
            net0="name=eth0,bridge=vmbr0,ip=dhcp"
        )
        
        cursor.execute('''
            INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at)
            VALUES (?, ?, ?, 'LXC', ?, 'running', ?, ?, ?, ?)
        ''', (vmid, user_id, vps_name, os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, expiration_time))
        
        conn.commit()
        conn.close()

        log_action(user_id, interaction.user, "MEMBER_DEPLOY_2DAYS", f"VMID: {vmid}, Expires: {expiration_time}")
        
        embed = discord.Embed(
            title="🚀 Legacy Cloud VPS Deployed (2-Day Temporary)",
            description=f"**VMID:** {vmid}\n**Name:** {vps_name}\n**OS:** {os_type}\n**Duration:** 2 Days (Expires: `{expiration_time} UTC`)",
            color=discord.Color.green()
        )
        embed.set_footer(text=WATERMARK)
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Failed Member VPS Deployment: {e}")
        await interaction.followup.send("❌ Deployment failed. Coins refunded.", ephemeral=True)
        conn.close()

@bot.tree.command(name="admin-give-vps", description="Admin Only: Give a Lifetime LXC VPS to any user")
@app_commands.describe(target_user="Target User", os_type="OS Template")
@app_commands.choices(os_type=[
    app_commands.Choice(name="Ubuntu 22.04", value="ubuntu"),
    app_commands.Choice(name="Debian 12", value="debian")
])
@app_commands.guild_only()
async def admin_give_vps(interaction: discord.Interaction, target_user: discord.User, os_type: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin access required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    proxmox = get_proxmox_api()
    if not proxmox:
        await interaction.followup.send("❌ Proxmox API Connection Error.", ephemeral=True)
        return

    vmid = random.randint(100, 999)
    vps_name = f"lxc-life-{target_user.id}-{vmid}"

    try:
        proxmox.nodes(PROXMOX_NODE).lxc.create(
            vmid=vmid,
            ostemplate=f"local:vztmpl/{os_type}.tar.xz",
            memory=DEFAULT_RAM,
            cores=DEFAULT_CPU,
            hostname=vps_name,
            net0="name=eth0,bridge=vmbr0,ip=dhcp"
        )

        conn = get_db_connection()
        conn.execute('''
            INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at)
            VALUES (?, ?, ?, 'LXC', ?, 'stopped', ?, ?, ?, NULL)
        ''', (vmid, target_user.id, vps_name, os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK))
        conn.commit()
        conn.close()

        log_action(interaction.user.id, interaction.user, "ADMIN_GIVE_LXC_LIFETIME", f"VMID {vmid} assigned to {target_user}")

        embed = discord.Embed(
            title="🎁 Legacy Cloud - Lifetime LXC Granted",
            description=f"**User:** {target_user.mention}\n**VMID:** {vmid}\n**Duration:** Lifetime\n**OS:** {os_type}",
            color=discord.Color.blue()
        )
        embed.set_footer(text=WATERMARK)
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Failed Admin LXC Deployment: {e}")
        await interaction.followup.send(f"❌ Failed to give LXC VPS: {e}", ephemeral=True)

@bot.tree.command(name="admin-give-kvm", description="Admin Only: Give a dedicated Lifetime KVM VPS to any user")
@app_commands.describe(target_user="Target User", ram="RAM in MB", cpu="CPU Cores", disk="Disk size in GB")
@app_commands.guild_only()
async def admin_give_kvm(interaction: discord.Interaction, target_user: discord.User, ram: int = 2048, cpu: int = 2, disk: int = 20):
    if not is_admin(interaction.user):
        await interaction.response.send_message("❌ Admin access required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    proxmox = get_proxmox_api()
    if not proxmox:
        await interaction.followup.send("❌ Proxmox API Connection Error.", ephemeral=True)
        return

    vmid = random.randint(100, 999)
    vps_name = f"kvm-life-{target_user.id}-{vmid}"

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

        conn = get_db_connection()
        conn.execute('''
            INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at)
            VALUES (?, ?, ?, 'KVM', 'Custom KVM', 'stopped', ?, ?, ?, NULL)
        ''', (vmid, target_user.id, vps_name, ram, cpu, disk))
        conn.commit()
        conn.close()

        log_action(interaction.user.id, interaction.user, "ADMIN_GIVE_KVM_LIFETIME", f"VMID {vmid} assigned to {target_user}")

        embed = discord.Embed(
            title="🖥️ Legacy Cloud - Lifetime KVM Granted",
            description=f"**User:** {target_user.mention}\n**VMID:** {vmid}\n**Type:** Dedicated KVM\n**Duration:** Lifetime\n**Specs:** {cpu} vCPU | {ram}MB RAM | {disk}GB Disk",
            color=discord.Color.purple()
        )
        embed.set_footer(text=WATERMARK)
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Failed KVM Creation: {e}")
        await interaction.followup.send(f"❌ Failed to create KVM VPS: {e}", ephemeral=True)

@bot.tree.command(name="about", description="View information about the bot and developer credits")
async def about(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 Legacy Cloud VPS Bot",
        description="Proxmox VPS management bot supporting 2-Day member deployments and Lifetime Admin VPS grants.",
        color=discord.Color.blue()
    )
    embed.add_field(name="👨‍💻 Developer Credits", value="Developed by **devaru007 & Legacy Cloud**", inline=False)
    embed.set_footer(text=WATERMARK)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.event
async def on_ready():
    logger.info(f'Legacy Cloud Bot Online: {bot.user}')
    cleanup_expired_vps.start()
    try:
        await bot.tree.sync()
    except Exception as e:
        logger.error(f'Sync error: {e}')

if __name__ == "__main__":
    if not TOKEN:
        sys.exit(1)
        
    threading.Thread(target=run_web_dashboard, daemon=True).start()
    logger.info(f"Dashboard online locally at http://{DASHBOARD_HOST}:{DASHBOARD_PORT}")
    
    bot.run(TOKEN)
