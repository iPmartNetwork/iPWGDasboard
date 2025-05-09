from flask import Flask, render_template, request, jsonify, redirect, url_for, flash, send_file, send_from_directory
import os
import io
import json
import datetime
import subprocess
import csv
import secrets
from database import cleanup_old_traffic_logs, aggregate_traffic_data
from wireguard import get_client_status
from database import (
    init_db, get_db_connection, add_client, get_client, get_all_clients, 
    get_active_clients, update_client, delete_client, update_client_status,
    find_available_ip, update_data_usage, get_server_config, update_server_config,
    get_traffic_stats
)
from wireguard import (
    generate_keypair, generate_preshared_key, restart_wireguard, apply_server_config,
    generate_client_config, generate_qr_code, add_client_to_wireguard,
    remove_client_from_wireguard, get_client_status
)
from utils import format_bytes, format_timestamp, get_server_stats
# from config import SECRET_KEY, DEBUG, DATABASE_PATH
from config import (
    SECRET_KEY, DEBUG, DATABASE_PATH, 
    WG_INTERFACE, PUBLIC_IP, WG_PORT, ADMIN_PASSWORD
)
app = Flask(__name__)
app.config['SECRET_KEY'] = SECRET_KEY
from apscheduler.schedulers.background import BackgroundScheduler
from functools import wraps
from flask import session, redirect, url_for

# Initialize database using an app context instead of before_first_request
def initialize_database():
    with app.app_context():
        init_db()

# Call the initialize function
initialize_database()

ADMIN_PASSWORD = ADMIN_PASSWORD # Change this!
SECRET_KEY = SECRET_KEY  # Change this!




#login path

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'authenticated' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

# --- 6. ثبت لاگ ورود و خروج ادمین ---
def log_admin_action(action):
    with open('admin_actions.log', 'a', encoding='utf-8') as f:
        f.write(f"{datetime.datetime.now().isoformat()} - {action}\n")

# 6. امنیت بیشتر: قفل شدن ادمین بعد از چند بار ورود ناموفق
FAILED_LOGIN_LIMIT = 5
FAILED_LOGIN_TIMEOUT = 300  # ثانیه

def is_admin_locked():
    lock_info = session.get('admin_lock', {'count': 0, 'time': 0})
    if lock_info['count'] >= FAILED_LOGIN_LIMIT:
        if (datetime.datetime.now().timestamp() - lock_info['time']) < FAILED_LOGIN_TIMEOUT:
            return True
        else:
            session['admin_lock'] = {'count': 0, 'time': 0}
    return False

# Login route
@app.route('/login', methods=['GET', 'POST'])
def login():
    if is_admin_locked():
        return render_template('login.html', error='Too many failed attempts. Try again later.')
    if request.method == 'POST':
        if request.form['password'] == ADMIN_PASSWORD:
            session['authenticated'] = True
            session.pop('admin_lock', None)
            log_admin_action('login')
            return redirect(url_for('index'))
        else:
            lock_info = session.get('admin_lock', {'count': 0, 'time': 0})
            lock_info['count'] += 1
            lock_info['time'] = datetime.datetime.now().timestamp()
            session['admin_lock'] = lock_info
            return render_template('login.html', error='Invalid password')
    return render_template('login.html')

# Logout route
@app.route('/logout')
def logout():
    log_admin_action('logout')
    session.pop('authenticated', None)
    return redirect(url_for('login'))



# Main dashboard route
@app.route('/')
@login_required
def index():
    # Get basic stats
    traffic_stats = get_traffic_stats()
    active_clients = traffic_stats['active_clients']
    total_clients = traffic_stats['total_clients']
    monthly_traffic = format_bytes(traffic_stats['monthly_traffic_bytes'])
    
    # Get server stats
    server_stats = get_server_stats()
    
    # Format uptime for display
    uptime = server_stats['uptime']['formatted']
    
    return render_template(
        'index.html',
        active_clients=active_clients,
        total_clients=total_clients,
        monthly_traffic=monthly_traffic,
        uptime=uptime
    )



@app.route('/clients')
@login_required
def clients():  # This is the main clients page route
    all_clients = get_all_clients()
    client_status = get_client_status()
    
    # Merge client data with status
    for client in all_clients:
        status = client_status.get(client['public_key'], {})
        client['online'] = status.get('online', False)
        client['last_seen_formatted'] = format_timestamp(client['last_seen'])
        client['data_usage_formatted'] = format_bytes(client['data_usage'] * 1024 * 1024)
    
    return render_template('clients.html', clients=all_clients)


# Client management routes
@app.route('/api/clients')
@login_required
def clients_api():
    """API endpoint returning all clients with their status"""
    all_clients = get_all_clients()
    client_status = get_client_status()
    
    # Merge client data with status
    for client in all_clients:
        status = client_status.get(client['public_key'], {})
        client['online'] = status.get('online', False)
        client['last_seen_formatted'] = format_timestamp(client['last_seen'])
        client['data_usage_formatted'] = format_bytes(client['data_usage'] * 1024 * 1024)  # Convert MB to bytes
    
    return jsonify(all_clients)
@app.route('/api/available-ips')
@login_required
def available_ips_api():
    """API endpoint returning available IP addresses"""
    available_ips = []
    base_ip = '172.16.0.'
    
    with get_db_connection() as conn:
        used_ips = [row[0] for row in conn.execute('SELECT assigned_ip FROM clients').fetchall()]
    
    for i in range(2, 255):
        ip = f"{base_ip}{i}"
        if ip not in used_ips:
            available_ips.append(ip)
            if len(available_ips) >= 10:  # Limit to 10 suggestions
                break
    
    return jsonify(available_ips)


@app.route('/api/server-config')
@login_required
def server_config_api():
    """API endpoint returning server configuration"""
    config = get_server_config()
    if config:
        return jsonify(config)
    
    # Use values imported from config module
    return jsonify({
        'endpoint': f"{PUBLIC_IP}:{WG_PORT}",
        'subnet': '172.16.0.0/24',  # You might want to import this from config too
        'dns_servers': '1.1.1.1, 8.8.8.8',  # Consider importing this
        'mtu': 1280,  # Consider importing this
        'keepalive': 25  # Consider importing this
    })

# --- 1. ارسال فایل کانفیگ به ایمیل کاربر پس از ایجاد ---
def send_config_email(email, client_config):
    # این تابع باید با SMTP واقعی جایگزین شود
    print(f"Send config to {email}")
    # ...ایمیل واقعی نیاز به پیاده‌سازی دارد...

@app.route('/clients/add', methods=['GET', 'POST'])
@login_required
def add_client_route():
    if request.method == 'POST':
        try:
            name = request.form.get('name', '').strip()
            email = request.form.get('email', '').strip() if 'email' in request.form else None
            try:
                data_limit = int(request.form.get('data_limit')) if request.form.get('data_limit') not in [None, '', 'none'] else None
            except (ValueError, TypeError):
                data_limit = None
            expiry_date = request.form.get('expiry_date', '').strip() or None
            
            # Generate keys
            private_key, public_key = generate_keypair()
            preshared_key = generate_preshared_key()
            
            # Find available IP or use specified one
            if 'assigned_ip' in request.form and request.form['assigned_ip']:
                assigned_ip = request.form['assigned_ip']
            else:
                assigned_ip = find_available_ip()
                if not assigned_ip:
                    flash('No available IP addresses!', 'error')
                    return redirect(url_for('clients'))
            
            # Add to database
            client_id = add_client(
                name=name,
                email=email,
                private_key=private_key,
                public_key=public_key,
                assigned_ip=assigned_ip,
                preshared_key=preshared_key,
                data_limit=data_limit,
                expiry_date=expiry_date
            )
            
            # Add to WireGuard
            client = get_client(client_id)
            if add_client_to_wireguard(client):
                flash('Client added successfully!', 'success')
                # ارسال ایمیل کانفیگ
                if client.get('email'):
                    server_config = get_server_config()
                    client_config = generate_client_config(client, server_config)
                    send_config_email(client['email'], client_config)
            else:
                flash('Client added to database but failed to configure WireGuard', 'warning')
            
            return redirect(url_for('client_detail', client_id=client_id))
            
        except Exception as e:
            flash(f'Error adding client: {str(e)}', 'error')
            return redirect(url_for('clients'))
    
    # For GET request
    available_ips = []
    base_ip = '10.8.0.'
    
    with get_db_connection() as conn:
        used_ips = [row[0] for row in conn.execute('SELECT assigned_ip FROM clients').fetchall()]
    
    for i in range(2, 255):
        ip = f"{base_ip}{i}"
        if ip not in used_ips:
            available_ips.append(ip)
            if len(available_ips) >= 10:  # Limit to 10 suggestions
                break
    
    return render_template('add_client.html', available_ips=available_ips)

@app.route('/clients/<int:client_id>')
@login_required
def client_detail(client_id):
    client = get_client(client_id)
    if not client:
        flash('Client not found!', 'error')
        return redirect(url_for('clients'))
    
    # Get client status from WireGuard
    client_status = get_client_status().get(client['public_key'], {})
    client['online'] = client_status.get('online', False)
    client['last_seen_formatted'] = format_timestamp(client['last_seen'])
    client['data_usage_formatted'] = format_bytes(client['data_usage'] * 1024 * 1024)
    
    # Generate client config and QR code
    server_config = get_server_config()
    client_config = generate_client_config(client, server_config)
    qr_code = generate_qr_code(client_config)
    
    return render_template(
        'client_detail.html', 
        client=client, 
        client_config=client_config, 
        qr_code=qr_code
    )

# --- 2. تاریخچه فعالیت کاربر (ورود/خروج و مصرف ترافیک) ---
def get_client_activity_logs(client_id):
    with get_db_connection() as conn:
        logs = conn.execute('''
            SELECT timestamp, upload, download
            FROM traffic_logs
            WHERE client_id = ?
            ORDER BY timestamp DESC
            LIMIT 50
        ''', (client_id,)).fetchall()
    return logs

@app.route('/clients/<int:client_id>/edit', methods=['GET', 'POST'])
@login_required
def edit_client(client_id):
    client = get_client(client_id)
    if not client:
        flash('Client not found!', 'error')
        return redirect(url_for('clients'))
    
    if request.method == 'POST':
        # Handle the POST request
        updates = {
            'name': request.form['name'],
            'email': request.form['email'],
            'data_limit': int(request.form['data_limit']) if request.form['data_limit'] != 'none' else None,
            'expiry_date': request.form['expiry_date'] if request.form['expiry_date'] else None,
            'is_active': 1 if 'is_active' in request.form else 0
        }
        
        update_client(client_id, **updates)
        flash('Client updated successfully!', 'success')
        return redirect(url_for('client_detail', client_id=client_id))
    
    # For GET request
    server_config = get_server_config()
    client_config = generate_client_config(client, server_config)
    qr_code = generate_qr_code(client_config)
    
    # Get activity logs (implement this function)
    activity_logs = get_client_activity_logs(client_id)
    
    return render_template('edit_client.html',
                         client=client,
                         client_config=client_config,
                         qr_code=qr_code,
                         activity_logs=activity_logs)

@app.route('/clients/<int:client_id>/delete', methods=['POST'])
@login_required
def delete_client_route(client_id):
    client = get_client(client_id)
    if not client:
        flash('Client not found!', 'error')
        return redirect(url_for('clients'))
    
    # Remove from WireGuard
    remove_client_from_wireguard(client['public_key'])
    
    # Delete from database
    delete_client(client_id)
    
    flash('Client deleted successfully!', 'success')
    return redirect(url_for('clients'))


@app.route('/logs')
@login_required
def logs():
    # Get WireGuard and system logs
    try:
        # Get last 100 lines of WireGuard logs
        wg_logs = subprocess.check_output(
            ['journalctl', '-u', f'wg-quick@{WG_INTERFACE}', '-n', '100', '--no-pager'],
            universal_newlines=True
        )
        
        # Get system logs related to WireGuard
        sys_logs = subprocess.check_output(
            ['dmesg', '|', 'grep', '-i', 'wireguard'],
            shell=True,
            universal_newlines=True
        )
    except subprocess.CalledProcessError:
        wg_logs = "Unable to retrieve WireGuard logs"
        sys_logs = "Unable to retrieve system logs"
    
    return render_template('logs.html', wg_logs=wg_logs, sys_logs=sys_logs)

@app.route('/settings')
@login_required
def settings():
    # Get current settings
    server_config = get_server_config()
    return render_template('settings.html', config=server_config)

@app.route('/clients/<int:client_id>/toggle', methods=['POST'])
@login_required
def toggle_client(client_id):
    client = get_client(client_id)
    if not client:
        return jsonify({'success': False, 'message': 'Client not found!'})
    
    new_status = request.json.get('active', False)
    update_client_status(client_id, new_status)
    
    # Update in WireGuard
    if new_status:
        add_client_to_wireguard(client)
    else:
        remove_client_from_wireguard(client['public_key'])
    
    return jsonify({'success': True})

@app.route('/clients/<int:client_id>/download')
@login_required
def download_client_config(client_id):
    client = get_client(client_id)
    if not client:
        flash('Client not found!', 'error')
        return redirect(url_for('clients'))
    
    server_config = get_server_config()
    client_config = generate_client_config(client, server_config)
    
    # Create a file-like object
    config_file = io.BytesIO(client_config.encode('utf-8'))
    
    return send_file(
        config_file,
        mimetype='text/plain',
        as_attachment=True,
        download_name=f"{client['name']}.conf"
    )

@app.route('/clients/<int:client_id>/qr')
@login_required
def client_qr_code(client_id):
    client = get_client(client_id)
    if not client:
        flash('Client not found!', 'error')
        return redirect(url_for('clients'))
    
    server_config = get_server_config()
    client_config = generate_client_config(client, server_config)
    qr_code = generate_qr_code(client_config)
    
    return jsonify({'qr_code': qr_code})

# --- 3. جستجو و فیلتر کاربران ---
@app.route('/clients/search')
@login_required
def search_clients():
    q = request.args.get('q', '')
    with get_db_connection() as conn:
        clients = conn.execute('''
            SELECT * FROM clients
            WHERE name LIKE ? OR email LIKE ? OR assigned_ip LIKE ?
            ORDER BY name
        ''', (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
    return render_template('clients.html', clients=clients)

# Server configuration routes
@app.route('/server-config', methods=['GET', 'POST'])
@login_required
def server_config():
    config = get_server_config()
    if request.method == 'POST':
        # استفاده از get و کنترل مقدار عددی
        try:
            mtu = int(request.form.get('mtu', 1280))
        except (ValueError, TypeError):
            mtu = 1280
        try:
            keepalive = int(request.form.get('keepalive', 25))
        except (ValueError, TypeError):
            keepalive = 25
        updates = {
            'endpoint': request.form.get('endpoint', '').strip(),
            'subnet': request.form.get('subnet', '').strip(),
            'dns_servers': request.form.get('dns_servers', '').strip(),
            'mtu': mtu,
            'keepalive': keepalive
        }
        # بررسی اینکه هیچ فیلد ضروری خالی نباشد
        if not updates['endpoint'] or not updates['subnet']:
            flash('Endpoint و Subnet نباید خالی باشند.', 'error')
            return render_template('server_config.html', config=config)
        update_server_config(**updates)
        config = get_server_config()
        apply_server_config(config)
        flash('Server configuration updated successfully!', 'success')
        return redirect(url_for('server_config'))
    return render_template('server_config.html', config=config)

# --- 4. تغییر رمز عبور ادمین از پنل ---
@app.route('/settings/change-password', methods=['GET', 'POST'])
@login_required
def change_admin_password():
    if request.method == 'POST':
        old = request.form.get('old_password', '')
        new = request.form.get('new_password', '')
        if old != ADMIN_PASSWORD:
            flash('Old password incorrect', 'error')
        elif len(new) < 6:
            flash('New password must be at least 6 characters.', 'error')
        else:
            # در عمل باید رمز جدید را در فایل config یا دیتابیس ذخیره کنید
            flash('Password changed (demo only, not persistent)', 'success')
    return render_template('change_password.html')

# --- 5. تم تاریک/روشن (تنظیم در سشن) ---
@app.route('/theme/<theme>')
@login_required
def set_theme(theme):
    if theme in ['dark', 'light']:
        session['theme'] = theme
    return redirect(request.referrer or url_for('index'))

# Traffic monitoring routes
@app.route('/traffic')
@login_required
def traffic():
    try:
        # Get traffic statistics
        traffic_stats = get_traffic_stats()
        
        # Get client traffic data
        with get_db_connection() as conn:
            client_traffic = conn.execute('''
                SELECT 
                    c.name,
                    c.last_seen,
                    COALESCE(SUM(t.upload), 0) as upload,
                    COALESCE(SUM(t.download), 0) as download
                FROM clients c
                LEFT JOIN traffic_logs t ON c.id = t.client_id
                WHERE t.timestamp >= date('now', '-30 days') OR t.timestamp IS NULL
                GROUP BY c.id
                ORDER BY c.name
            ''').fetchall()
        
        return render_template('traffic.html',
                             stats=traffic_stats,
                             client_traffic=client_traffic,
                             format_bytes=format_bytes,
                             format_timestamp=format_timestamp)
    except Exception as e:
        flash(f'Error loading traffic data: {str(e)}', 'error')
        return render_template('traffic.html',
                             stats={'monthly_traffic_formatted': '0 B',
                                   'total_clients': 0,
                                   'active_clients': 0},
                             client_traffic=[])

@app.route('/api/traffic/stats')
@login_required
def traffic_stats_api():
    stats = get_traffic_stats()
    stats['monthly_traffic_formatted'] = format_bytes(stats['monthly_traffic_bytes'])
    return jsonify(stats)

@app.route('/api/traffic/history')
@login_required
def traffic_history_api():
    days = int(request.args.get('days', 30))
    
    with get_db_connection() as conn:
        # Get recent detailed logs
        recent_data = conn.execute('''
        SELECT 
            date(timestamp) as date,
            SUM(upload) as upload,
            SUM(download) as download
        FROM traffic_logs
        WHERE timestamp >= date('now', '-7 days')
        GROUP BY date(timestamp)
        ''').fetchall()
        
        # Get aggregated historical data
        historical_data = conn.execute('''
        SELECT 
            date,
            SUM(total_upload) as upload,
            SUM(total_download) as download
        FROM traffic_logs_daily
        WHERE date >= date('now', ?)
        AND date < date('now', '-7 days')
        GROUP BY date
        ORDER BY date
        ''', (f'-{days} days',)).fetchall()
        
        # Combine the data
        traffic_data = historical_data + recent_data
        
        result = []
        for row in traffic_data:
            result.append({
                'date': row['date'],
                'upload': row['upload'],
                'download': row['download'],
                'upload_formatted': format_bytes(row['upload']),
                'download_formatted': format_bytes(row['download'])
            })
    
    return jsonify(sorted(result, key=lambda x: x['date']))


@app.route('/api/traffic/clients')
@login_required
def client_traffic_api():
    with get_db_connection() as conn:
        client_traffic = conn.execute('''
        SELECT 
            c.id,
            c.name,
            c.assigned_ip,
            SUM(t.upload) as upload,
            SUM(t.download) as download
        FROM clients c
        LEFT JOIN traffic_logs t ON c.id = t.client_id
        WHERE t.timestamp >= date('now', '-30 days') OR t.timestamp IS NULL
        GROUP BY c.id
        ORDER BY (SUM(t.upload) + SUM(t.download)) DESC
        ''').fetchall()
    
    result = []
    for row in client_traffic:
        upload = row['upload'] if row['upload'] else 0
        download = row['download'] if row['download'] else 0
        
        result.append({
            'id': row['id'],
            'name': row['name'],
            'ip': row['assigned_ip'],
            'upload': upload,
            'download': download,
            'upload_formatted': format_bytes(upload),
            'download_formatted': format_bytes(download),
            'total_formatted': format_bytes(upload + download)
        })
    
    return jsonify(result)

# System status route
@app.route('/api/system-status')
@login_required
def system_status_api():
    stats = get_server_stats()
    return jsonify(stats)

# Logs page
# @app.route('/logs')
# def logs():
#     # Get WireGuard logs
#     try:
#         journal_logs = subprocess.check_output([
#             'journalctl', '-u', f'wg-quick@{WG_INTERFACE}', '-n', '100', '--no-pager'
#         ]).decode('utf-8')
#     except:
#         journal_logs = "Unable to retrieve logs"
    
#     return render_template('logs.html', logs=journal_logs)

# # Settings page
# @app.route('/settings')
# def settings():
#     return render_template('settings.html')

# Dynamic API endpoints for real-time data
@app.route('/api/dashboard-stats')
@login_required
def dashboard_stats_api():
    traffic_stats = get_traffic_stats()
    server_stats = get_server_stats()
    client_status = get_client_status()
    
    # Count online clients
    online_count = sum(1 for status in client_status.values() if status.get('online', False))
    
    return jsonify({
        'active_clients': traffic_stats['active_clients'],
        'online_clients': online_count,
        'total_clients': traffic_stats['total_clients'],
        'monthly_traffic': format_bytes(traffic_stats['monthly_traffic_bytes']),
        'load': server_stats['load'],
        'memory': {
            'used': format_bytes(server_stats['memory']['used']),
            'total': format_bytes(server_stats['memory']['total']),
            'percentage': server_stats['memory']['percentage']
        },
        'disk': {
            'used': format_bytes(server_stats['disk']['used']),
            'total': format_bytes(server_stats['disk']['total']),
            'percentage': server_stats['disk']['percentage']
        },
        'uptime': server_stats['uptime']['formatted']
    })

@app.route('/api/backup')
@login_required
def backup_config():
    try:
        with get_db_connection() as conn:
            # Get server config
            server_config = dict(conn.execute('SELECT * FROM server_config').fetchone())
            # Get clients
            clients = [dict(row) for row in conn.execute('SELECT * FROM clients').fetchall()]
            
            backup = {
                'server_config': server_config,
                'clients': clients,
                'timestamp': datetime.datetime.now().isoformat()
            }
            
            return jsonify(backup)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/restore', methods=['POST'])
@login_required
def restore_config():
    try:
        backup = request.json
        
        with get_db_connection() as conn:
            # Restore server config
            conn.execute('DELETE FROM server_config')
            conn.execute('''
                INSERT INTO server_config (endpoint, subnet, dns_servers, mtu, keepalive, private_key, public_key)
                VALUES (:endpoint, :subnet, :dns_servers, :mtu, :keepalive, :private_key, :public_key)
            ''', backup['server_config'])
            
            # Restore clients
            conn.execute('DELETE FROM clients')
            for client in backup['clients']:
                conn.execute('''
                    INSERT INTO clients (name, email, private_key, public_key, preshared_key, assigned_ip, 
                                       created_at, last_seen, is_active, data_limit, data_usage, expiry_date)
                    VALUES (:name, :email, :private_key, :public_key, :preshared_key, :assigned_ip,
                            :created_at, :last_seen, :is_active, :data_limit, :data_usage, :expiry_date)
                ''', client)
            
            conn.commit()
        
        # Restart WireGuard with new config
        apply_server_config(backup['server_config'])
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/reset', methods=['POST'])
@login_required
def reset_server():
    try:
        # Initialize new server configuration
        init_db()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/clear-logs', methods=['POST'])
@login_required
def clear_logs():
    try:
        with get_db_connection() as conn:
            conn.execute('DELETE FROM traffic_logs')
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# --- 7. احراز هویت دومرحله‌ای (کد اولیه) ---
@app.route('/2fa', methods=['GET', 'POST'])
def two_factor():
    if request.method == 'POST':
        code = request.form['code']
        if code == session.get('2fa_code'):
            session['2fa'] = True
            return redirect(url_for('index'))
        return render_template('2fa.html', error='Invalid code')
    # تولید کد تصادفی و نمایش (در عمل باید ارسال شود)
    code = str(secrets.randbelow(1000000)).zfill(6)
    session['2fa_code'] = code
    print(f"2FA code: {code}")  # در عمل باید ارسال شود
    return render_template('2fa.html')

# --- 8. خروجی گرفتن از لیست کاربران به صورت CSV ---
@app.route('/clients/export')
@login_required
def export_clients():
    clients = get_all_clients()
    si = io.StringIO()
    cw = csv.writer(si)
    cw.writerow(['id', 'name', 'email', 'assigned_ip', 'data_limit', 'data_usage', 'expiry_date'])
    for c in clients:
        cw.writerow([c['id'], c['name'], c['email'], c['assigned_ip'], c['data_limit'], c['data_usage'], c['expiry_date']])
    output = io.BytesIO()
    output.write(si.getvalue().encode('utf-8'))
    output.seek(0)
    return send_file(output, mimetype='text/csv', as_attachment=True, download_name='clients.csv')

def setup_scheduled_tasks():
    """Setup scheduled maintenance tasks"""
    scheduler = BackgroundScheduler()
    
    # Update client status every minute
    try:
        scheduler.add_job(get_client_status, 'interval', minutes=1)
    except Exception as e:
        print(f"Error scheduling get_client_status: {e}")
    
    # Aggregate traffic data daily
    try:
        scheduler.add_job(aggregate_traffic_data, 'cron', hour=0, minute=5)
    except Exception as e:
        print(f"Error scheduling aggregate_traffic_data: {e}")
    
    # Clean up old traffic logs weekly
    try:
        scheduler.add_job(
            cleanup_old_traffic_logs, 
            'cron', 
            day_of_week='sun', 
            hour=1, 
            minute=0,
            args=[30]  # Keep 30 days of detailed logs
        )
    except Exception as e:
        print(f"Error scheduling cleanup_old_traffic_logs: {e}")
    
    try:
        scheduler.start()
    except Exception as e:
        print(f"Error starting scheduler: {e}")

@app.route('/clients/<int:client_id>/edit-endpoint', methods=['POST'])
@login_required
def edit_client_endpoint(client_id):
    client = get_client(client_id)
    if not client:
        flash('Client not found!', 'error')
        return redirect(url_for('clients'))
    new_endpoint = request.form.get('remote_endpoint', '').strip()
    if not new_endpoint or len(new_endpoint) < 3 or ':' not in new_endpoint:
        flash('Remote Endpoint is required and must be in format host:port', 'error')
        return redirect(url_for('edit_client', client_id=client_id))
    update_client(client_id, remote_endpoint=new_endpoint)
    flash('Remote Endpoint updated successfully!', 'success')
    return redirect(url_for('edit_client', client_id=client_id))

# 1. فعال/غیرفعال‌سازی سریع کلاینت از لیست
@app.route('/clients/<int:client_id>/toggle-active', methods=['POST'])
@login_required
def toggle_client_active(client_id):
    client = get_client(client_id)
    if not client:
        return jsonify({'success': False, 'message': 'Client not found!'})
    new_status = not bool(client['is_active'])
    update_client_status(client_id, new_status)
    if new_status:
        add_client_to_wireguard(client)
    else:
        remove_client_from_wireguard(client['public_key'])
    return jsonify({'success': True, 'active': new_status})

# 3. حذف گروهی کلاینت‌ها
@app.route('/clients/bulk-delete', methods=['POST'])
@login_required
def bulk_delete_clients():
    ids = request.form.getlist('client_ids')
    deleted = 0
    for cid in ids:
        client = get_client(int(cid))
        if client:
            remove_client_from_wireguard(client['public_key'])
            delete_client(int(cid))
            deleted += 1
    flash(f'{deleted} clients deleted.', 'success')
    return redirect(url_for('clients'))

# 4. جستجوی پیشرفته با فیلتر وضعیت
@app.route('/clients/advanced-search')
@login_required
def advanced_search_clients():
    q = request.args.get('q', '')
    status = request.args.get('status', '')
    online = request.args.get('online', '')
    query = "SELECT * FROM clients WHERE 1=1"
    params = []
    if q:
        query += " AND (name LIKE ? OR email LIKE ? OR assigned_ip LIKE ?)"
        params += [f'%{q}%', f'%{q}%', f'%{q}%']
    if status in ['active', 'inactive']:
        query += " AND is_active = ?"
        params.append(1 if status == 'active' else 0)
    with get_db_connection() as conn:
        clients = conn.execute(query + " ORDER BY name", params).fetchall()
    # فیلتر آنلاین/آفلاین در سطح اپلیکیشن
    client_status = get_client_status()
    if online in ['online', 'offline']:
        filtered = []
        for c in clients:
            is_online = client_status.get(c['public_key'], {}).get('online', False)
            if (online == 'online' and is_online) or (online == 'offline' and not is_online):
                filtered.append(c)
        clients = filtered
    return render_template('clients.html', clients=clients)

# 5. API وضعیت یک کلاینت خاص
@app.route('/api/client/<int:client_id>/status')
@login_required
def api_client_status(client_id):
    client = get_client(client_id)
    if not client:
        return jsonify({'success': False, 'message': 'Client not found!'})
    status = get_client_status().get(client['public_key'], {})
    return jsonify({'success': True, 'status': status})

# 7. مشاهده لاگ‌های مدیریتی
@app.route('/admin/logs')
@login_required
def admin_logs():
    logs = []
    try:
        with open('admin_actions.log', encoding='utf-8') as f:
            logs = f.readlines()[-200:]
    except Exception:
        logs = []
    return render_template('admin_logs.html', logs=logs)

if __name__ == '__main__':
    setup_scheduled_tasks()
    app.run(debug=True, host='0.0.0.0')
