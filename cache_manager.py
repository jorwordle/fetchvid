import json
import time
import hashlib
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
import asyncio
from collections import OrderedDict
import logging

logger = logging.getLogger(__name__)

class CacheManager:
    """
    In-memory cache with TTL support for video information
    Designed to reduce API calls and speed up repeated requests
    """
    
    def __init__(self, max_size: int = 100, default_ttl: int = 300):
        """
        Initialize cache manager
        max_size: Maximum number of items to cache
        default_ttl: Default time-to-live in seconds (5 minutes)
        """
        self.cache: OrderedDict = OrderedDict()
        self.max_size = max_size
        self.default_ttl = default_ttl
        self.hit_count = 0
        self.miss_count = 0
        self.lock = asyncio.Lock()
        
    def _generate_key(self, url: str) -> str:
        """Generate a cache key from URL"""
        # Normalize YouTube URLs
        if 'youtube.com' in url or 'youtu.be' in url:
            # Extract video ID for consistent caching
            import re
            patterns = [
                r'(?:youtube\.com\/watch\?v=)([\w-]+)',
                r'(?:youtu\.be\/)([\w-]+)',
                r'(?:youtube\.com\/embed\/)([\w-]+)',
                r'(?:youtube\.com\/v\/)([\w-]+)',
            ]
            
            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    return f"yt_{match.group(1)}"
        
        # For other URLs, use hash
        return hashlib.md5(url.encode()).hexdigest()
    
    async def get(self, url: str) -> Optional[Dict[str, Any]]:
        """
        Get cached video info if available and not expired
        """
        async with self.lock:
            key = self._generate_key(url)
            
            if key in self.cache:
                entry = self.cache[key]
                
                # Check if expired
                if time.time() < entry['expires_at']:
                    # Move to end (LRU)
                    self.cache.move_to_end(key)
                    self.hit_count += 1
                    
                    logger.info(f"Cache hit for {key}, hits: {self.hit_count}, misses: {self.miss_count}")
                    return entry['data']
                else:
                    # Expired, remove it
                    del self.cache[key]
                    logger.info(f"Cache expired for {key}")
            
            self.miss_count += 1
            return None
    
    async def set(self, url: str, data: Dict[str, Any], ttl: Optional[int] = None) -> None:
        """
        Store video info in cache
        """
        async with self.lock:
            key = self._generate_key(url)
            ttl = ttl or self.default_ttl
            
            # Store with expiration time
            self.cache[key] = {
                'data': data,
                'expires_at': time.time() + ttl,
                'created_at': time.time(),
                'url': url
            }
            
            # Move to end (most recently used)
            self.cache.move_to_end(key)
            
            # Enforce max size (remove oldest)
            while len(self.cache) > self.max_size:
                oldest_key = next(iter(self.cache))
                del self.cache[oldest_key]
                logger.info(f"Evicted oldest cache entry: {oldest_key}")
            
            logger.info(f"Cached {key} for {ttl} seconds")
    
    async def invalidate(self, url: str) -> bool:
        """
        Remove specific URL from cache
        """
        async with self.lock:
            key = self._generate_key(url)
            if key in self.cache:
                del self.cache[key]
                logger.info(f"Invalidated cache for {key}")
                return True
            return False
    
    async def clear(self) -> None:
        """
        Clear entire cache
        """
        async with self.lock:
            self.cache.clear()
            self.hit_count = 0
            self.miss_count = 0
            logger.info("Cache cleared")
    
    async def cleanup_expired(self) -> int:
        """
        Remove expired entries
        Returns number of entries removed
        """
        async with self.lock:
            current_time = time.time()
            expired_keys = [
                key for key, entry in self.cache.items()
                if current_time >= entry['expires_at']
            ]
            
            for key in expired_keys:
                del self.cache[key]
            
            if expired_keys:
                logger.info(f"Cleaned up {len(expired_keys)} expired entries")
            
            return len(expired_keys)
    
    def get_stats(self) -> Dict[str, Any]:
        """
        Get cache statistics
        """
        total_requests = self.hit_count + self.miss_count
        hit_rate = (self.hit_count / total_requests * 100) if total_requests > 0 else 0
        
        return {
            'size': len(self.cache),
            'max_size': self.max_size,
            'hit_count': self.hit_count,
            'miss_count': self.miss_count,
            'hit_rate': f"{hit_rate:.2f}%",
            'total_requests': total_requests
        }

class SessionManager:
    """
    Manage user sessions for tracking downloads and implementing rate limits
    """
    
    def __init__(self):
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.lock = asyncio.Lock()
    
    def _get_session_id(self, ip: str, user_agent: str) -> str:
        """Generate session ID from IP and user agent"""
        combined = f"{ip}_{user_agent}"
        return hashlib.md5(combined.encode()).hexdigest()
    
    async def get_or_create_session(self, ip: str, user_agent: str) -> Dict[str, Any]:
        """Get existing session or create new one"""
        async with self.lock:
            session_id = self._get_session_id(ip, user_agent)
            
            if session_id not in self.sessions:
                self.sessions[session_id] = {
                    'id': session_id,
                    'created_at': datetime.now(),
                    'last_seen': datetime.now(),
                    'download_count': 0,
                    'fetch_count': 0,
                    'daily_downloads': 0,
                    'last_reset': datetime.now(),
                    'is_premium': False,
                    'ad_views': 0,
                    'bypass_delay': False
                }
            else:
                # Update last seen
                self.sessions[session_id]['last_seen'] = datetime.now()
                
                # Reset daily counter if needed
                last_reset = self.sessions[session_id]['last_reset']
                if datetime.now() - last_reset > timedelta(days=1):
                    self.sessions[session_id]['daily_downloads'] = 0
                    self.sessions[session_id]['last_reset'] = datetime.now()
            
            return self.sessions[session_id]
    
    async def increment_download(self, session_id: str) -> None:
        """Increment download counter for session"""
        async with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id]['download_count'] += 1
                self.sessions[session_id]['daily_downloads'] += 1
    
    async def increment_ad_view(self, session_id: str) -> None:
        """Increment ad view counter"""
        async with self.lock:
            if session_id in self.sessions:
                self.sessions[session_id]['ad_views'] += 1
                
                # After 3 ad views, give fast lane access for 30 minutes
                if self.sessions[session_id]['ad_views'] >= 3:
                    self.sessions[session_id]['bypass_delay'] = True
                    self.sessions[session_id]['bypass_expiry'] = datetime.now() + timedelta(minutes=30)
    
    async def should_show_delay(self, session_id: str) -> bool:
        """Check if user should see delays (for ads)"""
        async with self.lock:
            if session_id not in self.sessions:
                return True
            
            session = self.sessions[session_id]
            
            # Premium users skip delays
            if session.get('is_premium'):
                return False
            
            # Check if user has bypass from watching ads
            if session.get('bypass_delay'):
                if datetime.now() < session.get('bypass_expiry', datetime.now()):
                    return False
                else:
                    # Bypass expired
                    session['bypass_delay'] = False
            
            return True
    
    async def get_rate_limit_status(self, session_id: str) -> Dict[str, Any]:
        """Get rate limit status for session"""
        async with self.lock:
            if session_id not in self.sessions:
                return {'limited': False, 'remaining': 10}
            
            session = self.sessions[session_id]
            
            # Premium users have no limits
            if session.get('is_premium'):
                return {'limited': False, 'remaining': 999}
            
            # Free tier: 10 downloads per day
            daily_limit = 10
            remaining = daily_limit - session['daily_downloads']
            
            return {
                'limited': remaining <= 0,
                'remaining': max(0, remaining),
                'reset_time': (session['last_reset'] + timedelta(days=1)).isoformat()
            }
    
    async def cleanup_old_sessions(self) -> int:
        """Remove sessions older than 24 hours"""
        async with self.lock:
            cutoff = datetime.now() - timedelta(hours=24)
            old_sessions = [
                sid for sid, session in self.sessions.items()
                if session['last_seen'] < cutoff
            ]
            
            for sid in old_sessions:
                del self.sessions[sid]
            
            if old_sessions:
                logger.info(f"Cleaned up {len(old_sessions)} old sessions")
            
            return len(old_sessions)

# Global instances
cache_manager = CacheManager(max_size=100, default_ttl=300)  # 5 minute TTL
session_manager = SessionManager()

# Background task to clean up expired entries
async def periodic_cleanup():
    """Run periodic cleanup tasks"""
    while True:
        await asyncio.sleep(600)  # Run every 10 minutes
        try:
            expired_cache = await cache_manager.cleanup_expired()
            old_sessions = await session_manager.cleanup_old_sessions()
            logger.info(f"Periodic cleanup: {expired_cache} expired cache entries, {old_sessions} old sessions")
        except Exception as e:
            logger.error(f"Error in periodic cleanup: {str(e)}")