"""
Celery tasks for background processing
"""
from celery import Celery, Task
from datetime import datetime
import asyncio
from app.models import db, Download
from app.hls_downloader_final import (
    HLSExtractor,
    parse_episode_range,
    parse_flexible_url,
    detect_series_info
)

# Celery instance will be created by create_celery function
celery = None


def create_celery(app):
    """Create and configure Celery instance"""
    celery = Celery(
        app.import_name,
        broker=app.config['CELERY_BROKER_URL'],
        backend=app.config['CELERY_RESULT_BACKEND']
    )
    celery.conf.update(app.config)

    class ContextTask(Task):
        """Task class that ensures Flask app context"""
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

    celery.Task = ContextTask
    return celery


class BrowserPool:
    """Browser pool manager to limit concurrent browsers"""
    def __init__(self, max_browsers=10):
        self.semaphore = asyncio.Semaphore(max_browsers)
        self.active_count = 0

    async def acquire(self):
        """Acquire browser slot"""
        await self.semaphore.acquire()
        self.active_count += 1

    def release(self):
        """Release browser slot"""
        self.semaphore.release()
        self.active_count -= 1


# Global browser pool
browser_pool = None


def init_browser_pool(max_browsers=10):
    """Initialize browser pool"""
    global browser_pool
    browser_pool = BrowserPool(max_browsers)


@celery.task(bind=True, name='tasks.process_download')
def process_download_task(self, session_id, user_id, url, options):
    """
    Celery task to process download in background
    """
    from app.web_gui_production import socketio, app

    download = Download.query.filter_by(session_id=session_id).first()

    if not download:
        return {'error': 'Download not found'}

    try:
        # Update download status
        download.status = 'processing'
        download.started_at = datetime.utcnow()
        download.celery_task_id = self.request.id
        db.session.commit()

        # Emit status update via SocketIO
        with app.app_context():
            socketio.emit('status', {
                'session_id': session_id,
                'status': 'processing',
                'message': 'Download started...'
            })

        # Parse URL
        base_url, series_slug, season, start_episode, url_type = parse_flexible_url(url)

        if not base_url:
            raise ValueError("Invalid URL format")

        download.series_name = series_slug
        download.season = season
        db.session.commit()

        # Get episodes
        if options.get('episodes'):
            episodes = parse_episode_range(options['episodes'])
        elif start_episode:
            episodes = [start_episode]
        else:
            episodes = [1]

        download.progress_total = len(episodes)
        download.episode_range = options.get('episodes', str(start_episode))
        db.session.commit()

        # Process downloads (simplified - full implementation would go here)
        # This would call the actual download logic from web_gui.py

        successful = 0
        failed = 0
        failed_episodes = []

        for i, ep_num in enumerate(episodes, 1):
            # Update progress
            download.progress_current = i
            db.session.commit()

            # Emit progress update
            with app.app_context():
                socketio.emit('progress', {
                    'session_id': session_id,
                    'current': i,
                    'total': len(episodes),
                    'episode': f"S{season:02d}E{ep_num:02d}"
                })

            # Here would be the actual download logic
            # For now, just placeholder
            try:
                # result = process_episode(...)
                successful += 1
            except Exception as e:
                failed += 1
                failed_episodes.append(ep_num)

        # Update final status
        download.status = 'completed'
        download.completed_at = datetime.utcnow()
        download.successful = successful
        download.failed = failed
        download.failed_episodes = failed_episodes
        db.session.commit()

        # Emit completion
        with app.app_context():
            socketio.emit('status', {
                'session_id': session_id,
                'status': 'completed',
                'message': f'Download completed! Success: {successful}, Failed: {failed}',
                'successful': successful,
                'failed': failed,
                'failed_episodes': failed_episodes,
                'total': len(episodes)
            })

        return {
            'session_id': session_id,
            'status': 'completed',
            'successful': successful,
            'failed': failed
        }

    except Exception as e:
        # Update error status
        download.status = 'error'
        download.error_message = str(e)
        download.completed_at = datetime.utcnow()
        db.session.commit()

        # Emit error
        with app.app_context():
            socketio.emit('status', {
                'session_id': session_id,
                'status': 'error',
                'message': f'Error: {str(e)}'
            })

        raise


@celery.task(name='tasks.cleanup_old_downloads')
def cleanup_old_downloads():
    """Cleanup old completed/failed downloads (older than 7 days)"""
    from datetime import timedelta

    cutoff_date = datetime.utcnow() - timedelta(days=7)

    old_downloads = Download.query.filter(
        Download.status.in_(['completed', 'error', 'cancelled']),
        Download.completed_at < cutoff_date
    ).all()

    count = len(old_downloads)

    for download in old_downloads:
        db.session.delete(download)

    db.session.commit()

    return {'deleted': count}


@celery.task(name='tasks.collect_metrics')
def collect_metrics():
    """Collect system metrics"""
    from app.models import SystemMetrics
    import psutil

    active_downloads = Download.query.filter_by(status='processing').count()
    queued_tasks = Download.query.filter_by(status='queued').count()

    # Calculate success rate
    completed = Download.query.filter(
        Download.status.in_(['completed', 'error'])
    ).all()

    if completed:
        success_rate = sum(1 for d in completed if d.status == 'completed') / len(completed) * 100
    else:
        success_rate = 0.0

    # System resources
    memory = psutil.virtual_memory()
    cpu = psutil.cpu_percent(interval=1)

    metrics = SystemMetrics(
        active_downloads=active_downloads,
        queued_tasks=queued_tasks,
        success_rate=success_rate,
        memory_usage_mb=memory.used / (1024 * 1024),
        cpu_usage_percent=cpu
    )

    db.session.add(metrics)
    db.session.commit()

    return {
        'active_downloads': active_downloads,
        'queued_tasks': queued_tasks,
        'success_rate': success_rate
    }
