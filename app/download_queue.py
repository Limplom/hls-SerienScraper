"""
Download Queue Manager
Manages download queue with persistence and retry functionality
"""

import json
import re
from pathlib import Path
from datetime import datetime
from enum import Enum
import threading
from typing import Optional, Dict, Any, List


class DownloadStatus(Enum):
    """Download status enumeration"""
    QUEUED = "queued"
    PROCESSING = "processing"
    PAUSED = "paused"  # New: Paused downloads
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SCHEDULED = "scheduled"  # New: Scheduled for later


class DownloadPriority(Enum):
    """Download priority levels for intelligent scheduling"""
    LOW = 0
    NORMAL = 1
    HIGH = 2
    URGENT = 3


class QueueItem:
    """Represents a download in the queue"""

    def __init__(self, session_id: str, url: str, options: Dict[str, Any],
                 episodes_per_season: Optional[Dict[int, List[int]]] = None,
                 priority: DownloadPriority = DownloadPriority.NORMAL,
                 scheduled_start: Optional[str] = None,
                 max_download_rate: Optional[int] = None):
        self.session_id = session_id
        self.url = url
        self.options = options
        self.episodes_per_season = episodes_per_season or {}
        self.status = DownloadStatus.SCHEDULED if scheduled_start else DownloadStatus.QUEUED
        self.priority = priority
        self.created_at = datetime.now().isoformat()
        self.started_at = None
        self.completed_at = None
        self.paused_at = None  # New: When download was paused
        self.scheduled_start = scheduled_start  # New: ISO timestamp for scheduled start
        self.max_download_rate = max_download_rate  # New: Max download rate in KB/s (None = unlimited)
        self.failed_episodes = []
        self.total_episodes = 0
        self.completed_episodes = 0
        self.series_name = options.get('series_display', 'Unknown Series')
        # Auto-retry tracking
        self.auto_retry_enabled = options.get('auto_retry', True)  # Default: enabled
        self.max_retries = 3
        self.episode_retry_counts = {}  # {episode_key: retry_count}
        # Individual episode tracking for UI
        # {episode_key: {status: 'queued'|'downloading'|'completed'|'failed', progress: 0-100, ...}}
        self.episode_status = {}
        # Pending merged episodes (added while download is processing)
        # {season_num: [episode_numbers]}
        self.pending_merged_episodes = {}

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            'session_id': self.session_id,
            'url': self.url,
            'options': self.options,
            'episodes_per_season': self.episodes_per_season,
            'status': self.status.value,
            'priority': self.priority.value,
            'created_at': self.created_at,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'paused_at': self.paused_at,
            'scheduled_start': self.scheduled_start,
            'max_download_rate': self.max_download_rate,
            'failed_episodes': self.failed_episodes,
            'total_episodes': self.total_episodes,
            'completed_episodes': self.completed_episodes,
            'series_name': self.series_name,
            'auto_retry_enabled': self.auto_retry_enabled,
            'max_retries': self.max_retries,
            'episode_retry_counts': self.episode_retry_counts,
            'episode_status': self.episode_status,
            'pending_merged_episodes': self.pending_merged_episodes
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> 'QueueItem':
        """Create QueueItem from dictionary"""
        # Get priority (with fallback for old queue items)
        priority_value = data.get('priority', DownloadPriority.NORMAL.value)
        priority = DownloadPriority(priority_value) if isinstance(priority_value, int) else DownloadPriority.NORMAL

        item = QueueItem(
            session_id=data['session_id'],
            url=data['url'],
            options=data['options'],
            episodes_per_season=data.get('episodes_per_season', {}),
            priority=priority,
            scheduled_start=data.get('scheduled_start'),
            max_download_rate=data.get('max_download_rate')
        )
        item.status = DownloadStatus(data['status'])
        item.created_at = data['created_at']
        item.started_at = data.get('started_at')
        item.completed_at = data.get('completed_at')
        item.paused_at = data.get('paused_at')
        item.failed_episodes = data.get('failed_episodes', [])
        item.total_episodes = data.get('total_episodes', 0)
        item.completed_episodes = data.get('completed_episodes', 0)
        item.series_name = data.get('series_name', 'Unknown Series')
        # Auto-retry fields (with defaults for backwards compatibility)
        item.auto_retry_enabled = data.get('auto_retry_enabled', True)
        item.max_retries = data.get('max_retries', 3)
        item.episode_retry_counts = data.get('episode_retry_counts', {})
        item.episode_status = data.get('episode_status', {})
        item.pending_merged_episodes = data.get('pending_merged_episodes', {})
        return item


class DownloadQueueManager:
    """Thread-safe download queue manager with persistence"""

    def __init__(self, persist_path: str = './download_queue.json'):
        self.queue: List[QueueItem] = []
        self.lock = threading.Lock()
        self.persist_path = Path(persist_path)
        self.load_queue()

    def add_to_queue(self, session_id: str, url: str, options: Dict[str, Any],
                     episodes_per_season: Optional[Dict[int, List[int]]] = None,
                     priority: DownloadPriority = DownloadPriority.NORMAL,
                     scheduled_start: Optional[str] = None,
                     max_download_rate: Optional[int] = None) -> QueueItem:
        """
        Add download to queue with optional priority and scheduling

        Args:
            session_id: Unique session identifier
            url: Download URL
            options: Download options dict
            episodes_per_season: Episodes to download per season
            priority: Download priority (LOW, NORMAL, HIGH, URGENT)
            scheduled_start: ISO timestamp for scheduled start (None = immediate)
            max_download_rate: Max download rate in KB/s (None = unlimited)
        """
        with self.lock:
            item = QueueItem(session_id, url, options, episodes_per_season,
                           priority, scheduled_start, max_download_rate)
            self.queue.append(item)
            self.save_queue()

            status_msg = f"✅ Added to queue: {item.series_name} (Session: {session_id})"
            if priority != DownloadPriority.NORMAL:
                status_msg += f" [Priority: {priority.name}]"
            if scheduled_start:
                status_msg += f" [Scheduled: {scheduled_start}]"
            if max_download_rate:
                status_msg += f" [Max Rate: {max_download_rate} KB/s]"

            print(status_msg)
            return item

    def find_existing_series(self, url: str) -> Optional[QueueItem]:
        """
        Find an existing queue item for the same series URL.
        Only returns items that are still active (queued or processing).

        Args:
            url: The series URL to search for

        Returns:
            QueueItem if found, None otherwise
        """
        with self.lock:
            # Extract the base series URL (without episode/season specifics)
            # URLs like: http://186.2.175.5/serie/stream/rick-and-morty
            base_url = url.split('?')[0].rstrip('/')

            for item in self.queue:
                # Only consider active items (queued or processing)
                if item.status not in [DownloadStatus.QUEUED, DownloadStatus.PROCESSING]:
                    continue

                item_base_url = item.url.split('?')[0].rstrip('/')
                if item_base_url == base_url:
                    return item

            return None

    def merge_episodes(self, session_id: str, new_episodes_per_season: Dict[int, List[int]]) -> Dict[str, Any]:
        """
        Merge new episodes into an existing queue item.

        Args:
            session_id: The session ID of the existing queue item
            new_episodes_per_season: New episodes to add {season: [episode_numbers]}

        Returns:
            Dict with merge results:
            {
                'success': True/False,
                'added_episodes': ['S01E04', 'S01E05', ...],
                'already_exists': ['S01E01', 'S01E02', ...],
                'total_new': int
            }
        """
        with self.lock:
            item = None
            for q_item in self.queue:
                if q_item.session_id == session_id:
                    item = q_item
                    break

            if not item:
                return {'success': False, 'error': 'Queue item not found'}

            added_episodes = []
            already_exists = []

            # Get existing episodes from options
            existing_eps = item.options.get('episodes_per_season', {})
            # Convert string keys to int if needed
            existing_eps = {int(k): v for k, v in existing_eps.items()}

            for season, episodes in new_episodes_per_season.items():
                season = int(season)
                if season not in existing_eps:
                    existing_eps[season] = []

                for ep in episodes:
                    ep_key = f"S{season:02d}E{ep:02d}"
                    if ep in existing_eps[season]:
                        already_exists.append(ep_key)
                    else:
                        existing_eps[season].append(ep)
                        added_episodes.append(ep_key)

                        # Initialize episode status for the new episode
                        if ep_key not in item.episode_status:
                            item.episode_status[ep_key] = {
                                'status': 'queued',
                                'progress': 0,
                                'started_at': None,
                                'completed_at': None
                            }

                # Sort episodes in season
                existing_eps[season] = sorted(existing_eps[season])

            # Update the item
            item.options['episodes_per_season'] = existing_eps
            item.total_episodes += len(added_episodes)

            # If item is actively processing, track new episodes as pending
            # These will be picked up after the current download batch completes
            is_processing = item.status == DownloadStatus.PROCESSING
            if is_processing and added_episodes:
                for season, episodes in new_episodes_per_season.items():
                    season = int(season)
                    for ep in episodes:
                        ep_key = f"S{season:02d}E{ep:02d}"
                        # Only add if it was actually added (not already existing)
                        if ep_key in added_episodes:
                            if season not in item.pending_merged_episodes:
                                item.pending_merged_episodes[season] = []
                            if ep not in item.pending_merged_episodes[season]:
                                item.pending_merged_episodes[season].append(ep)

                print(f"⏳ {len(added_episodes)} episodes queued as pending (download in progress)")

            self.save_queue()

            print(f"🔗 Merged {len(added_episodes)} episodes into {item.series_name} (Session: {session_id})")
            if added_episodes:
                print(f"   Added: {', '.join(added_episodes)}")
            if already_exists:
                print(f"   Already existed: {', '.join(already_exists)}")

            return {
                'success': True,
                'added_episodes': added_episodes,
                'already_exists': already_exists,
                'total_new': len(added_episodes),
                'is_processing': is_processing  # Signal that new episodes are pending
            }

    def consolidate_series(self, url: str) -> Dict[str, Any]:
        """
        Consolidate all queue entries for the same series into one.
        Merges all episodes from duplicate entries into the first (oldest) entry.

        Args:
            url: The series URL to consolidate

        Returns:
            Dict with consolidation results
        """
        with self.lock:
            base_url = url.split('?')[0].rstrip('/')

            # Find all entries for this series (queued or processing)
            matching_items = []
            for item in self.queue:
                if item.status not in [DownloadStatus.QUEUED, DownloadStatus.PROCESSING]:
                    continue
                item_base_url = item.url.split('?')[0].rstrip('/')
                if item_base_url == base_url:
                    matching_items.append(item)

            if len(matching_items) <= 1:
                return {
                    'success': False,
                    'error': 'Keine doppelten Einträge gefunden',
                    'entries_found': len(matching_items)
                }

            # Sort by created_at to keep the oldest one
            matching_items.sort(key=lambda x: x.created_at)
            primary_item = matching_items[0]
            items_to_remove = matching_items[1:]

            merged_episodes = []
            removed_sessions = []

            # Get existing episodes from primary item
            primary_eps = primary_item.options.get('episodes_per_season', {})
            primary_eps = {int(k): list(v) for k, v in primary_eps.items()}

            # Merge episodes from other items
            for item in items_to_remove:
                item_eps = item.options.get('episodes_per_season', {})

                for season, episodes in item_eps.items():
                    season = int(season)
                    if season not in primary_eps:
                        primary_eps[season] = []

                    for ep in episodes:
                        ep_key = f"S{season:02d}E{ep:02d}"
                        if ep not in primary_eps[season]:
                            primary_eps[season].append(ep)
                            merged_episodes.append(ep_key)

                            # Copy episode status if exists
                            if ep_key in item.episode_status:
                                primary_item.episode_status[ep_key] = item.episode_status[ep_key]
                            elif ep_key not in primary_item.episode_status:
                                primary_item.episode_status[ep_key] = {
                                    'status': 'queued',
                                    'progress': 0,
                                    'started_at': None,
                                    'completed_at': None
                                }

                    # Sort episodes in season
                    primary_eps[season] = sorted(primary_eps[season])

                # Track removed session
                removed_sessions.append(item.session_id[:8])

                # Remove the duplicate item from queue
                self.queue.remove(item)

            # Update primary item
            primary_item.options['episodes_per_season'] = primary_eps
            primary_item.total_episodes = sum(len(eps) for eps in primary_eps.values())

            self.save_queue()

            print(f"🔗 Consolidated {len(items_to_remove) + 1} entries for {primary_item.series_name}")
            print(f"   Primary session: {primary_item.session_id[:8]}...")
            print(f"   Removed sessions: {', '.join(removed_sessions)}")
            print(f"   Merged episodes: {', '.join(merged_episodes) if merged_episodes else 'None (all existed)'}")

            return {
                'success': True,
                'primary_session_id': primary_item.session_id,
                'removed_count': len(items_to_remove),
                'removed_sessions': removed_sessions,
                'merged_episodes': merged_episodes,
                'total_episodes': primary_item.total_episodes,
                'series_name': primary_item.series_name
            }

    def find_duplicate_series(self) -> List[Dict[str, Any]]:
        """
        Find all series that have multiple queue entries.

        Returns:
            List of dicts with duplicate series info
        """
        with self.lock:
            url_counts = {}

            for item in self.queue:
                if item.status not in [DownloadStatus.QUEUED, DownloadStatus.PROCESSING]:
                    continue

                base_url = item.url.split('?')[0].rstrip('/')
                if base_url not in url_counts:
                    url_counts[base_url] = {
                        'url': base_url,
                        'series_name': item.series_name,
                        'entries': []
                    }

                url_counts[base_url]['entries'].append({
                    'session_id': item.session_id,
                    'episodes': sum(len(eps) for eps in item.options.get('episodes_per_season', {}).values()),
                    'status': item.status.value
                })

            # Filter to only duplicates
            duplicates = [
                info for info in url_counts.values()
                if len(info['entries']) > 1
            ]

            return duplicates

    def get_next_queued(self) -> Optional[QueueItem]:
        """
        Get next queued item with intelligent priority scheduling

        Priority order:
        1. Process scheduled items whose time has come
        2. Sort by priority (URGENT > HIGH > NORMAL > LOW)
        3. Within same priority, FIFO (oldest first)
        """
        with self.lock:
            now = datetime.now()

            # First, check for scheduled items whose time has come
            scheduled_ready = []
            for item in self.queue:
                if item.status == DownloadStatus.SCHEDULED and item.scheduled_start:
                    try:
                        scheduled_time = datetime.fromisoformat(item.scheduled_start)
                        if scheduled_time <= now:
                            # Time to start this download
                            item.status = DownloadStatus.QUEUED
                            scheduled_ready.append(item)
                    except:
                        pass

            if scheduled_ready:
                self.save_queue()

            # Get all queued items (including newly converted scheduled items)
            queued_items = [i for i in self.queue if i.status == DownloadStatus.QUEUED]

            if not queued_items:
                return None

            # Sort by priority (descending) and then by created_at (ascending/FIFO)
            queued_items.sort(key=lambda x: (-x.priority.value, x.created_at))

            return queued_items[0]

    def get_item(self, session_id: str) -> Optional[QueueItem]:
        """Get specific queue item by session ID"""
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    return item
            return None

    def update_status(self, session_id: str, status: DownloadStatus, **kwargs) -> bool:
        """Update item status and additional attributes"""
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    item.status = status
                    for key, value in kwargs.items():
                        if hasattr(item, key):
                            setattr(item, key, value)
                    self.save_queue()
                    return True
        return False

    def update_progress(self, session_id: str, total: int = None,
                       completed: int = None, failed_episodes: List = None) -> bool:
        """Update download progress"""
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if total is not None:
                        item.total_episodes = total
                    if completed is not None:
                        item.completed_episodes = completed
                    if failed_episodes is not None:
                        item.failed_episodes = failed_episodes
                    self.save_queue()
                    return True
        return False

    def get_failed_episodes(self, session_id: str) -> List[Dict[str, Any]]:
        """Get failed episodes for retry"""
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    return item.failed_episodes
        return []

    def retry_failed(self, session_id: str) -> Optional[QueueItem]:
        """Create new queue item for failed episodes only"""
        with self.lock:
            original = next((i for i in self.queue if i.session_id == session_id), None)
            if not original or not original.failed_episodes:
                return None

            # Create new episodes_per_season with only failed episodes
            # Format can be either: "S02E04" string or {"season": 2, "episode": 4} dict
            retry_episodes = {}
            for fail in original.failed_episodes:
                season = None
                episode = None

                if isinstance(fail, str):
                    # Parse string format "S02E04"
                    match = re.match(r'S(\d+)E(\d+)', fail, re.IGNORECASE)
                    if match:
                        season = int(match.group(1))
                        episode = int(match.group(2))
                elif isinstance(fail, dict):
                    # Dict format {"season": 2, "episode": 4}
                    season = fail.get('season')
                    episode = fail.get('episode')

                if season is not None and episode is not None:
                    if season not in retry_episodes:
                        retry_episodes[season] = []
                    retry_episodes[season].append(episode)

            if not retry_episodes:
                return None

            # Create new queue item for retry
            retry_session_id = f"{session_id}_retry_{datetime.now().strftime('%Y%m%d%H%M%S')}"

            # IMPORTANT: Copy options and UPDATE episodes_per_season to ONLY failed episodes!
            # Bug fix: Previously, the old episodes_per_season from original.options was used,
            # which caused ALL episodes to be downloaded instead of just failed ones.
            retry_options = original.options.copy()
            retry_options['episodes_per_season'] = retry_episodes  # Only failed episodes!

            retry_item = QueueItem(
                session_id=retry_session_id,
                url=original.url,
                options=retry_options,
                episodes_per_season=retry_episodes
            )
            retry_item.series_name = f"{original.series_name} (Retry)"

            self.queue.append(retry_item)
            self.save_queue()

            print(f"🔄 Retry queued: {len(original.failed_episodes)} failed episodes from {original.series_name}")
            return retry_item

    def remove_item(self, session_id: str) -> bool:
        """Remove item from queue"""
        with self.lock:
            original_length = len(self.queue)
            self.queue = [item for item in self.queue if item.session_id != session_id]
            if len(self.queue) < original_length:
                self.save_queue()
                return True
        return False

    def clear_completed(self, keep_recent: int = 10) -> int:
        """Clear old completed/failed downloads, keep recent ones"""
        with self.lock:
            # Separate processing/queued from completed/failed/cancelled
            active = [i for i in self.queue if i.status in [DownloadStatus.QUEUED, DownloadStatus.PROCESSING]]
            inactive = [i for i in self.queue if i.status not in [DownloadStatus.QUEUED, DownloadStatus.PROCESSING]]

            # Keep only recent inactive items
            inactive.sort(key=lambda x: x.completed_at or x.created_at, reverse=True)
            kept_inactive = inactive[:keep_recent]

            removed_count = len(inactive) - len(kept_inactive)
            self.queue = active + kept_inactive

            if removed_count > 0:
                self.save_queue()
                print(f"🗑️ Cleared {removed_count} old downloads")

            return removed_count

    def save_queue(self):
        """Persist queue to file"""
        try:
            data = [item.to_dict() for item in self.queue]
            self.persist_path.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding='utf-8'
            )
        except Exception as e:
            print(f"⚠️ Error saving queue: {e}")

    def load_queue(self):
        """Load queue from file"""
        if not self.persist_path.exists():
            print("📂 No existing queue file found, starting with empty queue")
            return

        try:
            file_content = self.persist_path.read_text(encoding='utf-8')

            # Check if file is empty
            if not file_content.strip():
                print("⚠️ Queue file is empty, starting with empty queue")
                self.queue = []
                return

            data = json.loads(file_content)
            self.queue = [QueueItem.from_dict(item_data) for item_data in data]
            print(f"📂 Loaded {len(self.queue)} items from queue")
        except json.JSONDecodeError as e:
            print(f"❌ Queue file is corrupted (JSON decode error): {e}")
            # Backup corrupted file
            backup_path = self.persist_path.with_suffix('.json.backup')
            try:
                import shutil
                shutil.copy(self.persist_path, backup_path)
                print(f"💾 Corrupted queue backed up to: {backup_path}")
            except:
                pass
            print("🔄 Starting with empty queue")
            self.queue = []
        except Exception as e:
            print(f"⚠️ Error loading queue: {e}")
            self.queue = []

    def get_queue_status(self) -> Dict[str, Any]:
        """Get overall queue status"""
        with self.lock:
            return {
                'total': len(self.queue),
                'queued': sum(1 for i in self.queue if i.status == DownloadStatus.QUEUED),
                'processing': sum(1 for i in self.queue if i.status == DownloadStatus.PROCESSING),
                'paused': sum(1 for i in self.queue if i.status == DownloadStatus.PAUSED),
                'scheduled': sum(1 for i in self.queue if i.status == DownloadStatus.SCHEDULED),
                'completed': sum(1 for i in self.queue if i.status == DownloadStatus.COMPLETED),
                'failed': sum(1 for i in self.queue if i.status == DownloadStatus.FAILED),
                'cancelled': sum(1 for i in self.queue if i.status == DownloadStatus.CANCELLED),
                'items': [self._item_to_summary(i) for i in self.queue]
            }

    def _item_to_summary(self, item: QueueItem) -> Dict[str, Any]:
        """Convert item to summary dict for API response"""
        return {
            'session_id': item.session_id,
            'series_name': item.series_name,
            'status': item.status.value,
            'priority': item.priority.value,
            'priority_name': item.priority.name,
            'created_at': item.created_at,
            'started_at': item.started_at,
            'completed_at': item.completed_at,
            'paused_at': item.paused_at,
            'scheduled_start': item.scheduled_start,
            'max_download_rate': item.max_download_rate,
            'total_episodes': item.total_episodes,
            'completed_episodes': item.completed_episodes,
            'failed_episodes_count': len(item.failed_episodes),
            'has_failed_episodes': len(item.failed_episodes) > 0,
            'episode_status': item.episode_status  # Individual episode tracking
        }

    def get_queue_position(self, session_id: str) -> int:
        """Get position of item in queue (1-indexed)"""
        with self.lock:
            queued_items = [i for i in self.queue if i.status == DownloadStatus.QUEUED]
            for idx, item in enumerate(queued_items, 1):
                if item.session_id == session_id:
                    return idx
        return 0

    # ==========================================
    # NEW: Advanced Queue Management Features
    # ==========================================

    def pause_download(self, session_id: str) -> bool:
        """
        Pause a download (works for QUEUED or PROCESSING status)

        Returns:
            True if paused successfully, False otherwise
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if item.status in [DownloadStatus.QUEUED, DownloadStatus.PROCESSING]:
                        item.status = DownloadStatus.PAUSED
                        item.paused_at = datetime.now().isoformat()
                        self.save_queue()
                        print(f"⏸️ Paused: {item.series_name}")
                        return True
                    else:
                        print(f"⚠️ Cannot pause {item.series_name} - status: {item.status.value}")
                        return False
        return False

    def resume_download(self, session_id: str) -> bool:
        """
        Resume a paused download

        Returns:
            True if resumed successfully, False otherwise
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if item.status == DownloadStatus.PAUSED:
                        # Check if it's scheduled
                        if item.scheduled_start:
                            scheduled_time = datetime.fromisoformat(item.scheduled_start)
                            if scheduled_time > datetime.now():
                                item.status = DownloadStatus.SCHEDULED
                            else:
                                item.status = DownloadStatus.QUEUED
                        else:
                            item.status = DownloadStatus.QUEUED

                        item.paused_at = None
                        self.save_queue()
                        print(f"▶️ Resumed: {item.series_name}")
                        return True
                    else:
                        print(f"⚠️ Cannot resume {item.series_name} - status: {item.status.value}")
                        return False
        return False

    def set_priority(self, session_id: str, priority: DownloadPriority) -> bool:
        """
        Change priority of a queued/scheduled download

        Args:
            session_id: Session ID
            priority: New priority level

        Returns:
            True if priority changed, False otherwise
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if item.status in [DownloadStatus.QUEUED, DownloadStatus.SCHEDULED, DownloadStatus.PAUSED]:
                        old_priority = item.priority
                        item.priority = priority
                        self.save_queue()
                        print(f"🎯 Priority changed: {item.series_name} ({old_priority.name} → {priority.name})")
                        return True
                    else:
                        print(f"⚠️ Cannot change priority for {item.series_name} - status: {item.status.value}")
                        return False
        return False

    def set_bandwidth_limit(self, session_id: str, max_rate_kbps: Optional[int]) -> bool:
        """
        Set bandwidth limit for a download

        Args:
            session_id: Session ID
            max_rate_kbps: Max download rate in KB/s (None = unlimited)

        Returns:
            True if limit set, False otherwise
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    item.max_download_rate = max_rate_kbps
                    self.save_queue()
                    if max_rate_kbps:
                        print(f"📊 Bandwidth limit set: {item.series_name} ({max_rate_kbps} KB/s)")
                    else:
                        print(f"📊 Bandwidth limit removed: {item.series_name}")
                    return True
        return False

    def reschedule_download(self, session_id: str, new_start_time: str) -> bool:
        """
        Reschedule a download to a new time

        Args:
            session_id: Session ID
            new_start_time: ISO timestamp for new start time

        Returns:
            True if rescheduled, False otherwise
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if item.status in [DownloadStatus.QUEUED, DownloadStatus.SCHEDULED, DownloadStatus.PAUSED]:
                        try:
                            # Validate timestamp
                            scheduled_time = datetime.fromisoformat(new_start_time)

                            item.scheduled_start = new_start_time

                            # Update status based on time
                            if scheduled_time > datetime.now():
                                item.status = DownloadStatus.SCHEDULED
                            else:
                                item.status = DownloadStatus.QUEUED

                            self.save_queue()
                            print(f"📅 Rescheduled: {item.series_name} to {new_start_time}")
                            return True
                        except ValueError as e:
                            print(f"❌ Invalid timestamp format: {e}")
                            return False
                    else:
                        print(f"⚠️ Cannot reschedule {item.series_name} - status: {item.status.value}")
                        return False
        return False

    def get_scheduled_downloads(self) -> List[QueueItem]:
        """Get all scheduled downloads"""
        with self.lock:
            return [i for i in self.queue if i.status == DownloadStatus.SCHEDULED]

    def get_paused_downloads(self) -> List[QueueItem]:
        """Get all paused downloads"""
        with self.lock:
            return [i for i in self.queue if i.status == DownloadStatus.PAUSED]

    def reorder(self, session_ids: List[str]) -> bool:
        """
        Reorder queued items based on provided session_id order.
        Only QUEUED items can be reordered; processing/completed items stay in place.

        Args:
            session_ids: List of session IDs in desired order

        Returns:
            True if reordered successfully, False otherwise
        """
        with self.lock:
            # Separate queued items from others
            queued = [i for i in self.queue if i.status == DownloadStatus.QUEUED]
            others = [i for i in self.queue if i.status != DownloadStatus.QUEUED]

            if not queued:
                return True  # Nothing to reorder

            # Create mapping of session_id to item
            id_to_item = {i.session_id: i for i in queued}

            # Build new queued order based on provided session_ids
            new_queued = []
            for sid in session_ids:
                # Handle both full and partial session IDs
                matching_item = None
                for full_id, item in id_to_item.items():
                    if full_id == sid or full_id.startswith(sid):
                        matching_item = item
                        break

                if matching_item and matching_item not in new_queued:
                    new_queued.append(matching_item)

            # Add any queued items not in the provided order (at the end)
            for item in queued:
                if item not in new_queued:
                    new_queued.append(item)

            # Reconstruct queue: others first (processing, completed, etc.), then reordered queued
            self.queue = others + new_queued
            self.save_queue()

            print(f"🔀 Queue reordered: {len(new_queued)} items")
            return True

    # ==========================================
    # Auto-Retry with Exponential Backoff
    # ==========================================

    def calculate_retry_delay(self, retry_count: int) -> int:
        """
        Calculate exponential backoff delay for retry.

        Args:
            retry_count: Number of previous retries (0 = first retry)

        Returns:
            Delay in seconds (30s, 60s, 120s, 240s max)
        """
        base_delay = 30  # 30 seconds base
        max_delay = 240  # 4 minutes max
        delay = min(base_delay * (2 ** retry_count), max_delay)
        return delay

    def should_auto_retry(self, session_id: str, episode_key: str) -> tuple:
        """
        Check if an episode should be auto-retried.

        Args:
            session_id: Session ID
            episode_key: Episode identifier (e.g., "S01E05")

        Returns:
            Tuple of (should_retry: bool, retry_delay: int, attempt_number: int)
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if not item.auto_retry_enabled:
                        return (False, 0, 0)

                    current_retries = item.episode_retry_counts.get(episode_key, 0)
                    if current_retries >= item.max_retries:
                        return (False, 0, current_retries)

                    delay = self.calculate_retry_delay(current_retries)
                    return (True, delay, current_retries + 1)

        return (False, 0, 0)

    def increment_retry_count(self, session_id: str, episode_key: str) -> int:
        """
        Increment the retry count for an episode.

        Args:
            session_id: Session ID
            episode_key: Episode identifier

        Returns:
            New retry count
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    current = item.episode_retry_counts.get(episode_key, 0)
                    item.episode_retry_counts[episode_key] = current + 1
                    self.save_queue()
                    return current + 1
        return 0

    def reset_retry_count(self, session_id: str, episode_key: str) -> None:
        """
        Reset the retry count for an episode (on success).

        Args:
            session_id: Session ID
            episode_key: Episode identifier
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if episode_key in item.episode_retry_counts:
                        del item.episode_retry_counts[episode_key]
                        self.save_queue()
                    return

    # ==========================================
    # Individual Episode Status Tracking
    # ==========================================

    def init_episode_status(self, session_id: str, episodes: list) -> None:
        """
        Initialize episode status for all episodes in a download.

        Args:
            session_id: Session ID
            episodes: List of episode keys (e.g., ["S01E01", "S01E02", ...])
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    item.episode_status = {}
                    for ep_key in episodes:
                        item.episode_status[ep_key] = {
                            'status': 'queued',
                            'progress': 0,
                            'started_at': None,
                            'completed_at': None
                        }
                    self.save_queue()
                    return

    def update_episode_status(self, session_id: str, episode_key: str,
                              status: str = None, progress: float = None) -> bool:
        """
        Update status for a specific episode.

        Args:
            session_id: Session ID
            episode_key: Episode identifier (e.g., "S01E05")
            status: New status ('queued', 'downloading', 'completed', 'failed')
            progress: Download progress 0-100

        Returns:
            True if updated, False otherwise
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    if episode_key not in item.episode_status:
                        item.episode_status[episode_key] = {
                            'status': 'queued',
                            'progress': 0,
                            'started_at': None,
                            'completed_at': None
                        }

                    if status:
                        item.episode_status[episode_key]['status'] = status
                        if status == 'downloading' and not item.episode_status[episode_key]['started_at']:
                            item.episode_status[episode_key]['started_at'] = datetime.now().isoformat()
                        elif status in ['completed', 'failed']:
                            item.episode_status[episode_key]['completed_at'] = datetime.now().isoformat()

                    if progress is not None:
                        item.episode_status[episode_key]['progress'] = progress

                    # Don't save on every progress update (too frequent)
                    if status:
                        self.save_queue()
                    return True
        return False

    def get_episode_status(self, session_id: str) -> Dict[str, Any]:
        """
        Get all episode statuses for a session.

        Args:
            session_id: Session ID

        Returns:
            Dict of episode_key -> status info
        """
        with self.lock:
            for item in self.queue:
                if item.session_id == session_id:
                    return dict(item.episode_status)
        return {}
