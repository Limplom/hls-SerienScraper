"""
Database models for HLS Downloader
"""
from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    """User model for authentication"""
    __tablename__ = 'users'

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, default=True)
    is_admin = db.Column(db.Boolean, default=False)
    max_concurrent_downloads = db.Column(db.Integer, default=3)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    last_login = db.Column(db.DateTime)

    # Relationships
    downloads = db.relationship('Download', backref='user', lazy='dynamic', cascade='all, delete-orphan')

    def set_password(self, password):
        """Hash and set password"""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Check password against hash"""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'


class Download(db.Model):
    """Download task model"""
    __tablename__ = 'downloads'

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(36), unique=True, nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)

    # Download information
    url = db.Column(db.String(500), nullable=False)
    series_name = db.Column(db.String(200))
    season = db.Column(db.Integer)
    episode_range = db.Column(db.String(100))

    # Status tracking
    status = db.Column(db.String(20), default='queued', index=True)  # queued, processing, completed, error, cancelled
    progress_current = db.Column(db.Integer, default=0)
    progress_total = db.Column(db.Integer, default=0)
    successful = db.Column(db.Integer, default=0)
    failed = db.Column(db.Integer, default=0)
    failed_episodes = db.Column(db.JSON)

    # Celery task
    celery_task_id = db.Column(db.String(36), index=True)

    # Timestamps
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    # Error tracking
    error_message = db.Column(db.Text)

    def __repr__(self):
        return f'<Download {self.id}: {self.series_name} S{self.season}>'

    def to_dict(self):
        """Convert to dictionary for API"""
        return {
            'id': self.id,
            'session_id': self.session_id,
            'url': self.url,
            'series_name': self.series_name,
            'season': self.season,
            'episode_range': self.episode_range,
            'status': self.status,
            'progress_current': self.progress_current,
            'progress_total': self.progress_total,
            'successful': self.successful,
            'failed': self.failed,
            'failed_episodes': self.failed_episodes,
            'created_at': self.created_at.isoformat() if self.created_at else None,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'completed_at': self.completed_at.isoformat() if self.completed_at else None,
        }


class SystemMetrics(db.Model):
    """System metrics for monitoring"""
    __tablename__ = 'system_metrics'

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    # Resource usage
    active_downloads = db.Column(db.Integer, default=0)
    active_browsers = db.Column(db.Integer, default=0)
    queued_tasks = db.Column(db.Integer, default=0)

    # Performance metrics
    avg_download_time = db.Column(db.Float)
    success_rate = db.Column(db.Float)

    # System health
    memory_usage_mb = db.Column(db.Float)
    cpu_usage_percent = db.Column(db.Float)

    def __repr__(self):
        return f'<SystemMetrics {self.timestamp}>'
