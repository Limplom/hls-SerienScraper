#!/usr/bin/env python3
"""
HLS Downloader - Production Web GUI with Multi-User Support
- User Authentication
- Rate Limiting
- Celery Background Tasks
- Redis Session Management
- PostgreSQL Database
- Browser Pool Management
"""

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import os
import asyncio
import uuid
from datetime import datetime
from pathlib import Path

# Import configuration and models
from app.config import config
from app.models import db, User, Download, SystemMetrics
from app.tasks import create_celery

# Get the project root directory (parent of 'app' folder)
project_root = Path(__file__).parent.parent

# Create Flask app with correct template and static folders
app = Flask(__name__,
            template_folder=str(project_root / 'templates'),
            static_folder=str(project_root / 'static'))

# Load configuration
env = os.getenv('FLASK_ENV', 'development')
app.config.from_object(config[env])

# Initialize extensions
db.init_app(app)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='eventlet',
    message_queue=app.config['REDIS_URL']
)

# Initialize Celery
celery = create_celery(app)

# Initialize Login Manager
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# Initialize Rate Limiter
limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    storage_uri=app.config['RATELIMIT_STORAGE_URL']
)


@login_manager.user_loader
def load_user(user_id):
    """Load user by ID"""
    return User.query.get(int(user_id))


# ============================================================
# AUTHENTICATION ROUTES
# ============================================================

@app.route('/login', methods=['GET', 'POST'])
def login():
    """Login page"""
    if request.method == 'POST':
        data = request.json
        username = data.get('username')
        password = data.get('password')

        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            login_user(user)
            user.last_login = datetime.utcnow()
            db.session.commit()
            return jsonify({'success': True, 'redirect': '/'})

        return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

    # For now, return simple response (you can create a login.html template)
    return jsonify({'message': 'Login page - implement frontend'})


@app.route('/logout')
@login_required
def logout():
    """Logout current user"""
    logout_user()
    return redirect(url_for('login'))


@app.route('/register', methods=['POST'])
@limiter.limit("5 per hour")
def register():
    """Register new user"""
    data = request.json
    username = data.get('username')
    email = data.get('email')
    password = data.get('password')

    if not username or not email or not password:
        return jsonify({'error': 'Missing required fields'}), 400

    # Check if user exists
    if User.query.filter_by(username=username).first():
        return jsonify({'error': 'Username already exists'}), 400

    if User.query.filter_by(email=email).first():
        return jsonify({'error': 'Email already exists'}), 400

    # Create new user
    user = User(username=username, email=email)
    user.set_password(password)

    db.session.add(user)
    db.session.commit()

    return jsonify({'success': True, 'message': 'User registered successfully'})


# ============================================================
# MAIN ROUTES
# ============================================================

@app.route('/')
@login_required
def index():
    """Main page"""
    return render_template('index.html', user=current_user)


@app.route('/api/start', methods=['POST'])
@login_required
@limiter.limit(f"{app.config['MAX_DOWNLOADS_PER_HOUR']} per hour")
def start_download():
    """Start a new download"""
    # Check concurrent download limit
    active_count = Download.query.filter_by(
        user_id=current_user.id,
        status='processing'
    ).count()

    if active_count >= current_user.max_concurrent_downloads:
        return jsonify({
            'error': f'Maximum concurrent downloads reached ({current_user.max_concurrent_downloads})'
        }), 429

    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    # Generate session ID
    session_id = str(uuid.uuid4())

    # Create download record
    download = Download(
        session_id=session_id,
        user_id=current_user.id,
        url=url,
        episode_range=data.get('options', {}).get('episodes', ''),
        status='queued'
    )

    db.session.add(download)
    db.session.commit()

    # Start Celery task
    from tasks import process_download_task
    task = process_download_task.delay(
        session_id,
        current_user.id,
        url,
        data.get('options', {})
    )

    download.celery_task_id = task.id
    db.session.commit()

    return jsonify({
        'session_id': session_id,
        'status': 'started',
        'task_id': task.id
    })


@app.route('/api/cancel/<session_id>', methods=['POST'])
@login_required
def cancel_download(session_id):
    """Cancel a download"""
    download = Download.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not download:
        return jsonify({'error': 'Download not found'}), 404

    # Cancel Celery task
    if download.celery_task_id:
        from tasks import celery
        celery.control.revoke(download.celery_task_id, terminate=True)

    download.status = 'cancelled'
    download.completed_at = datetime.utcnow()
    db.session.commit()

    return jsonify({'status': 'cancelled'})


@app.route('/api/status/<session_id>')
@login_required
def get_status(session_id):
    """Get download status"""
    download = Download.query.filter_by(
        session_id=session_id,
        user_id=current_user.id
    ).first()

    if not download:
        return jsonify({'error': 'Download not found'}), 404

    return jsonify(download.to_dict())


@app.route('/api/history')
@login_required
def get_history():
    """Get user's download history"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)

    downloads = Download.query.filter_by(user_id=current_user.id)\
        .order_by(Download.created_at.desc())\
        .paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        'downloads': [d.to_dict() for d in downloads.items],
        'total': downloads.total,
        'pages': downloads.pages,
        'current_page': page
    })


@app.route('/api/parse-url', methods=['POST'])
@login_required
@limiter.limit("30 per minute")
def parse_url():
    """Parse URL and detect available episodes"""
    # Reuse the existing parsing logic from web_gui.py
    from web_gui import parse_url as original_parse_url

    # Call original function (you'll need to extract it from web_gui.py)
    # For now, placeholder
    data = request.json
    url = data.get('url')

    if not url:
        return jsonify({'error': 'URL is required'}), 400

    try:
        from hls_downloader_final import parse_flexible_url

        base_url, series_slug, season, start_episode, url_type = parse_flexible_url(url)

        if not base_url:
            return jsonify({'error': 'Invalid URL format'}), 400

        # For now, return basic info
        # In production, this would call the full scraping logic
        return jsonify({
            'url_type': url_type,
            'series_slug': series_slug,
            'season': season,
            'start_episode': start_episode,
            'total_seasons': 1,
            'seasons_data': {
                season: {
                    'episodes': list(range(1, 13)),
                    'episode_details': {}
                }
            },
            'series_cover_url': None,
            'series_description': None
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ============================================================
# ADMIN ROUTES
# ============================================================

@app.route('/admin/metrics')
@login_required
def admin_metrics():
    """View system metrics (admin only)"""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    # Get latest metrics
    latest_metrics = SystemMetrics.query.order_by(
        SystemMetrics.timestamp.desc()
    ).limit(100).all()

    return jsonify({
        'metrics': [
            {
                'timestamp': m.timestamp.isoformat(),
                'active_downloads': m.active_downloads,
                'active_browsers': m.active_browsers,
                'queued_tasks': m.queued_tasks,
                'success_rate': m.success_rate,
                'memory_usage_mb': m.memory_usage_mb,
                'cpu_usage_percent': m.cpu_usage_percent
            }
            for m in latest_metrics
        ]
    })


@app.route('/admin/users')
@login_required
def admin_users():
    """View all users (admin only)"""
    if not current_user.is_admin:
        return jsonify({'error': 'Admin access required'}), 403

    users = User.query.all()

    return jsonify({
        'users': [
            {
                'id': u.id,
                'username': u.username,
                'email': u.email,
                'is_active': u.is_active,
                'is_admin': u.is_admin,
                'created_at': u.created_at.isoformat(),
                'last_login': u.last_login.isoformat() if u.last_login else None,
                'download_count': u.downloads.count()
            }
            for u in users
        ]
    })


# ============================================================
# WEBSOCKET EVENTS
# ============================================================

@socketio.on('connect')
def handle_connect():
    """Handle client connection"""
    if current_user.is_authenticated:
        # Join user-specific room
        join_room(f'user_{current_user.id}')
        emit('connected', {'status': 'Connected to HLS Downloader'})
        print(f'User {current_user.username} connected')
    else:
        return False  # Reject connection


@socketio.on('disconnect')
def handle_disconnect():
    """Handle client disconnection"""
    if current_user.is_authenticated:
        leave_room(f'user_{current_user.id}')
        print(f'User {current_user.username} disconnected')


@socketio.on('subscribe')
def handle_subscribe(data):
    """Subscribe to download updates"""
    session_id = data.get('session_id')
    if session_id:
        join_room(f'download_{session_id}')


# ============================================================
# CLI COMMANDS
# ============================================================

@app.cli.command()
def init_db():
    """Initialize database"""
    db.create_all()
    print("✅ Database initialized")


@app.cli.command()
def create_admin():
    """Create admin user"""
    from getpass import getpass

    username = input("Admin username: ")
    email = input("Admin email: ")
    password = getpass("Admin password: ")

    user = User(username=username, email=email, is_admin=True)
    user.set_password(password)

    db.session.add(user)
    db.session.commit()

    print(f"✅ Admin user '{username}' created")


@app.cli.command()
def create_test_user():
    """Create test user for development"""
    user = User(username='test', email='test@example.com')
    user.set_password('test123')

    db.session.add(user)
    db.session.commit()

    print("✅ Test user created (username: test, password: test123)")


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(e):
    """404 error handler"""
    return jsonify({'error': 'Not found'}), 404


@app.errorhandler(500)
def internal_error(e):
    """500 error handler"""
    db.session.rollback()
    return jsonify({'error': 'Internal server error'}), 500


@app.errorhandler(429)
def ratelimit_handler(e):
    """Rate limit error handler"""
    return jsonify({
        'error': 'Rate limit exceeded',
        'message': str(e.description)
    }), 429


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    print("=" * 70)
    print("HLS Video Downloader - Production Web GUI")
    print("=" * 70)
    print(f"\n🌐 Starting server in {env} mode...")
    print("📍 Open your browser and go to: http://localhost:5000")

    if env == 'development':
        print("\n⚠️  DEVELOPMENT MODE")
        print("   - Debug enabled")
        print("   - Auto-reload enabled")
        print("\n💡 Create test user with: flask create-test-user")
    else:
        print("\n🔒 PRODUCTION MODE")
        print("   - Debug disabled")
        print("   - Use Gunicorn in production!")
        print("\n   gunicorn --worker-class eventlet -w 4 web_gui_production:app")

    print("\n🛑 Press Ctrl+C to stop\n")

    # Create tables if they don't exist
    with app.app_context():
        db.create_all()

    socketio.run(app, host='0.0.0.0', port=5000, debug=(env == 'development'))
