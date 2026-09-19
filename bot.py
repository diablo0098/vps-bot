import random
import logging
import sys
import os
import sqlite3
import asyncio
import string
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from proxmoxer import ProxmoxAPI

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

# VPS Defaults
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
            tmate_url TEXT,
            pve_user TEXT,
            pve_pass TEXT
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
    
    # Check if pve_user and pve_pass columns exist in vps table for schema updates
    cursor.execute("PRAGMA table_info(vps)")
    columns = [col['name'] for col in cursor.fetchall()]
    if 'pve_user' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN pve_user TEXT")
    if 'pve_pass' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN pve_pass TEXT")
    if 'sshx_url' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN sshx_url TEXT")
    if 'tmate_url' not in columns:
        cursor.execute("ALTER TABLE vps ADD COLUMN tmate_url TEXT")
        
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

def generate_password(length=12):
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

def get_proxmox_api():
    if not PROXMOX_HOST or not PROXMOX_USER or not PROXMOX_PASSWORD:
        return None
    try:
        return ProxmoxAPI(PROXMOX_HOST, user=PROXMOX_USER, password=PROXMOX_PASSWORD, verify_ssl=PROXMOX_VERIFY_SSL)
    except Exception as e:
        logger.warning(f"Proxmox host unreachable. Error: {e}")
        return None

def create_pve_user_account(proxmox, vmid, discord_id):
    pve_user_id = f"vps{vmid}_usr@pve"
    pve_pass = generate_password()
    if proxmox:
        try:
            # Create Proxmox Realm User
            proxmox.access.users.create(userid=pve_user_id, password=pve_pass, comment=f"Discord User ID: {discord_id}")
            # Assign VMUser role permissions to the VM/Container
            proxmox.access.acl.put(path=f"/vms/{vmid}", roles="VMUser", users=pve_user_id)
        except Exception as e:
            logger.error(f"Error provisioning PVE account {pve_user_id}: {e}")
    return pve_user_id, pve_pass

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
            pve_usr = server['pve_user']
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
                    
                    if pve_usr:
                        try:
                            proxmox.access.users(pve_usr).delete()
                        except Exception as pve_err:
                            logger.error(f"Failed deleting PVE user {pve_usr}: {pve_err}")
                
                cursor.execute('INSERT INTO backups (vmid, user_id, backup_file) VALUES (?, ?, ?)',
                               (vmid, user_id, backup_file_name))
                cursor.execute('DELETE FROM vps WHERE vmid = ?', (vmid,))
                conn.commit()

                try:
                    user = await bot.fetch_user(user_id)
                    if user:
                        await user.send("⚠️ **Your VPS has expired and was removed.** Your data backup has been stored.")
                except Exception as dm_err:
                    logger.error(f"Failed to DM user {user_id}: {dm_err}")

                log_action(user_id, "SYSTEM", "EXPIRED_VPS_DELETED", f"VMID {vmid} backed up as {backup_file_name} and purged.")

            except Exception as e:
                logger.error(f"Failed to process expired VMID {vmid}: {e}")

    conn.close()

# Help Command
@bot.tree.command(name="help", description="Displays all available bot commands and instructions")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="⚡ Legacy Cloud - System Help Menu",
        description="Here is a complete list of commands available in the server.",
        color=discord.Color.blue()
    )
    
    embed.add_field(
        name="🚀 **VPS Deployment & Operations**",
        value=(
            "`/deploy <os>` - Deploy a 10-Day LXC instance (Costs 50 LC)\n"
            "`/myvps` - View all your active virtual instances and details\n"
            "`/start <vmid>` - Power on a stopped VPS\n"
            "`/stop <vmid>` - Gracefully shut down a running VPS\n"
            "`/reboot <vmid>` - Reboot a running VPS\n"
            "`/delete <vmid>` - Permanently delete one of your active VPS instances\n"
            "`/status <vmid>` - View current performance metrics of a specific VPS"
        ),
        inline=False
    )
    
    embed.add_field(
        name="💰 **Economy & Rewards**",
        value=(
            "`/balance` - Check your wallet balance (LC Coins)\n"
            "`/daily` - Claim daily LC Coins (24h cooldown)\n"
            "`/work` - Work to earn additional LC Coins (1h cooldown)"
        ),
        inline=False
    )

    if is_admin(interaction.user):
        embed.add_field(
            name="👑 **Admin Provisioning**",
            value=(
                "`/admin-give-vps` - Provision a custom lifetime LXC container\n"
                "`/admin-give-kvm` - Provision a custom lifetime KVM Virtual Machine"
            ),
            inline=False
        )

    embed.set_footer(text=WATERMARK)
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
            logger.warning(f"Proxmox creation warning: {e}")

    pve_user, pve_pass = create_pve_user_account(proxmox, vmid, user_id)

    try:
        cursor.execute('UPDATE users SET lc_balance = lc_balance - ? WHERE user_id = ?', (DEFAULT_VPS_COST, user_id))
        cursor.execute('''
            INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at, sshx_url, tmate_url, pve_user, pve_pass)
            VALUES (?, ?, ?, 'LXC', ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (vmid, user_id, vps_name, os_type, DEFAULT_RAM, DEFAULT_CPU, DEFAULT_DISK, expiration_time, sshx_link, tmate_link, pve_user, pve_pass))
        
        conn.commit()
        conn.close()

        log_action(user_id, interaction.user, "MEMBER_DEPLOY_10DAYS", f"VMID: {vmid}, PVE Account: {pve_user}")
        
        embed = discord.Embed(
            title="🚀 Legacy Cloud VPS Deployed",
            description=f"**VMID:** `{vmid}`\n**Specs:** 10GB RAM | 2 vCPU | 20GB Disk\n**Expires:** `{expiration_time} UTC`",
            color=discord.Color.green()
        )
        embed.add_field(name="🔐 Proxmox User", value=f"`{pve_user}`", inline=True)
        embed.add_field(name="🔑 Proxmox Password", value=f"`{pve_pass}`", inline=True)
        embed.add_field(name="🔗 sshx Console", value=f"[Open Session]({sshx_link})", inline=False)
        embed.add_field(name="🔗 tmate Console", value=f"[Open Session]({tmate_link})", inline=False)
        embed.set_footer(text=WATERMARK)
        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        logger.error(f"Failed Member VPS Deployment: {e}")
        await interaction.followup.send("❌ Deployment failed.", ephemeral=True)
        conn.close()

@bot.tree.command(name="myvps", description="List all your active VPS instances")
async def myvps(interaction: discord.Interaction):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE user_id = ?', (interaction.user.id,))
    user_vps = cursor.fetchall()
    conn.close()

    if not user_vps:
        await interaction.response.send_message("❌ You have no active VPS instances.", ephemeral=True)
        return

    embed = discord.Embed(title="📱 Your Active Instances", color=discord.Color.blue())
    for item in user_vps:
        duration = item['expires_at'] if item['expires_at'] else "Lifetime"
        pve_u = item['pve_user'] if item['pve_user'] else "N/A"
        pve_p = item['pve_pass'] if item['pve_pass'] else "N/A"
        embed.add_field(
            name=f"🖥️ {item['vps_name']} (VMID: {item['vmid']})",
            value=(
                f"**Type:** {item['type']} | **OS:** {item['os_type']}\n"
                f"**Status:** `{item['status']}` | **Duration:** `{duration}`\n"
                f"**Specs:** {item['cpu']} Cores / {item['ram']}MB RAM / {item['disk']}GB Disk\n"
                f"**PVE Login:** `{pve_u}` | **Pass:** `{pve_p}`"
            ),
            inline=False
        )
    embed.set_footer(text=WATERMARK)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="start", description="Start a stopped VPS")
async def start_vps(interaction: discord.Interaction, vmid: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE vmid = ? AND user_id = ?', (vmid, interaction.user.id))
    server = cursor.fetchone()

    if not server and not is_admin(interaction.user):
        await interaction.response.send_message("❌ Instance not found or unauthorized.", ephemeral=True)
        conn.close()
        return

    await interaction.response.defer(ephemeral=True)
    proxmox = get_proxmox_api()
    if proxmox:
        try:
            node = proxmox.nodes(PROXMOX_NODE)
            if server['type'] == 'KVM':
                node.qemu(vmid).status.start.post()
            else:
                node.lxc(vmid).status.start.post()
        except Exception as e:
            logger.error(f"Proxmox start error: {e}")

    cursor.execute("UPDATE vps SET status = 'running' WHERE vmid = ?", (vmid,))
    conn.commit()
    conn.close()

    await interaction.followup.send(f"▶️ Power signal sent to start VPS `{vmid}`.", ephemeral=True)

@bot.tree.command(name="stop", description="Stop a running VPS")
async def stop_vps(interaction: discord.Interaction, vmid: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE vmid = ? AND user_id = ?', (vmid, interaction.user.id))
    server = cursor.fetchone()

    if not server and not is_admin(interaction.user):
        await interaction.response.send_message("❌ Instance not found or unauthorized.", ephemeral=True)
        conn.close()
        return

    await interaction.response.defer(ephemeral=True)
    proxmox = get_proxmox_api()
    if proxmox:
        try:
            node = proxmox.nodes(PROXMOX_NODE)
            if server['type'] == 'KVM':
                node.qemu(vmid).status.stop.post()
            else:
                node.lxc(vmid).status.stop.post()
        except Exception as e:
            logger.error(f"Proxmox stop error: {e}")

    cursor.execute("UPDATE vps SET status = 'stopped' WHERE vmid = ?", (vmid,))
    conn.commit()
    conn.close()

    await interaction.followup.send(f"⏹️ Power signal sent to stop VPS `{vmid}`.", ephemeral=True)

@bot.tree.command(name="reboot", description="Reboot a running VPS")
async def reboot_vps(interaction: discord.Interaction, vmid: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE vmid = ? AND user_id = ?', (vmid, interaction.user.id))
    server = cursor.fetchone()

    if not server and not is_admin(interaction.user):
        await interaction.response.send_message("❌ Instance not found or unauthorized.", ephemeral=True)
        conn.close()
        return

    await interaction.response.defer(ephemeral=True)
    proxmox = get_proxmox_api()
    if proxmox:
        try:
            node = proxmox.nodes(PROXMOX_NODE)
            if server['type'] == 'KVM':
                node.qemu(vmid).status.reboot.post()
            else:
                node.lxc(vmid).status.reboot.post()
        except Exception as e:
            logger.error(f"Proxmox reboot error: {e}")

    conn.close()
    await interaction.followup.send(f"🔄 Reboot command issued for VPS `{vmid}`.", ephemeral=True)

@bot.tree.command(name="delete", description="Permanently delete a VPS instance")
async def delete_vps_cmd(interaction: discord.Interaction, vmid: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE vmid = ? AND user_id = ?', (vmid, interaction.user.id))
    server = cursor.fetchone()

    if not server and not is_admin(interaction.user):
        await interaction.response.send_message("❌ Instance not found or unauthorized.", ephemeral=True)
        conn.close()
        return

    await interaction.response.defer(ephemeral=True)
    proxmox = get_proxmox_api()
    if proxmox:
        try:
            node = proxmox.nodes(PROXMOX_NODE)
            if server['type'] == 'KVM':
                node.qemu(vmid).status.stop.post()
                node.qemu(vmid).delete()
            else:
                node.lxc(vmid).status.stop.post()
                node.lxc(vmid).delete()
            if server['pve_user']:
                try:
                    proxmox.access.users(server['pve_user']).delete()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Proxmox deletion error: {e}")

    cursor.execute('DELETE FROM vps WHERE vmid = ?', (vmid,))
    conn.commit()
    conn.close()

    log_action(interaction.user.id, interaction.user, "USER_DELETED_VPS", f"VMID {vmid}")
    await interaction.followup.send(f"🗑️ VPS `{vmid}` has been deleted.", ephemeral=True)

@bot.tree.command(name="status", description="Check execution status of a VPS")
async def status_vps(interaction: discord.Interaction, vmid: int):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM vps WHERE vmid = ? AND user_id = ?', (vmid, interaction.user.id))
    server = cursor.fetchone()
    conn.close()

    if not server and not is_admin(interaction.user):
        await interaction.response.send_message("❌ Instance not found or unauthorized.", ephemeral=True)
        return

    pve_status = "Unknown / Mock Mode"
    proxmox = get_proxmox_api()
    if proxmox:
        try:
            node = proxmox.nodes(PROXMOX_NODE)
            if server['type'] == 'KVM':
                pve_status = node.qemu(vmid).status.current.get().get('status', 'unknown')
            else:
                pve_status = node.lxc(vmid).status.current.get().get('status', 'unknown')
        except Exception as e:
            pve_status = f"Unreachable ({e})"

    embed = discord.Embed(
        title=f"📊 Status Report for VMID {vmid}",
        color=discord.Color.purple()
    )
    embed.add_field(name="Name", value=server['vps_name'], inline=True)
    embed.add_field(name="Type", value=server['type'], inline=True)
    embed.add_field(name="DB Record Status", value=f"`{server['status']}`", inline=True)
    embed.add_field(name="Live Proxmox Node Status", value=f"`{pve_status}`", inline=False)
    embed.set_footer(text=WATERMARK)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# Admin Commands

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

    pve_user, pve_pass = create_pve_user_account(proxmox, vmid, target_user.id)

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at, sshx_url, tmate_url, pve_user, pve_pass)
        VALUES (?, ?, ?, 'LXC', ?, 'running', ?, ?, ?, NULL, ?, ?, ?, ?)
    ''', (vmid, target_user.id, vps_name, os_type, ram, cpu, disk, sshx_link, tmate_link, pve_user, pve_pass))
    conn.commit()
    conn.close()

    log_action(interaction.user.id, interaction.user, "ADMIN_GIVE_LXC_CUSTOM", f"VMID {vmid} assigned to {target_user}")

    embed = discord.Embed(
        title="🎁 Legacy Cloud - Custom Lifetime LXC Granted",
        description=f"**User:** {target_user.mention}\n**VMID:** `{vmid}`\n**Specs:** {cpu} Core | {ram}MB RAM | {disk}GB Disk\n**Duration:** Lifetime",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔐 Proxmox User", value=f"`{pve_user}`", inline=True)
    embed.add_field(name="🔑 Proxmox Password", value=f"`{pve_pass}`", inline=True)
    embed.add_field(name="🔗 sshx Console", value=f"[Open Session]({sshx_link})", inline=False)
    embed.add_field(name="🔗 tmate Console", value=f"[Open Session]({tmate_link})", inline=False)
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

    pve_user, pve_pass = create_pve_user_account(proxmox, vmid, target_user.id)

    conn = get_db_connection()
    conn.execute('''
        INSERT INTO vps (vmid, user_id, vps_name, type, os_type, status, ram, cpu, disk, expires_at, pve_user, pve_pass)
        VALUES (?, ?, ?, 'KVM', 'Custom KVM', 'running', ?, ?, ?, NULL, ?, ?)
    ''', (vmid, target_user.id, vps_name, ram, cpu, disk, pve_user, pve_pass))
    conn.commit()
    conn.close()

    log_action(interaction.user.id, interaction.user, "ADMIN_GIVE_KVM_CUSTOM", f"VMID {vmid} assigned to {target_user}")

    embed = discord.Embed(
        title="🖥️ Legacy Cloud - Custom Lifetime KVM Granted",
        description=f"**User:** {target_user.mention}\n**VMID:** `{vmid}`\n**Type:** Dedicated KVM\n**Specs:** {cpu} vCPU | {ram}MB RAM | {disk}GB Disk\n**Duration:** Lifetime",
        color=discord.Color.purple()
    )
    embed.add_field(name="🔐 Proxmox User", value=f"`{pve_user}`", inline=True)
    embed.add_field(name="🔑 Proxmox Password", value=f"`{pve_pass}`", inline=True)
    embed.set_footer(text=WATERMARK)
    await interaction.followup.send(embed=embed, ephemeral=True)

# Economy System

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

# Startup Lifecycle
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
    if TOKEN:
        bot.run(TOKEN)
    else:
        logger.critical("Error: No bot TOKEN provided in .env parameters.")
